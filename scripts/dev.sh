#!/usr/bin/env bash
# Starts the Vantage backend (127.0.0.1:8765) and the Vite dev server (localhost:5173) together.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
(cd "$root/backend" && uv run vantage) &
backend=$!
trap 'kill $backend 2>/dev/null || true' EXIT
cd "$root/frontend"
[ -d node_modules ] || npm install
npm run dev
