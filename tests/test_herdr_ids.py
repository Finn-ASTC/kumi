"""Real public-ID grammar at the protocol, observation and cleanup boundaries."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase, protocol
import watch


class HerdrIdentityTests(ToolCase):
    def test_public_alphabet_is_accepted_in_all_resource_positions(self):
        # herdr v0.9.1 src/workspace.rs:105; this is not base-10 or base-36.
        base = self.record()
        for number in (*"123456789ABCDEFGHJKMNPQRSTVWXYZ0", "1A", "11"):
            with self.subTest(number=number):
                record = {**base, "pane": f"w{number}:p{number}",
                          "workspace": f"w{number}", "tab": f"w{number}:t{number}",
                          "parent_pane": f"wZZ:p{number}", "parent_tab": f"wZZ:t{number}",
                          "owns_workspace": True}
                self.assertEqual(protocol.validate_resources(record, self.request), record)

    def test_numeric_child_with_letter_parent_is_accepted(self):
        record = self.record(pane="w15:p1", workspace="w15", tab="w15:t1",
                             parent_pane="wJ:p1", parent_tab="wJ:t1", owns_workspace=True)
        self.assertEqual(record["parent_pane"], "wJ:p1")

    def test_public_ids_reject_aliases_noncanonical_letters_and_control_text(self):
        base = self.record()
        bad = ("current", "focused", "1", "w_1", "wI", "wL", "wO", "wU",
               "wa", "w", "w1\n", "w１", "w1:extra", "--all")
        for workspace in bad:
            for field, value in (("workspace", workspace), ("pane", workspace + ":p1"),
                                 ("tab", workspace + ":t1"),
                                 ("parent_pane", workspace + ":p1"),
                                 ("parent_tab", workspace + ":t1")):
                with self.subTest(field=field, value=value), self.assertRaises(protocol.ProtocolError):
                    protocol.validate_resources({**base, field: value}, self.request)
        for field, prefix in (("pane", "p"), ("tab", "t"),
                              ("parent_pane", "p"), ("parent_tab", "t")):
            for suffix in ("I", "L", "O", "U", "a", "", "1\n", "１", "1:extra"):
                with self.subTest(field=field, suffix=suffix), self.assertRaises(protocol.ProtocolError):
                    protocol.validate_resources({**base, field: f"w1:{prefix}{suffix}"}, self.request)

    def test_letter_tab_cli_preserves_parent_and_closes_only_owned_tab(self):
        result = self.cli("record", "--request", self.info["request_path"], "--mode", "insider",
                          "--session", "user-session", "--agent", "child", "--pane", "w1A:pZ",
                          "--workspace", "w1A", "--tab", "w1A:tB", "--parent-pane", "w1A:pN",
                          "--parent-tab", "w1A:tA", "--owns-agent", "--owns-pane", "--owns-tab")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = protocol.cleanup_plan(argparse.Namespace(request=self.info["request_path"]))
        self.assertEqual([a["argv"] for a in plan["actions"] if "argv" in a],
                         [["herdr", "--session", "user-session", "tab", "close", "w1A:tB"]])
        resources = json.loads(result.stdout)
        for changes in ({"parent_pane": "w1A:pZ"}, {"parent_tab": "w1A:tB"},
                        {"tab": "w1B:tB"}, {"parent_tab": "w1B:tA"}, {"owns_workspace": True}):
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                protocol.validate_resources({**resources, **changes}, self.request)

    def test_letter_resource_survives_observation_blocked_followup_and_cleanup(self):
        resources = self.record(pane="w1R:pB", workspace="w1R", tab="w1R:tA",
                                parent_pane="wJ:pN", parent_tab="wJ:tM", owns_workspace=True)
        handle = watch.init_watch([self.info["request_path"]], self.root)["watch_path"]

        def read(argv):
            if argv == ["herdr", "--session", "user-session", "agent", "get", "w1R:pB"]:
                return json.dumps({"result": {"agent": {"name": "child", "pane_id": "w1R:pB",
                                                       "agent_status": "working"}}})
            if argv == ["herdr", "--session", "user-session", "pane", "read", "w1R:pB",
                        "--source", "detection", "--lines", "80"]:
                return "Command: read fixture\nAllow once / Deny"
            raise AssertionError(f"Unexpected transport operation: {argv}")

        with patch.object(watch, "run_read", side_effect=read):
            events = watch.poll_watch(handle)["events"]
            self.assertIn("attention", [e["kind"] for e in events])
            protocol.publish(self.info["result_path"], self.response(
                status="blocked", blocked_reason="Which fixture should be used?"))
            self.assertIn("result", [e["kind"] for e in watch.poll_watch(handle)["events"]])
            follow = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
            self.assertEqual(follow["job_id"], self.info["job_id"])
            self.assertNotEqual(follow["round_id"], self.info["round_id"])
            self.assertFalse(Path(follow["result_path"]).exists())
            self.assertEqual(protocol.read_json(Path(follow["request_path"]).parent / "resources.json"),
                             resources)
            next_handle = watch.init_watch([follow["request_path"]], self.root)["watch_path"]
            response = self.response(round_id=follow["round_id"])
            protocol.publish(follow["result_path"], response)
            events = watch.poll_watch(next_handle)["events"]
            self.assertTrue(any(e["kind"] == "result" and e["round_id"] == follow["round_id"]
                                for e in events))
        plan = protocol.cleanup_plan(argparse.Namespace(request=follow["request_path"]))
        self.assertEqual([a["argv"] for a in plan["actions"] if "argv" in a],
                         [["herdr", "--session", "user-session", "workspace", "close", "w1R"]])
