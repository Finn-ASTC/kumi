"""UX-02a directory reads only explicitly registered run identities."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_token_tools import ToolCase
import jobs
import protocol
import runs
import sessions


class SessionDirectoryTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.run = runs.init_run(self.root)
        self.now = 100.0
        self.counter = 0

    def add_job(self, task="Implement parser\nPRIVATE PROMPT DETAILS", *, bind=True):
        self.counter += 1
        self.task.write_text(task, encoding="utf-8")
        info = self.prepare(root=None, run=self.run["run_path"])
        protocol.record(argparse.Namespace(
            request=info["request_path"], mode="insider", session="main",
            agent=f"worker-{self.counter}", pane=f"w1:p{self.counter + 1}", workspace=None,
            tab=None, parent_pane=None, parent_tab=None, owns_tab=False,
            owns_agent=True, owns_pane=True, owns_workspace=False, owns_session=False,
            tmux_socket=None, tmux_server=None, tmux_default_server=False,
        ))
        state = jobs.register(self.run["index_path"], info["request_path"], now=self.now)
        state = jobs.claim(self.run["index_path"], state["job_id"], state["revision"],
                           f"controller-{self.counter}", 600, now=self.now)
        if bind:
            state = jobs.change(self.run["index_path"], state["job_id"], state["revision"],
                                state["lease"]["token"], "update", {
                                    "launch": {"argv": ["/usr/bin/hermes", "--profile", "private"],
                                               "profile": "private-profile"},
                                    "native": {"session_id": f"native-session-{self.counter}",
                                               "turn_id": "turn-1"}}, now=self.now + 1)
        return info, state

    def snapshot_files(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in Path(self.run["run_path"]).parent.rglob("*") if path.is_file()}

    def test_lists_only_known_run_jobs_and_redacts_full_task(self):
        _, first = self.add_job()
        _, second = self.add_job("Verify API\nPRIVATE SECOND LINE")
        view = sessions.directory(self.run["run_path"])
        self.assertEqual(view["counts"]["indexed_jobs"], 2)
        self.assertEqual(view["counts"]["returned_sessions"], 2)
        self.assertEqual({item["job_id"] for item in view["sessions"]},
                         {first["job_id"], second["job_id"]})
        row = next(item for item in view["sessions"] if item["job_id"] == second["job_id"])
        self.assertEqual(row["task_title"], "Verify API")
        self.assertNotIn("PRIVATE SECOND LINE", json.dumps(view))
        self.assertEqual(row["native_identity"]["host"], None)
        self.assertEqual(row["native_identity"]["status"], "host_unknown")
        self.assertEqual(row["native_identity"]["session_id"], "native-session-2")
        self.assertEqual(row["resources"]["agent"], "worker-2")
        self.assertFalse(row["recovery_preview"]["available"])
        self.assertTrue(row["recovery_preview"]["candidate"])
        self.assertIn("native_archive_adapter_unavailable", row["archive_preview"]["blockers"])
        self.assertFalse(view["executes_commands"])
        self.assertFalse(view["writes_host"])
        self.assertFalse(view["archive"]["supported"])
        self.assertFalse(view["recovery"]["supported"])
        self.assertEqual(len(view["host_capabilities"]), 5)
        self.assertEqual({capability["session_store_isolation"] for capability in view["host_capabilities"]},
                         {"unknown"})

    def test_unbound_identity_is_visible_as_unknown_and_not_resumable(self):
        _, state = self.add_job(bind=False)
        row = sessions.directory(self.run["run_path"], job_id=state["job_id"])["sessions"][0]
        self.assertEqual(row["native_identity"]["status"], "unbound")
        self.assertFalse(row["recovery_preview"]["available"])
        self.assertEqual(row["relationship_status"], "not_recorded")

    def test_pagination_and_exact_job_selector(self):
        _, first = self.add_job()
        _, second = self.add_job()
        _, third = self.add_job()
        first_page = sessions.directory(self.run["run_path"], limit=2)
        self.assertEqual(len(first_page["sessions"]), 2)
        self.assertTrue(first_page["has_more"])
        last_page = sessions.directory(self.run["run_path"], limit=2, offset=2)
        self.assertEqual(len(last_page["sessions"]), 1)
        self.assertFalse(last_page["has_more"])
        selected = sessions.directory(self.run["run_path"], job_id=second["job_id"])
        self.assertEqual([row["job_id"] for row in selected["sessions"]], [second["job_id"]])
        for bad_job in ("latest", "../job", "A" * 32):
            with self.subTest(job=bad_job), self.assertRaises(ValueError):
                sessions.directory(self.run["run_path"], job_id=bad_job)
        for args in ({"limit": 0}, {"limit": 1001}, {"offset": -1}, {"limit": True}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                sessions.directory(self.run["run_path"], **args)
        self.assertEqual(len({first["job_id"], second["job_id"], third["job_id"]}), 3)

    def test_pagination_reads_only_the_requested_job_snapshots(self):
        self.add_job()
        self.add_job()
        self.add_job()
        with patch.object(sessions.jobs, "load", wraps=jobs.load) as load:
            view = sessions.directory(self.run["run_path"], limit=1, offset=1)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(len(view["sessions"]), 1)
        self.assertTrue(view["has_more"])

    def test_read_only_does_not_change_any_run_file(self):
        self.add_job()
        before = self.snapshot_files()
        for _ in range(3):
            sessions.directory(self.run["run_path"])
        self.assertEqual(self.snapshot_files(), before)

    def test_corrupt_and_symlinked_job_directories_are_visible_errors(self):
        _, state = self.add_job()
        job_dir = Path(self.run["index_path"]) / state["job_id"]
        (job_dir / "000000002.json").write_text("{bad", encoding="utf-8")
        redirected = Path(self.run["index_path"]) / ("f" * 32)
        outside = self.root / "outside-job"
        outside.mkdir()
        redirected.symlink_to(outside, target_is_directory=True)
        view = sessions.directory(self.run["run_path"])
        self.assertEqual(view["counts"], {"indexed_jobs": 2, "returned_sessions": 0, "errors": 2})
        self.assertEqual({item["code"] for item in view["errors"]},
                         {"job_session_unreadable", "job_directory_redirected"})
        self.assertNotIn(str(outside), json.dumps(view))

    def test_cli_returns_structured_read_only_summary_and_redacts_failure(self):
        self.add_job()
        cli = subprocess.run([sys.executable, str(Path(sessions.__file__)),
                              "--run", self.run["run_path"], "--limit", "1"],
                             capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["mode"], "read_only_session_directory")
        bad = subprocess.run([sys.executable, str(Path(sessions.__file__)), "--run",
                              str(self.root / "missing" / "run.json")], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn(str(self.root), bad.stderr)

    def test_errors_are_generic_without_echoing_invalid_task_payload(self):
        self.add_job()
        index = Path(self.run["index_path"])
        with patch.object(sessions.jobs, "load", side_effect=ValueError("PRIVATE FAILURE DETAIL")):
            view = sessions.directory(self.run["run_path"])
        self.assertEqual(view["errors"][0]["code"], "job_session_unreadable")
        self.assertNotIn("PRIVATE FAILURE DETAIL", json.dumps(view))
