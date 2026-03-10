# syntax=docker/dockerfile:1
FROM ghcr.io/labmarketai/agentic-node-base:latest

USER root

# System deps — apt download cache persists across builds (BuildKit cache mount)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        pandoc \
        texlive-xetex \
        texlive-fonts-recommended \
        texlive-plain-generic

# Python deps — copy requirements first so code changes don't bust this layer
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /app/requirements.txt

# Playwright — browser binaries must live in the image; system deps use apt cache mount
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    playwright install --with-deps chromium

# App code — copied last so frequent changes don't invalidate the layers above
COPY . /app/

ENV NODE_CLASS=node.JupyterNode
ENV NODE_TYPE=jupyter
ENV JUPYTER_LAB_PORT=8888

CMD ["sh", "/app/start.sh"]
