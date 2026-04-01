#!/bin/sh
set -e

export PATH="${PATH}:/home/appuser/.local/bin"

# Ensure workspace exists. Notebook reconciliation is handled in node.py so we
# have version-aware, conflict-safe behavior across mounted volumes.
WORKSPACE="${JUPYTER_ROOT_DIR:-/workspace}"
mkdir -p "$WORKSPACE"

# JupyterLab is started and managed by node.py (FastAPI lifespan).
# Start the FastAPI node
exec uvicorn app.main:app --host 0.0.0.0 --port "${NODE_PORT:-8003}"
