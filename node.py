"""
Jupyter-node: dedicated sibling container for Jupyter kernel management.

Extends agentic-node-base and exposes:
- MCP tools for kernel lifecycle and code execution
- /ws/pubsub/<topic>  — real-time IOPub output streaming
- /ws/pubsub/<topic>/history — ring-buffer history (inherited from base)
- /ws/topics          — list active pubsub topics
- POST /infer         — direct LLM inference via SiblingClient (no A2A)

Communication layers used:
  Postgres   → app.state.db (asyncpg, direct — no agent layer)
  model-node → SiblingClient("SIBLING_MODEL_NODE_URL") (direct HTTP)
  A2A /rpc   → JupyterExecutor (orchestrator-mediated tasks only)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import structlog
from contextlib import asynccontextmanager
from typing import Any

try:
    from app.base_node import BaseNode
    from app.services.sibling import SiblingClient
    from app.services.pubsub import PubSubManager
except ImportError:
    from agentic_node_base.base_node import BaseNode  # type: ignore
    SiblingClient = None  # type: ignore
    PubSubManager = None  # type: ignore

import httpx
from a2a.client.client_factory import ClientFactory, ClientConfig
from a2a.server.agent_execution import AgentExecutor
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, Message
from a2a.utils import new_agent_text_message
from fastapi import Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger()

# ── IOPub topic tag parsing ───────────────────────────────────────────────────

_PUBSUB_TAG = re.compile(r"#\s*pubsub\s*:\s*(\S+)")


def _extract_topic(code: str) -> str | None:
    """Return the pubsub topic from a '# pubsub: <topic>' comment, or None."""
    m = _PUBSUB_TAG.search(code)
    return m.group(1) if m else None


# ── In-memory kernel registry ─────────────────────────────────────────────────

_kernels: dict[str, object] = {}       # name → AsyncKernelManager
_kernel_topics: dict[str, str] = {}    # kernel_name → current pubsub topic


async def _start_kernel(name: str, app: Any = None) -> dict:
    """Launch a named IPython kernel, no-op if already running."""
    try:
        from jupyter_client import AsyncKernelManager
    except ImportError:
        return {"error": "jupyter_client not installed"}

    if name in _kernels:
        return {"name": name, "status": "already_running"}

    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel()
    _kernels[name] = km
    logger.info("kernel.started", name=name)

    # Start IOPub listener for this kernel if pubsub is available
    if app is not None and hasattr(app.state, "pubsub"):
        asyncio.create_task(_iopub_listener(name, km, app.state.pubsub))

    return {"name": name, "status": "started"}


async def _iopub_listener(
    kernel_name: str, km: Any, pubsub: "PubSubManager"
) -> None:
    """
    Listen on the kernel's IOPub socket and publish tagged cell outputs
    to the PubSubManager under /ws/pubsub/<topic>.

    Cells opt in with a comment: # pubsub: <topic>
    All subsequent outputs for that execution are published to that topic.
    """
    logger.info("iopub_listener.start", kernel=kernel_name)
    kc = km.client()
    kc.start_channels()
    try:
        await asyncio.to_thread(kc.wait_for_ready, timeout=10)
    except Exception as exc:
        logger.error("iopub_listener.ready_failed", kernel=kernel_name, error=str(exc))
        kc.stop_channels()
        return

    current_topic: str | None = None
    current_msg_id: str | None = None

    try:
        while kernel_name in _kernels:
            try:
                msg = await asyncio.wait_for(
                    asyncio.to_thread(kc.get_iopub_msg, timeout=1.0),
                    timeout=2.0,
                )
            except (asyncio.TimeoutError, Exception):
                continue

            msg_type = msg.get("msg_type", "")
            content = msg.get("content", {})
            header = msg.get("header", {})
            parent = msg.get("parent_header", {})
            msg_id = header.get("msg_id", "")
            parent_id = parent.get("msg_id", "")

            # Detect a new cell execution: extract pubsub tag from source
            if msg_type == "execute_input":
                code = content.get("code", "")
                topic = _extract_topic(code)
                if topic:
                    current_topic = topic
                    current_msg_id = parent_id
                    _kernel_topics[kernel_name] = topic
                    logger.info("iopub_listener.tagged", kernel=kernel_name, topic=topic)
                else:
                    current_topic = None
                    current_msg_id = None
                    _kernel_topics.pop(kernel_name, None)
                continue

            # Only publish outputs that belong to the tagged execution
            if current_topic is None or parent_id != current_msg_id:
                continue

            envelope: dict | None = None

            if msg_type == "stream":
                envelope = {
                    "topic": current_topic,
                    "kernel": kernel_name,
                    "msg_id": msg_id,
                    "msg_type": "stream",
                    "name": content.get("name", "stdout"),
                    "data": content.get("text", ""),
                }
            elif msg_type == "execute_result":
                envelope = {
                    "topic": current_topic,
                    "kernel": kernel_name,
                    "msg_id": msg_id,
                    "msg_type": "execute_result",
                    "data": content.get("data", {}),
                    "metadata": content.get("metadata", {}),
                }
            elif msg_type == "display_data":
                envelope = {
                    "topic": current_topic,
                    "kernel": kernel_name,
                    "msg_id": msg_id,
                    "msg_type": "display_data",
                    "data": content.get("data", {}),
                    "metadata": content.get("metadata", {}),
                }
            elif msg_type == "error":
                envelope = {
                    "topic": current_topic,
                    "kernel": kernel_name,
                    "msg_id": msg_id,
                    "msg_type": "error",
                    "ename": content.get("ename", ""),
                    "evalue": content.get("evalue", ""),
                    "traceback": content.get("traceback", []),
                }
            elif msg_type == "status" and content.get("execution_state") == "idle":
                # Cell finished — clear topic association
                current_topic = None
                current_msg_id = None
                _kernel_topics.pop(kernel_name, None)

            if envelope is not None:
                await pubsub.publish(envelope["topic"], envelope)
                logger.debug("iopub_listener.published",
                             topic=envelope["topic"], msg_type=msg_type)

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("iopub_listener.error", kernel=kernel_name, error=str(exc))
    finally:
        kc.stop_channels()
        logger.info("iopub_listener.stopped", kernel=kernel_name)


async def _run_cell(kernel_name: str, code: str, timeout: float = 30.0) -> dict:
    """Execute *code* in a named kernel; return collected outputs."""
    km = _kernels.get(kernel_name)
    if km is None:
        return {"error": f"Kernel '{kernel_name}' not found. Call start_kernel first."}

    kc = km.client()
    kc.start_channels()
    try:
        await asyncio.to_thread(kc.wait_for_ready, timeout=10)
        msg_id = kc.execute(code)

        outputs: list[dict] = []
        while True:
            try:
                msg = await asyncio.wait_for(
                    asyncio.to_thread(kc.get_iopub_msg, timeout=timeout),
                    timeout=timeout + 5,
                )
            except asyncio.TimeoutError:
                return {"outputs": outputs, "warning": "execution timed out"}

            msg_type = msg["msg_type"]
            content = msg["content"]
            parent_id = msg.get("parent_header", {}).get("msg_id")

            if parent_id != msg_id:
                continue

            if msg_type == "stream":
                outputs.append(
                    {"type": "stream", "name": content.get("name"), "text": content.get("text")}
                )
            elif msg_type == "execute_result":
                outputs.append({"type": "result", "data": content.get("data", {})})
            elif msg_type == "error":
                outputs.append(
                    {
                        "type": "error",
                        "ename": content.get("ename"),
                        "evalue": content.get("evalue"),
                        "traceback": content.get("traceback", []),
                    }
                )
            elif msg_type == "status" and content.get("execution_state") == "idle":
                break

        return {"outputs": outputs}
    finally:
        kc.stop_channels()


async def _stop_kernel(name: str) -> dict:
    """Terminate a named kernel."""
    km = _kernels.pop(name, None)
    _kernel_topics.pop(name, None)
    if km is None:
        return {"error": f"Kernel '{name}' not found"}
    await km.shutdown_kernel()
    logger.info("kernel.stopped", name=name)
    return {"name": name, "status": "stopped"}


async def _list_kernels() -> list[dict]:
    """Return all active kernels with liveness status and active pubsub topic."""
    result = []
    for name, km in _kernels.items():
        try:
            alive = await km.is_alive()
        except Exception:
            alive = False
        result.append({
            "name": name,
            "alive": alive,
            "pubsub_topic": _kernel_topics.get(name),
        })
    return result


# ── A2A message parsing ───────────────────────────────────────────────────────

def _parse_command(text: str) -> dict | None:
    t = text.strip()

    try:
        parsed = json.loads(t)
        if isinstance(parsed, dict) and "action" in parsed:
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    m = re.match(r"^start\s+kernel\s+(\S+)$", t, re.IGNORECASE)
    if m:
        return {"action": "start_kernel", "name": m.group(1)}

    m = re.match(r"^run\s+in\s+kernel\s+(\S+)\s*:\s*(.+)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return {"action": "run_cell", "kernel": m.group(1), "code": m.group(2)}

    m = re.match(r"^stop\s+kernel\s+(\S+)$", t, re.IGNORECASE)
    if m:
        return {"action": "stop_kernel", "name": m.group(1)}

    if re.match(r"^list\s+kernels?$", t, re.IGNORECASE):
        return {"action": "list_kernels"}

    return None


# ── LLM fallback via model-node ───────────────────────────────────────────────

async def _llm_fallback(text: str, event_queue) -> bool:
    """
    Forward *text* to model-node via A2A when it can't be parsed as a command.
    Returns True if the call succeeded, False if model-node is unavailable.
    """
    model_url = os.environ.get("SIBLING_MODEL_NODE_URL", "").rstrip("/")
    if not model_url:
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as probe:
            r = await probe.get(f"{model_url}/.well-known/a2a")
            if r.status_code != 200:
                return False
            card = AgentCard.model_validate(r.json())

        async with httpx.AsyncClient(timeout=60.0) as http_client:
            factory = ClientFactory(ClientConfig(httpx_client=http_client))
            client = factory.create(card)
            async for event in client.send_message(new_agent_text_message(text)):
                if isinstance(event, tuple):
                    _, update = event
                    if update is not None:
                        await event_queue.enqueue_event(update)
                else:
                    await event_queue.enqueue_event(event)
        return True
    except Exception as exc:
        logger.warning("jupyter_executor.llm_fallback_failed", error=str(exc))
        return False


# ── Executor ─────────────────────────────────────────────────────────────────

class JupyterExecutor(AgentExecutor):
    async def execute(self, context, event_queue) -> None:
        message: Message | None = getattr(context, "message", None)
        parts = getattr(message, "parts", []) or []
        text = " ".join(getattr(p, "text", "") for p in parts if hasattr(p, "text")).strip()

        logger.info("jupyter_executor.execute", text_preview=text[:120])

        cmd = _parse_command(text)
        if cmd is None:
            # Try to answer via model-node LLM before falling back to the error.
            if await _llm_fallback(text, event_queue):
                return
            await event_queue.enqueue_event(
                new_agent_text_message(
                    "Unrecognised command. Supported: "
                    "'start kernel <name>', "
                    "'run in kernel <name>: <code>', "
                    "'stop kernel <name>', "
                    "'list kernels'"
                )
            )
            return

        action = cmd.get("action")
        try:
            if action == "start_kernel":
                result = await _start_kernel(cmd["name"])
            elif action == "run_cell":
                result = await _run_cell(cmd["kernel"], cmd["code"])
            elif action == "stop_kernel":
                result = await _stop_kernel(cmd["name"])
            elif action == "list_kernels":
                result = await _list_kernels()
            else:
                result = {"error": f"Unknown action: {action}"}
        except Exception as exc:
            logger.error("jupyter_executor.error", action=action, error=str(exc))
            result = {"error": str(exc)}

        await event_queue.enqueue_event(new_agent_text_message(json.dumps(result)))

    async def cancel(self, context, event_queue) -> None:
        logger.info("jupyter_executor.cancel")


# ── Node ─────────────────────────────────────────────────────────────────────

class JupyterNode(BaseNode):
    def get_agent_executor(self) -> JupyterExecutor:
        return JupyterExecutor()

    def get_agent_card(self) -> AgentCard:
        node_name = os.environ.get("NODE_NAME", "jupyter-node")
        node_port = os.environ.get("NODE_PORT", "8002")
        return AgentCard(
            name=node_name,
            description=(
                "Jupyter kernel manager — launches isolated IPython kernels, "
                "executes code cells, and streams outputs via /ws/pubsub/<topic>. "
                "Has direct Postgres access for loading data into kernel namespaces. "
                "Calls model-node directly via HTTP for inline LLM inference."
            ),
            url=f"http://{node_name}:{node_port}/rpc",
            version="0.2.0",
            skills=[
                AgentSkill(
                    id="kernel-start",
                    name="kernel/start",
                    description="Launch a named IPython kernel",
                    tags=["jupyter", "kernel"],
                ),
                AgentSkill(
                    id="kernel-execute",
                    name="kernel/execute",
                    description="Execute a code cell in a running kernel and return outputs",
                    tags=["jupyter", "execution", "kernel"],
                ),
                AgentSkill(
                    id="kernel-stop",
                    name="kernel/stop",
                    description="Terminate a named IPython kernel",
                    tags=["jupyter", "kernel"],
                ),
                AgentSkill(
                    id="pubsub-stream",
                    name="pubsub/stream",
                    description=(
                        "Stream live cell outputs tagged with '# pubsub: <topic>' "
                        "via WebSocket at /ws/pubsub/<topic>"
                    ),
                    tags=["jupyter", "pubsub", "streaming"],
                ),
            ],
            default_input_modes=["MESSAGES"],
            default_output_modes=["MESSAGES"],
            capabilities=AgentCapabilities(),
        )

    def register_routes(self, app) -> None:
        lab_port = int(os.environ.get("JUPYTER_LAB_PORT", "8888"))
        token = os.environ.get("JUPYTERLAB_TOKEN", "")

        _original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def _lifespan_with_lab(fastapi_app):
            cmd = [
                sys.executable, "-m", "jupyterlab",
                "--ip=0.0.0.0",
                f"--port={lab_port}",
                "--no-browser",
                f"--IdentityProvider.token={token}",
                "--ServerApp.allow_origin=*",
            ]
            proc = await asyncio.create_subprocess_exec(*cmd)
            logger.info("jupyterlab.started", port=lab_port)

            try:
                async with _original_lifespan(fastapi_app):
                    yield
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                logger.info("jupyterlab.stopped")

        app.router.lifespan_context = _lifespan_with_lab

        # ── POST /infer — direct model-node inference (no A2A) ────────────────
        @app.post("/infer")
        async def infer(request: Request) -> JSONResponse:
            """
            Proxy an inference request directly to model-node via SiblingClient.
            Bypasses A2A — use this from notebook cells or internal routes.

            Body: { "prompt": "...", "model": "phi3:mini" }
            """
            if SiblingClient is None:
                return JSONResponse(
                    {"error": "SiblingClient not available (base image too old)"},
                    status_code=503,
                )
            sibling_url_var = "SIBLING_MODEL_NODE_URL"
            if not os.environ.get(sibling_url_var):
                return JSONResponse(
                    {"error": f"{sibling_url_var} not set"},
                    status_code=503,
                )
            body = await request.json()
            try:
                client = SiblingClient(sibling_url_var, timeout=180.0)
                result = await client.post("/generate", json=body)
                return JSONResponse(result)
            except Exception as exc:
                logger.error("infer.error", error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

        # ── POST /circuit/run — direct qiskit-node simulation (no A2A) ────────
        @app.post("/circuit/run")
        async def run_circuit_proxy(request: Request) -> JSONResponse:
            """
            Proxy a circuit execution request directly to qiskit-node.
            Bypasses A2A. Optional 'pubsub_topic' in body will stream progress back to it.

            Body: { "code": "...", "language": "openqasm3", "shots": 1024, "pubsub_topic": "..." }
            """
            if SiblingClient is None:
                return JSONResponse(
                    {"error": "SiblingClient not available (base image too old)"},
                    status_code=503,
                )
            sibling_url_var = "SIBLING_QISKIT_NODE_URL"
            if not os.environ.get(sibling_url_var):
                return JSONResponse(
                    {"error": f"{sibling_url_var} not set"},
                    status_code=503,
                )
            body = await request.json()
            try:
                client = SiblingClient(sibling_url_var, timeout=120.0) # Longer timeout for simulations
                result = await client.post("/circuit/run", json=body)
                return JSONResponse(result)
            except Exception as exc:
                logger.error("circuit_run.error", error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

    def register_mcp_tools(self, mcp) -> None:
        @mcp.tool()
        async def start_kernel(name: str) -> dict:
            """Launch a named IPython kernel. No-op if already running."""
            return await _start_kernel(name)

        @mcp.tool()
        async def run_cell(kernel_name: str, code: str, timeout: float = 30.0) -> dict:
            """
            Execute code in a running kernel. Returns a list of outputs
            (stream, result, error) collected until the kernel becomes idle.
            Tag a cell with '# pubsub: <topic>' to also stream outputs via
            WebSocket at /ws/pubsub/<topic>.
            """
            return await _run_cell(kernel_name, code, timeout)

        @mcp.tool()
        async def stop_kernel(name: str) -> dict:
            """Terminate a named IPython kernel and remove it from the registry."""
            return await _stop_kernel(name)

        @mcp.tool()
        async def list_kernels() -> list:
            """List all active kernels with liveness status and active pubsub topic."""
            return await _list_kernels()
