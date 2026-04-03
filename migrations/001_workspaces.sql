-- Workspaces: group-owned folders within the Jupyter node.
-- Each workspace maps to a path prefix under JUPYTER_ROOT_DIR.
-- File and notebook operations are constrained to that prefix.

CREATE TABLE IF NOT EXISTS workspaces (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       VARCHAR(255) NOT NULL,
    group_id   VARCHAR(255) NOT NULL,   -- group name from JWT (no FK — orchestrator owns groups)
    path       TEXT NOT NULL,           -- relative path prefix, e.g. "groups/team-alpha/analysis/"
    created_by VARCHAR(255) NOT NULL,   -- user_id from JWT
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (group_id, name)
);

CREATE INDEX IF NOT EXISTS idx_workspaces_group_id ON workspaces(group_id);
