"""Integration test: PDF export of the cheng_demo notebook via JupyterLab's nbconvert endpoint.

Requires the full stack running:

    cd agentic-orchestrator && make up

Then run from the host:

    JUPYTER_LAB_URL=http://localhost:8005 \\
    pytest tests/integration/test_pdf_export.py -v -m integration
"""
import os

import httpx
import pytest

# JupyterLab server URL (the JupyterLab UI port, not the node API port).
JUPYTER_LAB_URL = os.getenv("JUPYTER_LAB_URL", "http://localhost:8005").rstrip("/")
NOTEBOOK_PATH = "cheng_demo.ipynb"
TIMEOUT = 120.0

pytestmark = pytest.mark.integration


def _jupyterlab_reachable() -> bool:
    try:
        r = httpx.get(f"{JUPYTER_LAB_URL}/lab", timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# PDF download via nbconvert
# ---------------------------------------------------------------------------

def test_cheng_notebook_pdf_download_returns_200():
    """GET /nbconvert/pdf/notebooks/cheng_demo.ipynb returns 200."""
    if not _jupyterlab_reachable():
        pytest.skip("JupyterLab not reachable — is the stack running?")
    resp = httpx.get(
        f"{JUPYTER_LAB_URL}/nbconvert/pdf/notebooks/{NOTEBOOK_PATH}",
        params={"download": "true"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}.\nBody: {resp.text[:500]}"
    )


def test_cheng_notebook_pdf_download_content_type():
    """Response Content-Type is application/pdf."""
    if not _jupyterlab_reachable():
        pytest.skip("JupyterLab not reachable — is the stack running?")
    resp = httpx.get(
        f"{JUPYTER_LAB_URL}/nbconvert/pdf/notebooks/{NOTEBOOK_PATH}",
        params={"download": "true"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
    content_type = resp.headers.get("content-type", "")
    assert "application/pdf" in content_type, (
        f"Expected application/pdf, got: {content_type}"
    )


def test_cheng_notebook_pdf_download_is_valid_pdf():
    """Response body begins with the PDF magic bytes %PDF-."""
    if not _jupyterlab_reachable():
        pytest.skip("JupyterLab not reachable — is the stack running?")
    resp = httpx.get(
        f"{JUPYTER_LAB_URL}/nbconvert/pdf/notebooks/{NOTEBOOK_PATH}",
        params={"download": "true"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
    assert resp.content[:4] == b"%PDF", (
        f"Response does not start with PDF magic bytes. "
        f"First 16 bytes: {resp.content[:16]!r}"
    )


def test_cheng_notebook_pdf_download_non_empty():
    """PDF body is at least 1 KB (not a stub or empty response)."""
    if not _jupyterlab_reachable():
        pytest.skip("JupyterLab not reachable — is the stack running?")
    resp = httpx.get(
        f"{JUPYTER_LAB_URL}/nbconvert/pdf/notebooks/{NOTEBOOK_PATH}",
        params={"download": "true"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
    assert len(resp.content) >= 1024, (
        f"PDF is suspiciously small ({len(resp.content)} bytes)"
    )
