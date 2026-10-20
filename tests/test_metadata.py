"""Disposable SQLite/Git fixtures only; never starts Codex or performs inference."""

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from runtime.projects import ProjectError, inspect_project, register_project
from runtime.store import Store


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-metadata-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state.sqlite3"

    def test_migration_wal_foreign_keys_and_reopen(self):
        with Store(self.db) as store:
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            project = store.register_project(self.root)
            session = store.create_session(project["id"], model="gpt-6-luna")
            self.assertEqual(store.list_codex_threads(session["id"]), [])
            references = [store.attach_codex_thread(session["id"], native)
                          for native in ("native-a", "native-b")]
            self.assertNotIn(session["id"], {ref["native_thread_id"] for ref in references})
        # A fresh connection re-runs migration checks and sees durable records.
        with Store(self.db) as store:
            self.assertEqual(store.list_projects(), [project])
            self.assertEqual(store.list_sessions(project["id"]), [session])
            self.assertEqual(store.get_session(session["id"])["model"], "gpt-6-luna")
            self.assertCountEqual(store.list_codex_threads(session["id"]), references)

    def test_project_registration_is_idempotent_and_does_not_reconfigure(self):
        with Store(self.db) as store:
            project = store.register_project(self.root)
            self.assertEqual(store.register_project(self.root / "."), project)
            with self.assertRaisesRegex(ValueError, "different canonical branch"):
                store.register_project(self.root, "develop")
            self.assertEqual(store.list_projects(), [project])

    def test_foreign_keys_uniqueness_and_failed_write_rollback(self):
        with Store(self.db) as store:
            with self.assertRaises(sqlite3.IntegrityError):
                store.create_session("missing", model="gpt-6-luna")
            with self.assertRaises(sqlite3.IntegrityError):
                store.attach_codex_thread("missing", "native")
            project = store.register_project(self.root)
            first = store.create_session(project["id"], model="gpt-6-luna")
            second = store.create_session(project["id"], model="another-explicit-model")
            store.attach_codex_thread(first["id"], "native")
            with self.assertRaises(sqlite3.IntegrityError):
                store.attach_codex_thread(second["id"], "native")
            self.assertEqual(store.list_codex_threads(second["id"]), [])
            self.assertEqual(len(store.list_sessions(project["id"])), 2)

    def test_explicit_model_and_reference_validation(self):
        with Store(self.db) as store:
            project = store.register_project(self.root)
            with self.assertRaises(TypeError):
                store.create_session(project["id"])
            for value in ("", "  ", None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store.create_session(project["id"], model=value)
            session = store.create_session(project["id"], model="gpt-6-luna")
            with self.assertRaises(ValueError):
                store.attach_codex_thread(session["id"], "")
            with self.assertRaises(KeyError):
                store.get_session("missing")
            with self.assertRaises(KeyError):
                store.get_project("missing")

    def test_future_schema_rejected_without_changing_it(self):
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        with self.assertRaisesRegex(ValueError, "Unsupported database schema"):
            Store(self.db)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_failed_migration_is_atomic(self):
        with patch("runtime.store.MIGRATIONS", (("CREATE TABLE partial (id TEXT)", "INVALID SQL"),)):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.db)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall(), [])
        with Store(self.db) as store:
            self.assertEqual(store.list_projects(), [])


class ProjectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-project-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Runtime Test")
        self.git("config", "user.email", "runtime@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "tracked.txt").write_text("baseline\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "fixture")
        self.store = Store(self.root / "state.sqlite3")
        self.addCleanup(self.store.close)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True, timeout=10,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        ).stdout.strip()

    def snapshot(self):
        return (self.git("rev-parse", "HEAD"), self.git("symbolic-ref", "HEAD"),
                self.git("status", "--porcelain=v1"), self.git("diff"),
                self.git("diff", "--cached"),
                (self.repo / "tracked.txt").read_bytes(),
                (self.repo / "untracked.txt").read_bytes())

    async def test_dirty_feature_checkout_registration_preserves_all_changes(self):
        self.git("switch", "-qc", "feature")
        (self.repo / "tracked.txt").write_text("staged\n")
        self.git("add", "tracked.txt")
        (self.repo / "tracked.txt").write_text("unstaged\n")
        (self.repo / "untracked.txt").write_text("user data\n")
        before = self.snapshot()
        project = await register_project(self.store, self.repo)
        self.assertEqual(project["canonical_branch"], "main")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(await register_project(self.store, self.repo), project)

    async def test_explicit_branch_and_absent_main(self):
        self.git("branch", "-m", "trunk")
        with self.assertRaises(ProjectError):
            await register_project(self.store, self.repo)
        self.assertEqual(self.store.list_projects(), [])
        inspected = await inspect_project(self.repo, "trunk")
        self.assertEqual(inspected["revision"], self.git("rev-parse", "trunk"))
        project = await register_project(self.store, self.repo, "trunk")
        self.assertEqual(project["canonical_branch"], "trunk")

    async def test_invalid_inputs_do_not_create_metadata(self):
        child = self.repo / "child"
        child.mkdir()
        bare = self.root / "bare.git"
        self.git("init", "-q", "--bare", str(bare))
        for path, branch in ((self.root, "main"), (child, "main"),
                             (bare, "main"), (self.repo, "missing"),
                             (self.repo, "bad..branch")):
            with self.subTest(path=path, branch=branch), self.assertRaises(ProjectError):
                await register_project(self.store, path, branch)
        self.assertEqual(self.store.list_projects(), [])

    async def test_linked_worktree_rejected_and_symlink_deduplicated(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "-qb", "work", str(linked), "main")
        with self.assertRaisesRegex(ProjectError, "linked worktree"):
            await register_project(self.store, linked)
        alias = self.root / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        project = await register_project(self.store, alias)
        self.assertEqual(await register_project(self.store, self.repo), project)

    async def test_detached_checkout_and_inherited_git_routing(self):
        self.git("checkout", "--detach", "-q")
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "nonexistent"),
                                     "GIT_WORK_TREE": str(self.root)}):
            project = await register_project(self.store, self.repo)
        self.assertEqual(project["path"], str(self.repo))
        self.assertEqual(project["canonical_branch"], "main")

    async def test_git_timeout_and_cancellation_reap_process(self):
        # Exercise cleanup deterministically without waiting for a real hung Git.
        from runtime.projects import git

        class FakeProcess:
            returncode = None
            killed = False
            waited = False

            async def communicate(self):
                raise asyncio.CancelledError

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True
                self.returncode = -9

        for failure in (asyncio.CancelledError, TimeoutError):
            proc = FakeProcess()
            with patch("runtime.projects.asyncio.create_subprocess_exec", return_value=proc), \
                    patch.object(proc, "communicate", side_effect=failure):
                with self.assertRaises(failure):
                    await git(self.repo, "rev-parse", "HEAD")
            self.assertTrue(proc.killed)
            self.assertTrue(proc.waited)


if __name__ == "__main__":
    unittest.main()
