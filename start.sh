#!/bin/sh
set -e

export PATH="${PATH}:/home/appuser/.local/bin"

# Ensure the workspace directory exists and seed demo notebooks on first run.
# The workspace is bind-mounted from the host (./workspace) so notebooks
# survive container restarts and are never committed to git.
WORKSPACE="${JUPYTER_ROOT_DIR:-/workspace}"
mkdir -p "$WORKSPACE"
if [ -d /app/notebooks ]; then
    SEED_MARKER="$WORKSPACE/.notebook_seed_v1"
    if [ ! -f "$SEED_MARKER" ]; then
        for nb in /app/notebooks/*.ipynb; do
            [ -f "$nb" ] || continue
            dest="$WORKSPACE/$(basename "$nb")"
            # Only copy if the file does not already exist so user edits are preserved
            [ -f "$dest" ] || cp "$nb" "$dest"
        done
        date -u +"%Y-%m-%dT%H:%M:%SZ" > "$SEED_MARKER"
    fi

    # Keep deployed guidance current for persistent workspaces.
    if [ -f /app/notebooks/README.ipynb ]; then
        cp /app/notebooks/README.ipynb "$WORKSPACE/README.ipynb"
        mkdir -p "$WORKSPACE/notebooks"
        cp /app/notebooks/README.ipynb "$WORKSPACE/notebooks/README.ipynb"
    fi
fi

# Start JupyterLab in background (no auth, bound to all interfaces)
jupyter lab \
    --ip=0.0.0.0 \
    --port="${JUPYTER_LAB_PORT:-8888}" \
    --no-browser \
    --ServerApp.token="" \
    --ServerApp.password="" \
    --ServerApp.allow_origin="*" \
    --ServerApp.root_dir="${JUPYTER_ROOT_DIR:-/workspace}" \
    --ServerApp.iopub_data_rate_limit=0 \
    &

# Start the FastAPI node
exec uvicorn app.main:app --host 0.0.0.0 --port "${NODE_PORT:-8003}"
