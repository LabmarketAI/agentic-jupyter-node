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
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
import structlog
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
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
from a2a.types import AgentCard, AgentCapabilities, AgentExtension, AgentSkill, Message
from a2a.utils import new_agent_text_message
import websockets as _ws_lib
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.websockets import WebSocket

logger = structlog.get_logger()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_sync_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"files": {}}


def _save_sync_state(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _warn_preloaded_in_user_notebooks(workspace: Path) -> None:
    """Warn when bundled example notebook names are present in user folder."""
    source_dir = Path("/app/notebooks")
    user_dir = workspace / "notebooks"
    if not source_dir.exists() or not user_dir.exists():
        return

    try:
        source_names = {p.name for p in source_dir.glob("*.ipynb")}
        user_names = {p.name for p in user_dir.glob("*.ipynb")}
        overlap = sorted(source_names & user_names)
        if overlap:
            logger.warning(
                "notebook.guard.preloaded_in_user_folder",
                user_folder=str(user_dir),
                overlap_count=len(overlap),
                overlap_names=overlap,
            )
    except Exception as exc:
        logger.warning("notebook.guard.check_failed", error=str(exc), user_folder=str(user_dir))


def _sync_template_notebooks() -> dict[str, Any]:
    """Reconcile bundled notebooks into workspace mounts.

        Modes (NOTEBOOK_SYNC_MODE):
    - missing: only add missing files.
    - overwrite: always replace destination files.
    - reconcile (default): update stale files, preserve user-edited files, and
      write <name>.upstream.ipynb when upstream changed.

        Folder policies:
        - NOTEBOOK_SYNC_TARGET_POLICIES can define per-folder modes using
            comma-separated entries like "examples:overwrite,notebooks:disabled".
        - If NOTEBOOK_SYNC_TARGET_POLICIES is unset, NOTEBOOK_SYNC_TARGET_DIRS plus
            NOTEBOOK_SYNC_MODE are used as global fallback behavior.
        - Default behavior when unset: examples=overwrite, notebooks=disabled.
    """
    mode = os.environ.get("NOTEBOOK_SYNC_MODE", "reconcile").strip().lower()
    if mode not in {"missing", "overwrite", "reconcile"}:
        mode = "reconcile"

    sync_version = os.environ.get("NOTEBOOK_SYNC_VERSION", "unknown")
    state_file_name = os.environ.get("NOTEBOOK_SYNC_STATE_FILE", ".agentic-notebook-sync.json")

    source_dir = Path("/app/notebooks")
    if not source_dir.exists():
        logger.warning("notebook.sync.source_missing", source=str(source_dir))
        return {
            "mode": mode,
            "version": sync_version,
            "copied": 0,
            "skipped": 0,
            "conflicts": 0,
            "failed": 0,
            "state_files": [],
        }

    workspace = Path(os.environ.get("JUPYTER_ROOT_DIR", "/workspace"))

    # Keep preloaded notebooks out of the Jupyter root itself to avoid
    # cluttering the top-level file browser.
    #
    # Preferred config (per-target):
    #   NOTEBOOK_SYNC_TARGET_POLICIES="examples:overwrite,notebooks:disabled"
    #
    # Backward-compatible config (global mode):
    #   NOTEBOOK_SYNC_TARGET_DIRS="notebooks"
    #   NOTEBOOK_SYNC_MODE="reconcile"
    raw_policies = os.environ.get("NOTEBOOK_SYNC_TARGET_POLICIES", "").strip()
    if raw_policies:
        target_policies: list[tuple[Path, str]] = []
        for raw in raw_policies.split(","):
            item = raw.strip()
            if not item:
                continue
            if ":" in item:
                name, target_mode = item.split(":", 1)
            else:
                name, target_mode = item, mode
            name = name.strip()
            target_mode = target_mode.strip().lower()
            if target_mode not in {"missing", "overwrite", "reconcile", "disabled"}:
                target_mode = mode
            if not name:
                continue
            p = Path(name)
            target_dir = p if p.is_absolute() else workspace / p
            target_policies.append((target_dir, target_mode))
        if not target_policies:
            target_policies = [
                (workspace / "examples", "overwrite"),
                (workspace / "notebooks", "disabled"),
            ]
    else:
        raw_targets = os.environ.get("NOTEBOOK_SYNC_TARGET_DIRS", "").strip()
        if raw_targets:
            target_policies = []
            for raw in raw_targets.split(","):
                name = raw.strip()
                if not name:
                    continue
                p = Path(name)
                target_policies.append((p if p.is_absolute() else workspace / p, mode))
            if not target_policies:
                target_policies = [
                    (workspace / "examples", "overwrite"),
                    (workspace / "notebooks", "disabled"),
                ]
        else:
            target_policies = [
                (workspace / "examples", "overwrite"),
                (workspace / "notebooks", "disabled"),
            ]

    copied = 0
    skipped = 0
    conflicts = 0
    failed = 0
    state_files: list[str] = []

    for target_dir, target_mode in target_policies:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            state_path = target_dir / state_file_name
            state = _load_sync_state(state_path)
            tracked = state.get("files", {}) if isinstance(state.get("files"), dict) else {}

            for nb in sorted(source_dir.glob("*.ipynb")):
                dest = target_dir / nb.name
                src_hash = _file_sha256(nb)
                prev = tracked.get(nb.name, {}) if isinstance(tracked.get(nb.name), dict) else {}
                prev_src_hash = prev.get("source_hash")

                should_copy = False
                should_shadow = False

                if target_mode == "disabled":
                    should_copy = False
                elif target_mode == "overwrite":
                    should_copy = True
                elif not dest.exists():
                    should_copy = True
                elif target_mode == "missing":
                    should_copy = False
                else:
                    dest_hash = _file_sha256(dest)
                    if dest_hash == src_hash:
                        should_copy = False
                    elif prev_src_hash and dest_hash == prev_src_hash:
                        # Safe stale update: user did not modify dest since last sync.
                        should_copy = True
                    else:
                        # Preserve user edits and expose new upstream copy.
                        should_shadow = True

                if should_copy:
                    shutil.copy2(nb, dest)
                    copied += 1
                elif should_shadow:
                    shadow = target_dir / f"{nb.stem}.upstream{nb.suffix}"
                    shutil.copy2(nb, shadow)
                    conflicts += 1
                    logger.warning(
                        "notebook.sync.conflict_preserved",
                        destination=str(dest),
                        upstream_shadow=str(shadow),
                        mode=target_mode,
                    )
                else:
                    skipped += 1

                tracked[nb.name] = {
                    "source_hash": src_hash,
                    "sync_version": sync_version,
                    "synced_at": int(time.time()),
                    "mode": target_mode,
                }

            state["files"] = tracked
            state["meta"] = {
                "mode": target_mode,
                "sync_version": sync_version,
                "source": str(source_dir),
                "updated_at": int(time.time()),
            }
            _save_sync_state(state_path, state)
            state_files.append(str(state_path))
        except Exception as exc:
            failed += 1
            logger.warning(
                "notebook.sync.target_failed",
                destination=str(target_dir),
                error=str(exc),
            )

    logger.info(
        "notebook.sync.completed",
        mode=mode,
        version=sync_version,
        target_policies=[{"target": str(t), "mode": m} for t, m in target_policies],
        copied=copied,
        skipped=skipped,
        conflicts=conflicts,
        failed=failed,
        workspace=str(workspace),
        state_files=state_files,
    )
    return {
        "mode": mode,
        "version": sync_version,
        "target_policies": [{"target": str(t), "mode": m} for t, m in target_policies],
        "copied": copied,
        "skipped": skipped,
        "conflicts": conflicts,
        "failed": failed,
        "workspace": str(workspace),
        "state_files": state_files,
    }

# ── Bearer token auth middleware ──────────────────────────────────────────────

# Paths that don't require authentication even when A2A_AUTH_TOKEN is set.
_AUTH_EXEMPT = frozenset(["/health", "/ready"])
_AUTH_EXEMPT_PREFIXES = ("/.well-known/",)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require 'Authorization: Bearer <token>' on all non-exempt paths.

    Only active when A2A_AUTH_TOKEN environment variable is set.
    Allows unauthenticated access to /health, /ready, and /.well-known/* so
    container orchestrators and service discovery always work.
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in _AUTH_EXEMPT or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return Response(
                content='{"detail":"Missing Bearer token"}',
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )
        provided = auth_header[len("Bearer "):]
        if provided != self._token:
            return Response(
                content='{"detail":"Invalid Bearer token"}',
                status_code=403,
                media_type="application/json",
            )
        return await call_next(request)

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
                if inspect.isawaitable(msg):
                    msg = await asyncio.wait_for(msg, timeout=timeout + 5)
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
        # A2A parts may be plain objects (p.text) or RootModel wrappers (p.root.text).
        text = " ".join(
            (
                getattr(getattr(p, "root", None), "text", None)
                or getattr(p, "text", "")
            )
            for p in parts
        ).strip()

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
        lab_port = os.environ.get("JUPYTER_LAB_PORT", "8888")
        return AgentCard(
            name=node_name,
            description=(
                "Jupyter kernel manager — launches isolated IPython kernels, "
                "executes code cells, and streams outputs via /ws/pubsub/<topic>. "
                "Has direct Postgres access for loading data into kernel namespaces. "
                "Calls model-node directly via HTTP for inline LLM inference."
            ),
            url=f"http://{node_name}:{node_port}/rpc",
            version=(
                os.environ.get("AGENT_CARD_VERSION")
                or os.environ.get("JUPYTER_NODE_VERSION")
                or os.environ.get("APP_VERSION")
                or "0.5.8"
            ),
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
            capabilities=AgentCapabilities(
                extensions=[
                    AgentExtension(
                        uri="jupyter-lab",
                        description="JupyterLab web UI",
                        params={"url": "/jupyter/lab/tree"},
                    )
                ]
            ),
        )

    def register_routes(self, app) -> None:
        lab_port = int(os.environ.get("JUPYTER_LAB_PORT", "8888"))
        token = os.environ.get("JUPYTERLAB_TOKEN", "")

        # Install bearer token middleware if A2A_AUTH_TOKEN is configured.
        # In production (NODE_ENV=production) this must be set.
        auth_token = os.environ.get("A2A_AUTH_TOKEN", "")
        node_env = os.environ.get("NODE_ENV", "development")
        if auth_token:
            app.add_middleware(BearerTokenMiddleware, token=auth_token)
            logger.info("jupyter_node.bearer_auth_enabled")
        elif node_env == "production":
            logger.warning(
                "SECURITY: A2A_AUTH_TOKEN is not set in production — "
                "jupyter-node API is unauthenticated. Set A2A_AUTH_TOKEN."
            )

        _original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def _lifespan_with_lab(fastapi_app):
            workspace = os.environ.get("JUPYTER_ROOT_DIR", "/workspace")
            fastapi_app.state.notebook_sync_status = _sync_template_notebooks()
            _warn_preloaded_in_user_notebooks(Path(workspace))
            cmd = [
                sys.executable, "-m", "jupyterlab",
                "--ip=0.0.0.0",
                f"--port={lab_port}",
                "--no-browser",
                "--allow-root",
                f"--IdentityProvider.token={token}",
                "--ServerApp.allow_origin=*",
                "--ServerApp.disable_check_xsrf=True",
                "--ServerApp.iopub_data_rate_limit=0",
                "--ServerApp.base_url=/jupyter",
                "--ServerApp.default_url=/lab/tree",
                f"--ServerApp.root_dir={workspace}",
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

        # ── JupyterLab reverse proxy (/jupyter/lab → localhost:8888) ──────────
        # JupyterLab runs on port 8888 inside the container with
        # --ServerApp.base_url=/jupyter.  All HTTP and WebSocket traffic
        # from the orchestrator is forwarded here transparently.

        _HOP_HEADERS = frozenset({
            "connection", "transfer-encoding", "te", "trailers",
            "upgrade", "keep-alive", "proxy-authorization", "proxy-authenticate",
        })

        _STRIP_FOR_JUPYTER = _HOP_HEADERS | {
            "host", "x-forwarded-host", "x-forwarded-for",
            "x-forwarded-proto", "x-forwarded-port",
            "origin", "referer",
        }

        _LOCALHOST_LAB = f"http://localhost:{lab_port}/jupyter/lab"

        async def _lab_http(request: Request, path: str) -> Response:
            qs = str(request.query_params)
            target = _LOCALHOST_LAB
            if path:
                target += f"/{path}"
            if qs:
                target += f"?{qs}"
            # Strip all forwarded-host headers and pin Host to localhost so
            # JupyterLab never sees the public ACA hostname and can't embed it
            # in redirect Location headers.
            # follow_redirects=False: let redirect responses pass through to
            # the browser so it updates its URL bar correctly. We rewrite the
            # Location header from the
            # internal localhost URL back to the proxy-relative path so the
            # browser follows the redirect through the orchestrator proxy.
            fwd = {k: v for k, v in request.headers.items()
                   if k.lower() not in _STRIP_FOR_JUPYTER}
            fwd["host"] = f"localhost:{lab_port}"

            # Jupyter requires a token header or form arg on unsafe methods.
            # When the browser only sends the _xsrf cookie through the proxy,
            # synthesize X-XSRFToken to avoid 403 "_xsrf argument missing".
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and "x-xsrftoken" not in {
                k.lower() for k in fwd
            }:
                cookie_header = request.headers.get("cookie", "")
                if cookie_header:
                    cookies = SimpleCookie()
                    cookies.load(cookie_header)
                    xsrf = cookies.get("_xsrf")
                    if xsrf and xsrf.value:
                        fwd["X-XSRFToken"] = xsrf.value

            async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as c:
                resp = await c.request(
                    method=request.method, url=target,
                    headers=fwd, content=await request.body(),
                )
            out = Response(content=resp.content, status_code=resp.status_code)
            for k, v in resp.headers.multi_items():
                kl = k.lower()
                if kl in _HOP_HEADERS or kl == "content-length":
                    continue
                if kl == "location":
                    v = v.replace(_LOCALHOST_LAB, "/jupyter/lab")
                if kl == "set-cookie":
                    out.headers.append(k, v)
                else:
                    out.headers[k] = v
            return out

        @app.api_route("/jupyter/lab", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
        async def lab_proxy_root(request: Request) -> Response:
            return await _lab_http(request, "")

        @app.get("/notebooks/sync-status")
        async def notebook_sync_status() -> JSONResponse:
            return JSONResponse(getattr(app.state, "notebook_sync_status", {}))

        @app.api_route("/jupyter/lab/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
        async def lab_proxy(request: Request, path: str) -> Response:
            return await _lab_http(request, path)

        @app.websocket("/jupyter/lab/{path:path}")
        async def lab_ws_proxy(websocket: WebSocket, path: str) -> None:
            qs = str(websocket.query_params)
            target = f"ws://localhost:{lab_port}/jupyter/lab/{path}"
            if qs:
                target += f"?{qs}"
            skip = {"host", "connection", "upgrade", "sec-websocket-key",
                    "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-accept"}
            extra = [(k.encode(), v.encode()) for k, v in websocket.headers.items()
                     if k.lower() not in skip]
            await websocket.accept()
            try:
                async with _ws_lib.connect(target, additional_headers=extra) as ws:
                    async def _to_server():
                        try:
                            while True:
                                msg = await websocket.receive()
                                if msg.get("type") == "websocket.disconnect":
                                    break
                                if msg.get("bytes"):
                                    await ws.send(msg["bytes"])
                                elif msg.get("text"):
                                    await ws.send(msg["text"])
                        except Exception:
                            pass

                    async def _to_client():
                        try:
                            async for msg in ws:
                                if isinstance(msg, bytes):
                                    await websocket.send_bytes(msg)
                                else:
                                    await websocket.send_text(msg)
                        except Exception:
                            pass

                    tasks = [asyncio.create_task(_to_server()), asyncio.create_task(_to_client())]
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in tasks:
                        t.cancel()
            except Exception:
                pass
            finally:
                try:
                    await websocket.close()
                except Exception:
                    pass

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
            except httpx.HTTPStatusError as exc:
                try:
                    detail = exc.response.json()
                except Exception:
                    detail = {"error": exc.response.text}
                logger.error("infer.error", error=str(exc), detail=detail)
                return JSONResponse(detail, status_code=exc.response.status_code)
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
            except httpx.HTTPStatusError as exc:
                # Forward the upstream error body and status so the caller sees the real error
                try:
                    detail = exc.response.json()
                except Exception:
                    detail = {"error": exc.response.text}
                logger.error("circuit_run.error", error=str(exc), detail=detail)
                return JSONResponse(detail, status_code=exc.response.status_code)
            except Exception as exc:
                logger.error("circuit_run.error", error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

        # ── GET /cheng/summary — dataset overview via cheng-node HTTP API ──────
        @app.get("/cheng/summary")
        async def cheng_summary_proxy() -> JSONResponse:
            """
            Proxy /data/summary from cheng-dataset-node.
            Returns paper reference and row counts for all four tables.
            """
            cheng_url = os.environ.get("SIBLING_CHENG_DATASET_NODE_URL", "").rstrip("/")
            if not cheng_url:
                return JSONResponse({"error": "SIBLING_CHENG_DATASET_NODE_URL not set"}, status_code=503)
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.get(f"{cheng_url}/data/summary")
                    return JSONResponse(r.json(), status_code=r.status_code)
            except Exception as exc:
                logger.error("cheng_summary.error", error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

        # ── POST /cheng/query — read-only SQL against cheng-node ──────────────
        @app.post("/cheng/query")
        async def cheng_query_proxy(request: Request) -> JSONResponse:
            """
            Proxy a read-only SQL query to cheng-dataset-node /data/query.
            Body: { "sql": "SELECT ..." }
            """
            cheng_url = os.environ.get("SIBLING_CHENG_DATASET_NODE_URL", "").rstrip("/")
            if not cheng_url:
                return JSONResponse({"error": "SIBLING_CHENG_DATASET_NODE_URL not set"}, status_code=503)
            body = await request.json()
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.post(f"{cheng_url}/data/query", json=body)
                    return JSONResponse(r.json(), status_code=r.status_code)
            except Exception as exc:
                logger.error("cheng_query.error", error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

        # ── POST /cheng/ask — natural-language Q&A via cheng-node A2A ─────────
        @app.post("/cheng/ask")
        async def cheng_ask_proxy(request: Request) -> JSONResponse:
            """
            Forward a natural-language question to cheng-dataset-node via A2A.
            Body: { "question": "How many drug targets are there?" }
            Returns: { "answer": "..." }
            """
            cheng_url = os.environ.get("SIBLING_CHENG_DATASET_NODE_URL", "").rstrip("/")
            if not cheng_url:
                return JSONResponse({"error": "SIBLING_CHENG_DATASET_NODE_URL not set"}, status_code=503)
            body = await request.json()
            question = body.get("question", "")
            if not question:
                return JSONResponse({"error": "Missing 'question' field"}, status_code=400)
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": question}],
                        "messageId": "jupyter-cheng-ask-1",
                        "kind": "message",
                    },
                    "metadata": {},
                },
            }
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    r = await client.post(f"{cheng_url}/rpc", json=rpc_payload)
                    data = r.json()
                # Extract the first text part from the A2A response
                result = data.get("result", {})
                parts = (
                    result.get("parts")
                    or (result.get("status", {}) or {}).get("message", {}).get("parts", [])
                )
                answer = " ".join(
                    (p.get("text") or p.get("content", ""))
                    for p in parts
                    if isinstance(p, dict)
                ).strip() or json.dumps(result)
                return JSONResponse({"answer": answer})
            except Exception as exc:
                logger.error("cheng_ask.error", error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

        # ── GET /cheng/graph — PPI neighbourhood graph for a drug ────────────
        @app.get("/cheng/graph")
        async def cheng_graph_proxy(drug_id: str = "DB00945", limit: int = 300) -> JSONResponse:
            """
            Proxy GET /data/graph from cheng-dataset-node.
            Returns {drug_id, nodes, edges, node_count, edge_count}.
            Nodes carry an is_target flag; layout is done client-side.
            """
            cheng_url = os.environ.get("SIBLING_CHENG_DATASET_NODE_URL", "").rstrip("/")
            if not cheng_url:
                return JSONResponse({"error": "SIBLING_CHENG_DATASET_NODE_URL not set"}, status_code=503)
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.get(
                        f"{cheng_url}/data/graph",
                        params={"drug_id": drug_id, "limit": limit},
                    )
                    return JSONResponse(r.json(), status_code=r.status_code)
            except Exception as exc:
                logger.error("cheng_graph.error", error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

        # ── /ctgov/* passthrough — forward to ctgov-dataset-node ───────────
        @app.api_route("/ctgov", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
        async def ctgov_root_proxy(request: Request) -> JSONResponse:
            """Proxy root ctgov calls to ctgov-dataset-node."""
            return await _ctgov_proxy(request, "")

        @app.api_route("/ctgov/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
        async def ctgov_path_proxy(request: Request, path: str) -> JSONResponse:
            """Proxy ctgov API calls to ctgov-dataset-node."""
            return await _ctgov_proxy(request, path)

        async def _ctgov_proxy(request: Request, path: str) -> JSONResponse:
            ctgov_url = os.environ.get("SIBLING_CTGOV_DATASET_NODE_URL", "").rstrip("/")
            if not ctgov_url:
                return JSONResponse({"error": "SIBLING_CTGOV_DATASET_NODE_URL not set"}, status_code=503)

            query = f"?{request.url.query}" if request.url.query else ""
            suffix = f"/{path}" if path else ""
            candidates = [ctgov_url]
            # In ACA internal networking, sibling URLs sometimes omit the target
            # port for non-ingress apps. Try :8010 as a safe fallback for ctgov.
            if ctgov_url.startswith("http://") and ":" not in ctgov_url.removeprefix("http://"):
                candidates.append(f"{ctgov_url}:8010")

            hop_headers = {
                "connection",
                "transfer-encoding",
                "te",
                "trailers",
                "upgrade",
                "keep-alive",
                "proxy-authorization",
                "proxy-authenticate",
                "host",
            }
            forward_headers = {
                k: v for k, v in request.headers.items() if k.lower() not in hop_headers
            }

            last_error = None
            for base in candidates:
                target = f"{base}{suffix}{query}"
                try:
                    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
                        resp = await client.request(
                            method=request.method,
                            url=target,
                            headers=forward_headers,
                            content=await request.body(),
                        )

                    # If the first candidate lands on an unroutable endpoint,
                    # allow fallback to the next candidate.
                    if resp.status_code == 404 and base != candidates[-1]:
                        continue

                    out = Response(content=resp.content, status_code=resp.status_code)
                    for k, v in resp.headers.items():
                        kl = k.lower()
                        if kl in hop_headers or kl == "content-length":
                            continue
                        out.headers[k] = v
                    return out
                except Exception as exc:
                    last_error = str(exc)
                    continue

            if last_error:
                logger.error("ctgov_proxy.error", error=last_error, candidates=candidates)
                return JSONResponse({"error": last_error}, status_code=502)
            return JSONResponse({"error": "ctgov upstream not reachable"}, status_code=502)

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
