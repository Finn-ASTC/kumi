"""Evidence validity must survive separate CLI processes and fault boundaries."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_token_tools import ToolCase
import capabilities
import jobs
import protocol
import runs


class CapabilityTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.run = runs.init_run(self.root)
        self.proof = self.root / "proof.json"
        self.proof.write_text('{"fixture": true, "private": "SECRET BODY"}')
        self.args = dict(run_path=self.run["run_path"], host="omp", profile="default",
                         capability="resume", status="verified", evidence_path=str(self.proof),
                         observed_at=datetime.now(timezone.utc).isoformat(), host_version="fixture-1")

    def record(self, **changes):
        return capabilities.register(**{**self.args, **changes})

    def test_verified_requires_existing_regular_absolute_evidence(self):
        link = self.root / "linked.json"
        link.symlink_to(self.proof)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        for path in (str(self.root / "absent"), str(link), "relative.json", str(self.root), str(fifo)):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.record(evidence_path=path)
        self.assertEqual(capabilities.list_records(self.run["run_path"]), [])

    def test_verified_requires_version_and_real_nonfuture_timestamp(self):
        for fields in ({"host_version": None}, {"observed_at": "not-time"},
                       {"observed_at": "2099-01-01T00:00:00Z"},
                       {"observed_at": "2026-01-01T00:00:00"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.record(**fields)

    def test_digest_pin_and_changed_missing_evidence_degrade_without_writing(self):
        record = self.record()
        self.assertEqual(record["evidence_sha256"], jobs.fingerprint(self.proof))
        path = Path(self.run["run_path"]).parent / capabilities.REGISTRY_NAME / (record["record_id"] + ".json")
        committed = path.read_bytes()
        view = capabilities.list_records(self.run["run_path"])[0]
        self.assertEqual(view["effective_status"], "verified")
        self.assertEqual(view["evidence_status"], "valid")
        self.proof.write_text("replaced proof")
        view = capabilities.list_records(self.run["run_path"])[0]
        self.assertEqual(view["status"], "verified")
        self.assertEqual(view["effective_status"], "unknown")
        self.assertEqual(view["evidence_status"], "invalid")
        self.proof.unlink()
        self.assertEqual(capabilities.list_records(self.run["run_path"])[0]["effective_status"], "unknown")
        self.assertEqual(path.read_bytes(), committed)
        self.assertNotIn("SECRET BODY", json.dumps(view))

    def test_legacy_unpinned_verified_claim_remains_readable_but_unknown(self):
        record = self.record()
        path = Path(self.run["run_path"]).parent / capabilities.REGISTRY_NAME / (record["record_id"] + ".json")
        legacy = {key: value for key, value in record.items() if key != "evidence_sha256"}
        legacy["version"] = 1
        path.write_text(json.dumps(legacy))
        view = capabilities.list_records(self.run["run_path"])[0]
        self.assertEqual(view["effective_status"], "unknown")
        self.assertEqual(view["evidence_status"], "unpinned")

    def test_redirected_registry_cannot_be_read_or_written(self):
        outside = self.root / "outside"
        outside.mkdir()
        root = Path(self.run["run_path"]).parent / capabilities.REGISTRY_NAME
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.record()
        with self.assertRaises(ValueError):
            capabilities.list_records(self.run["run_path"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_lost_receipt_retry_reuses_one_immutable_record(self):
        with patch.object(capabilities.jobs, "sync_directory", side_effect=OSError("lost receipt")):
            with self.assertRaises(OSError):
                self.record()
        first = self.record()
        self.assertEqual(self.record(), first)
        self.assertEqual(len(capabilities.list_records(self.run["run_path"])), 1)

    def test_record_filename_and_run_binding_are_checked(self):
        record = self.record()
        path = Path(self.run["run_path"]).parent / capabilities.REGISTRY_NAME / (record["record_id"] + ".json")
        renamed = path.with_name("f" * 64 + ".json")
        path.rename(renamed)
        with self.assertRaises(ValueError):
            capabilities.list_records(self.run["run_path"])
        renamed.rename(path)
        record["run_id"] = "f" * 32
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            capabilities.list_records(self.run["run_path"])

    def test_lock_busy_publishes_nothing(self):
        with jobs.locked(Path(self.run["index_path"])), self.assertRaises(ValueError):
            self.record()
        self.assertEqual(capabilities.list_records(self.run["run_path"]), [])

    def test_failed_publish_then_retry_and_two_process_readback(self):
        with patch.object(capabilities.protocol, "publish", side_effect=OSError("prepublish")):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(capabilities.list_records(self.run["run_path"]), [])
        record = self.record()
        for _ in range(2):
            reply = self.run_tool("capabilities.py", "list", "--run", self.run["run_path"])
            self.assertEqual(reply.returncode, 0, reply.stderr)
            view = json.loads(reply.stdout)["records"][0]
            self.assertEqual(view["record_id"], record["record_id"])
            self.assertEqual(view["effective_status"], "verified")

    def test_two_process_same_identity_has_one_record_and_idempotent_retry(self):
        argv = [sys.executable, capabilities.__file__, "register", "--run", self.run["run_path"],
                "--host", "omp", "--profile", "default", "--capability", "resume", "--status", "verified",
                "--evidence-path", str(self.proof), "--host-version", "fixture-1",
                "--observed-at", self.args["observed_at"]]
        children = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(2)]
        for child in children:
            child.communicate(timeout=10)
        self.assertIn(0, [child.returncode for child in children])
        self.assertTrue(all(child.returncode in (0, 2) for child in children))
        replay = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(len(capabilities.list_records(self.run["run_path"])), 1)

    def test_same_contract_for_all_five_configurations(self):
        for host, profile in (("codex", "default"), ("omp", "default"), ("hermes", "default"),
                              ("opencode", "omo"), ("opencode", "pure")):
            self.record(host=host, profile=profile)
        records = capabilities.list_records(self.run["run_path"])
        self.assertEqual(len(records), 5)
        self.assertEqual({r["effective_status"] for r in records}, {"verified"})
        self.assertEqual(capabilities.by_host_profile(records, "opencode", "unregistered"), {})

    def test_cli_failure_does_not_echo_private_path_or_evidence(self):
        reply = self.run_tool("capabilities.py", "register", "--run", self.run["run_path"],
                              "--host", "omp", "--profile", "default", "--capability", "resume",
                              "--status", "verified", "--evidence-path", str(self.root / "SECRET"),
                              "--observed-at", self.args["observed_at"], "--host-version", "fixture-1")
        self.assertEqual(reply.returncode, 2)
        self.assertNotIn("SECRET", reply.stderr)
