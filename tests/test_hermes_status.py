"""Metering prerequisites must never masquerade as capture or orchestration health."""

import json
import shutil
import os
from unittest.mock import patch

from test_token_tools import ToolCase, SCRIPTS


class HermesStatusTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.host = self.root / "host"
        self.home = self.root / "home"
        (self.host / "hermes_cli").mkdir(parents=True)
        (self.host / "hermes_cli/__init__.py").write_text("")
        self.home.mkdir()
        self.source = SCRIPTS.parents[1] / "agent-hermes/plugins/orch-usage"
        self.installed = self.home / "plugins/orch-usage"
        shutil.copytree(self.source, self.installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.config = dict(plugins=dict(enabled=["orch-usage"]))
        # This fixture supplies the optional host YAML dependency for the subprocess;
        # real Hermes/PyYAML resolution is exercised separately by the native probe.
        (self.host / "yaml.py").write_text("import json\nsafe_load = json.loads\n")
        self.main_hooks = ["pre_llm_call", "pre_api_request", "post_api_request", "api_request_error", "on_session_end"]
        self.set_hooks(self.main_hooks)

    def set_hooks(self, hooks):
        (self.host / "hermes_cli/plugins.py").write_text("VALID_HOOKS = " + repr(set(hooks)) + "\n"
            "def discover_plugins():\n    raise AssertionError('status must not discover or execute plugins')\n")

    def run_status(self):
        (self.home / "config.yaml").write_text(json.dumps(self.config))
        result = self.run_tool("hermes_status.py", "--hermes-root", self.host, "--profile-home", self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("PRIVATE", result.stdout)
        return json.loads(result.stdout)

    def test_stock_host_degrades_auxiliary_without_blocking_dispatch(self):
        report = self.run_status()
        self.assertEqual(report["layers"]["main_loop"]["status"], "candidate")
        self.assertEqual(report["layers"]["auxiliary"]["status"], "unavailable")
        self.assertFalse(report["layers"]["orchestration"]["metering_required"])
        self.assertEqual(report["layers"]["orchestration"]["health"], "not_checked")
        self.assertFalse(report["capture_verified"])
        self.assertIsNone(report["totals"])
        self.assertFalse(report["coverage_complete"])

    def test_advertised_auxiliary_still_does_not_prove_capture(self):
        self.set_hooks(self.main_hooks + ["on_aux_usage"])
        report = self.run_status()
        self.assertEqual(report["layers"]["auxiliary"]["status"], "candidate")
        self.assertEqual(report["stores"]["auxiliary"], "missing")
        self.assertFalse(report["capture_verified"])

    def test_new_observers_do_not_masquerade_as_supported_aux_usage(self):
        self.set_hooks(self.main_hooks + ["pre_auxiliary_call", "post_auxiliary_call"])
        report = self.run_status()
        self.assertEqual(report["host"]["auxiliary_observer_hooks"],
                         {"pre_auxiliary_call": True, "post_auxiliary_call": True})
        self.assertFalse(report["host"]["auxiliary_hook"])
        self.assertEqual(report["layers"]["auxiliary"]["status"], "unavailable")
        self.assertIn("auxiliary_observer_adapter_not_implemented", report["layers"]["auxiliary"]["reasons"])
        self.assertFalse(report["capture_verified"])

    def test_partial_observers_keep_legacy_contract_independent(self):
        self.set_hooks(self.main_hooks + ["on_aux_usage", "pre_auxiliary_call"])
        report = self.run_status()
        self.assertEqual(report["host"]["auxiliary_observer_hooks"],
                         {"pre_auxiliary_call": True, "post_auxiliary_call": False})
        self.assertEqual(report["layers"]["auxiliary"]["status"], "candidate")
        self.assertFalse(report["capture_verified"])

    def test_observer_absence_and_unknown_remain_distinct(self):
        report = self.run_status()
        self.assertEqual(set(report["host"]["auxiliary_observer_hooks"].values()), {False})
        (self.host / "hermes_cli/plugins.py").unlink()
        report = self.run_status()
        self.assertEqual(set(report["host"]["auxiliary_observer_hooks"].values()), {None})

    def test_disabled_plugin_and_retained_store_are_independent(self):
        self.config["plugins"]["disabled"] = ["orch-usage"]
        (self.home / "usage-hooks").mkdir()
        database = self.home / "usage-hooks/events.sqlite3"
        database.write_bytes(b"PRIVATE retained unvalidated evidence")
        report = self.run_status()
        self.assertFalse(report["plugin"]["configured_enabled"])
        self.assertEqual(report["stores"]["main_loop"], "present_uninspected")
        self.assertEqual(report["layers"]["main_loop"]["status"], "unavailable")
        self.assertEqual(database.read_bytes(), b"PRIVATE retained unvalidated evidence")

    def test_modified_plugin_is_unknown_and_never_executed(self):
        (self.installed / "__init__.py").write_text("raise RuntimeError('PRIVATE plugin should not execute')")
        report = self.run_status()
        self.assertEqual(report["plugin"]["files"], "different")
        self.assertEqual(report["layers"]["main_loop"]["status"], "unknown")

    def test_missing_plugin_is_unavailable(self):
        self.installed.rename(self.home / "saved-plugin")
        report = self.run_status()
        self.assertEqual(report["plugin"]["files"], "missing")
        self.assertEqual(report["layers"]["main_loop"]["status"], "unavailable")

    def test_import_failure_redacts_output_and_preserves_unknown(self):
        (self.host / "hermes_cli/plugins.py").write_text(
            "import sys\nprint('PRIVATE stdout')\nprint('PRIVATE stderr', file=sys.stderr)\n"
            "raise RuntimeError('PRIVATE exception')\n")
        report = self.run_status()
        self.assertEqual(report["host"]["status"], "unknown")
        self.assertIsNone(report["host"]["main_loop_hooks"])
        self.assertEqual(report["layers"]["main_loop"]["status"], "unknown")

    def test_config_shape_errors_stay_unknown(self):
        for config in (None, [], {"plugins": []}, {"plugins": {"enabled": "orch-usage"}},
                       {"plugins": {"enabled": [True]}}, {"plugins": {"disabled": "orch-usage"}}):
            with self.subTest(config=config):
                self.config = config
                report = self.run_status()
                self.assertIsNone(report["plugin"]["configured_enabled"])
                self.assertEqual(report["layers"]["main_loop"]["status"], "unknown")

    def test_configuration_is_not_echoed_and_status_does_not_write(self):
        self.config["private"] = "PRIVATE secret and endpoint"
        self.run_status()
        before = {str(p): p.read_bytes() for base in (self.home, self.host) for p in base.rglob('*') if p.is_file()}
        self.run_status()
        after = {str(p): p.read_bytes() for base in (self.home, self.host) for p in base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(list(self.host.rglob("*.pyc")))

    def test_safe_mode_disables_capture_even_when_config_enabled(self):
        with patch.dict(os.environ, {"HERMES_SAFE_MODE": "true"}):
            report = self.run_status()
        self.assertTrue(report["plugin"]["configured_enabled"])
        self.assertEqual(report["layers"]["main_loop"]["status"], "unavailable")
        self.assertIn("host_safe_mode", report["layers"]["main_loop"]["reasons"])

    def test_missing_host_source_does_not_fall_back_to_another_install(self):
        (self.host / "hermes_cli/plugins.py").unlink()
        report = self.run_status()
        self.assertEqual(report["host"]["reason"], "host_source_missing")
        self.assertIsNone(report["host"]["auxiliary_hook"])

    def test_store_symlinks_are_not_read_as_current_capture(self):
        (self.home / "usage-hooks").symlink_to(self.installed, target_is_directory=True)
        report = self.run_status()
        self.assertEqual(report["stores"]["main_loop"], "redirected_uninspected")
        self.assertFalse(report["capture_verified"])

    def test_unrecognized_registry_and_wrong_origin_are_unknown(self):
        for source, reason in (("VALID_HOOKS = None\n", "host_registry_unrecognized"),
                               ("VALID_HOOKS = set()\n__file__ = '/different/host.py'\n", "host_origin_mismatch")):
            with self.subTest(reason=reason):
                (self.host / "hermes_cli/plugins.py").write_text(source)
                report = self.run_status()
                self.assertEqual(report["host"]["reason"], reason)
                self.assertIsNone(report["host"]["main_loop_hooks"])
