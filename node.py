"""
Jupyter-node: dedicated sibling container for Jupyter kernel management.

Extends agentic-node-base and exposes MCP tools for kernel lifecycle and
code execution. Agent interactions requiring LLM assistance are delegated
to the model-node via A2A.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import structlog
from contextlib import asynccontextmanager

try:
    from app.base_node import BaseNode
except ImportError:
    from agentic_node_base.base_node import BaseNode

from a2a.server.agent_execution import AgentExecutor
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, Message
from a2a.utils import new_agent_text_message

logger = structlog.get_logger()

# ── In-memory kernel registry ─────────────────────────────────────────────────

_kernels: dict[str, object] = {}  # name → AsyncKernelManager


async def _start_kernel(name: str) -> dict:
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
    return {"name": name, "status": "started"}


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
    if km is None:
        return {"error": f"Kernel '{name}' not found"}
    await km.shutdown_kernel()
    logger.info("kernel.stopped", name=name)
    return {"name": name, "status": "stopped"}


async def _list_kernels() -> list[dict]:
    """Return all active kernels with liveness status."""
    result = []
    for name, km in _kernels.items():
        try:
            alive = await km.is_alive()
        except Exception:
            alive = False
        result.append({"name": name, "alive": alive})
    return result


# ── A2A message parsing ───────────────────────────────────────────────────────

def _parse_command(text: str) -> dict | None:
    """
    Parse a simple text command into a structured action dict.

    Supported forms:
      start kernel <name>
      run in kernel <name>: <code>
      stop kernel <name>
      list kernels
    """
    t = text.strip()

    # Try JSON first
    try:
        parsed = json.loads(t)
        if isinstance(parsed, dict) and "action" in parsed:
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # "start kernel <name>"
    m = re.match(r"^start\s+kernel\s+(\S+)$", t, re.IGNORECASE)
    if m:
        return {"action": "start_kernel", "name": m.group(1)}

    # "run in kernel <name>: <code>"
    m = re.match(r"^run\s+in\s+kernel\s+(\S+)\s*:\s*(.+)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return {"action": "run_cell", "kernel": m.group(1), "code": m.group(2)}

    # "stop kernel <name>"
    m = re.match(r"^stop\s+kernel\s+(\S+)$", t, re.IGNORECASE)
    if m:
        return {"action": "stop_kernel", "name": m.group(1)}

    # "list kernels"
    if re.match(r"^list\s+kernels?$", t, re.IGNORECASE):
        return {"action": "list_kernels"}

    return None


# ── Executor ─────────────────────────────────────────────────────────────────

class JupyterExecutor(AgentExecutor):
    async def execute(self, context, event_queue) -> None:
        message: Message | None = getattr(context, "message", None)
        parts = getattr(message, "parts", []) or []
        text = " ".join(getattr(p, "text", "") for p in parts if hasattr(p, "text")).strip()

        logger.info("jupyter_executor.execute", text_preview=text[:120])

        cmd = _parse_command(text)
        if cmd is None:
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
                "executes code cells, and returns stdout/stderr/results. "
                "Has direct Postgres access for loading data into kernel namespaces."
            ),
            url=f"http://{node_name}:{node_port}/rpc",
            version="0.1.0",
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
            ],
            default_input_modes=["MESSAGES"],
            default_output_modes=["MESSAGES"],
            capabilities=AgentCapabilities(),
        )

    def register_routes(self, app) -> None:
        lab_port = int(os.environ.get("JUPYTERLAB_INTERNAL_PORT", "8888"))
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
            """
            return await _run_cell(kernel_name, code, timeout)

        @mcp.tool()
        async def stop_kernel(name: str) -> dict:
            """Terminate a named IPython kernel and remove it from the registry."""
            return await _stop_kernel(name)

        @mcp.tool()
        async def list_kernels() -> list:
            """List all active kernels with their liveness status."""
            return await _list_kernels()
