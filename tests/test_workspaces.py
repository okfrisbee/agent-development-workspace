"""Real disposable Git repositories and injected failures; no provider calls."""

import asyncio
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from runtime.projects import git, register_project
from runtime.store import MIGRATIONS, Store
from runtime.workspaces import WorkspaceError, Workspaces


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-workspaces-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "canonical"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)],
                       check=True, capture_output=True, timeout=10)
        await git(self.repo, "config", "user.name", "Workspace Test")
        await git(self.repo, "config", "user.email", "test@example.invalid")
        await git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "source").write_text("baseline\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        await git(self.repo, "add", ".")
        await git(self.repo, "commit", "-qm", "baseline")
        self.db = self.root / "state.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(lambda: self.store.close())
        self.project = await register_project(self.store, self.repo)
        self.session = self.store.create_session(self.project["id"], model="gpt-6-luna")
        self.manager = Workspaces(self.store, self.root / "workspaces")

    async def create(self):
        return await self.manager.create(self.session["id"])

    def records(self):
        return self.store.list_workspaces(self.session["id"])

    async def test_create_from_canonical_preserves_dirty_feature_checkout(self):
        base = await git(self.repo, "rev-parse", "main")
        await git(self.repo, "switch", "-qc", "feature")
        (self.repo / "source").write_text("feature commit\n")
        await git(self.repo, "commit", "-am", "feature")
        (self.repo / "source").write_text("staged user work\n")
        await git(self.repo, "add", "source")
        (self.repo / "source").write_text("unstaged user work\n")
        (self.repo / "untracked").write_text("user data")
        before = [await git(self.repo, *args) for args in
                  (("rev-parse", "HEAD"), ("status", "--porcelain"),
                   ("diff",), ("diff", "--cached"), ("symbolic-ref", "HEAD"))]
        record = await self.create()
        path = Path(record["path"])
        self.assertEqual(record["state"], "ready")
        self.assertEqual(record["base_revision"], base)
        self.assertEqual((path / "source").read_text(), "baseline\n")
        self.assertEqual(before, [await git(self.repo, *args) for args in
                         (("rev-parse", "HEAD"), ("status", "--porcelain"),
                          ("diff",), ("diff", "--cached"), ("symbolic-ref", "HEAD"))])
        info = await self.manager.inspect(record["id"])
        self.assertFalse(info["dirty"])
        self.assertEqual(info["head_revision"], base)
        # Metadata survives reopening; no filesystem recreation is necessary.
        self.store.close()
        self.store = Store(self.db)
        self.assertEqual(self.records(), [record])

    async def test_clean_removal_retains_commits_session_and_thread(self):
        reference = self.store.attach_codex_thread(self.session["id"], "native-thread")
        record = await self.create()
        path = Path(record["path"])
        (path / "source").write_text("accepted later\n")
        await git(path, "commit", "-am", "proposed change")
        head = await git(path, "rev-parse", "HEAD")
        removed = await self.manager.remove(record["id"])
        self.assertEqual(removed["state"], "removed")
        self.assertFalse(path.exists())
        self.assertEqual(await git(self.repo, "rev-parse", record["branch"]), head)
        self.assertEqual(self.store.get_session(self.session["id"]), self.session)
        self.assertEqual(self.store.list_codex_threads(self.session["id"]), [reference])
        self.assertEqual(await self.manager.remove(record["id"]), removed)
        replacement = await self.create()
        self.assertNotEqual(replacement["path"], record["path"])
        self.assertEqual(replacement["base_revision"], record["base_revision"])

    async def test_removal_refuses_every_kind_of_local_change(self):
        record = await self.create()
        path = Path(record["path"])
        for kind in ("unstaged", "staged", "untracked", "ignored"):
            with self.subTest(kind=kind):
                target = path / (kind if kind in {"untracked", "ignored"} else "source")
                target.write_text("keep user work\n")
                if kind == "staged":
                    await git(path, "add", "source")
                self.assertTrue((await self.manager.inspect(record["id"]))["dirty"])
                with self.assertRaisesRegex(WorkspaceError, "contains changes"):
                    await self.manager.remove(record["id"])
                self.assertEqual(target.read_text(), "keep user work\n")
                self.assertEqual(self.store.get_workspace(record["id"])["state"], "ready")
                # Explicitly restore only our disposable test fixture.
                if kind in {"staged", "unstaged"}:
                    await git(path, "restore", "--staged", "source")
                    await git(path, "restore", "source")
                else:
                    target.unlink()

    async def test_ownership_branch_symlink_and_missing_directory_refusals(self):
        record = await self.create()
        path = Path(record["path"])
        await git(path, "switch", "-qc", "user-branch")
        with self.assertRaisesRegex(WorkspaceError, "branch changed"):
            await self.manager.remove(record["id"])
        await git(path, "switch", record["branch"])
        moved = path.with_name("moved")
        path.rename(moved)
        with self.assertRaisesRegex(WorkspaceError, "missing"):
            await self.manager.remove(record["id"])
        path.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(WorkspaceError, "owned location"):
            await self.manager.remove(record["id"])
        self.assertEqual((moved / "source").read_text(), "baseline\n")
        self.assertTrue((self.repo / ".git").is_dir())

    async def test_copy_of_git_pointer_and_changed_identity_are_refused(self):
        record = await self.create()
        self.store.connection.execute(
            "UPDATE workspaces SET git_dir = ? WHERE id = ?", ("/different", record["id"]))
        self.store.connection.commit()
        with self.assertRaisesRegex(WorkspaceError, "identity changed"):
            await self.manager.remove(record["id"])
        self.store.connection.execute(
            "UPDATE workspaces SET git_dir = ? WHERE id = ?", (record["git_dir"], record["id"]))
        self.store.connection.commit()
        path = Path(record["path"])
        saved = path.with_name("saved")
        path.rename(saved)
        path.mkdir()
        (path / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        with self.assertRaisesRegex(WorkspaceError, "another repository"):
            await self.manager.remove(record["id"])

    async def test_one_active_workspace_and_separate_session_ownership(self):
        first = await self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.create()
        second_session = self.store.create_session(self.project["id"], model="gpt-6-luna")
        second = await self.manager.create(second_session["id"])
        self.assertNotEqual(first["branch"], second["branch"])
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(self.records()), 1)

    async def test_competing_managers_cannot_provision_the_same_session(self):
        other = Workspaces(self.store, self.manager.root)
        results = await asyncio.gather(self.create(), other.create(self.session["id"]),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, sqlite3.IntegrityError) for result in results), 1)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["state"], "ready")

    async def test_hidden_tracked_changes_prevent_cleanup(self):
        record = await self.create()
        path = Path(record["path"])
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                await git(path, "update-index", f"--{flag}", "source")
                (path / "source").write_text("hidden user work\n")
                self.assertEqual(await git(path, "status", "--porcelain"), "")
                with self.assertRaisesRegex(WorkspaceError, "contains changes"):
                    await self.manager.remove(record["id"])
                self.assertEqual((path / "source").read_text(), "hidden user work\n")
                await git(path, "update-index", f"--no-{flag}", "source")
                await git(path, "restore", "source")

    async def test_configured_checkout_hook_is_not_executed(self):
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        marker = self.root / "hook-ran"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        await self.create()
        self.assertFalse(marker.exists())

    async def test_canonical_and_existing_destination_are_never_used(self):
        for root in (self.repo, self.repo / "nested"):
            with self.subTest(root=root), self.assertRaises(WorkspaceError):
                await Workspaces(self.store, root).create(self.session["id"])
        destination = self.manager.root / "fixed-id"
        destination.mkdir(parents=True)
        (destination / "user-file").write_text("preserve")
        with patch("runtime.workspaces.uuid4", return_value="fixed-id"):
            with self.assertRaisesRegex(WorkspaceError, "already exists"):
                await self.create()
        self.assertEqual((destination / "user-file").read_text(), "preserve")
        self.assertEqual(self.records(), [])

    async def test_failed_git_creation_preserves_partial_files_and_intent(self):
        async def fail(repo, *args):
            if "add" in args and "worktree" in args:
                record = self.records()[0]
                self.assertEqual(record["state"], "creating")
                self.assertFalse(self.store.connection.in_transaction)
                path = Path(record["path"])
                path.mkdir()
                (path / "partial").write_text("keep")
                raise RuntimeError("injected checkout failure")
            return await git(repo, *args)

        with patch("runtime.workspaces.git", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "checkout failure"):
                await self.create()
        record = self.records()[0]
        self.assertEqual(record["state"], "needs_attention")
        self.assertEqual((Path(record["path"]) / "partial").read_text(), "keep")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.create()
        with self.assertRaisesRegex(WorkspaceError, "recovery"):
            await self.manager.remove(record["id"])

    async def test_database_reservation_failure_prevents_git_mutation(self):
        before = await git(self.repo, "worktree", "list", "--porcelain")
        with patch.object(self.store, "reserve_workspace", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                await self.create()
        self.assertEqual(before, await git(self.repo, "worktree", "list", "--porcelain"))
        self.assertFalse(self.manager.root.exists())

    async def test_database_failure_after_checkout_keeps_recoverable_intent(self):
        with patch.object(self.store, "transition_workspace", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                await self.create()
        record = self.records()[0]
        self.assertEqual(record["state"], "creating")
        self.assertEqual((Path(record["path"]) / "source").read_text(), "baseline\n")
        self.assertEqual(await git(self.repo, "rev-parse", record["branch"]), record["base_revision"])

    async def test_cancellation_after_git_checkout_keeps_worktree(self):
        async def cancel(repo, *args):
            result = await git(repo, *args)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

        with patch("runtime.workspaces.git", side_effect=cancel):
            with self.assertRaises(asyncio.CancelledError):
                await self.create()
        record = self.records()[0]
        self.assertEqual(record["state"], "needs_attention")
        self.assertTrue(Path(record["path"]).is_dir())

    async def test_nonforced_remove_failure_retains_record_and_files(self):
        record = await self.create()
        await git(self.repo, "worktree", "lock", record["path"])
        with self.assertRaises(ValueError):
            await self.manager.remove(record["id"])
        self.assertEqual(self.store.get_workspace(record["id"])["state"], "needs_attention")
        self.assertTrue(Path(record["path"]).is_dir())

    async def test_database_failure_after_removal_retains_removing_intent(self):
        record = await self.create()
        transition = self.store.transition_workspace

        def fail(workspace_id, expected, state, **kwargs):
            if expected == "removing":
                raise sqlite3.OperationalError("injected database failure")
            return transition(workspace_id, expected, state, **kwargs)

        with patch.object(self.store, "transition_workspace", side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):
                await self.manager.remove(record["id"])
        self.assertFalse(Path(record["path"]).exists())
        self.assertEqual(self.store.get_workspace(record["id"])["state"], "removing")
        self.assertEqual(await git(self.repo, "rev-parse", record["branch"]), record["base_revision"])

    async def test_canonical_advancement_is_reported_without_rebase(self):
        record = await self.create()
        (self.repo / "source").write_text("canonical advanced\n")
        await git(self.repo, "commit", "-am", "advance")
        info = await self.manager.inspect(record["id"])
        self.assertNotEqual(info["canonical_revision"], info["base_revision"])
        self.assertEqual(info["head_revision"], info["base_revision"])
        self.assertEqual((Path(record["path"]) / "source").read_text(), "baseline\n")


class MigrationTests(unittest.TestCase):
    def test_v1_upgrade_preserves_existing_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite3"
            with patch("runtime.store.SCHEMA_VERSION", 1), patch("runtime.store.MIGRATIONS", MIGRATIONS[:1]):
                with Store(db) as store:
                    project = store.register_project(temp)
                    session = store.create_session(project["id"], model="gpt-6-luna")
                    reference = store.attach_codex_thread(session["id"], "native")
            with Store(db) as store:
                self.assertEqual(store.get_project(project["id"]), project)
                self.assertEqual(store.get_session(session["id"]), session)
                self.assertEqual(store.list_codex_threads(session["id"]), [reference])
                self.assertEqual(store.list_workspaces(session["id"]), [])
                self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
