"""A later live OpenCode turn must not block selection of earlier settled work."""

import copy
import json
from pathlib import Path

from test_token_tools import ToolCase
import test_usage_adapters as fixtures


class LiveSelectionTests(ToolCase):
    export = fixtures.UsageAdapterTests.export
    manifest = fixtures.UsageAdapterTests.manifest
    opencode_db = fixtures.UsageAdapterTests.opencode_db

    def live(self):
        path, value = self.export()
        more = copy.deepcopy(value["messages"])
        more[0]["info"]["id"] = "u2"
        more[1]["info"].update(id="a2", parentID="u2", time={"created": 3})
        value["messages"].extend(more)
        path.write_text(json.dumps(value))
        return path, value

    def test_inspection_exposes_pending_ids_without_counting_them(self):
        import native_usage
        path, _ = self.live()
        report = native_usage.inspect_log("opencode", str(path))
        turns = {t["native_id"]: t for t in report["turns"]}
        self.assertEqual(turns["u1"]["calls"], 1)
        self.assertEqual(turns["u1"]["pending_sample_ids"], [])
        self.assertEqual(turns["u2"]["calls"], 0)
        self.assertEqual(turns["u2"]["pending_sample_ids"], ["a2"])

    def test_previous_completed_turn_imports_from_export_and_sqlite(self):
        import native_usage
        path, value = self.live()
        for source in (path, self.opencode_db(value)):
            report = native_usage.import_manifest(str(self.manifest(source)))
            self.assertEqual(report["selected_calls"], 1)
            self.assertEqual(report["pending_calls"], 1)
            self.assertEqual(report["pending_native_ids"], ["u2"])

    def test_selecting_live_turn_fails_before_any_calls_commit(self):
        import native_usage
        path, _ = self.live()
        manifest = self.manifest(path)
        value = json.loads(manifest.read_text())
        value["mappings"].append({**value["mappings"][0], "native_id": "u2"})
        manifest.write_text(json.dumps(value))
        for dry_run in (True, False):
            with self.assertRaisesRegex(ValueError, "incomplete|pending"):
                native_usage.import_manifest(str(manifest), dry_run=dry_run)
        self.assertFalse((Path(self.info["request_path"]).parent / "usage").exists())

    def test_pending_call_under_selected_user_also_blocks(self):
        import native_usage
        path, value = self.live()
        value["messages"][-1]["info"]["parentID"] = "u1"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "incomplete|pending"):
            native_usage.import_manifest(str(self.manifest(path)))

    def test_unselected_corruption_still_rejected(self):
        import native_usage
        path, value = self.live()
        value["messages"][-1]["info"]["sessionID"] = "wrong-session"
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            native_usage.import_manifest(str(self.manifest(path)))
        value["messages"][-1]["info"]["sessionID"] = "ses-1"
        value["messages"][-1]["info"]["tokens"]["input"] = -1
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            native_usage.import_manifest(str(self.manifest(path)))

    def test_later_completion_reimport_adds_only_new_call(self):
        import native_usage
        import usage
        path, value = self.live()
        manifest = self.manifest(path)
        native_usage.import_manifest(str(manifest))
        value["messages"][-1]["info"]["time"]["completed"] = 4
        path.write_text(json.dumps(value))
        spec = json.loads(manifest.read_text())
        spec["mappings"].append({**spec["mappings"][0], "native_id": "u2"})
        manifest.write_text(json.dumps(spec))
        report = native_usage.import_manifest(str(manifest))
        self.assertEqual((report["imported"], report["duplicates"], report["pending_calls"]), (1, 1, 0))
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 2)
