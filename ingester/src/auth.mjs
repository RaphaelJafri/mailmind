#!/usr/bin/env node
// Gmail OAuth2 flow — loopback redirect (Google's recommended pattern for
// desktop apps). Starts a one-shot HTTP server on a random port, opens the
// user's browser, captures the auth code, exchanges for tokens, persists them.
//
// Two callers:
//   - CLI: `npm run auth` — runs main(), exits when done. The legacy path.
//   - HTTP: ingester `POST /auth/start` calls runLoopbackFlow() and tracks
//           progress in a module-level state map so the UI can poll.
//
// Flags (CLI only):
//   --force        delete existing tokens and re-auth
//   --port N       pin the loopback port (default: random available)

import { createServer } from 'node:http';
import { existsSync, unlinkSync } from 'node:fs';
import { exec } from 'node:child_process';
import 'dotenv/config';

import {
  GMAIL_READONLY_SCOPES,
  makeOAuthClient,
  makeGmailClient,
  saveTokens,
  tokensExist,
} from './lib/gmail-client.mjs';
import { tokenPath } from './lib/paths.mjs';
import { loadOAuthConfig } from './lib/oauth-config.mjs';

function parseArgs(argv) {
  const args = { force: false, port: 0 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--force') args.force = true;
    else if (a === '--port') args.port = Number(argv[++i]);
  }
  return args;
}

export function openBrowser(url) {
  const cmd =
    process.platform === 'darwin'
      ? `open "${url}"`
      : process.platform === 'win32'
      ? `start "" "${url}"`
      : `xdg-open "${url}"`;
  exec(cmd, () => {});
}

const SUCCESS_HTML = `<!doctype html>
<html><body style="font-family:system-ui,sans-serif;padding:3rem;max-width:520px">
<h2 style="color:#1b7f3a">Authorized.</h2>
<p>mailmind now has a read-only token for your Gmail. You can close this tab and return to the app.</p>
</body></html>`;

const ERROR_HTML_TEMPLATE = (msg) => `<!doctype html>
<html><body style="font-family:system-ui,sans-serif;padding:3rem;max-width:520px">
<h2 style="color:#b00020">Authorization failed.</h2>
<p>${msg}</p>
</body></html>`;

/**
 * Start the loopback OAuth flow. The flow runs asynchronously: we boot the
 * loopback server, generate the auth URL, and return immediately. The
 * caller is responsible for showing the URL to the user (open in browser,
 * copy/paste, etc.) and polling `state.status` until it becomes
 * "authorized" or "error".
 *
 * @param {{ clientId: string, clientSecret: string, port?: number,
 *           openBrowser?: boolean }} opts
 * @returns {{
 *   authUrl: string,
 *   redirectUri: string,
 *   state: { status: 'waiting' | 'authorized' | 'error',
 *            account_email?: string, error?: string },
 *   shutdown: () => void
 * }}
 */
