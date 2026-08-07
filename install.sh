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
command -v npm >/dev/null 2>&1 || { echo "✗ npm not found — install Node 18+ (needed to build the UI)"; exit 1; }

# Find a Python 3.10+ interpreter. macOS ships 3.9 as `python3`, which is too old, so
# also look for versioned binaries (python3.13 … python3.10) a newer install provides.
PYBIN=""
for cand in python3 python3.13 python3.12 python3.11 python3.10; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
    PYBIN="$cand"; break
  fi
done
if [ -z "$PYBIN" ]; then
  have=$(python3 -V 2>&1 | awk '{print $2}')
  echo "✗ Cairn needs Python 3.10+ (found ${have:-none})."
  echo "  Install a newer Python and re-run ./install.sh, e.g.:"
  echo "      brew install python@3.12          # Homebrew"
  echo "  or download it from https://www.python.org/downloads/"
  exit 1
fi

# --- python env + package --------------------------------------------------
echo "==> Python environment + package  (using $PYBIN, $("$PYBIN" -V 2>&1 | awk '{print $2}'))"
# Recreate the venv if it's missing OR was built with an old Python (e.g. a failed first run).
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
  echo "   existing .venv uses an old Python — recreating it"
  rm -rf .venv
fi
[ -d .venv ] || "$PYBIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
# embed = model2vec + scikit-learn, the offline embeddings the topic organizer needs;
# leaving it out gives a working app whose auto-organizing quietly no-ops.
python -m pip install --quiet -e ".[api,extract,web,embed]"

# --- web UI ----------------------------------------------------------------
echo "==> Building the web UI (first run downloads npm packages)"
( cd ui && npm install --no-audit --no-fund --silent && npm run build >/dev/null )

# --- background agents -----------------------------------------------------
if [ "${CAIRN_NO_AGENTS:-0}" = "1" ]; then
  echo "==> Skipping background agents (CAIRN_NO_AGENTS=1)"
  echo
  echo "✅ Setup complete. Start Cairn with:  source .venv/bin/activate && tt serve"
else
  echo "==> Installing & starting the background agents (serve · poll · autosave · backup)"
  tt autostart
  echo
  echo "✅ Cairn is running:  http://localhost:8765"
fi
echo
echo "Next:"
echo "  • Open it, then Settings → Agent to pick a model backend (or None — search and"
echo "    manual tagging work without one)."
echo "  • Save from Chrome: open chrome://extensions, enable Developer mode, Load"
echo "    unpacked ./extension, then press ⌘⇧E on any tab."
echo "  • Tip: install the page as an app (Chrome ⋮ → Install page as app) for a window"
echo "    of its own."
