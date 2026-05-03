#!/usr/bin/env bash
# INDEVAH Downloader — build.sh
# Runs during Render build phase.
# Installs Python deps + Node.js bgutil PO token server.

set -e

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo "=== Installing Node.js (via nvm) ==="
export NVM_DIR="$HOME/.nvm"

# Install nvm if not present
if [ ! -f "$NVM_DIR/nvm.sh" ]; then
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
fi

# Load nvm
source "$NVM_DIR/nvm.sh"

# Install Node.js LTS
nvm install --lts
nvm use --lts

node --version
npm --version

echo "=== Cloning bgutil-ytdlp-pot-provider server ==="
if [ -d "$HOME/bgutil-server" ]; then
  echo "bgutil-server already exists, pulling latest..."
  cd "$HOME/bgutil-server"
  git pull || true
else
  git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$HOME/bgutil-server"
fi

echo "=== Installing bgutil server dependencies ==="
cd "$HOME/bgutil-server/server"
npm ci --prefer-offline || npm install

echo "=== Building bgutil server (TypeScript → JavaScript) ==="
npx tsc || true   # compile; ignore warnings

echo "=== Build complete ==="
