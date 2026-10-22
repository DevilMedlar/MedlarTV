"""Synthetic messages must not appear in production diagnostic streams."""

import ast
import io
import logging
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from MedlarTV.core import (content_filter, emotion_emote_selector,
                           emotional_system, sentiment_advanced, translation,
                           twitch_events)


MARKER = "synthetic-private-42@example.invalid"


class ProductionDiagnosticsPrivacyTests(unittest.TestCase):
    def test_stream_title_and_game_diagnostics_keep_arguments_and_api_bodies_private(self):
        source = (Path(__file__).resolve().parents[1] / "MedlarTV" / "core" /
                  "stream_management.py")
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        names = {"update_stream_title", "update_stream_category", "search_game"}
        nodes = [item for item in tree.body if isinstance(item, ast.FunctionDef)
                 and item.name in names]
        self.assertEqual(names, {item.name for item in nodes})
        future = ast.ImportFrom(module="__future__",
                                names=[ast.alias(name="annotations")], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
        output = io.StringIO()
        logger = logging.Logger("synthetic_stream_privacy", level=logging.DEBUG)
        logger.addHandler(logging.StreamHandler(output))
        requests = SimpleNamespace(patch=Mock(), get=Mock())
        namespace = {
            "DEBUG": True, "log": logger, "requests": requests,
            "TWITCH_API_BASE": "https://api.invalid",
            "get_broadcaster_id": lambda: "synthetic-room",
            "_broadcaster_headers": lambda: {"Authorization": "Bearer synthetic-token"},
            "_ensure_app_token": lambda: "synthetic-token",
            "_app_headers": lambda _token: {"Authorization": "Bearer synthetic-token"},
        }
        exec(compile(module, str(source), "exec"), namespace)
        title = namespace["update_stream_title"]
        category = namespace["update_stream_category"]
        search = namespace["search_game"]

        with redirect_stdout(output):
            requests.patch.return_value = SimpleNamespace(status_code=204, text=MARKER)
            self.assertTrue(title(MARKER))
            self.assertEqual(MARKER, requests.patch.call_args.kwargs["json"]["title"])
            requests.get.return_value = SimpleNamespace(
                status_code=200, text=MARKER,
                json=lambda: {"data": [{"id": "synthetic-game"}]},
            )
            self.assertTrue(category(MARKER))
            self.assertEqual(MARKER, requests.get.call_args.kwargs["params"]["query"])
            self.assertEqual("synthetic-game", requests.patch.call_args.kwargs["json"]["game_id"])

            requests.patch.return_value = SimpleNamespace(status_code=403, text=MARKER)
            self.assertFalse(title(MARKER))
            self.assertFalse(category(MARKER))
            requests.get.return_value = SimpleNamespace(status_code=403, text=MARKER)
            self.assertIsNone(search(MARKER))

            requests.patch.side_effect = RuntimeError(MARKER)
            self.assertFalse(title(MARKER))
            requests.patch.side_effect = None
            requests.get.side_effect = RuntimeError(MARKER)
            self.assertIsNone(search(MARKER))
        self.assertNotIn(MARKER, output.getvalue())

    def test_stream_diagnostics_do_not_reference_api_response_text(self):
        source = (Path(__file__).resolve().parents[1] / "MedlarTV" / "core" /
                  "stream_management.py")
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = ast.unparse(node.func)
            if callee == "print" or callee.startswith("log."):
                diagnostic = ast.unparse(node)
                self.assertNotIn("resp.text", diagnostic)
                self.assertNotIn("str(e)", diagnostic)

    def test_irc_events_and_reply_helpers_log_metadata_only(self):
        output = io.StringIO()
        with patch.object(twitch_events, "DEBUG", True), \
                patch.object(twitch_events, "ENABLE_RAID_DETECTION", True), \
                patch.object(twitch_events, "ENABLE_SUB_DETECTION", True), \
                patch.object(twitch_events, "ENABLE_CHANNEL_POINTS", True), \
                patch.object(twitch_events, "ENABLE_EMOTE_RESPONSES", True), \
                redirect_stdout(output):
            malformed = (f"@msg-id=raid;msg-param-viewerCount=invalid;"
                         f"msg-param-displayName={MARKER} :tmi.twitch.tv USERNOTICE #room")
            self.assertIsNone(twitch_events.detect_raid(malformed))
            subscription = twitch_events.detect_subscription(
                f"@msg-id=sub;display-name={MARKER};msg-param-cumulative-months=2;"
                "msg-param-sub-plan=1000 :tmi.twitch.tv USERNOTICE #room"
            )
            self.assertIn(MARKER, twitch_events.get_sub_response(subscription))
            redemption = twitch_events.detect_channel_point_redemption(
                f"@msg-id=reward-redeemed;display-name=Viewer;"
                f"msg-param-user-input={MARKER};msg-param-reward-title={MARKER};"
                "msg-param-reward-cost=100 :tmi.twitch.tv USERNOTICE #room"
            )
            self.assertIn(MARKER, twitch_events.get_channel_point_response(redemption))
            self.assertIsNotNone(twitch_events.detect_bits(
                f"@bits=5;display-name={MARKER} :viewer PRIVMSG #room :cheer5"
            ))
            self.assertIn(MARKER, twitch_events.add_random_emote(
                MARKER, ["PogChamp"],
            ))
        self.assertNotIn(MARKER, output.getvalue())

    def test_emote_api_diagnostics_never_print_synthetic_token_or_body(self):
        output = io.StringIO()
        response = SimpleNamespace(status_code=403, text=MARKER)
        with patch.object(twitch_events, "DEBUG", True), \
                patch.object(twitch_events, "TWITCH_CLIENT_ID", "synthetic-id"), \
                patch.object(twitch_events, "_global_emotes", None), \
                patch.object(twitch_events, "_channel_emotes", None), \
                patch.object(twitch_events.requests, "get", return_value=response), \
                redirect_stdout(output):
            self.assertEqual(twitch_events.load_global_emotes(MARKER), [])
            self.assertEqual(twitch_events.load_channel_emotes(MARKER, MARKER), [])
        self.assertNotIn(MARKER, output.getvalue())

    def test_sentiment_emotion_and_filter_debug_omit_message(self):
        output = io.StringIO()
        config = {"blocked_words": [], "blocked_topics": [],
                  "safety": {"protected_users": [], "max_message_length": 500}}

        def stop_after_first_diagnostic():
            raise RuntimeError("stop")

        fake_emotional_system = SimpleNamespace(
            get_dominant_emotion=stop_after_first_diagnostic,
        )
        with patch.object(sentiment_advanced, "DEBUG", True), \
                patch.object(emotional_system, "DEBUG", True), \
                patch.object(content_filter, "DEBUG", True), \
                patch.object(content_filter, "load_filter_config", return_value=config), \
                patch.object(content_filter, "limit_emojis", side_effect=lambda text: text), \
                patch.object(content_filter, "apply_all_caps_mode", side_effect=lambda text: text), \
                redirect_stdout(output):
            sentiment_advanced.analyze_sentiment_advanced(MARKER)
            sentiment_advanced.detect_emotional_keywords(MARKER)
            with self.assertRaisesRegex(RuntimeError, "stop"):
                emotional_system.EmotionalSystem.process_message(
                    fake_emotional_system, MARKER, username=MARKER,
                )
            safe, filtered, _ = content_filter.filter_message(MARKER, username=MARKER)
            self.assertTrue(safe)
            self.assertEqual(filtered, MARKER)
        self.assertNotIn(MARKER, output.getvalue())
        self.assertNotIn("synthetic", output.getvalue())

    def test_emote_and_translation_keep_text_out_of_diagnostics(self):
        output = io.StringIO()
        logger = logging.Logger("synthetic_emote_privacy", level=logging.DEBUG)
        logger.addHandler(logging.StreamHandler(output))
        with patch.object(emotion_emote_selector, "log", logger), \
                patch.object(emotion_emote_selector, "get_emotion_emote",
                             return_value="PogChamp"):
            self.assertIn(MARKER, emotion_emote_selector.add_emotion_emote(
                MARKER, "hype", ["PogChamp"],
            ))
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {"translatedText": MARKER + " translated"},
        )
        with patch.object(translation, "DEBUG", True), \
                patch.object(translation, "_base_url", return_value="http://local.invalid"), \
                patch.object(translation.requests, "post", return_value=response), \
                redirect_stdout(output):
            ok, translated = translation.translate_text(
                MARKER, "es", supported={"en", "es"},
            )
            self.assertTrue(ok)
            self.assertIn(MARKER, translated)
        self.assertNotIn(MARKER, output.getvalue())

    def test_llm_dependency_exception_text_is_never_diagnostic_output(self):
        source = (Path(__file__).resolve().parents[1] / "MedlarTV" / "core" /
                  "llm_brain.py")
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == "generate_response")
        future = ast.ImportFrom(module="__future__",
                                names=[ast.alias(name="annotations")], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
        output = io.StringIO()
        logger = logging.Logger("synthetic_brain_privacy", level=logging.DEBUG)
        logger.addHandler(logging.StreamHandler(output))

        def fail(*_args, **_kwargs):
            raise RuntimeError(MARKER)

        namespace = {
            "DEBUG": True, "log": logger,
            "_load_personality": lambda: {},
            "_build_system_prompt": lambda *_args, **_kwargs: "system",
            "analyze_sentiment_advanced": fail,
            "_estimate_severity": lambda *_args: 1,
            "get_emotional_system": lambda: SimpleNamespace(
                process_message=fail, get_emotional_state=fail,
            ),
            "get_emotion_state": fail,
            "compute_mood": fail,
            "get_current_emotion": lambda: "neutral",
            "check_ollama_health": lambda: False,
            "_compose_local_reply": lambda **_kwargs: "Safe.",
            "_append_history": lambda *_args: None,
        }
        exec(compile(module, str(source), "exec"), namespace)
        with redirect_stdout(output):
            reply = namespace["generate_response"](MARKER, "Ada", history=[])
        self.assertEqual(reply, "Safe.")
        self.assertNotIn(MARKER, output.getvalue())


if __name__ == "__main__":
    unittest.main()
