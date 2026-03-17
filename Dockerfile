# syntax=docker/dockerfile:1
FROM acragenticaiprod.azurecr.io/agentic-node-base:latest

USER root

# System deps — layer is cached by Docker; only re-runs when these packages change
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc \
        texlive-xetex \
        texlive-fonts-recommended \
        texlive-plain-generic && \
    rm -rf /var/lib/apt/lists/*

# Python deps — pip wheel cache persists across builds (BuildKit cache mount)
# Copy requirements first so code changes don't bust this layer
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /app/requirements.txt

# Playwright browser binaries must live in the image
RUN playwright install --with-deps chromium

# App code — copied last so routine code edits don't invalidate any layer above
COPY . /app/

ENV NODE_CLASS=node.JupyterNode
ENV NODE_TYPE=jupyter
ENV JUPYTER_LAB_PORT=8888

CMD ["sh", "/app/start.sh"]