export function runLoopbackFlow({ clientId, clientSecret, port = 0, openBrowser: doOpen = true }) {
  const state = { status: 'waiting' };
  let server;
  let authUrl;
  let redirectUri;

  const finish = (next) => {
    Object.assign(state, next);
    setTimeout(() => server && server.close(), 50);
  };

  server = createServer(async (req, res) => {
    const safeReply = (status, body) => {
      try {
        if (!res.headersSent) {
          res.writeHead(status, { 'content-type': 'text/html' });
        }
        if (!res.writableEnded) res.end(body);
      } catch {}
    };
    try {
      if (!req.url || !req.url.startsWith('/callback')) {
        safeReply(404, '');
        return;
      }
      // If we've already finished, just show the success page (the user
      // may have refreshed the tab).
      if (state.status === 'authorized') {
        safeReply(200, SUCCESS_HTML);
        return;
      }
      const reqUrl = new URL(req.url, `http://127.0.0.1:${server.address().port}`);
      const code = reqUrl.searchParams.get('code');
      const err = reqUrl.searchParams.get('error');
      if (err || !code) {
        safeReply(400, ERROR_HTML_TEMPLATE(err || 'No code returned.'));
        finish({ status: 'error', error: err || 'no_code' });
        return;
      }

      const client = makeOAuthClient({ clientId, clientSecret, redirectUri });
      const { tokens } = await client.getToken(code);
      saveTokens(tokens);

      // Best-effort: fetch the email so the UI can show "connected as X".
      let account_email = null;
      try {
        const oauth = makeOAuthClient({ clientId, clientSecret });
        oauth.setCredentials(tokens);
        const profile = await makeGmailClient(oauth).getProfile();
        account_email = profile.emailAddress;
      } catch {
        /* tokens saved but profile lookup failed — let the UI re-fetch */
      }

      safeReply(200, SUCCESS_HTML);
      finish({ status: 'authorized', account_email });
    } catch (exc) {
      safeReply(500, ERROR_HTML_TEMPLATE(exc.message));
      finish({ status: 'error', error: exc.message });
    }
  });

  server.listen(port, '127.0.0.1');

  // Wait synchronously for the listen to bind so the caller can open the
  // browser immediately. Express + http servers emit 'listening' on next
  // tick; we just resolve the address here.
  return new Promise((resolve, reject) => {
    server.once('listening', () => {
      try {
        const actualPort = server.address().port;
        redirectUri = `http://127.0.0.1:${actualPort}/callback`;
        const client = makeOAuthClient({ clientId, clientSecret, redirectUri });
        authUrl = client.generateAuthUrl({
          access_type: 'offline',
          prompt: 'consent',
          scope: GMAIL_READONLY_SCOPES,
          include_granted_scopes: true,
        });
        if (doOpen) openBrowser(authUrl);
        resolve({
          authUrl,
          redirectUri,
          state,
          shutdown: () => {
            try { server.close(); } catch {}
          },
        });
      } catch (err) {
        reject(err);
      }
    });
    server.once('error', (err) => reject(err));
  });
}

// ---------- CLI entry point ----------

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const TOKEN_PATH = tokenPath();

  if (tokensExist() && !args.force) {
    console.log(`Tokens already exist at ${TOKEN_PATH}.`);
    console.log('Use --force to re-authorize.');
    process.exit(0);
  }
  if (args.force && existsSync(TOKEN_PATH)) {
    unlinkSync(TOKEN_PATH);
    console.log(`Removed existing tokens: ${TOKEN_PATH}`);
  }

  const cfg = loadOAuthConfig();
  if (!cfg) {
    console.error(
      'No OAuth client configured. Set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET in\n' +
      'ingester/.env, or open the dashboard → Settings → Gmail Connection and\n' +
      'paste your credentials there.'
    );
    process.exit(3);
  }

  const flow = await runLoopbackFlow({
    clientId: cfg.client_id,
    clientSecret: cfg.client_secret,
    port: args.port,
    openBrowser: true,
  });
  console.log(`\nOpening browser for Google authorization...`);
  console.log(`If it doesn't open, paste this URL manually:\n  ${flow.authUrl}\n`);
  console.log(`Waiting on loopback ${flow.redirectUri} ...`);

  // Poll the shared state until the loopback handler resolves it.
  while (flow.state.status === 'waiting') {
    await new Promise((r) => setTimeout(r, 250));
  }
  if (flow.state.status === 'error') {
    console.error('OAuth failed:', flow.state.error);
    process.exit(1);
  }

  console.log(`Tokens written to ${TOKEN_PATH} (mode 0600).`);
  if (flow.state.account_email) {
    console.log(`Authorized as ${flow.state.account_email}.`);
  }
}

import { fileURLToPath } from 'node:url';
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((err) => {
    console.error('auth failed:', err.message || err);
    process.exit(1);
  });
}
