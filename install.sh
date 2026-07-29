#!/usr/bin/env bash
#
# Cairn — one-shot setup. Creates the Python environment, builds the web UI, and
# installs the background agents that keep the app running at localhost:8765.
# Safe to re-run: it reuses an existing venv and just refreshes everything.
#
set -euo pipefail
cd "$(dirname "$0")"

echo "Cairn setup"
echo "==========="

# --- prerequisites ---------------------------------------------------------
command -v python3 >/dev/null 2>&1 || { echo "✗ python3 not found — install Python 3.10+"; exit 1; }
command -v npm     >/dev/null 2>&1 || { echo "✗ npm not found — install Node 18+ (needed to build the UI)"; exit 1; }

# --- python env + package --------------------------------------------------
echo "==> Python environment + package"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[api,extract,web]"

# --- web UI ----------------------------------------------------------------
echo "==> Building the web UI (first run downloads npm packages)"
( cd ui && npm install --no-audit --no-fund --silent && npm run build >/dev/null )

# --- background agents -----------------------------------------------------
echo "==> Installing & starting the background agents (serve · poll · autosave · backup)"
tt autostart

echo
echo "✅ Cairn is running:  http://localhost:8765"
echo
echo "Next:"
echo "  • Open it, then Settings → Agent to pick a model backend (or None — search and"
echo "    manual tagging work without one)."
echo "  • Save from Chrome: open chrome://extensions, enable Developer mode, Load"
echo "    unpacked ./extension, then press ⌘⇧E on any tab."
echo "  • Tip: install the page as an app (Chrome ⋮ → Install page as app) for a window"
echo "    of its own."
