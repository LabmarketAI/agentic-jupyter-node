# syntax=docker/dockerfile:1
ARG BASE_IMAGE=ghcr.io/labmarketai/agentic-node-base:latest
FROM ${BASE_IMAGE}

USER root

# System deps — layer is cached by Docker; only re-runs when these packages change
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc \
        texlive-xetex \
        texlive-fonts-recommended \
        texlive-plain-generic && \
    rm -rf /var/lib/apt/lists/*

# Python deps
# Copy requirements first so code changes don't bust this layer.
# Use a standard pip install so ACR builds work even without BuildKit mounts.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Playwright browser binaries must live in the image
RUN playwright install --with-deps chromium

# App code — copied last so routine code edits don't invalidate any layer above
COPY --chown=appuser:appuser . /app/

RUN chown -R appuser:appuser /app

USER appuser

ENV NODE_CLASS=node.JupyterNode
ENV NODE_TYPE=jupyter
ENV JUPYTER_LAB_PORT=8888

CMD ["sh", "/app/start.sh"]
