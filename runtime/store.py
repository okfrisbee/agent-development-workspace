"""SQLite metadata owned by one runtime thread. No provider history or source code."""

from pathlib import Path
import sqlite3
from uuid import uuid4


SCHEMA_VERSION = 2

# Each tuple is one atomic migration. Append migrations rather than editing v1.
MIGRATIONS = (
    (
        """CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            canonical_branch TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            model TEXT NOT NULL CHECK (length(trim(model)) > 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
        """CREATE TABLE provider_threads (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            native_thread_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
        "CREATE INDEX sessions_project ON sessions(project_id)",
        "CREATE INDEX threads_session ON provider_threads(session_id)",
    ),
    (
        """CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            path TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL UNIQUE,
            base_revision TEXT NOT NULL,
            common_dir TEXT NOT NULL,
            git_dir TEXT,
            state TEXT NOT NULL CHECK (state IN
                ('creating', 'ready', 'removing', 'removed', 'needs_attention')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
        """CREATE UNIQUE INDEX workspace_session_active ON workspaces(session_id)
            WHERE state != 'removed'""",
    ),
)


def nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


class Store:
    """Use as a context manager; methods return detached, JSON-ready records.

    The default sqlite thread check deliberately enforces single-thread ownership.
    Filesystem/Git validation belongs outside database transactions.
    """

    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(f"Unsupported database schema version: {version}")
            self.connection.execute("PRAGMA journal_mode = WAL")
            # Serialize migration checks with other database openers.
            self.connection.execute("BEGIN IMMEDIATE")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(f"Unsupported database schema version: {version}")
            for index in range(version, SCHEMA_VERSION):
                for statement in MIGRATIONS[index]:
                    self.connection.execute(statement)
                self.connection.execute(f"PRAGMA user_version = {index + 1}")
            self.connection.commit()
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.connection.close()

    def register_project(self, path: str | Path, canonical_branch: str = "main") -> dict:
        """Persist a checkout already validated by projects.inspect_project.

        Re-registering the same path is idempotent; changing its canonical branch
        requires a future explicit configuration operation.
        """
        path = str(Path(path).resolve(strict=True))
        nonempty(canonical_branch, "canonical_branch")
        with self.connection:
            existing = self.connection.execute(
                "SELECT * FROM projects WHERE path = ?", (path,)
            ).fetchone()
            if existing:
                if existing["canonical_branch"] != canonical_branch:
                    raise ValueError("Project already registered with a different canonical branch")
                return dict(existing)
            project_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO projects (id, path, canonical_branch) VALUES (?, ?, ?)",
                (project_id, path, canonical_branch),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return dict(row)

    def list_projects(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM projects ORDER BY created_at, id"
        )]

    def create_session(self, project_id: str, *, model: str) -> dict:
        # No inherited Codex configuration and no provider call at creation time.
        nonempty(model, "model")
        session_id = str(uuid4())
        with self.connection:
            self.connection.execute(
                "INSERT INTO sessions (id, project_id, model) VALUES (?, ?, ?)",
                (session_id, project_id, model),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    def list_sessions(self, project_id: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        )]

    def attach_codex_thread(self, session_id: str, native_thread_id: str) -> dict:
        """Record a known native reference; this does not create/resume a thread."""
        nonempty(native_thread_id, "native_thread_id")
        reference_id = str(uuid4())
        with self.connection:
            self.connection.execute(
                "INSERT INTO provider_threads (id, session_id, native_thread_id) VALUES (?, ?, ?)",
                (reference_id, session_id, native_thread_id),
            )
        return dict(self.connection.execute(
            "SELECT * FROM provider_threads WHERE id = ?", (reference_id,)
        ).fetchone())

    def list_codex_threads(self, session_id: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM provider_threads WHERE session_id = ? ORDER BY created_at, id",
            (session_id,),
        )]

    def reserve_workspace(self, workspace_id: str, session_id: str, path: str,
                          branch: str, base_revision: str, common_dir: str) -> dict:
        """Write creation intent before any Git mutation. Called by Workspaces."""
        with self.connection:
            self.connection.execute(
                """INSERT INTO workspaces
                   (id, session_id, path, branch, base_revision, common_dir, state)
                   VALUES (?, ?, ?, ?, ?, ?, 'creating')""",
                (workspace_id, session_id, path, branch, base_revision, common_dir),
            )
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return dict(row)

    def list_workspaces(self, session_id: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM workspaces WHERE session_id = ? ORDER BY created_at, id",
            (session_id,),
        )]

    def transition_workspace(self, workspace_id: str, expected: str, state: str,
                             *, git_dir: str | None = None) -> dict:
        with self.connection:
            result = self.connection.execute(
                """UPDATE workspaces SET state = ?, git_dir = coalesce(?, git_dir)
                   WHERE id = ? AND state = ?""",
                (state, git_dir, workspace_id, expected),
            )
            if result.rowcount != 1:
                raise ValueError("Workspace state changed; inspect before retrying")
        return self.get_workspace(workspace_id)
