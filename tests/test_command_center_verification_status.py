"""Current-run GUI verification status with real temporary files and fake children."""
import ast
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_startup_stage_diagnostics import ROOT, SECRET

OK = b"[TwitchVerify] stage=complete outcome=ok\n"
FAIL = b"[TwitchVerify] stage=broadcaster_validation outcome=failed exception=SSLError\n"


def source(namespace):
    path = ROOT / "MedlarTV/app/medlar_app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    nodes = [node for node in tree.body if getattr(node, "name", None) in
             ("_verification_status_after_log_chunk", "_VerificationLog", "_IRCLog", "_irc_connected_after_log_chunk")]
    center = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ControlCenter")
    nodes += [node for node in center.body if isinstance(node, ast.FunctionDef)
              and node.name in ("refresh_status", "_spawn_launcher_mode")]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])),
                 str(path), "exec"), namespace)
    return namespace


class VerificationStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.path = self.logs / "stream_management.txt"
        fake_os = SimpleNamespace(environ={}, getpid=lambda: 12345, name="posix",
                                  fstat=os.fstat, SEEK_END=os.SEEK_END)
        self.ns = source({"os": fake_os, "Path": Path,
                          "tempfile": SimpleNamespace(gettempdir=lambda: self.temp.name),
                          "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="mock-run")),
                          "subprocess": SimpleNamespace(Popen=Mock(return_value=Mock()), DEVNULL=-3)})

    def append(self, value):
        with self.path.open("ab") as stream: stream.write(value)

    def reader(self): return self.ns["_VerificationLog"](self.path)

    def test_historical_success_is_pending_then_fresh_failure_wins(self):
        self.path.write_bytes(b"verify_twitch_tokens() -> True\n" + OK)
        reader = self.reader()
        self.assertEqual(reader.read_status(), "PENDING")
        self.append(FAIL)
        self.assertEqual(reader.read_status(), "FAIL")
        self.assertEqual(reader.read_status(), "FAIL")

    def test_only_fresh_aggregate_success_restores_ok(self):
        reader = self.reader()
        self.append(FAIL + b"[TwitchVerify] stage=app_token outcome=ok http_status=200\n")
        self.assertEqual(reader.read_status(), "FAIL")
        self.append(OK)
        self.assertEqual(reader.read_status(), "OK")
        self.append(FAIL)
        self.assertEqual(reader.read_status(), "FAIL")

    def test_partial_line_waits_for_newline_without_false_success(self):
        reader = self.reader()
        self.append(OK[:25])
        self.assertEqual(reader.read_status(), "PENDING")
        self.append(OK[25:-1])
        self.assertEqual(reader.read_status(), "PENDING")
        self.append(b"\n")
        self.assertEqual(reader.read_status(), "OK")

    def test_truncation_clears_success_before_new_failure(self):
        reader = self.reader()
        self.append(OK)
        self.assertEqual(reader.read_status(), "OK")
        self.path.write_bytes(b"")
        self.assertEqual(reader.read_status(), "PENDING")
        self.append(FAIL)
        self.assertEqual(reader.read_status(), "FAIL")

    def test_replacement_resets_file_identity_and_reads_fresh_result(self):
        reader = self.reader()
        self.append(OK)
        self.assertEqual(reader.read_status(), "OK")
        replacement = self.root / "replacement.txt"
        replacement.write_bytes(FAIL)
        replacement.replace(self.path)
        self.assertEqual(reader.read_status(), "FAIL")

    def test_truncation_and_regrowth_between_polls_does_not_retain_success(self):
        reader = self.reader()
        self.append(OK)
        self.assertEqual(reader.read_status(), "OK")
        self.path.write_bytes(FAIL + b"ordinary metadata\n" * 10)
        self.assertEqual(reader.read_status(), "FAIL")

    def test_missing_or_unreadable_log_shows_pending(self):
        reader = self.reader()
        self.assertEqual(reader.read_status(), "PENDING")
        with patch.object(Path, "open", side_effect=PermissionError(SECRET)):
            self.assertEqual(reader.read_status(), "PENDING")

    def test_large_backlog_and_incomplete_buffer_are_bounded(self):
        reader = self.reader()
        self.append(b"x" * 100000 + b"\n" + FAIL)
        self.assertEqual(reader.read_status(), "PENDING")
        self.assertLessEqual(len(reader.pending), 2048)
        self.assertEqual(reader.offset, 65536)
        self.assertEqual(reader.read_status(), "FAIL")

    def test_unknown_stage_and_nonexact_outcome_do_not_change_status(self):
        parse = self.ns["_verification_status_after_log_chunk"]
        self.assertEqual(parse(b"[TwitchVerify] stage=unknown outcome=ok\n"), "PENDING")
        self.assertEqual(parse(b"[TwitchVerify] stage=complete outcome=okay\n"), "PENDING")
        self.assertEqual(parse(SECRET.encode() + b"\n", "FAIL"), "FAIL")

    def window(self):
        window = SimpleNamespace(_root=self.root, _service_root=lambda: self.root,
                                 launcher_proc=Mock(poll=Mock(return_value=None)),
                                 ollama_proc=Mock(poll=Mock(return_value=None)),
                                 _ollama_ready=True, _ollama_starting=False, _ollama_error=None,
                                 _shutdown_failure=None, status_label=Mock(),
                                 _twitch_listener_log_offset=0, _twitch_irc_scan_offset=0,
                                 _twitch_irc_connected=False)
        return window

    def test_gui_keeps_irc_ok_separate_from_fresh_verification_failure(self):
        self.path.write_bytes(OK)
        window = self.window()
        self.ns["_spawn_launcher_mode"](window, "twitch")
        self.append(FAIL)
        (self.logs / "twitch_listener_log.txt").write_bytes(b"[IRC] State: CONNECTED\n")
        self.ns["refresh_status"](window)
        text = window.status_label.setText.call_args.args[0]
        self.assertIn("IRC=OK", text)
        self.assertIn("Twitch=FAIL", text)
        self.assertNotIn(SECRET, text)

    def test_new_start_and_restart_reset_baseline_before_child_spawn(self):
        window = self.window()
        self.path.write_bytes(OK)
        self.ns["_spawn_launcher_mode"](window, "twitch")
        self.assertEqual(window._twitch_verification_log.read_status(), "PENDING")
        self.append(FAIL + OK)
        self.assertEqual(window._twitch_verification_log.read_status(), "OK")
        first = window._twitch_verification_log
        self.ns["_spawn_launcher_mode"](window, "twitch")
        self.assertIsNot(window._twitch_verification_log, first)
        self.assertEqual(window._twitch_verification_log.read_status(), "PENDING")
        self.append(FAIL)
        self.assertEqual(window._twitch_verification_log.read_status(), "FAIL")

    def test_immediate_child_records_are_not_skipped(self):
        window = self.window()
        self.ns["subprocess"].Popen.side_effect = lambda *args, **kwargs: self.append(OK) or Mock()
        self.ns["_spawn_launcher_mode"](window, "twitch")
        self.assertEqual(window._twitch_verification_log.read_status(), "OK")

    def test_gui_never_uses_historical_success_without_a_run_baseline(self):
        self.path.write_bytes(OK)
        window = self.window()
        self.ns["refresh_status"](window)
        self.assertIn("Twitch=PENDING", window.status_label.setText.call_args.args[0])

    def test_legacy_results_keep_latest_order_within_the_current_run(self):
        parse = self.ns["_verification_status_after_log_chunk"]
        self.assertEqual(parse(b"verify_twitch_tokens() -> True\nTwitch token verification FAILED\n"), "FAIL")
        self.assertEqual(parse(b"verify_twitch_tokens() -> False\n" + OK), "OK")


if __name__ == "__main__": unittest.main()
