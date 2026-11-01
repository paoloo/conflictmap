#!/usr/bin/env bash
# Launch the conflict map in CyberEther.
# Regenerates flowgraph.yml (bakes absolute paths), then opens the app.
set -euo pipefail
cd "$(dirname "$0")"

# Prefer the conda env with the intel deps (numpy, requests) as the Python
# runtime CyberEther's python blocks will use.
CONDA_ENV="${CONDA_ENV:-core}"
PY="$HOME/Workspace/CONDA/envs/$CONDA_ENV/bin/python"
if [[ ! -x "$PY" ]]; then
    PY="$(command -v python3)"
fi

"$PY" generate_flowgraph.py

APP="/Applications/CyberEther.app/Contents/MacOS/CyberEther"
if [[ ! -x "$APP" ]]; then
    echo "CyberEther not found at $APP" >&2
    exit 1
fi

exec "$APP" \
    --python-runtime "$PY" \
    --dependency-policy allow \
    "$(pwd)/flowgraph.yml"
