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
command -v node >/dev/null 2>&1 || { echo "✗ Node not found — install Node 18+ (https://nodejs.org, or: brew install node)"; exit 1; }
command -v npm  >/dev/null 2>&1 || { echo "✗ npm not found — install Node 18+ (https://nodejs.org, or: brew install node)"; exit 1; }
node_major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
[ "${node_major:-0}" -ge 18 ] || { echo "✗ Node 18+ required (found $(node -v)). Update Node and re-run ./install.sh"; exit 1; }

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
  have=$(command -v python3 >/dev/null 2>&1 && python3 -V 2>&1 | awk '{print $2}' || echo none)
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

# --- background agents + launch --------------------------------------------
if [ "${CAIRN_NO_AGENTS:-0}" = "1" ]; then
  echo "==> Skipping background agents (CAIRN_NO_AGENTS=1)"
  echo
  echo "✅ Setup complete. Start Cairn with:  source .venv/bin/activate && tt serve"
else
  echo "==> Installing & starting the background agents (serve · poll · autosave · backup)"
  tt autostart
  # Confirm the server actually came up (launchd starts it in the background). The
  # FIRST boot on a fresh machine imports the whole scientific stack cold, so give
  # it a generous window before deciding something is wrong.
  printf "==> Waiting for the server (first cold start can take up to a minute)"
  up=0
  for _ in $(seq 1 90); do
    if curl -s -m 4 -o /dev/null http://localhost:8765/api/stats 2>/dev/null; then up=1; break; fi
    printf "."; sleep 1
  done
  echo
  if [ "$up" = "1" ]; then
    echo "✅ Cairn is running:  http://localhost:8765"
    command -v open >/dev/null 2>&1 && open http://localhost:8765 >/dev/null 2>&1 || true
  else
    # Don't just point at a log the user then has to go read -- SHOW it, and run a
    # one-off foreground boot on a spare port to surface the real Python error
    # (launchd can swallow a traceback; a direct run prints it).
    echo "⚠️  The server didn't answer in 90s. Its launchd log (/tmp/cairn.serve.err):"
    echo "    ----------------------------------------------------------------"
    tail -n 25 /tmp/cairn.serve.err 2>/dev/null | sed 's/^/    /' || echo "    (no log at /tmp/cairn.serve.err yet)"
    echo "    ----------------------------------------------------------------"
    echo "==> Trying a one-off direct boot on port 8766 to surface any startup error…"
    tt serve --port 8766 >/tmp/cairn.diagnose.log 2>&1 &
    diagpid=$!
    diag_up=0
    for _ in $(seq 1 60); do
      if curl -s -m 4 -o /dev/null http://localhost:8766/api/stats 2>/dev/null; then diag_up=1; break; fi
      sleep 1
    done
    if [ "$diag_up" = "1" ]; then
      echo "    ✓ A direct boot WORKS — the app is fine; launchd was just slow to start it."
      echo "      Reload http://localhost:8765 in ~30s, or run:  tt autostart"
    else
      echo "    ✗ The direct boot also failed. The actual error is below — send this:"
      echo "    ----------------------------------------------------------------"
      tail -n 30 /tmp/cairn.diagnose.log 2>/dev/null | sed 's/^/    /'
      echo "    ----------------------------------------------------------------"
    fi
    kill "$diagpid" 2>/dev/null || true
  fi
fi
echo
echo "Next steps:"
echo "  • Pick a model backend in Settings → Agent (or None — search + manual tagging"
echo "    work without one; keys stay in ~/.cairn/config.json)."
echo "  • Load the Chrome extension: chrome://extensions → Developer mode → Load unpacked"
echo "    → the ./extension folder, then press ⌘⇧E on any tab to save it. (macOS may ask"
echo "    to allow Chrome automation the first time autosave runs — approve it.)"
echo "  • Optional: install the page as a Chrome app (⋮ → Install page as app) for its"
echo "    own window."
