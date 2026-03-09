"""Cross-container integration tests: jupyter-node ↔ cheng-dataset-node.

Requires both stacks running on the agentnet Docker network.  Start with:

    cd agentic-orchestrator && make up

or, for standalone testing:

    cd agentic-dataset-cheng && docker compose up -d   # creates agentnet
    cd agentic-jupyter-node  && docker compose up -d

Then run from the host:

    JUPYTER_NODE_URL=http://localhost:8002 \\
    CHENG_NODE_URL=http://localhost:8004   \\
    pytest tests/integration/test_jupyter_cheng_integration.py -v -m integration-cross
"""
import json
import os

import httpx
import pytest

JUPYTER_URL = os.getenv("JUPYTER_NODE_URL", "http://localhost:8002").rstrip("/")
CHENG_URL = os.getenv("CHENG_NODE_URL", "http://localhost:8004").rstrip("/")
TIMEOUT = 30.0
A2A_TIMEOUT = 120.0

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cheng_reachable() -> bool:
    try:
        r = httpx.get(f"{CHENG_URL}/health", timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


def _model_node_available() -> bool:
    return bool(os.getenv("SIBLING_MODEL_NODE_URL"))


# ---------------------------------------------------------------------------
# 1. Cheng summary proxy
# ---------------------------------------------------------------------------

def test_cheng_summary_via_jupyter_proxy():
    """GET /cheng/summary returns paper PMID and all four table names."""
    resp = httpx.get(f"{JUPYTER_URL}/cheng/summary", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    assert body["paper"]["pmid"] == "30867426"
    table_names = [t["table"] for t in body["tables"]]
    for expected in ("ppi_edges", "drug_targets", "drug_combinations", "adverse_ddis"):
        assert expected in table_names, f"Missing table: {expected}"


# ---------------------------------------------------------------------------
# 2. Cheng SQL query proxy
# ---------------------------------------------------------------------------

def test_cheng_sql_query_via_jupyter_proxy():
    """POST /cheng/query with a COUNT query returns a positive row count."""
    resp = httpx.post(
        f"{JUPYTER_URL}/cheng/query",
        json={"sql": "SELECT COUNT(*) AS n FROM drug_targets"},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    assert "rows" in body
    assert body["rows"][0]["n"] > 0


def test_cheng_sql_rejects_non_select_via_proxy():
    """Non-SELECT statements must be rejected with a 4xx."""
    resp = httpx.post(
        f"{JUPYTER_URL}/cheng/query",
        json={"sql": "DELETE FROM drug_targets"},
        timeout=TIMEOUT,
    )
    assert resp.status_code in (400, 422), (
        f"Expected 4xx for non-SELECT, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 3. Cheng agent ask proxy
# ---------------------------------------------------------------------------

def test_cheng_ask_via_jupyter_proxy():
    """POST /cheng/ask returns a non-empty answer string."""
    if not _model_node_available():
        pytest.skip("SIBLING_MODEL_NODE_URL not set — model node not available")
    resp = httpx.post(
        f"{JUPYTER_URL}/cheng/ask",
        json={"question": "How many tables are in the Cheng dataset?"},
        timeout=A2A_TIMEOUT,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    assert "answer" in body
    assert len(body["answer"]) > 0


def test_cheng_ask_missing_question_returns_400():
    """POST /cheng/ask without 'question' field must return 400."""
    resp = httpx.post(
        f"{JUPYTER_URL}/cheng/ask",
        json={},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 4. Kernel executes SQL against Cheng Postgres directly
# ---------------------------------------------------------------------------

def test_notebook_kernel_can_query_cheng_postgres():
    """Start a kernel via A2A, run a psycopg2 cell, confirm row count > 0."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable — skipping kernel SQL test")

    cheng_dsn = os.getenv(
        "CHENG_POSTGRES_DSN",
        "postgresql://cheng:cheng@cheng-postgres:5432/cheng",
    )

    # Start kernel
    r = httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "start kernel cheng-test"}],
                    "messageId": "integ-cheng-k1",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200

    # Run psycopg2 cell
    code = (
        f"import psycopg2, json\n"
        f"conn = psycopg2.connect('{cheng_dsn}')\n"
        f"cur = conn.cursor()\n"
        f"cur.execute('SELECT COUNT(*) FROM drug_targets')\n"
        f"n = cur.fetchone()[0]\n"
        f"conn.close()\n"
        f"print(json.dumps({{'count': n}}))"
    )
    r2 = httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 2, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text",
                               "text": f"run in kernel cheng-test: {code}"}],
                    "messageId": "integ-cheng-k2",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=60.0,
    )
    assert r2.status_code == 200
    body = r2.json()
    result_text = json.dumps(body)
    assert "count" in result_text.lower() or "drug_targets" in result_text.lower() or "outputs" in result_text.lower()

    # Cleanup
    httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 3, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "stop kernel cheng-test"}],
                    "messageId": "integ-cheng-k3",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=TIMEOUT,
    )


# ---------------------------------------------------------------------------
# 5. Kernel calls cheng HTTP API via proxy
# ---------------------------------------------------------------------------

def test_notebook_kernel_can_call_cheng_http_api():
    """Kernel cell hits /cheng/summary via httpx and gets a 200."""
    # Start kernel
    r = httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "start kernel cheng-http-test"}],
                    "messageId": "integ-cheng-h1",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200

    jupyter_internal_url = "http://localhost:8002"
    code = (
        f"import httpx, json\n"
        f"r = httpx.get('{jupyter_internal_url}/cheng/summary', timeout=20)\n"
        f"print(json.dumps({{'status': r.status_code, 'pmid': r.json()['paper']['pmid']}}))"
    )
    r2 = httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 2, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text",
                               "text": f"run in kernel cheng-http-test: {code}"}],
                    "messageId": "integ-cheng-h2",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=60.0,
    )
    assert r2.status_code == 200
    result_text = json.dumps(r2.json())
    assert "30867426" in result_text or "outputs" in result_text.lower()

    # Cleanup
    httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 3, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "stop kernel cheng-http-test"}],
                    "messageId": "integ-cheng-h3",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=TIMEOUT,
    )


# ---------------------------------------------------------------------------
# 6. Kernel asks cheng agent
# ---------------------------------------------------------------------------

def test_notebook_kernel_can_ask_cheng_agent():
    """Kernel cell calls /cheng/ask and gets a non-empty answer."""
    if not _model_node_available():
        pytest.skip("SIBLING_MODEL_NODE_URL not set — model node not available")

    r = httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "start kernel cheng-ask-test"}],
                    "messageId": "integ-cheng-a1",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200

    jupyter_internal_url = "http://localhost:8002"
    code = (
        f"import httpx, json\n"
        f"r = httpx.post('{jupyter_internal_url}/cheng/ask', "
        f"json={{'question': 'How many drug targets are in the dataset?'}}, timeout=120)\n"
        f"body = r.json()\n"
        f"print(json.dumps({{'has_answer': bool(body.get('answer'))}}))"
    )
    r2 = httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 2, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text",
                               "text": f"run in kernel cheng-ask-test: {code}"}],
                    "messageId": "integ-cheng-a2",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=A2A_TIMEOUT,
    )
    assert r2.status_code == 200
    result_text = json.dumps(r2.json())
    assert "has_answer" in result_text or "outputs" in result_text.lower()

    # Cleanup
    httpx.post(
        f"{JUPYTER_URL}/rpc",
        json={
            "jsonrpc": "2.0", "id": 3, "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "stop kernel cheng-ask-test"}],
                    "messageId": "integ-cheng-a3",
                    "kind": "message",
                },
                "metadata": {},
            },
        },
        timeout=TIMEOUT,
    )


# ---------------------------------------------------------------------------
# 7. /cheng/graph proxy — PPI neighbourhood graph (issue #31)
# ---------------------------------------------------------------------------

def test_cheng_graph_via_jupyter_proxy_returns_200():
    """GET /cheng/graph returns 200."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"


def test_cheng_graph_via_jupyter_proxy_is_json():
    """Content-Type is application/json."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    assert "application/json" in resp.headers.get("content-type", ""), (
        f"Expected application/json, got: {resp.headers.get('content-type')}"
    )


def test_cheng_graph_via_jupyter_proxy_shape():
    """Response body has keys: drug_id, nodes, edges, node_count, edge_count."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    for key in ("drug_id", "nodes", "edges", "node_count", "edge_count"):
        assert key in body, f"Missing key in response: {key}"


def test_cheng_graph_nodes_have_correct_fields():
    """Every node in nodes list has: id (int), label (str), is_target (bool)."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    nodes = resp.json()["nodes"]
    for node in nodes:
        assert isinstance(node.get("id"), int), f"node 'id' must be int, got: {node}"
        assert isinstance(node.get("label"), str), f"node 'label' must be str, got: {node}"
        assert isinstance(node.get("is_target"), bool), f"node 'is_target' must be bool, got: {node}"


def test_cheng_graph_edges_have_correct_fields():
    """Every edge in edges list has: source (int), target (int)."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    edges = resp.json()["edges"]
    for edge in edges:
        assert isinstance(edge.get("source"), int), f"edge 'source' must be int, got: {edge}"
        assert isinstance(edge.get("target"), int), f"edge 'target' must be int, got: {edge}"


def test_cheng_graph_aspirin_has_targets():
    """For drug_id=DB00945 (aspirin), at least one node has is_target=True."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", params={"drug_id": "DB00945"}, timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    nodes = resp.json()["nodes"]
    target_nodes = [n for n in nodes if n.get("is_target") is True]
    assert len(target_nodes) > 0, "Expected at least one node with is_target=True for aspirin (DB00945)"


def test_cheng_graph_limit_is_respected():
    """GET /cheng/graph?limit=10 returns edge_count <= 10."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", params={"limit": 10}, timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    assert body["edge_count"] <= 10, (
        f"Expected edge_count <= 10 with limit=10, got: {body['edge_count']}"
    )


def test_cheng_graph_node_count_matches_nodes_list():
    """node_count field equals len(nodes)."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    assert body["node_count"] == len(body["nodes"]), (
        f"node_count={body['node_count']} does not match len(nodes)={len(body['nodes'])}"
    )


def test_cheng_graph_edge_count_matches_edges_list():
    """edge_count field equals len(edges)."""
    if not _cheng_reachable():
        pytest.skip("Cheng node not reachable")
    resp = httpx.get(f"{JUPYTER_URL}/cheng/graph", timeout=TIMEOUT)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    body = resp.json()
    assert body["edge_count"] == len(body["edges"]), (
        f"edge_count={body['edge_count']} does not match len(edges)={len(body['edges'])}"
    )
