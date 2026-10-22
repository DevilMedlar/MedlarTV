"""Offline outgoing-frame and diagnostic regressions; no app imports or network."""
from __future__ import annotations

import ast
import copy
import importlib.util
import logging
from pathlib import Path
import queue
import random
import re
import sys
import threading
import time
import types
import unittest
from chat_pacing_fixture import install_fake_chat_clock
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
LOGGER = "test_twitch_outgoing"


def load_definitions(path, namespace, names=None, assignments=()):
    source = ROOT / path
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if names is None or node.name in names:
                selected.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in assignments for t in targets):
                selected.append(node)
    # Selected parsed nodes have full locations; compile inherits future annotations.
    compiled = ast.Module(body=selected, type_ignores=[])
    exec(compile(compiled, str(source), "exec"), namespace)
    return namespace


# The fixture later replaces core with an empty synthetic package. Load this
# reviewed pure formatting leaf explicitly so isolated runs remain meaningful.
from MedlarTV.core import text_clusters
from MedlarTV import config_contract, config_state


class RecordingSocket:
    def __init__(self):
        self.sent = []
        self.failure = None
        self.closed = False

    def sendall(self, payload):
        if self.failure is not None:
            raise self.failure
        self.sent.append(payload)

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


class TwitchOutgoingTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "blocked_words": [], "blocked_topics": [],
            "safety": {"max_message_length": 500},
            "response_modes": {
                "all_caps": {"enabled": False},
                "emoji_limit": {"enabled": True, "max_per_message": 3},
            },
        }
        self.filter_ns = load_definitions(
            "MedlarTV/core/content_filter.py", {"DEBUG": False, "re": re, "time": time},
            assignments={"all_caps_state"},
        )
        self.filter_ns["load_filter_config"] = lambda: copy.deepcopy(self.config)
        self.filter_module = types.ModuleType("MedlarTV.core.content_filter")
        self.filter_module.filter_message = self.filter_ns["filter_message"]
        self.filter_module.get_safety_response = self.filter_ns["get_safety_response"]
        self.filter_module.apply_all_caps_mode = self.filter_ns["apply_all_caps_mode"]
        self.filter_module.limit_emojis = self.filter_ns["limit_emojis"]
        emote_ns = load_definitions(
            "MedlarTV/core/emotion_emote_selector.py", {"random": random, "log": logging.getLogger(LOGGER)},
            names={"get_emotion_emote", "add_emotion_emote"},
            assignments={"EMOTION_EMOTE_MAP", "ALLOWED_TWITCH_EMOTES", "DEFAULT_EMOTES"},
        )
        self.emote_module = types.ModuleType("MedlarTV.core.emotion_emote_selector")
        self.emote_module.add_emotion_emote = emote_ns["add_emotion_emote"]
        self.emotion_module = types.ModuleType("MedlarTV.core.emotional_system")
        self.emotion_module.get_current_emotion = lambda: "chill"
        self.event_module = types.ModuleType("MedlarTV.core.twitch_events")
        self.event_module.add_random_emote = Mock(side_effect=lambda body, _avail: body + " CoolCat")
        modules = {}
        for name in ("MedlarTV", "MedlarTV.core"):
            module = types.ModuleType(name)
            module.__path__ = []
            modules[name] = module
        for module in (self.filter_module, self.emote_module, self.emotion_module, self.event_module):
            modules[module.__name__] = module
        self.module_patch = patch.dict("sys.modules", modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        spec = importlib.util.spec_from_file_location(
            "MedlarTV.core.twitch_output", ROOT / "MedlarTV/core/twitch_output.py",
        )
        self.policy_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.policy_module
        spec.loader.exec_module(self.policy_module)
        self.flags = {"content_filter": True}
        self.emotes_enabled = True
        self.ns = {
            "threading": threading, "time": time, "log": logging.getLogger(LOGGER),
            "socket": types.SimpleNamespace(SHUT_RDWR=2),
            "os": types.SimpleNamespace(getenv=lambda key, default="": (
                str(self.emotes_enabled).lower() if key == "ENABLE_EMOTE_RESPONSES" else default
            )),
            "CHANNEL": "#devilmedlar", "BOT_NICK": "MedlarTV",
            "is_enabled": lambda key: self.flags.get(key, False),
        }
        load_definitions("MedlarTV/tools/twitch_listener.py", self.ns, names={"TwitchIRC"})
        self.client_class = self.ns["TwitchIRC"]
        self.client = self.new_client()

    def new_client(self):
        client = self.client_class()
        install_fake_chat_clock(client)
        client.connected = True
        client._generation = 1
        client.sock = RecordingSocket()
        client.user_emotes = ["Kappa"]
        client._load_output_policy = lambda: self.policy_module.TwitchPolicy(
            self.flags.get("content_filter", True), self.flags.get("llm_brain", False),
            "synthetic", self.policy_module.validate_config(copy.deepcopy(self.config)),
        )
        return client

    def assert_frame(self, frame, channel="#devilmedlar"):
        text = frame.decode("utf-8")
        prefix = f"PRIVMSG {channel} :"
        self.assertTrue(text.startswith(prefix))
        self.assertTrue(text.endswith("\r\n"))
        body = text[len(prefix):-2]
        self.assertLessEqual(len(body), 500)
        self.assertLessEqual(len(frame), 512)
        self.assertNotIn("\r", body)
        self.assertNotIn("\n", body)
        self.assertNotIn("\x00", body)
        return body

    def test_long_ascii_reserves_complete_chosen_emote_and_ellipsis(self):
        self.assertTrue(self.client.send_message("x" * 1200))
        body = self.assert_frame(self.client.sock.sent[0])
        self.assertEqual(body, "x" * 479 + "... Kappa")
        self.assertEqual(len(self.client.sock.sent[0]), 512)

    def test_exact_boundary_preserves_text_and_one_extra_character_is_limited(self):
        for size in (481, 482, 483):
            with self.subTest(size=size):
                client = self.new_client()
                self.assertTrue(client.send_message("x" * size))
                body = self.assert_frame(client.sock.sent[0])
                self.assertTrue(body.endswith(" Kappa"))
                if size <= 482:
                    self.assertEqual(body, "x" * size + " Kappa")
                else:
                    self.assertEqual(body, "x" * 479 + "... Kappa")

    def test_multibyte_unicode_and_emoji_stay_valid_with_decoration(self):
        # Exercise the wire budget independently of the corrected emoji quota.
        self.config["response_modes"]["emoji_limit"]["enabled"] = False
        cases = ("\u00e9" * 600, "\u6f22" * 600, "\U0001f600" * 600,
                 "\U0001f469\u200d\U0001f4bb" * 200,
                 "\U0001f1fa\U0001f1f8" * 300, "e\u0301" * 600)
        for text in cases:
            with self.subTest(first_codepoint=ord(text[0])):
                client = self.new_client()
                self.assertTrue(client.send_message(text))
                body = self.assert_frame(client.sock.sent[0])
                self.assertTrue(body.endswith("... Kappa"))
                self.assertNotIn("\ufffd", body)
                self.assertEqual(client.sock.sent[0].decode("utf-8").encode("utf-8"), client.sock.sent[0])
                units = set(text_clusters.clusters(text))
                self.assertTrue(all(unit in units for unit in text_clusters.clusters(body[:-len("... Kappa")])))

    def test_multibyte_decoration_is_reserved_once(self):
        self.emote_module.add_emotion_emote = Mock(side_effect=lambda body, *args: body + " \U0001f600")
        self.assertTrue(self.client.send_message("\u6f22" * 600))
        body = self.assert_frame(self.client.sock.sent[0])
        self.assertTrue(body.endswith("... \U0001f600"))
        self.emote_module.add_emotion_emote.assert_called_once()

    def test_emote_fallback_and_disabled_emotes_obey_final_budget(self):
        self.emotion_module.get_current_emotion = Mock(side_effect=ValueError("synthetic failure"))
        self.assertTrue(self.client.send_message("x" * 1200))
        body = self.assert_frame(self.client.sock.sent[0])
        self.assertTrue(body.endswith("... CoolCat"))
        self.event_module.add_random_emote.assert_called_once()
        self.emotes_enabled = False
        client = self.new_client()
        self.assertTrue(client.send_message("x" * 1200))
        self.assertEqual(self.assert_frame(client.sock.sent[0]), "x" * 485 + "...")

    def test_short_and_copilot_prefixed_content_keep_existing_destination(self):
        for text in ("hello", "Pilot hello", "Co-Pilot @viewer hello", "@viewer hello"):
            with self.subTest(text=text):
                client = self.new_client()
                self.assertTrue(client.send_message(text))
                self.assertEqual(self.assert_frame(client.sock.sent[0]), text + " Kappa")
        client = self.new_client()
        self.assertTrue(client.send_message("Co-Pilot @viewer " + "x" * 1200))
        self.assertTrue(self.assert_frame(client.sock.sent[0]).startswith("Co-Pilot @viewer "))

    def test_filter_block_is_replaced_and_runtime_failure_is_withheld(self):
        self.config["blocked_topics"] = ["synthetic forbidden topic"]
        self.assertTrue(self.client.send_message("synthetic forbidden topic"))
        body = self.assert_frame(self.client.sock.sent[0])
        self.assertNotIn("synthetic forbidden topic", body)
        self.filter_module.limit_emojis = Mock(side_effect=ValueError("private marker"))
        client = self.new_client()
        self.assertFalse(client.send_message("\U0001f600" * 1200))
        self.assertEqual(client.sock.sent, [])

    def test_disabled_filter_and_newlines_invalid_surrogate_remain_safe(self):
        self.flags["content_filter"] = False
        text = "line1\r\nline2\x00\n\ud800 " + "\u6f22" * 1200
        self.assertTrue(self.client.send_message(text))
        body = self.assert_frame(self.client.sock.sent[0])
        self.assertTrue(body.startswith("line1 line2 ? "))
        self.assertTrue(body.endswith(" Kappa"))

    def test_raw_privmsg_and_generation_socket_use_target_specific_budget(self):
        target = "#" + "a" * 25
        self.assertTrue(self.client._send_raw(f"PRIVMSG {target} :" + "\u6f22" * 1000))
        self.assert_frame(self.client.sock.sent[0], target)
        adapter = self.client_class._GenerationSocket(self.client, 1)
        payload = ("PRIVMSG #devilmedlar :" + "x" * 1200 + "\r\n").encode("utf-8")
        self.assertEqual(adapter.send(payload), len(payload))
        self.assert_frame(self.client.sock.sent[1])

    def test_actual_sender_preserves_one_bounded_queued_frame(self):
        self.client._sender_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.client._outbound_queue = queue.Queue()
        self.assertTrue(self.client.send_message("x" * 1200))
        self.assertEqual(self.client.sock.sent, [])
        self.client._outbound_queue.put(None)
        self.client._sender_loop()
        self.assertEqual(len(self.client.sock.sent), 1)
        self.assertTrue(self.assert_frame(self.client.sock.sent[0]).endswith(" Kappa"))
        self.assertEqual(self.client._outbound_queue.unfinished_tasks, 0)

    def test_lengths_and_queue_failure_logs_do_not_retain_payloads(self):
        marker = "PRIVATE-PAYLOAD-oauth:synthetic-secret"
        self.client._sender_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.client._outbound_queue = queue.Queue(maxsize=1)
        with self.assertLogs(LOGGER, level="DEBUG") as capture:
            self.assertTrue(self.client.send_message(marker))
            self.assertFalse(self.client.send_message(marker))
        logs = "\n".join(capture.output)
        self.assertIn("outcome=queued", logs)
        self.assertIn("reason=queue_full", logs)
        self.assertIn("chars=", logs)
        self.assertIn("body_bytes=", logs)
        self.assertIn("frame_bytes=", logs)
        self.assertNotIn(marker, logs)
        self.assertNotIn("synthetic-secret", logs)
        self.assertNotIn("outcome=written", logs)

    def test_sync_and_async_write_failures_log_type_without_exception_payload(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                client = self.new_client()
                sock = client.sock
                sock.failure = OSError("oauth:PRIVATE-FAILURE-PAYLOAD")
                if queued:
                    client._sender_thread = types.SimpleNamespace(is_alive=lambda: True)
                    client._outbound_queue = queue.Queue()
                with self.assertLogs(LOGGER, level="DEBUG") as capture:
                    accepted = client.send_message("private outgoing marker")
                    self.assertEqual(accepted, queued)
                    if queued:
                        client._outbound_queue.put(None)
                        client._sender_loop()
                logs = "\n".join(capture.output)
                self.assertIn("outcome=failed", logs)
                self.assertIn("exception=OSError", logs)
                self.assertNotIn("PRIVATE-FAILURE-PAYLOAD", logs)
                self.assertNotIn("private outgoing marker", logs)
                self.assertNotIn("outcome=written", logs)
                self.assertFalse(client.connected)
                self.assertTrue(sock.closed)

    def test_stale_work_and_queued_item_report_drop_without_write(self):
        self.client._work_local.generation = 0
        with self.assertLogs(LOGGER, level="WARNING") as capture:
            self.assertFalse(self.client.send_message("private stale body"))
        self.assertIn("reason=stale_or_inactive", "\n".join(capture.output))
        self.assertEqual(self.client.sock.sent, [])
        self.client._outbound_queue = queue.Queue()
        done = threading.Event()
        self.client._outbound_queue.put((0, "PRIVMSG #devilmedlar :private queued body", done, False))
        self.client._outbound_queue.put(None)
        with self.assertLogs(LOGGER, level="WARNING") as capture:
            self.client._sender_loop()
        self.assertTrue(done.is_set())
        self.assertNotIn("private queued body", "\n".join(capture.output))
        self.assertEqual(self.client.sock.sent, [])

    def test_rejection_notice_logs_known_reason_without_tags_or_private_text(self):
        raw = ("@msg-id=msg_rejected;target-user-id=PRIVATE-TARGET "
               ":tmi.twitch.tv NOTICE #PRIVATE-CHANNEL :oauth:PRIVATE-NOTICE-BODY")
        self.client._workers_started = True
        self.client._work_queue = queue.Queue(maxsize=1)
        self.client._work_queue.put((1, "occupied"))
        with self.assertLogs(LOGGER, level="WARNING") as capture:
            self.client._handle_raw_message(raw, 1)
        logs = "\n".join(capture.output)
        self.assertIn("msg_id=msg_rejected", logs)
        self.assertIn("reason=moderation_pending", logs)
        for private in ("PRIVATE-TARGET", "PRIVATE-CHANNEL", "PRIVATE-NOTICE-BODY", "oauth:"):
            self.assertNotIn(private, logs)
        self.assertEqual(self.client._work_queue.qsize(), 1)
        self.assertTrue(self.client.connected)
        self.assertEqual(self.client.sock.sent, [])

    def test_duplicate_rate_limit_and_unknown_notice_are_safe_and_never_resent(self):
        for notice_id, expected in (("msg_duplicate", "duplicate"), ("msg_ratelimit", "rate_limit"),
                                    ("msg_slowmode", "slow_mode"), ("msg_bad_characters", "unprocessable_characters")):
            with self.subTest(notice_id=notice_id), self.assertLogs(LOGGER, level="WARNING") as capture:
                self.client._process_raw_message(
                    f"@msg-id={notice_id} :tmi.twitch.tv NOTICE #devilmedlar :PRIVATE-NOTICE"
                )
                self.assertIn(f"reason={expected}", "\n".join(capture.output))
                self.assertNotIn("PRIVATE-NOTICE", "\n".join(capture.output))
        with self.assertLogs(LOGGER, level="DEBUG") as capture:
            self.client._handle_raw_message(
                "@msg-id=msg_PRIVATE-TAG :tmi.twitch.tv NOTICE #devilmedlar :PRIVATE-BODY", 1,
            )
        self.assertIn("reason=unclassified", "\n".join(capture.output))
        self.assertNotIn("PRIVATE", "\n".join(capture.output))
        self.assertEqual(self.client.sock.sent, [])

    def test_chat_content_containing_notice_does_not_fake_rejection(self):
        self.client._process_incoming = Mock()
        line = (":viewer!viewer@synthetic PRIVMSG #devilmedlar "
                ":@msg-id=msg_rejected NOTICE #channel :private text")
        with self.assertNoLogs(LOGGER, level="WARNING"):
            self.client._handle_raw_message(line, 1)
        self.client._process_incoming.assert_called_once_with(line)

    def test_socket_success_logs_written_without_claiming_delivery_and_auth_is_omitted(self):
        with self.assertLogs(LOGGER, level="DEBUG") as capture:
            self.assertTrue(self.client.send_message("private short reply"))
            self.client._send_immediate("PASS oauth:PRIVATE-CREDENTIAL", 1)
            self.client._send_immediate("PONG :server", 1)
        logs = "\n".join(capture.output)
        self.assertIn("outcome=written reason=socket_write", logs)
        self.assertNotIn("delivered", logs)
        self.assertNotIn("private short reply", logs)
        self.assertNotIn("PRIVATE-CREDENTIAL", logs)
        self.assertEqual(self.client.sock.sent[1], b"PASS oauth:PRIVATE-CREDENTIAL\r\n")
        self.assertEqual(self.client.sock.sent[2], b"PONG :server\r\n")

    def test_invalid_target_reports_failure_without_payload_or_disconnection(self):
        done = threading.Event()
        with self.assertLogs(LOGGER, level="WARNING") as capture:
            self.assertFalse(self.client._queue_outbound(
                "PRIVMSG #invalid\r\nPRIVATE-TARGET :PRIVATE-BODY", 1, completion=done,
            ))
        self.assertTrue(done.is_set())
        self.assertTrue(self.client.connected)
        self.assertEqual(self.client.sock.sent, [])
        self.assertNotIn("PRIVATE", "\n".join(capture.output))

    def test_private_command_center_chat_keeps_long_reply_and_desktop_context(self):
        source = ROOT / "MedlarTV/app/medlar_app.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ControlCenter")
        method = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == "_generate_chat_reply")
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        namespace = {}
        exec(compile(module, str(source), "exec"), namespace)
        brain_module = types.ModuleType("MedlarTV.core.llm_brain")
        brain_module.generate_response = Mock(return_value="x" * 1200)
        signal = Mock()
        window = types.SimpleNamespace(chat_response=signal)
        with patch.dict("sys.modules", {brain_module.__name__: brain_module}):
            namespace["_generate_chat_reply"](window, "private question", [], False)
        self.assertEqual(len(signal.emit.call_args.args[0]), 1200)
        self.assertEqual(brain_module.generate_response.call_args.kwargs["context"], "desktop")


if __name__ == "__main__":
    unittest.main()
