#!/bin/sh
set -e

export PATH="${PATH}:/home/appuser/.local/bin"

# Ensure the workspace directory exists and seed template notebooks.
# Templates under /app/notebooks are treated as canonical starters and are
# backfilled into the workspace whenever missing (for example, if a user deleted
# one), while existing workspace files are never overwritten.
WORKSPACE="${JUPYTER_ROOT_DIR:-/workspace}"
mkdir -p "$WORKSPACE"
if [ -d /app/notebooks ]; then
    for nb in /app/notebooks/*.ipynb; do
        [ -f "$nb" ] || continue
        dest="$WORKSPACE/$(basename "$nb")"
        [ -f "$dest" ] || cp "$nb" "$dest"
    done

    # Keep deployed README notebook synchronized for persistent workspaces.
    if [ -f /app/notebooks/README.ipynb ]; then
        mkdir -p "$WORKSPACE/notebooks"
        cp /app/notebooks/README.ipynb "$WORKSPACE/notebooks/README.ipynb"
        cp /app/notebooks/README.ipynb "$WORKSPACE/README.ipynb"
    fi
fi

# JupyterLab is started and managed by node.py (FastAPI lifespan).
# Start the FastAPI node
exec uvicorn app.main:app --host 0.0.0.0 --port "${NODE_PORT:-8003}"
