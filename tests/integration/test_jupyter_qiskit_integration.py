"""Cross-container integration tests: jupyter-node ↔ qiskit-node.

Requires both nodes running on the agentnet Docker network.  Start with:

    cd agentic-orchestrator && make up

or, for standalone testing:

    cd agentic-qiskit-node  && docker compose up -d   # creates agentnet
    cd agentic-jupyter-node && docker compose up -d

Then run from the host:

    JUPYTER_NODE_URL=http://localhost:8002 \\
    QISKIT_NODE_URL=http://localhost:8003  \\
    pytest tests/integration/test_jupyter_qiskit_integration.py -v -m integration

Covers:
- POST /circuit/run proxy: jupyter-node → qiskit-node
- Circuit diagram via the proxy (text and mpl)
- Pubsub streaming: WebSocket events during simulation
- Kernel-based circuit execution (Qiskit Python in an IPython kernel)
"""
import base64
import json
import os
import threading
import time

import httpx
import pytest

JUPYTER_URL = os.getenv("JUPYTER_NODE_URL", "http://localhost:8002").rstrip("/")
QISKIT_URL = os.getenv("QISKIT_NODE_URL", "http://localhost:8003").rstrip("/")
TIMEOUT = 30.0
KERNEL_TIMEOUT = 60.0

pytestmark = pytest.mark.integration

# A minimal 2-qubit Bell state circuit (OpenQASM 2)
_BELL_QASM2 = """\
OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0],q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
"""

_SHOTS = 512


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qiskit_reachable() -> bool:
    try:
        return httpx.get(f"{QISKIT_URL}/health", timeout=5.0).status_code == 200
    except Exception:
        return False


def _a2a_send(url: str, msg_id: str, text: str, timeout: float = TIMEOUT) -> httpx.Response:
    return httpx.post(
        f"{url}/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": text}],
                    "messageId": msg_id,
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 1. /circuit/run proxy — basic functionality
# ---------------------------------------------------------------------------

def test_circuit_run_proxy_returns_200():
    """POST /circuit/run on jupyter-node proxies to qiskit-node and returns 200."""
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")
    resp = httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={"code": _BELL_QASM2, "language": "openqasm2", "shots": _SHOTS},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"


def test_circuit_run_proxy_returns_counts():
    """Response from /circuit/run proxy contains a non-empty counts dict."""
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")
    resp = httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={"code": _BELL_QASM2, "language": "openqasm2", "shots": _SHOTS},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "counts" in body, f"Missing 'counts' in response: {body}"
    assert isinstance(body["counts"], dict)
    assert len(body["counts"]) > 0


def test_circuit_run_proxy_counts_sum_to_shots():
    """Total counts from proxy must equal the requested number of shots."""
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")
    resp = httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={"code": _BELL_QASM2, "language": "openqasm2", "shots": _SHOTS},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    total = sum(counts.values())
    assert total == _SHOTS, f"Expected counts to sum to {_SHOTS}, got {total}: {counts}"


def test_circuit_run_proxy_success_flag():
    """success flag is True for a valid circuit."""
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")
    resp = httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={"code": _BELL_QASM2, "language": "openqasm2", "shots": _SHOTS},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200
    assert resp.json().get("success") is True


# ---------------------------------------------------------------------------
# 2. Circuit diagram via the proxy
# ---------------------------------------------------------------------------

def test_circuit_run_proxy_diagram_text():
    """include_diagram=true with diagram_format='text' returns ASCII diagram via proxy."""
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")
    resp = httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={
            "code": _BELL_QASM2,
            "language": "openqasm2",
            "shots": _SHOTS,
            "include_diagram": True,
            "diagram_format": "text",
        },
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    assert "diagram" in body, f"Missing 'diagram' key: {body}"
    assert isinstance(body["diagram"], str) and len(body["diagram"]) > 10


def test_circuit_run_proxy_diagram_mpl_is_valid_png():
    """include_diagram=true with diagram_format='mpl' returns a base64-encoded PNG via proxy."""
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")
    resp = httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={
            "code": _BELL_QASM2,
            "language": "openqasm2",
            "shots": _SHOTS,
            "include_diagram": True,
            "diagram_format": "mpl",
        },
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    diagram = resp.json().get("diagram", "")
    assert diagram, "Expected non-empty diagram"
    png_bytes = base64.b64decode(diagram)
    assert png_bytes[:4] == b"\x89PNG", "Expected PNG magic bytes"


# ---------------------------------------------------------------------------
# 3. Pubsub streaming — WebSocket events from jupyter-node's pubsub
# ---------------------------------------------------------------------------

def test_circuit_run_pubsub_via_jupyter_proxy():
    """
    Subscribe to jupyter-node's pubsub topic, then run a circuit through the
    jupyter-node /circuit/run proxy with pubsub_topic set.
    Verify 'running' and 'completed' events arrive over WebSocket.
    """
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")

    try:
        import websockets.sync.client as wsclient
    except ImportError:
        pytest.skip("websockets package not installed")

    topic = "jup-integ-bell"
    ws_url = JUPYTER_URL.replace("http://", "ws://") + f"/ws/pubsub/{topic}"

    received: list[dict] = []
    ws_errors: list[Exception] = []

    def _listen():
        try:
            with wsclient.connect(ws_url, open_timeout=10) as ws:
                for raw in ws:
                    msg = json.loads(raw)
                    received.append(msg)
                    if msg.get("status") in ("completed", "error"):
                        break
        except Exception as exc:
            ws_errors.append(exc)

    listener = threading.Thread(target=_listen, daemon=True)
    listener.start()
    time.sleep(0.3)  # let WS handshake complete before circuit starts

    resp = httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={
            "code": _BELL_QASM2,
            "language": "openqasm2",
            "shots": _SHOTS,
            "pubsub_topic": topic,
        },
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"circuit/run proxy failed: {resp.status_code} — {resp.text}"

    listener.join(timeout=15.0)

    if ws_errors:
        pytest.fail(f"WebSocket listener raised: {ws_errors[0]}")

    statuses = [e.get("status") for e in received]
    assert "running" in statuses, f"Expected 'running' event, got: {statuses}"
    assert "completed" in statuses, f"Expected 'completed' event, got: {statuses}"


