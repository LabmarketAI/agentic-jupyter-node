#!/bin/sh
set -e

export PATH="${PATH}:/home/appuser/.local/bin"

# Ensure the workspace directory exists and seed template notebooks.
# NOTEBOOK_SYNC_MODE controls refresh behavior for files under /app/notebooks:
#   - missing   (default): only backfill missing files
#   - overwrite: replace existing workspace copies on every startup
WORKSPACE="${JUPYTER_ROOT_DIR:-/workspace}"
NOTEBOOK_SYNC_MODE="${NOTEBOOK_SYNC_MODE:-missing}"
mkdir -p "$WORKSPACE"
if [ -d /app/notebooks ]; then
    mkdir -p "$WORKSPACE/notebooks"
    for nb in /app/notebooks/*.ipynb; do
        [ -f "$nb" ] || continue
        base="$(basename "$nb")"
        for dest in "$WORKSPACE/$base" "$WORKSPACE/notebooks/$base"; do
            if [ "$NOTEBOOK_SYNC_MODE" = "overwrite" ] || [ ! -f "$dest" ]; then
                cp "$nb" "$dest"
            fi
        done
    done
fi

# JupyterLab is started and managed by node.py (FastAPI lifespan).
# Start the FastAPI node
exec uvicorn app.main:app --host 0.0.0.0 --port "${NODE_PORT:-8003}"
