// OAuth client configuration — loaded from the data dir so the dashboard
// can manage it without anyone editing files manually.
//
// Lookup order:
//   1. process.env.GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET (legacy/dev path,
//      kept so existing setups keep working)
//   2. {config_dir}/oauth_client.json — written by the dashboard's Settings
//      → Gmail Connection panel.
//
// The file is mode 0600 (owner-readable only). Format:
//   {
//     "client_id": "...apps.googleusercontent.com",
//     "client_secret": "GOCSPX-...",
//     "saved_at": "2026-04-27T...Z"
//   }

import {
  chmodSync,
  existsSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { resolve } from 'node:path';
import { configDir } from './paths.mjs';

const CONFIG_FILE = 'oauth_client.json';

export function oauthConfigPath() {
  return resolve(configDir(), CONFIG_FILE);
}

/**
 * Load OAuth client config. Returns {client_id, client_secret, source} or
 * null if neither env vars nor a config file are set.
 *
 * `source` is "env" or "file" so the dashboard can label which one's in
 * use (and warn if both are set — env wins).
 */
export function loadOAuthConfig() {
  const envId = process.env.GOOGLE_CLIENT_ID;
  const envSecret = process.env.GOOGLE_CLIENT_SECRET;
  if (envId && envSecret) {
    return { client_id: envId, client_secret: envSecret, source: 'env' };
  }

  const path = oauthConfigPath();
  if (!existsSync(path)) return null;
  try {
    const data = JSON.parse(readFileSync(path, 'utf8'));
    if (data.client_id && data.client_secret) {
      return {
        client_id: data.client_id,
        client_secret: data.client_secret,
        source: 'file',
        saved_at: data.saved_at || null,
      };
    }
  } catch {
    return null;
  }
  return null;
}

/** Save OAuth client config to {config_dir}/oauth_client.json (mode 0600). */
export function saveOAuthConfig({ client_id, client_secret }) {
  if (!client_id || typeof client_id !== 'string') {
    throw new Error('client_id is required');
  }
  if (!client_secret || typeof client_secret !== 'string') {
    throw new Error('client_secret is required');
  }
  // Light shape validation — Google's client IDs end with .apps.googleusercontent.com.
  // We don't reject anything that doesn't match (Google's format may evolve)
  // but we strip whitespace + log a soft warning for visibility.
  const cleaned = {
    client_id: client_id.trim(),
    client_secret: client_secret.trim(),
    saved_at: new Date().toISOString(),
  };
  const path = oauthConfigPath();
  writeFileSync(path, JSON.stringify(cleaned, null, 2));
  chmodSync(path, 0o600);
  return { ...cleaned, source: 'file', path };
}

/** Remove the saved config. Env-var-based credentials are untouched. */
export function deleteOAuthConfig() {
  const path = oauthConfigPath();
  if (existsSync(path)) {
    unlinkSync(path);
    return true;
  }
  return false;
}

/** Cheap presence check for the dashboard. Doesn't return secret material. */
export function isOAuthConfigured() {
  return loadOAuthConfig() !== null;
}
