# agentic-jupyter-node

Jupyter node for the Labmarket agentic ecosystem. Provides an interactive JupyterLab environment alongside a FastAPI node that integrates with the orchestrator via the [Google A2A protocol](https://github.com/google-a2a/a2a-python). Supports live cell output streaming, direct access to sibling nodes (model-node, qiskit-node, cheng-dataset-node), and LLM-assisted code generation.

> **Questions or issues?** Visit the orchestrator at <!-- ORCHESTRATOR_URL -->http://localhost:8000<!-- /ORCHESTRATOR_URL --> — the main application and entry point for the full stack. Set `ORCHESTRATOR_URL` in `.env` to point here when deployed.

---

## Deployed JupyterLab (Production-Focused)

In deployed environments, notebooks are saved in a persistent Azure Files share mounted at `/workspace` inside the Jupyter container.

- Application code and demo notebooks live in the image at `/app`.
- User notebooks and generated files live in `/workspace`.
- Deployments replace the image, but preserve `/workspace`.

This split is what keeps user notebooks safe across revisions and rollouts.

---

## How Notebook Saving Works In Deployed Environments

Notebooks created in JupyterLab are saved under `/workspace`.

In production, `/workspace` is mounted from an Azure Files share (`jupyter-workspace`) configured in Azure Container Apps infrastructure.

### How it works

| Path | What it is |
|------|-----------|
| `/workspace` | JupyterLab root and persistent user data location |
| `/app/notebooks` | Demo notebooks baked into the image (release content) |
| Azure Files share (`jupyter-workspace`) | Backing storage mounted to `/workspace` |

On first start, the demo notebooks (`cheng_demo.ipynb`, `qiskit_demo.ipynb`, `README.ipynb`) are copied into `/workspace` automatically.

To protect user content during upgrades, startup writes a one-time marker file (`/workspace/.notebook_seed_v1`) after initial seed. On later restarts/deployments, seeding is skipped entirely for that workspace.

### Finding your notebooks

```
workspace/
├── cheng_demo.ipynb
├── qiskit_demo.ipynb
├── README.ipynb
└── my_notebook.ipynb
```

### Best Practices (Do Not Lose User Notebooks)

1. Mount persistent storage at the same path used by Jupyter root (`JUPYTER_ROOT_DIR`, default `/workspace`).
2. Keep demo notebooks in `/app/notebooks` (image content) and all user work in `/workspace` (persistent data).
3. Treat `/app` as immutable release content and `/workspace` as user-owned content.
4. Snapshot or back up `/workspace` on a schedule.
5. Never write user notebooks into `/app` paths.

With this layout, deployments update the application image without overwriting notebooks created by users.

---

## Accessing JupyterLab In Deployment

JupyterLab is exposed through the deployed stack URL published by the orchestrator environment.

If you open JupyterLab in deployment and create/save notebooks, files are written to `/workspace` and retained across deployments as long as the mounted Azure Files share is preserved.

---

## Available Demo Notebooks

| Notebook | What it demonstrates |
|----------|---------------------|
| `cheng_demo.ipynb` | Querying the Cheng protein-protein interaction dataset via SQL and natural language |
| `qiskit_demo.ipynb` | Running quantum circuits through the qiskit-node and visualising results |

---

## Kernel Management

Kernels are started on demand and are independent of the JupyterLab UI. You can manage them via the orchestrator chat, the node's MCP tools, or its A2A interface.

### Via the orchestrator chat

```
start kernel analysis
run in kernel analysis: import pandas as pd; df = pd.read_csv('/workspace/data.csv'); df.head()
stop kernel analysis
list kernels
```

### Via the REST API (docs at `/docs`)

| Endpoint | Description |
|----------|-------------|
| `POST /rpc` | A2A JSON-RPC — `message/send` with kernel commands |
| `POST /infer` | Proxy inference request to model-node |
| `POST /circuit/run` | Proxy quantum circuit to qiskit-node |
| `POST /cheng/query` | Run a SQL query against the Cheng dataset |
| `POST /cheng/ask` | Ask a natural-language question about the dataset |
| `GET  /cheng/graph` | Fetch a PPI neighbourhood graph |
| `GET  /health` | Liveness check |
| `GET  /.well-known/a2a` | A2A agent card |

### Live output streaming

Tag a cell with `# pubsub: <topic>` and subscribe via WebSocket to receive output in real time:

```python
# pubsub: my-analysis
for i in range(10):
    print(f"step {i}")
```

```bash
# Subscribe from another terminal or service
wscat -c ws://localhost:8002/ws/pubsub/my-analysis
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NODE_PORT` | `8002` | FastAPI node port |
| `JUPYTER_LAB_PORT` | `8888` | JupyterLab UI port |
| `JUPYTER_ROOT_DIR` | `/workspace` | JupyterLab root directory inside container |
| `SIBLING_MODEL_NODE_URL` | `http://model-node:8001` | Model-node for LLM inference |
| `SIBLING_QISKIT_NODE_URL` | `http://qiskit-node:8003` | Qiskit-node for quantum circuits |
| `SIBLING_CHENG_NODE_URL` | `http://cheng-dataset-node:8004` | Cheng dataset node |
| `CHENG_POSTGRES_DSN` | — | Postgres DSN for direct DB access from kernels |
| `A2A_AUTH_TOKEN` | — | Bearer token required on all routes (set in production) |
| `NODE_ENV` | `development` | Set to `production` to enable security guards |

---

## Tips

**Sharing data between kernels and the host**

Files written to `/workspace` inside the container are immediately available in `./workspace/` on your host:

```python
# In a kernel
import pandas as pd
df.to_csv('/workspace/results.csv')
```

**Installing packages in a running kernel**

```python
import subprocess, sys
subprocess.run([sys.executable, '-m', 'pip', 'install', 'seaborn'], check=True)
import seaborn
```

**Exporting a notebook to PDF**

PDF export (via LaTeX + Pandoc) is supported. Use JupyterLab's `File → Save and Export Notebook As → PDF` menu, or call `nbconvert` directly in a terminal cell:

```bash
jupyter nbconvert --to pdf /workspace/my_notebook.ipynb
```

**Restarting without losing work**

Because `./workspace/` is on the host, restarting the container is safe:

```bash
docker compose restart jupyter-node
```

Your notebooks and any files in `/workspace` are untouched.

---

## Further Reading

- Orchestrator app: <!-- ORCHESTRATOR_URL -->http://localhost:8000<!-- /ORCHESTRATOR_URL --> — main application, UI, and API docs
- Orchestrator API docs: <!-- ORCHESTRATOR_URL -->http://localhost:8000<!-- /ORCHESTRATOR_URL -->/docs
- [Google A2A protocol](https://github.com/google-a2a/a2a-python) — agent-to-agent communication spec
- [agentic-node-base](https://github.com/LabmarketAI/agentic-node-base) — base class shared by all nodes
