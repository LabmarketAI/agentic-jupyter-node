FROM ghcr.io/labmarketai/agentic-node-base:latest

COPY . /app/

RUN pip install --no-cache-dir -r /app/requirements.txt

ENV NODE_CLASS=node.JupyterNode
ENV NODE_TYPE=jupyter
ENV JUPYTER_LAB_PORT=8888

CMD ["sh", "/app/start.sh"]
