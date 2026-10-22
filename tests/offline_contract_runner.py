"""Inspected B0/B1/B2 suites only. No discovery, application startup or live adapters.

Example (use the existing interpreter, no installation):
  python -I -S -B tests/offline_contract_runner.py --dependencies PATH --evidence NEW_DIRECTORY
Repeat with --module NAME for standalone collection or --reverse for order checks.
Evidence is synthetic; this runner is not full-suite or live acceptance.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import time
import traceback
import unittest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
from fixture_bootstrap import OfflineGuard

SELECTED = (
    "test_chat", "test_launcher_connections", "test_twitch_feature_toggles",
    "test_twitch_command_routing", "test_time_lookup", "test_twitch_memory_wiring",
    "test_conversation_context", "test_twitch_autosearch",
    "test_twitch_outgoing", "test_twitch_output_filter",
)
B1_SELECTED = (
    "test_twitch_authority", "test_twitch_identity",
    "test_twitch_listener_reliability", "test_twitch_tls",
)
B2_SELECTED = ("test_memory_privacy_boundaries", "test_viewer_profiles", "test_viewer_profile_safety")
B2_HEALTH_SELECTED = ("test_health_conditions", "test_memory_exclusions")
B2_FINAL_SELECTED = ("test_memory_control", "test_written_receipts", "test_feature_config_saves")
# Stable integration includes source-isolated launcher and startup evidence.
B9_INTEGRATION_SELECTED = ("test_avatar_delivery", "test_startup_stage_diagnostics", "test_launcher_focused")
B9_UTIL_SELECTED = ("test_b9_utilities",)
B9_COUNTER_SELECTED = ("test_b9_counters",)
B9_SOUND_SELECTED = ("test_b9_sound",)
B8_SELECTED = ("test_b8_catalog", "test_b8_commands_link")
B7_STATUS_SELECTED = ("test_b7_endpoint_status",)
B7_LIFECYCLE_SELECTED = ("test_b7_lifecycle_intent",)
B7_ROOT_SELECTED = ("test_command_center_root",)
B6_SELECTED = ("test_medlar_diagnostics", "test_b6_diagnostics")
B5_CONTRACT_SELECTED = ("test_config_contract",)
B5_WRITER_SELECTED = ("test_config_writer_locks",)
B4_PACING_SELECTED = ("test_chat_pacing",)
B4_ENDPOINT_SELECTED = ("test_ollama_endpoint",)
B4_PARSER_SELECTED = ("test_ollama_response",)
B3_SELECTED = ("test_irc_framing", "test_unicode_output", "test_fuzzy_keyboard", "test_fuzzy_trigger_privacy")


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, guard, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard = guard
        self.started = []
        self.guard_failures = []

    def startTest(self, test):
        self.guard.current_test = test.id()
        self.started.append(test.id())
        self.denial_start = len(self.guard.denials)
        super().startTest(test)

    def stopTest(self, test):
        if len(self.guard.denials) > self.denial_start:
            self.guard_failures.append(test.id())
            try:
                raise AssertionError("An offline guard denial occurred, even if caught by the test")
            except AssertionError:
                self.addFailure(test, sys.exc_info())
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--batch", choices=("b0", "b1", "b2", "b2-health", "b2-final", "b3", "b4-parser", "b4-endpoint", "b4-pacing", "b4", "b5-writer", "b5-contract", "b5", "b6", "b7-root", "b7", "b7-lifecycle", "b7-combined", "b7-status", "b7-final", "b8", "b8-targeted", "b9-sound", "b9", "b9-counters", "b9-combined", "b9-utils", "b9-final", "b9-integration"), default="b0")
    parser.add_argument("--module", action="append", choices=SELECTED + B1_SELECTED + B2_SELECTED + B2_HEALTH_SELECTED + B2_FINAL_SELECTED + B3_SELECTED + B4_PARSER_SELECTED + B4_ENDPOINT_SELECTED + B4_PACING_SELECTED + B5_WRITER_SELECTED + B5_CONTRACT_SELECTED + B6_SELECTED + B7_ROOT_SELECTED + B7_LIFECYCLE_SELECTED + B7_STATUS_SELECTED + B8_SELECTED + B9_SOUND_SELECTED + B9_COUNTER_SELECTED + B9_UTIL_SELECTED + B9_INTEGRATION_SELECTED)
    parser.add_argument("--case", help="One exact collected test ID within the selected modules")
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("Use -I -S -B so site hooks, inherited Python paths and bytecode writes are disabled")
    evidence = args.evidence.resolve()
    if evidence == ROOT or ROOT in evidence.parents:
        parser.error("Evidence must be outside the source checkout")
    evidence.mkdir(parents=True, exist_ok=False)
    dependencies = args.dependencies.resolve(strict=True)
    sys.path.append(str(dependencies))
    guard = OfflineGuard(ROOT, evidence, dependencies)
    guard.install()
    report = {"scope": args.batch.upper() + " selected offline contracts", "source_root": str(ROOT),
              "selected_modules": args.module or list({
                  "b0": SELECTED, "b1": SELECTED+B1_SELECTED,
                  "b2": SELECTED+B1_SELECTED+B2_SELECTED,
                  "b2-health": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED,
                  "b2-final": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED,
                  "b3": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED,
                  "b4-parser": B4_PARSER_SELECTED, "b4-endpoint": B4_ENDPOINT_SELECTED,
                  "b4-pacing": B4_PACING_SELECTED, "b5-writer": B5_WRITER_SELECTED,
                  "b5-contract": B5_CONTRACT_SELECTED,
                  "b4": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED,
                  "b5": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED,
                  "b6": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED,
                  "b7": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED,
                  "b7-combined": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED+B7_LIFECYCLE_SELECTED,
                  "b7-final": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED+B7_LIFECYCLE_SELECTED+B7_STATUS_SELECTED,
                  "b8": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED+B7_LIFECYCLE_SELECTED+B7_STATUS_SELECTED+B8_SELECTED,
                  "b8-targeted": B8_SELECTED,
                  "b9-sound": B9_SOUND_SELECTED,
                  "b9": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED+B7_LIFECYCLE_SELECTED+B7_STATUS_SELECTED+B8_SELECTED+B9_SOUND_SELECTED,
                  "b9-combined": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED+B7_LIFECYCLE_SELECTED+B7_STATUS_SELECTED+B8_SELECTED+B9_SOUND_SELECTED+B9_COUNTER_SELECTED,
                  "b9-final": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED+B7_LIFECYCLE_SELECTED+B7_STATUS_SELECTED+B8_SELECTED+B9_SOUND_SELECTED+B9_COUNTER_SELECTED+B9_UTIL_SELECTED,
                  "b9-integration": SELECTED+B1_SELECTED+B2_SELECTED+B2_HEALTH_SELECTED+B2_FINAL_SELECTED+B3_SELECTED+B4_PARSER_SELECTED+B4_ENDPOINT_SELECTED+B4_PACING_SELECTED+B5_WRITER_SELECTED+B5_CONTRACT_SELECTED+B6_SELECTED+B7_ROOT_SELECTED+B7_LIFECYCLE_SELECTED+B7_STATUS_SELECTED+B8_SELECTED+B9_SOUND_SELECTED+B9_COUNTER_SELECTED+B9_UTIL_SELECTED+B9_INTEGRATION_SELECTED,
                  "b9-utils": B9_UTIL_SELECTED,
                  "b9-counters": B9_COUNTER_SELECTED,
                  "b7-status": B7_STATUS_SELECTED,
                  "b7-lifecycle": B7_LIFECYCLE_SELECTED,
                  "b7-root": B7_ROOT_SELECTED,
              }[args.batch]), "network": "denied",
              "subprocess": "denied", "credentials": "denied",
              "write_root": str(evidence), "production_writes_during_tests": False}
    started_at = time.monotonic()
    output = io.StringIO()
    result = None
    try:
        report["guard_checks"] = guard.self_check()
        suites = []
        static_ids = []
        loader = unittest.TestLoader()
        for name in report["selected_modules"]:
            source = TESTS / (name + ".py")
            tree = ast.parse(source.read_text(encoding="utf-8"))
            static_ids.extend(name + "." + cls.name + "." + method.name
                              for cls in tree.body if isinstance(cls, ast.ClassDef)
                              for method in cls.body if isinstance(method, ast.FunctionDef)
                              and method.name.startswith("test_"))
            with redirect_stdout(output), redirect_stderr(output):
                module = importlib.import_module(name)
            suites.append(loader.loadTestsFromModule(module))
        cases = list(flatten(unittest.TestSuite(suites)))
        collected = [test.id() for test in cases]
        if loader.errors or len(collected) != len(set(collected)) or set(collected) != set(static_ids):
            raise AssertionError("Selected/collected test IDs disagree or collection errors occurred")
        report["available_count"] = len(collected)
        if args.case:
            cases = [test for test in cases if test.id() == args.case]
            if len(cases) != 1:
                raise AssertionError("Exact case did not collect once")
        if args.reverse:
            cases.reverse()
        report["selected_count"] = len(cases)
        report["selected_ids"] = [test.id() for test in cases]
        report["source_hashes"] = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [ROOT / "launcher.py", *sorted((ROOT / "MedlarTV").rglob("*.py")),
                         *[TESTS / (name + ".py") for name in report["selected_modules"]],
                          TESTS / "chat_pacing_fixture.py", TESTS / "fixture_bootstrap.py",
                          TESTS / "offline_contract_runner.py"]}
        runner = unittest.TextTestRunner(
            stream=output, verbosity=2,
            resultclass=lambda *a, **kw: RecordedResult(*a, guard=guard, **kw))
        with redirect_stdout(output), redirect_stderr(output):
            result = runner.run(unittest.TestSuite(cases))
        report.update(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                      skips=len(result.skipped), expected_failures=len(result.expectedFailures),
                      unexpected_successes=len(result.unexpectedSuccesses), started_ids=result.started,
                      guard_failures=result.guard_failures)
        report["ok"] = (result.wasSuccessful() and not result.skipped and not result.expectedFailures
                        and result.started == report["selected_ids"] and not guard.denials)
    except Exception:
        report["ok"] = False
        traceback.print_exc(file=output)
    finally:
        guard.current_test = "evidence"
        report["unexpected_denials"] = guard.denials
        report["expected_guard_denials"] = guard.expected_denials
        report["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
        (evidence / "test-output.txt").write_text(output.getvalue(), encoding="utf-8")
        (evidence / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in
                     ("ok", "selected_count", "tests_run", "failures", "errors", "skips", "elapsed_seconds")}
                     | {"unexpected_denials": len(guard.denials), "evidence": str(evidence)}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
