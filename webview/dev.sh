#!/usr/bin/env bash
# Wrapper so vite respects $PORT env var (preview_start sets it dynamically).
set -e
cd "$(dirname "$0")"
exec npx vite --host 127.0.0.1 --port "${PORT:-5173}"
