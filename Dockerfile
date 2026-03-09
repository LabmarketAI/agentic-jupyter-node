FROM ghcr.io/labmarketai/agentic-node-base:latest

COPY . /app/

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc \
        texlive-xetex \
        texlive-fonts-recommended \
        texlive-plain-generic && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    playwright install --with-deps chromium

ENV NODE_CLASS=node.JupyterNode
ENV NODE_TYPE=jupyter
ENV JUPYTER_LAB_PORT=8888

CMD ["sh", "/app/start.sh"]
