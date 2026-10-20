"""Read-only Git inspection before registering an existing canonical checkout."""

import asyncio
import os
from pathlib import Path

from .store import Store, nonempty


class ProjectError(ValueError):
    pass


async def git(path: Path, *args: str) -> str:
    # Inherited Git routing variables must not redirect inspection to another repo.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(path), *args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            stdout, _ = await proc.communicate()
        if proc.returncode:
            # Do not forward arbitrary Git output/configuration into application errors.
            raise ProjectError(f"Git {args[0]} failed (exit {proc.returncode})")
        return os.fsdecode(stdout).rstrip("\n")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def inspect_project(path: str | Path, canonical_branch: str = "main") -> dict:
    """Accept a main checkout root, even dirty or detached, without changing Git."""
    nonempty(canonical_branch, "canonical_branch")
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise ProjectError("Project path must be a directory")
    top = Path(await git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise ProjectError("Project path must be the checkout root")
    git_dir = Path(await git(root, "rev-parse", "--absolute-git-dir")).resolve()
    common = Path(await git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if git_dir != common:
        raise ProjectError("A linked worktree cannot be the canonical checkout")
    await git(root, "check-ref-format", f"refs/heads/{canonical_branch}")
    revision = await git(root, "rev-parse", "--verify", f"refs/heads/{canonical_branch}^{{commit}}")
    return {"path": str(root), "canonical_branch": canonical_branch, "revision": revision}


async def register_project(store: Store, path: str | Path,
                           canonical_branch: str = "main") -> dict:
    inspected = await inspect_project(path, canonical_branch)
    # No await or filesystem operations inside the short SQLite transaction.
    return store.register_project(inspected["path"], inspected["canonical_branch"])
