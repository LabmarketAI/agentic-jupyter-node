"""
Workspace API router for the Jupyter node.

A workspace is a group-owned folder within JUPYTER_ROOT_DIR. All file and
notebook operations are restricted to the workspace's path prefix to prevent
cross-group access and path traversal.

Endpoints:
  GET    /api/workspaces                             — list workspaces for caller's groups
  POST   /api/workspaces                             — create workspace
  DELETE /api/workspaces/{workspace_id}              — delete workspace (owner or admin)
  GET    /api/workspaces/{workspace_id}/files        — list files in workspace
  POST   /api/workspaces/{workspace_id}/files        — upload file
  GET    /api/workspaces/{workspace_id}/files/{name} — download file
  DELETE /api/workspaces/{workspace_id}/files/{name} — delete file

Auth: requires a valid session JWT (via NodeAuthMiddleware or get_current_user dep).
Group membership is sourced from JWT claims; no orchestrator call is needed.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from app.auth.dependencies import get_current_user
    from app.auth.context import UserContext
except ImportError:
    # Fallback when running outside the node-base container (e.g. local unit tests)
    get_current_user = None  # type: ignore
    UserContext = None  # type: ignore

logger = structlog.get_logger()
router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

_JUPYTER_ROOT = Path(os.environ.get("JUPYTER_ROOT_DIR", "/workspace"))

# Workspace paths live under JUPYTER_ROOT/groups/<group_name>/<workspace_name>/
_GROUPS_BASE = "groups"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_name(name: str, label: str) -> str:
    """Validate and return *name*, raising 422 if it contains unsafe characters."""
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"{label} must be 1-128 alphanumeric/dash/underscore characters",
        )
    return name


def _workspace_path(group_id: str, workspace_name: str) -> Path:
    """Resolve the absolute filesystem path for a workspace."""
    return _JUPYTER_ROOT / _GROUPS_BASE / group_id / workspace_name


def _safe_file_path(workspace_path: Path, filename: str) -> Path:
    """
    Resolve and validate a file path within the workspace.
    Raises 422 if the resolved path escapes the workspace directory.
    """
    if ".." in Path(filename).parts or "/" in filename:
        raise HTTPException(status_code=422, detail="Filename must not contain path separators or '..'")
    resolved = (workspace_path / filename).resolve()
    try:
        resolved.relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=422, detail="Path traversal detected")
    return resolved


def _get_caller_groups(user) -> list[str]:
    if user is None:
        return ["public"]
    if hasattr(user, "groups"):
        return user.groups
    if isinstance(user, dict):
        return user.get("groups", ["public"])
    return ["public"]


def _is_admin(user) -> bool:
    if user is None:
        return False
    if hasattr(user, "is_admin"):
        return user.is_admin
    if isinstance(user, dict):
        return user.get("is_admin", False)
    return False


def _get_user_id(user) -> str:
    if user is None:
        return "anonymous"
    if hasattr(user, "user_id"):
        return user.user_id
    if isinstance(user, dict):
        return user.get("id", "anonymous")
    return "anonymous"


# ── Request/response schemas ──────────────────────────────────────────────────

class CreateWorkspaceRequest(BaseModel):
    name: str         # workspace name (safe chars only)
    group_id: str     # group that owns this workspace (must be in caller's groups)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    group_id: str
    path: str
    created_by: str
    created_at: str


# ── Workspace CRUD ────────────────────────────────────────────────────────────

@router.get("")
async def list_workspaces(request: Request):
    """List workspaces accessible to the caller's groups."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    user = getattr(request.state, "user", None)
    caller_groups = _get_caller_groups(user)

    async with db.acquire() as conn:
        if _is_admin(user):
            rows = await conn.fetch(
                "SELECT id, name, group_id, path, created_by, created_at FROM workspaces ORDER BY group_id, name"
            )
        else:
            rows = await conn.fetch(
                "SELECT id, name, group_id, path, created_by, created_at FROM workspaces WHERE group_id = ANY($1::text[]) ORDER BY group_id, name",
                caller_groups,
            )

    return [_row_to_out(r) for r in rows]


