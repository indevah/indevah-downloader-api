#!/usr/bin/env bash
# INDEVAH Downloader — start.sh
# Runs on every Render startup.
# 1. Starts the bgutil PO Token HTTP server on port 4416 (background)
# 2. Starts Flask/gunicorn on $PORT (foreground)

set -e

export NVM_DIR="$HOME/.nvm"
source "$NVM_DIR/nvm.sh" 2>/dev/null || true
nvm use --lts 2>/dev/null || true

BGUTIL_DIR="$HOME/bgutil-server/server"
BGUTIL_PORT="${BGUTIL_PORT:-4416}"

echo "=== Starting bgutil PO Token server on port $BGUTIL_PORT ==="

if [ -f "$BGUTIL_DIR/build/main.js" ]; then
  # Compiled JS exists — use it (faster startup)
  node "$BGUTIL_DIR/build/main.js" --port "$BGUTIL_PORT" &
else
  # Fallback: run TypeScript directly with ts-node if available
  echo "WARNING: bgutil build/main.js not found. Trying ts-node fallback..."
  cd "$BGUTIL_DIR"
  npx ts-node src/main.ts --port "$BGUTIL_PORT" &
fi

BGUTIL_PID=$!
echo "bgutil PID: $BGUTIL_PID"

# Wait for bgutil server to be ready (max 30s)
echo "=== Waiting for bgutil server to be ready ==="
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$BGUTIL_PORT/ping" > /dev/null 2>&1; then
    echo "bgutil server is ready (took ${i}s)"
    break
  fi
  sleep 1
done

echo "=== Starting Flask API on port $PORT ==="
exec gunicorn app:app \
  --workers 2 \
  --bind "0.0.0.0:$PORT" \
  --timeout 120 \
  --keep-alive 5 \
  --log-level info
