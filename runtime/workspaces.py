"""Owned Git worktrees with durable intent records; never force or reset Git."""

import asyncio
from pathlib import Path
from uuid import uuid4

from .projects import git, inspect_project
from .store import Store


class WorkspaceError(ValueError):
    pass


class Workspaces:
    """One runtime-owned manager. Keep its root outside canonical checkouts.

    Git and SQLite cannot commit atomically. Persist intent first and retain all
    artifacts on failure. `creating`, `removing`, and `needs_attention` records
    block replacement until explicit recovery (a later increment).
    """

    def __init__(self, store: Store, root: str | Path):
        self.store = store
        self.root = Path(root).resolve()
        self.lock = asyncio.Lock()

    def _path(self, record: dict) -> Path:
        path = Path(record["path"])
        if (path != self.root / record["id"] or path.is_symlink()
                or path.resolve() != path or self.root.resolve() != self.root):
            raise WorkspaceError("Workspace path no longer matches its owned location")
        return path

    async def create(self, session_id: str) -> dict:
        async with self.lock:
            session = self.store.get_session(session_id)
            project = self.store.get_project(session["project_id"])
            inspected = await inspect_project(project["path"], project["canonical_branch"])
            repo = Path(inspected["path"])
            if any(self.root.is_relative_to(Path(item["path"]))
                   for item in self.store.list_projects()):
                raise WorkspaceError("Workspace root must be outside the canonical checkout")
            if self.root.resolve() != self.root:
                raise WorkspaceError("Workspace root was redirected")
            common_dir = await git(repo, "rev-parse", "--absolute-git-dir")
            if self.root.is_relative_to(Path(common_dir)):
                raise WorkspaceError("Workspace root must be outside Git metadata")
            workspace_id = str(uuid4())
            path = self.root / workspace_id
            if path.exists() or path.is_symlink():
                raise WorkspaceError("Workspace destination already exists")
            branch = f"runtime/{workspace_id}"
            # Unique index also protects against competing managers/connections.
            record = self.store.reserve_workspace(
                workspace_id, session_id, str(path), branch,
                inspected["revision"], common_dir,
            )
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self._path(record)
                # Pin the resolved commit, not a branch that may advance mid-operation.
                # Disable checkout hooks, which are arbitrary repo/user code.
                await git(repo, "-c", "core.hooksPath=/dev/null", "worktree", "add",
                          "-b", branch, str(path), record["base_revision"])
                git_dir = await self._verify(record, repo, require_identity=False)
                if await git(path, "rev-parse", "HEAD") != record["base_revision"]:
                    raise WorkspaceError("Workspace HEAD differs from its recorded base")
                return self.store.transition_workspace(
                    workspace_id, "creating", "ready", git_dir=git_dir,
                )
            except BaseException:
                # Never undo a partial checkout: it may contain user changes.
                # If SQLite also fails, the committed 'creating' intent remains.
                self._attention(workspace_id, "creating")
                raise

    def _attention(self, workspace_id: str, expected: str):
        try:
            self.store.transition_workspace(workspace_id, expected, "needs_attention")
        except Exception:
            # Preserve the original failure; the intent row still signals uncertainty.
            pass

    async def _verify(self, record: dict, repo: Path, *, require_identity=True) -> str:
        path = self._path(record)
        if not path.is_dir():
            raise WorkspaceError("Workspace directory is missing")
        if Path(await git(repo, "rev-parse", "--absolute-git-dir")) != Path(record["common_dir"]):
            raise WorkspaceError("Canonical repository identity changed")
        if Path(await git(path, "rev-parse", "--show-toplevel")) != path:
            raise WorkspaceError("Workspace root changed")
        common = await git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        git_dir = await git(path, "rev-parse", "--absolute-git-dir")
        if common != record["common_dir"] or git_dir == common:
            raise WorkspaceError("Workspace belongs to another repository")
        if require_identity and git_dir != record["git_dir"]:
            raise WorkspaceError("Workspace Git identity changed")
        if await git(path, "symbolic-ref", "HEAD") != f"refs/heads/{record['branch']}":
            raise WorkspaceError("Workspace branch changed")
        # NUL framing avoids ambiguity for unusual paths. Confirm Git's registration
        # rather than trusting a copied .git file pointing at another worktree.
        entries = (await git(repo, "worktree", "list", "--porcelain", "-z")).split("\0\0")
        fields = next((entry.split("\0") for entry in entries
                       if entry.split("\0")[0] == f"worktree {path}"), None)
        if fields is None or f"branch refs/heads/{record['branch']}" not in fields:
            raise WorkspaceError("Workspace is not registered to its owned branch")
        return git_dir

    async def inspect(self, workspace_id: str) -> dict:
        async with self.lock:
            record = self.store.get_workspace(workspace_id)
            project = self.store.get_project(
                self.store.get_session(record["session_id"])["project_id"]
            )
            repo = Path(project["path"])
            await self._verify(record, repo)
            path = Path(record["path"])
            return {**record,
                    "head_revision": await git(path, "rev-parse", "HEAD"),
                    "canonical_revision": await git(
                        repo, "rev-parse", "--verify",
                        f"refs/heads/{project['canonical_branch']}^{{commit}}"),
                    "dirty": await self._dirty(path)}

    async def _dirty(self, path: Path) -> bool:
        # Status can hide modified tracked files marked assume-unchanged or
        # skip-worktree. Refuse cleanup conservatively while either flag is set.
        flags = (await git(path, "ls-files", "-v", "-z")).split("\0")
        if any(entry and (entry[0].islower() or entry[0] == "S") for entry in flags):
            return True
        return bool(await git(path, "status", "--porcelain=v1", "-z",
                              "--untracked-files=all", "--ignored=matching",
                              "--ignore-submodules=none"))

    async def remove(self, workspace_id: str) -> dict:
        """Remove only an owned clean worktree. Retain its branch and history."""
        async with self.lock:
            record = self.store.get_workspace(workspace_id)
            if record["state"] == "removed":
                return record
            if record["state"] != "ready":
                raise WorkspaceError("Workspace requires inspection/recovery before removal")
            project = self.store.get_project(
                self.store.get_session(record["session_id"])["project_id"]
            )
            repo = Path(project["path"])
            await self._verify(record, repo)
            if await self._dirty(Path(record["path"])):
                raise WorkspaceError("Workspace contains changes or untracked/ignored files")
            self.store.transition_workspace(workspace_id, "ready", "removing")
            try:
                # Git rechecks tracked/untracked changes and locks. Never --force.
                await git(repo, "worktree", "remove", record["path"])
                return self.store.transition_workspace(workspace_id, "removing", "removed")
            except BaseException:
                self._attention(workspace_id, "removing")
                raise
