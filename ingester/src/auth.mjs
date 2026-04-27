#!/usr/bin/env node
// Gmail OAuth2 flow — loopback redirect (Google's recommended pattern for
// desktop apps). Starts a one-shot HTTP server on a random port, opens the
// user's browser, captures the auth code, exchanges for tokens, persists them.
//
// Flags:
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

function parseArgs(argv) {
  const args = { force: false, port: 0 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--force') args.force = true;
    else if (a === '--port') args.port = Number(argv[++i]);
  }
  return args;
}

function openBrowser(url) {
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

  const { GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET } = process.env;
  if (!GOOGLE_CLIENT_ID || !GOOGLE_CLIENT_SECRET) {
    console.error('GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set.');
    process.exit(3);
  }

  const { tokens } = await runLoopback({
    clientId: GOOGLE_CLIENT_ID,
    clientSecret: GOOGLE_CLIENT_SECRET,
    port: args.port,
  });
  saveTokens(tokens);
  console.log(`Tokens written to ${TOKEN_PATH} (mode 0600).`);

  const oauth = makeOAuthClient({
    clientId: GOOGLE_CLIENT_ID,
    clientSecret: GOOGLE_CLIENT_SECRET,
  });
  oauth.setCredentials(tokens);
  const { getProfile } = makeGmailClient(oauth);
  const profile = await getProfile();
  console.log(`Authorized as ${profile.emailAddress}.`);
  console.log(`Mailbox size: ${profile.messagesTotal} messages.`);
}

function runLoopback({ clientId, clientSecret, port }) {
  return new Promise((resolvePromise, rejectPromise) => {
    let settled = false;
    const finish = (fn) => {
      if (settled) return;
      settled = true;
      fn();
    };

    const safeReply = (res, status, body) => {
      try {
        if (!res.headersSent) {
          res.writeHead(status, { 'content-type': 'text/html' });
        }
        if (!res.writableEnded) res.end(body);
      } catch {}
    };

    const server = createServer(async (req, res) => {
      try {
        if (!req.url || !req.url.startsWith('/callback')) {
          safeReply(res, 404, '');
          return;
        }
        if (settled) {
          safeReply(res, 200, SUCCESS_HTML);
          return;
        }
        const reqUrl = new URL(
          req.url,
          `http://127.0.0.1:${server.address().port}`
        );
        const code = reqUrl.searchParams.get('code');
        const err = reqUrl.searchParams.get('error');
        if (err || !code) {
          safeReply(res, 400, ERROR_HTML_TEMPLATE(err || 'No code returned.'));
          finish(() => {
            server.close();
            rejectPromise(new Error(`OAuth failed: ${err || 'no code'}`));
          });
          return;
        }

        const client = makeOAuthClient({
          clientId,
          clientSecret,
          redirectUri: `http://127.0.0.1:${server.address().port}/callback`,
        });
        const { tokens } = await client.getToken(code);
        if (!tokens.refresh_token) {
          console.warn('[warn] No refresh_token returned. Re-run with --force.');
        }
        saveTokens(tokens);
        safeReply(res, 200, SUCCESS_HTML);
        finish(() => {
          setTimeout(() => server.close(), 50);
          resolvePromise({ tokens });
        });
      } catch (exchangeErr) {
        safeReply(res, 500, ERROR_HTML_TEMPLATE(exchangeErr.message));
        finish(() => {
          server.close();
          rejectPromise(exchangeErr);
        });
      }
    });

    server.listen(port, '127.0.0.1', () => {
      const actualPort = server.address().port;
      const redirectUri = `http://127.0.0.1:${actualPort}/callback`;
      const client = makeOAuthClient({ clientId, clientSecret, redirectUri });
      const authUrl = client.generateAuthUrl({
        access_type: 'offline',
        prompt: 'consent',
        scope: GMAIL_READONLY_SCOPES,
        include_granted_scopes: true,
      });
      console.log(`\nOpening browser for Google authorization...`);
      console.log(`If it doesn't open, paste this URL manually:\n  ${authUrl}\n`);
      console.log(`Waiting on loopback http://127.0.0.1:${actualPort}/callback ...`);
      openBrowser(authUrl);
    });

    server.on('error', (e) => rejectPromise(e));
  });
}

main().catch((err) => {
  console.error('auth failed:', err.message || err);
  process.exit(1);
});
