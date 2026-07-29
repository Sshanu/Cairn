#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Save window to cairn
# @raycast.mode compact
#
# Optional parameters:
# @raycast.icon 🗂
# @raycast.packageName cairn
#
# Documentation:
# @raycast.description Save every tab in the front Chrome window. Tag them later with `tt enrich`.

set -euo pipefail

CAIRN_HOME="${CAIRN_HOME:-$HOME/code/cairn}"
TT="$CAIRN_HOME/.venv/bin/tt"

if [ ! -x "$TT" ]; then
  echo "cairn not installed at $CAIRN_HOME (set CAIRN_HOME)"
  exit 1
fi

exec "$TT" save --window --no-enrich
