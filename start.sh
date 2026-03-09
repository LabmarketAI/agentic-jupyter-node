#!/bin/sh
set -e

export PATH="${PATH}:/home/appuser/.local/bin"

# Start JupyterLab in background (no auth, bound to all interfaces)
jupyter lab \
    --ip=0.0.0.0 \
    --port="${JUPYTER_LAB_PORT:-8888}" \
    --no-browser \
    --ServerApp.token="" \
    --ServerApp.password="" \
    --ServerApp.allow_origin="*" \
    --ServerApp.root_dir=/app \
    --ServerApp.iopub_data_rate_limit=0 \
    &

# Start the FastAPI node
exec uvicorn app.main:app --host 0.0.0.0 --port "${NODE_PORT:-8003}"
