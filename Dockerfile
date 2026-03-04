FROM ghcr.io/labmarketai/agentic-node-base:latest

COPY . /app/

RUN pip install --no-cache-dir -r /app/requirements.txt

RUN pip install --no-cache-dir jupyterlab

ENV NODE_CLASS=node.JupyterNode
ENV NODE_TYPE=jupyter
