// Path resolution for mailmind data, tokens, config, and reports.
//
// Default lives under macOS Application Support so the desktop app keeps its
// state outside the repo. Override with MAILMIND_DATA_DIR for tests, demo
// mode, or non-default install locations.

import { homedir } from 'node:os';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

function defaultDataDir() {
  if (process.env.MAILMIND_DATA_DIR) {
    return resolve(process.env.MAILMIND_DATA_DIR);
  }
  // macOS canonical app-support path. Linux/Windows wired up later.
  return resolve(homedir(), 'Library', 'Application Support', 'mailmind');
}

const DATA_DIR = defaultDataDir();

export function dataDir() {
  return DATA_DIR;
}

export function dbDir() {
  const p = resolve(DATA_DIR, 'db');
  mkdirSync(p, { recursive: true });
  return p;
}

export function tokensDir() {
  const p = resolve(DATA_DIR, 'tokens');
  mkdirSync(p, { recursive: true });
  return p;
}

export function configDir() {
  const p = resolve(DATA_DIR, 'config');
  mkdirSync(p, { recursive: true });
  return p;
}

export function reportsDir() {
  const p = resolve(DATA_DIR, 'reports');
  mkdirSync(p, { recursive: true });
  return p;
}

export function logsDir() {
  // Logs go to ~/Library/Logs/mailmind on macOS for OS-level discoverability.
  const p =
    process.env.MAILMIND_LOGS_DIR ||
    resolve(homedir(), 'Library', 'Logs', 'mailmind');
  mkdirSync(p, { recursive: true });
  return p;
}

export function tokenPath() {
  return resolve(tokensDir(), 'gmail.json');
}

export function filtersPath() {
  return resolve(configDir(), 'filters.yml');
}
