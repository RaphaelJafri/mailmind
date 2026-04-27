#!/usr/bin/env node
// HTTP server for the Node ingester sidecar. Exposes deterministic plumbing
// (Gmail sync, classification, reconcile) to the Tauri shell over localhost.
//
// Default port: 8766. Override via PORT env or --port flag.
//
// P0 surface: /health only. Sync + classification endpoints land in P1.

import 'dotenv/config';
import express from 'express';
import { existsSync } from 'node:fs';

import { dbPath } from './lib/db.mjs';
import { tokensExist } from './lib/gmail-client.mjs';
import { dataDir } from './lib/paths.mjs';

const DEFAULT_PORT = 8766;

function parsePort(argv) {
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--port') return Number(argv[++i]);
  }
  return Number(process.env.PORT) || DEFAULT_PORT;
}

const app = express();
app.use(express.json({ limit: '1mb' }));

app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    sidecar: 'mailmind-ingester',
    node_version: process.version,
    data_dir: dataDir(),
    raw_db_exists: existsSync(dbPath('raw')),
    derived_db_exists: existsSync(dbPath('derived')),
    gmail_tokens_present: tokensExist(),
    started_at: STARTED_AT,
  });
});

const STARTED_AT = new Date().toISOString();
const port = parsePort(process.argv.slice(2));

const server = app.listen(port, '127.0.0.1', () => {
  console.log(`[ingester] listening on http://127.0.0.1:${port}`);
});

function shutdown(signal) {
  console.log(`[ingester] ${signal} — shutting down`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