@router.post("", status_code=201)
async def create_workspace(body: CreateWorkspaceRequest, request: Request):
    """Create a workspace. Caller must be a member of the target group."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    user = getattr(request.state, "user", None)
    caller_groups = _get_caller_groups(user)
    user_id = _get_user_id(user)

    name = _safe_name(body.name, "Workspace name")
    group_id = _safe_name(body.group_id, "Group ID")

    if not _is_admin(user) and group_id not in caller_groups:
        raise HTTPException(status_code=403, detail="You are not a member of that group")

    ws_path = _workspace_path(group_id, name)
    ws_path.mkdir(parents=True, exist_ok=True)

    relative_path = f"{_GROUPS_BASE}/{group_id}/{name}/"

    async with db.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO workspaces (name, group_id, path, created_by)
                VALUES ($1, $2, $3, $4)
                RETURNING id, name, group_id, path, created_by, created_at
                """,
                name, group_id, relative_path, user_id,
            )
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(
                    status_code=409,
                    detail=f"Workspace '{name}' already exists for group '{group_id}'",
                )
            raise

    logger.info("workspace.created", name=name, group_id=group_id, created_by=user_id)
    return _row_to_out(row)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: str, request: Request):
    """Delete a workspace and all its files. Group member or admin only."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    user = getattr(request.state, "user", None)
    caller_groups = _get_caller_groups(user)

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, group_id, path FROM workspaces WHERE id = $1::uuid",
            workspace_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if not _is_admin(user) and row["group_id"] not in caller_groups:
        raise HTTPException(status_code=403, detail="You are not a member of that group")

    # Remove filesystem folder
    ws_path = (_JUPYTER_ROOT / row["path"]).resolve()
    if ws_path.exists():
        import shutil
        shutil.rmtree(ws_path, ignore_errors=True)

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM workspaces WHERE id = $1::uuid", workspace_id)

    logger.info("workspace.deleted", workspace_id=workspace_id, name=row["name"])


# ── File operations ───────────────────────────────────────────────────────────

async def _resolve_workspace(db, workspace_id: str, caller_groups: list[str], is_admin: bool) -> dict:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, group_id, path FROM workspaces WHERE id = $1::uuid",
            workspace_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not is_admin and row["group_id"] not in caller_groups:
        raise HTTPException(status_code=403, detail="You are not a member of that group")
    return dict(row)


@router.get("/{workspace_id}/files")
async def list_files(workspace_id: str, request: Request):
    """List files in a workspace."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    user = getattr(request.state, "user", None)
    ws = await _resolve_workspace(db, workspace_id, _get_caller_groups(user), _is_admin(user))
    ws_path = (_JUPYTER_ROOT / ws["path"]).resolve()
    ws_path.mkdir(parents=True, exist_ok=True)

    files = []
    for entry in sorted(ws_path.iterdir()):
        if entry.is_file():
            stat = entry.stat()
            files.append({
                "name": entry.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    return {"workspace_id": workspace_id, "files": files}


@router.post("/{workspace_id}/files", status_code=201)
async def upload_file(workspace_id: str, file: UploadFile, request: Request):
    """Upload a file to a workspace."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    user = getattr(request.state, "user", None)
    ws = await _resolve_workspace(db, workspace_id, _get_caller_groups(user), _is_admin(user))
    ws_path = (_JUPYTER_ROOT / ws["path"]).resolve()
    ws_path.mkdir(parents=True, exist_ok=True)

    dest = _safe_file_path(ws_path, file.filename or "upload")
    content = await file.read()
    dest.write_bytes(content)

    logger.info("workspace.file_uploaded", workspace_id=workspace_id, filename=file.filename, size=len(content))
    return {"filename": dest.name, "size": len(content)}


@router.get("/{workspace_id}/files/{filename}")
async def download_file(workspace_id: str, filename: str, request: Request):
    """Download a file from a workspace."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    user = getattr(request.state, "user", None)
    ws = await _resolve_workspace(db, workspace_id, _get_caller_groups(user), _is_admin(user))
    ws_path = (_JUPYTER_ROOT / ws["path"]).resolve()
    dest = _safe_file_path(ws_path, filename)

    if not dest.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(str(dest), filename=filename)


@router.delete("/{workspace_id}/files/{filename}", status_code=204)
async def delete_file(workspace_id: str, filename: str, request: Request):
    """Delete a file from a workspace."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    user = getattr(request.state, "user", None)
    ws = await _resolve_workspace(db, workspace_id, _get_caller_groups(user), _is_admin(user))
    ws_path = (_JUPYTER_ROOT / ws["path"]).resolve()
    dest = _safe_file_path(ws_path, filename)

    if not dest.exists():
        raise HTTPException(status_code=404, detail="File not found")

    dest.unlink()
    logger.info("workspace.file_deleted", workspace_id=workspace_id, filename=filename)


# ── Helper ────────────────────────────────────────────────────────────────────

def _row_to_out(row) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "group_id": row["group_id"],
        "path": row["path"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(),
    }
