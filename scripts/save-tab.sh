#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Save tab to cairn
# @raycast.mode compact
#
# Optional parameters:
# @raycast.icon 📑
# @raycast.packageName cairn
# @raycast.argument1 { "type": "text", "placeholder": "tags (space separated)", "optional": true }
#
# Documentation:
# @raycast.description Save the front Chrome tab to the cairn library.

set -euo pipefail

CAIRN_HOME="${CAIRN_HOME:-$HOME/code/cairn}"
TT="$CAIRN_HOME/.venv/bin/tt"

if [ ! -x "$TT" ]; then
  echo "cairn not installed at $CAIRN_HOME (set CAIRN_HOME)"
  exit 1
fi

args=()
for tag in ${1:-}; do
  args+=(--tag "$tag")
done

exec "$TT" save "${args[@]}"
