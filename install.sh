#!/usr/bin/env bash
# install.sh — one-shot setup for hermes-web-clis
set -e

echo "=== Hermes Web CLIs Setup ==="

# Detect Python
PY="${PYTHON:-python3}"
if ! command -v "$PY" &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+ first."
    exit 1
fi
echo "Python: $($PY --version)"

# Install dependencies
echo "Installing Python packages..."
$PY -m pip install playwright browser-cookie3

# Install Chromium for Playwright
echo "Installing Chromium..."
$PY -m playwright install chromium

# Create config directory
mkdir -p ~/.hermes-web-clis

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Log into AI platforms in Firefox and Chrome"
echo "  2. Extract cookies:"
echo "     $PY cli/claude.py --save-all"
echo "     $PY cli/grok.py --save-auth"
echo "     $PY cli/mimo.py --login"
echo "  3. Create CDP profile:"
echo "     $PY scripts/cdp_server.py login"
echo "  4. Start CDP daemon:"
echo "     $PY scripts/cdp_server.py start --headed"
echo "  5. Test:"
echo "     $PY cli/claude.py 'Hello'"
echo ""
echo "See README.md for full documentation."