def test_circuit_run_pubsub_completed_event_has_counts():
    """The 'completed' pubsub event via jupyter-node proxy must contain simulation counts."""
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")

    try:
        import websockets.sync.client as wsclient
    except ImportError:
        pytest.skip("websockets package not installed")

    topic = "jup-integ-counts"
    ws_url = JUPYTER_URL.replace("http://", "ws://") + f"/ws/pubsub/{topic}"

    received: list[dict] = []

    def _listen():
        try:
            with wsclient.connect(ws_url, open_timeout=10) as ws:
                for raw in ws:
                    msg = json.loads(raw)
                    received.append(msg)
                    if msg.get("status") in ("completed", "error"):
                        break
        except Exception:
            pass

    listener = threading.Thread(target=_listen, daemon=True)
    listener.start()
    time.sleep(0.3)

    httpx.post(
        f"{JUPYTER_URL}/circuit/run",
        json={
            "code": _BELL_QASM2,
            "language": "openqasm2",
            "shots": _SHOTS,
            "pubsub_topic": topic,
        },
        timeout=TIMEOUT,
    )

    listener.join(timeout=15.0)

    completed = next((e for e in received if e.get("status") == "completed"), None)
    assert completed is not None, f"No 'completed' event received. Got: {received}"
    result = completed.get("result", {})
    assert "counts" in result, f"'completed' event missing counts: {completed}"


# ---------------------------------------------------------------------------
# 4. Kernel-based circuit execution
# ---------------------------------------------------------------------------

def test_kernel_can_run_qiskit_python_circuit():
    """
    Start an IPython kernel via A2A, execute Qiskit Python code that builds
    and runs a circuit, and verify the output contains simulation results.
    """
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")

    kernel_name = "qiskit-integ-test"

    # Start kernel
    r = _a2a_send(JUPYTER_URL, "qk-start", f"start kernel {kernel_name}")
    assert r.status_code == 200, f"Failed to start kernel: {r.status_code} — {r.text}"

    # Run Qiskit Python circuit via the /circuit/run proxy from inside the kernel
    jupyter_internal_url = "http://localhost:8002"
    code = (
        "import httpx, json\n"
        f"bell = '''{_BELL_QASM2}'''\n"
        f"r = httpx.post('{jupyter_internal_url}/circuit/run', "
        "json={'code': bell, 'language': 'openqasm2', 'shots': 256}, timeout=30)\n"
        "body = r.json()\n"
        "print(json.dumps({'counts': body.get('counts'), 'success': body.get('success')}))"
    )
    r2 = _a2a_send(JUPYTER_URL, "qk-run", f"run in kernel {kernel_name}: {code}", timeout=KERNEL_TIMEOUT)
    assert r2.status_code == 200, f"Kernel run failed: {r2.status_code} — {r2.text}"

    result_text = json.dumps(r2.json())
    assert "counts" in result_text.lower() or "outputs" in result_text.lower(), (
        f"Kernel output did not contain counts: {result_text[:500]}"
    )

    # Cleanup
    _a2a_send(JUPYTER_URL, "qk-stop", f"stop kernel {kernel_name}")


def test_kernel_can_run_native_qiskit_import():
    """
    Kernel can import qiskit and build a QuantumCircuit natively,
    then call the /circuit/run proxy for simulation.
    """
    if not _qiskit_reachable():
        pytest.skip("qiskit-node not reachable")

    kernel_name = "qiskit-native-test"

    r = _a2a_send(JUPYTER_URL, "qkn-start", f"start kernel {kernel_name}")
    assert r.status_code == 200

    # Qiskit Python code — build GHZ circuit natively, then post to proxy
    jupyter_internal_url = "http://localhost:8002"
    code = (
        "import httpx, json\n"
        "ghz_code = '''\n"
        "from qiskit import QuantumCircuit\n"
        "qc = QuantumCircuit(3)\n"
        "qc.h(0)\n"
        "qc.cx(0, 1)\n"
        "qc.cx(0, 2)\n"
        "'''\n"
        f"r = httpx.post('{jupyter_internal_url}/circuit/run', "
        "json={'code': ghz_code, 'language': 'qiskit', 'shots': 128}, timeout=30)\n"
        "body = r.json()\n"
        "print(json.dumps({'ok': body.get('success'), 'n_keys': len(body.get('counts', {}))}))"
    )
    r2 = _a2a_send(JUPYTER_URL, "qkn-run", f"run in kernel {kernel_name}: {code}", timeout=KERNEL_TIMEOUT)
    assert r2.status_code == 200

    result_text = json.dumps(r2.json())
    assert "ok" in result_text.lower() or "outputs" in result_text.lower(), (
        f"Unexpected kernel output: {result_text[:500]}"
    )

    # Cleanup
    _a2a_send(JUPYTER_URL, "qkn-stop", f"stop kernel {kernel_name}")
