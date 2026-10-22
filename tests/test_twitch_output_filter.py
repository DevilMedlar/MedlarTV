"""Isolated Twitch output policy/route checks; no app startup or real providers."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, TypedDict, runtime_checkable
import threading

import importlib.util
import logging
from pathlib import Path
import queue
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = runpy.run_path(str(ROOT / "tests/test_twitch_outgoing.py"))


class _BrainModule(types.ModuleType):
    generate_twitch_redirect: Mock


class _SettingsModule(types.ModuleType):
    settings_path: Callable[[], str]


class _Requests(types.SimpleNamespace):
    post: Mock


@runtime_checkable
class _ConfigStore(Protocol):
    ConfigConflictError: type[ValueError]
    def load_moderation(self, path: Path) -> tuple[dict[str, list[str]], str]: ...
    def save_moderation(self, path: Path, words: list[str], topics: list[str], revision: str) -> str: ...


def checked_store(value: object) -> _ConfigStore:
    assert isinstance(value, _ConfigStore), "Expected synthetic config-store interface"
    return value


class _RedirectNamespace(dict[str, object]):
    @property
    def requests(self) -> _Requests:
        value = self["requests"]
        assert isinstance(value, _Requests), "Expected inert requests fixture"
        return value


def call_text(namespace: _RedirectNamespace, name: str, *args: str, **kwargs: str) -> str | None:
    function = namespace[name]
    assert isinstance(function, types.FunctionType), "Expected extracted generation function"
    result: object = function(*args, **kwargs)
    assert result is None or isinstance(result, str), "Expected generated text or no reply"
    return result


def mock_in(namespace: _RedirectNamespace, name: str) -> Mock:
    value = namespace[name]
    assert isinstance(value, Mock), "Expected named inert mock"
    return value


def call_kwargs(mock: Mock) -> Mapping[str, object]:
    captured = mock.call_args
    assert captured is not None, "Expected a captured backend call"
    return captured.kwargs


class _Raid(TypedDict):
    raider: str
    viewer_count: int


def synthetic_raid(_raw: str) -> _Raid:
    return {"raider": "violet bridge", "viewer_count": 2}


def neutral_sentiment(_text: str) -> tuple[float, dict[str, object]]:
    return 0.0, {}


def low_severity(*_args: object) -> int:
    return 1


def desktop_reply(_messages: object) -> str:
    return "violet bridge PRIVATE desktop reply"


_OutboundItem = tuple[int, str, threading.Event | None, bool, bool, object | None, float] | None
_WorkItem = tuple[int, tuple[str, str]] | None


class TwitchOutputFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = FIXTURE["TwitchOutgoingTests"]()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.client = self.f.client
        self.config = self.f.config
        self.policy = self.f.policy_module
        self.brain = _BrainModule("MedlarTV.core.llm_brain")
        self.brain.generate_twitch_redirect = Mock(return_value="Tea break: pick your favorite mug!")
        self.brain_patch = patch.dict(sys.modules, {self.brain.__name__: self.brain})
        self.brain_patch.start()
        self.addCleanup(self.brain_patch.stop)

    def worker(self) -> None:
        self.f.flags["llm_brain"] = True
        self.client._work_local.generation = 1
        self.client._work_local.redirect_allowed = True

    def body(self, frame: bytes | None = None) -> str:
        result: object = self.f.assert_frame(frame or self.client.sock.sent[-1])
        assert isinstance(result, str), "Expected decoded IRC body"
        return result

    def file_policy(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.settings_file = directory / "app_settings.yaml"
        self.config_file = directory / "content_filter.yaml"
        self.settings_file.write_text("content_filter: true\nllm_brain: true\n", encoding="utf-8")
        self.save_config()
        settings = _SettingsModule("MedlarTV.core.settings")
        settings.settings_path = lambda: str(self.settings_file)
        self.settings_patch = patch.dict(sys.modules, {settings.__name__: settings})
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        del self.client._load_output_policy
        return directory

    def save_config(self) -> None:
        self.config_file.write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def test_blocked_reply_gets_one_context_free_generated_redirect(self) -> None:
        self.worker()
        self.config["blocked_topics"] = ["violet bridge"]
        self.assertTrue(self.client.send_message("Original PRIVATE violet bridge"))
        self.assertEqual(self.body(), "Tea break: pick your favorite mug! Kappa")
        self.brain.generate_twitch_redirect.assert_called_once_with()
        self.assertEqual(len(self.client.sock.sent), 1)

    def test_unsafe_redirect_is_replaced_by_validated_neutral_fallback(self) -> None:
        self.worker()
        self.config["blocked_topics"] = ["violet bridge"]
        self.brain.generate_twitch_redirect.return_value = "Instead of violet bridge, let's talk tea."
        self.assertTrue(self.client.send_message("violet bridge"))
        self.assertEqual(self.body(), self.policy.NEUTRAL_FALLBACK + " Kappa")
        self.brain.generate_twitch_redirect.assert_called_once_with()

    def test_unsafe_redirect_and_fallback_suppress_without_recursion(self) -> None:
        self.worker()
        self.config["blocked_topics"] = ["violet bridge", "tiny victory"]
        self.brain.generate_twitch_redirect.return_value = "violet bridge"
        self.assertFalse(self.client.send_message("violet bridge"))
        self.assertEqual(self.client.sock.sent, [])
        self.brain.generate_twitch_redirect.assert_called_once_with()
        self.assertTrue(self.client.connected)

    def test_unavailable_generation_uses_checked_fallback_once(self) -> None:
        self.worker()
        self.config["blocked_topics"] = ["violet bridge"]
        self.brain.generate_twitch_redirect.side_effect = RuntimeError("PRIVATE exception")
        self.assertTrue(self.client.send_message("violet bridge"))
        self.assertEqual(self.body(), self.policy.NEUTRAL_FALLBACK + " Kappa")
        self.brain.generate_twitch_redirect.assert_called_once_with()

    def test_disabled_brain_and_nonworker_do_not_generate(self) -> None:
        self.config["blocked_topics"] = ["violet bridge"]
        for worker, enabled in ((False, True), (True, False)):
            with self.subTest(worker=worker, enabled=enabled):
                self.client._work_local.generation = 1
                self.client._work_local.redirect_allowed = worker
                self.f.flags["llm_brain"] = enabled
                self.assertTrue(self.client.send_message("violet bridge"))
        self.brain.generate_twitch_redirect.assert_not_called()

    def test_filter_runtime_failure_withholds_original_and_diagnostics_are_counts_only(self) -> None:
        self.worker()
        self.f.filter_module.limit_emojis = Mock(side_effect=RuntimeError("PRIVATE exception"))
        with self.assertLogs(FIXTURE["LOGGER"], level="WARNING") as output:
            self.assertFalse(self.client.send_message("PRIVATE unchecked original"))
        self.assertEqual(self.client.sock.sent, [])
        self.brain.generate_twitch_redirect.assert_not_called()
        self.assertNotIn("PRIVATE", "\n".join(output.output))
        self.assertIn("reason=validation_unavailable", "\n".join(output.output))

    def test_validator_exception_fails_closed_even_for_raw_writes(self) -> None:
        policy = types.SimpleNamespace(permits=Mock(side_effect=ValueError("PRIVATE")))
        self.client._load_output_policy = lambda: policy
        self.assertFalse(self.client.send_message("original"))
        self.assertFalse(self.client._send_raw("PRIVMSG #devilmedlar :original"))
        self.assertFalse(self.client._send_immediate("PRIVMSG #devilmedlar :original", 1))
        self.assertEqual(self.client.sock.sent, [])
        self.assertTrue(self.client.connected)

    def test_decorated_emote_is_checked_and_unsafe_fallback_with_emote_is_suppressed(self) -> None:
        self.config["blocked_words"] = ["kappa"]
        self.assertFalse(self.client.send_message("innocent message"))
        self.assertEqual(self.client.sock.sent, [])

    def test_caps_suffix_cannot_escape_final_validation(self) -> None:
        self.config["response_modes"]["all_caps"] = {"enabled": True, "max_messages": 3}
        self.config["blocked_topics"] = ["done yelling now"]
        self.f.filter_ns["all_caps_state"].update(active=True, message_count=2)
        self.assertTrue(self.client.send_message("innocent message"))
        self.assertNotIn("done yelling now", self.body().lower())
        self.assertEqual(self.f.filter_ns["all_caps_state"]["message_count"], 0)

    def test_linebreak_and_repeated_spaces_cannot_hide_blocked_phrase(self) -> None:
        self.config["blocked_topics"] = ["violet bridge"]
        for text in ("violet\nbridge", "violet  bridge", "violet\tbridge"):
            self.assertTrue(self.client.send_message(text))
            self.assertNotIn("violet", self.body())

    def test_transport_truncation_is_also_validated(self) -> None:
        self.f.emotes_enabled = False
        self.config["blocked_words"] = ["planeta"]
        text = "x " * 239 + "planetarium"
        self.assertTrue(self.client.send_message(text))
        self.assertEqual(self.body(), self.policy.NEUTRAL_FALLBACK)
        self.client.sock.sent.clear()
        self.assertFalse(self.client._send_raw("PRIVMSG #devilmedlar :" + text))
        self.assertEqual(self.client.sock.sent, [])

    def test_raw_queue_immediate_and_adapter_have_no_content_bypass(self) -> None:
        self.config["blocked_topics"] = ["violet bridge"]
        self.assertFalse(self.client._send_raw("privmsg #devilmedlar :violet\nbridge"))
        self.assertFalse(self.client._queue_outbound("PRIVMSG #devilmedlar :violet bridge", 1))
        self.assertFalse(self.client._send_immediate("PRIVMSG #devilmedlar :violet bridge", 1))
        adapter = self.f.client_class._GenerationSocket(self.client, 1)
        with self.assertRaises(OSError):
            adapter.send(b"PRIVMSG #devilmedlar :violet bridge\r\n")
        self.assertEqual(self.client.sock.sent, [])
        self.brain.generate_twitch_redirect.assert_not_called()

    def test_control_frames_bypass_unavailable_policy_but_cannot_inject_chat(self) -> None:
        self.client._load_output_policy = Mock(side_effect=RuntimeError("missing policy"))
        for frame in ("PASS oauth:synthetic", "PONG :server", "JOIN #devilmedlar"):
            self.assertTrue(self.client._send_immediate(frame, 1))
        self.client._load_output_policy.assert_not_called()
        for frame in ("PONG :server\r\nPRIVMSG #devilmedlar :unchecked",
                      "@tag PRIVMSG #devilmedlar :unchecked", " PRIVMSG #devilmedlar :unchecked"):
            self.assertFalse(self.client._queue_outbound(frame, 1))
        self.assertEqual(len(self.client.sock.sent), 3)

    def test_operational_exemption_is_closed_and_explicit(self) -> None:
        self.config["blocked_topics"] = ["pokecatch", "pokeshop"]
        self.assertFalse(self.client._send_raw("PRIVMSG #devilmedlar :!pokecatch ultraball"))
        self.client._load_output_policy = Mock(side_effect=RuntimeError("missing"))
        self.assertTrue(self.client._send_operational("!pokecatch ultraball"))
        self.assertTrue(self.client._send_operational("!pokeshop ultraball 1"))
        self.assertFalse(self.client._send_operational("!pokecatch ultraball PRIVATE"))
        self.assertFalse(self.client._queue_outbound(
            "PRIVMSG #other :!pokecatch ultraball", 1, trusted_command=True,
        ))
        self.client._load_output_policy.assert_not_called()
        self.assertEqual(len(self.client.sock.sent), 2)

    def test_trusted_operational_metadata_survives_sender_queue(self) -> None:
        self.client._sender_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.client._outbound_queue = queue.Queue[_OutboundItem]()
        self.client._load_output_policy = Mock(side_effect=RuntimeError("missing"))
        self.assertTrue(self.client._send_operational("!pokecatch ultraball"))
        self.client._outbound_queue.put(None)
        self.client._sender_loop()
        self.assertEqual(self.body(), "!pokecatch ultraball")
        self.client._load_output_policy.assert_not_called()

    def test_missing_malformed_and_wrong_schema_policy_never_send_original(self) -> None:
        self.file_policy()
        for value in (None, "blocked_topics: [", "- list root", "{}",
                      "blocked_words: []\nblocked_topics: [12]",
                      "blocked_words: []\nblocked_topics: ['---']"):
            with self.subTest(value=value):
                if value is None:
                    self.config_file.unlink()
                else:
                    self.config_file.write_text(value, encoding="utf-8")
                self.assertFalse(self.client.send_message("PRIVATE original"))
                self.assertFalse(self.client._send_raw("PRIVMSG #devilmedlar :PRIVATE original"))
        self.assertEqual(self.client.sock.sent, [])

    def test_malformed_feature_yaml_is_not_treated_as_filter_disabled(self) -> None:
        self.file_policy()
        for value in ("[", "- list root", "content_filter: 'false'", "content_filter: 0"):
            self.settings_file.write_text(value, encoding="utf-8")
            self.assertFalse(self.client.send_message("PRIVATE original"))
        self.assertEqual(self.client.sock.sent, [])

    def test_explicit_filter_off_retains_frame_budget_and_ignores_broken_filter_yaml(self) -> None:
        self.file_policy()
        self.settings_file.write_text("content_filter: false\nllm_brain: false", encoding="utf-8")
        self.config_file.write_text("[", encoding="utf-8")
        self.assertTrue(self.client.send_message("\u6f22" * 1200))
        self.assertTrue(self.body().endswith("... Kappa"))
        self.brain.generate_twitch_redirect.assert_not_called()

    def test_real_gui_store_save_reloads_without_restart_and_rejects_stale_revision(self) -> None:
        self.file_policy()
        spec = importlib.util.spec_from_file_location("isolated_gui_store", ROOT / "MedlarTV/app/config_editor_store.py")
        assert spec is not None and spec.loader is not None, "Expected isolated config module spec/loader"
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        store = checked_store(module)
        _old, revision = store.load_moderation(self.config_file)
        before = self.client._load_output_policy().revision
        store.save_moderation(self.config_file, ["orchid"], ["violet bridge"], revision)
        self.assertNotEqual(before, self.client._load_output_policy().revision)
        self.assertTrue(self.client.send_message("violet bridge"))
        self.assertNotIn("violet", self.body())
        with self.assertRaises(store.ConfigConflictError):
            store.save_moderation(self.config_file, [], [], revision)
        self.assertEqual(store.load_moderation(self.config_file)[0]["blocked_topics"], ["violet bridge"])

    def test_queued_chat_is_revalidated_after_edit_and_drop_completes_without_disconnect(self) -> None:
        self.file_policy()
        self.client._sender_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.client._outbound_queue = queue.Queue[_OutboundItem]()
        completion = threading.Event()
        self.assertTrue(self.client._queue_outbound(
            "PRIVMSG #devilmedlar :violet bridge", 1, completion=completion,
        ))
        self.config["blocked_topics"] = ["violet bridge"]
        self.save_config()
        self.client._outbound_queue.put(None)
        self.client._sender_loop()
        self.assertEqual(self.client.sock.sent, [])
        self.assertTrue(completion.is_set())
        self.assertTrue(self.client.connected)

    def test_malformed_policy_after_queue_is_withheld(self) -> None:
        self.file_policy()
        self.client._sender_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.client._outbound_queue = queue.Queue[_OutboundItem]()
        self.assertTrue(self.client.send_message("innocent"))
        self.config_file.write_text("[", encoding="utf-8")
        self.client._outbound_queue.put(None)
        self.client._sender_loop()
        self.assertEqual(self.client.sock.sent, [])
        self.assertTrue(self.client.connected)

    def test_formatting_uses_snapshot_without_legacy_reloads(self) -> None:
        self.f.filter_ns["load_filter_config"] = Mock(side_effect=AssertionError("legacy reread"))
        self.assertTrue(self.client.send_message("hello"))
        self.f.filter_ns["load_filter_config"].assert_not_called()

    def test_advanced_flags_are_honored_without_disabling_transport_limits(self) -> None:
        self.config["blocked_topics"] = ["violet bridge"]
        self.config["advanced"] = {"check_blocked_topics": False, "enforce_message_length": False}
        self.assertTrue(self.client.send_message("violet bridge " + "x" * 1200))
        self.assertIn("violet bridge", self.body())

    def test_worker_timer_task_is_filtered_and_stale_worker_is_cancelled(self) -> None:
        self.worker()
        self.config["blocked_topics"] = ["violet bridge"]
        self.client._work_queue = queue.Queue[_WorkItem]()
        self.client._work_queue.put((0, ("send_message", "stale")))
        self.client._work_queue.put((1, ("send_message", "violet bridge")))
        self.client._work_queue.put(None)
        self.client._worker_loop()
        self.assertEqual(self.body(), "Tea break: pick your favorite mug! Kappa")
        self.assertEqual(len(self.client.sock.sent), 1)
        self.assertFalse(getattr(self.client._work_local, "redirect_allowed", False))

    def test_ping_and_reconnect_cancellation_remain_responsive_during_redirect(self) -> None:
        self.f.flags["llm_brain"] = True
        self.config["blocked_topics"] = ["violet bridge"]
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def generate() -> str:
            entered.set()
            release.wait(1.0)
            finished.set()
            return "Tea break!"

        self.brain.generate_twitch_redirect.side_effect = generate
        self.client._ensure_background_workers()
        self.addCleanup(self.client.stop)
        self.addCleanup(release.set)
        socket = self.client.sock
        self.assertTrue(self.client._enqueue_application_message("violet bridge", 1))
        self.assertTrue(entered.wait(1.0))
        self.client._handle_raw_message("PING :server", 1)
        self.assertEqual(socket.sent, [b"PONG :server\r\n"])
        self.assertTrue(self.client._disconnect_generation(1, "synthetic_reconnect"))
        release.set()
        self.assertTrue(finished.wait(1.0))
        self.client.stop()
        self.assertEqual(socket.sent, [b"PONG :server\r\n"])
        self.brain.generate_twitch_redirect.assert_called_once_with()

    def test_online_and_actual_shutdown_notice_are_withheld_when_blocked(self) -> None:
        self.config["blocked_topics"] = ["medlartv online", "medlartv offline"]
        self.assertFalse(self.client._send_raw("PRIVMSG #devilmedlar :MedlarTV online"))
        socket = self.client.sock
        self.client.stop()
        self.assertEqual(socket.sent, [])
        self.brain.generate_twitch_redirect.assert_not_called()

    def test_command_timer_event_and_viewer_input_keep_output_only_filtering(self) -> None:
        fixture = runpy.run_path(str(ROOT / "tests/test_twitch_feature_toggles.py"))["TwitchFeatureToggleTests"]()
        fixture.setUp()
        fixture.client.send_message = self.client.send_message
        self.config["blocked_topics"] = ["violet bridge"]
        fixture.execute_command.return_value = "violet bridge"
        fixture.client._handle_command("viewer", "!search violet bridge", {}, {})
        self.assertNotIn("violet", self.body())
        fixture.client._timers = [{"id": "test", "type": "chats", "enabled": True,
                                   "interval_chats": 1, "message": "violet bridge"}]
        fixture.enabled["fuzzy_trigger"] = False
        fixture.regular_message("violet bridge")
        self.assertNotIn("violet", self.body())
        # Viewer text still reaches generation; no incoming moderation is activated.
        fixture.enabled["fuzzy_trigger"] = True
        fixture.generate_response.return_value = "violet bridge"
        fixture.regular_message("violet bridge")
        self.assertIn("violet bridge", fixture.generate_response.call_args.args)
        self.assertNotIn("violet", self.body())
        fixture.namespace["detect_raid"] = synthetic_raid
        parser_ns = FIXTURE["load_definitions"](
            "MedlarTV/core/twitch_events.py", {"re": FIXTURE["re"]}, names={"parse_usernotice"},
        )
        self.f.event_module.parse_usernotice = parser_ns["parse_usernotice"]
        self.f.event_module.get_sub_response = Mock()
        fixture.client._process_raw_message(
            "@msg-id=raid;msg-param-displayName=violet\\sbridge;msg-param-viewerCount=2 "
            ":tmi.twitch.tv USERNOTICE #testchannel"
        )
        self.assertNotIn("violet", self.body())


class TwitchRedirectGenerationTests(unittest.TestCase):
    def namespace(self) -> _RedirectNamespace:
        ns = _RedirectNamespace({
            "DEBUG": False, "log": logging.getLogger("redirect_test"),
            "OLLAMA_HOST": "http://synthetic.invalid", "OLLAMA_MODEL": "synthetic",
            "_load_personality": lambda: {"name": "Medlar", "tone": "witty", "hidden": "PRIVATE topic"},
            "get_current_emotion": lambda: "chill", "get_emotion_state": lambda: {"happiness": 0.4, "history": "PRIVATE topic"},
            "compute_mood": Mock(return_value={"label": "Supportive"}),
            "get_mood_label": Mock(return_value="Supportive"),
            "_conversation_history": [{"role": "user", "content": "PRIVATE topic"}],
            "requests": _Requests(post=Mock(return_value=types.SimpleNamespace(
                status_code=200, json=lambda: {"message": {"content": "Tea time!"}},
            ))),
        })
        FIXTURE["load_definitions"]("MedlarTV/core/llm_brain.py", ns,
            names={"generate_twitch_redirect", "_build_system_prompt"})
        return ns

    def test_redirect_model_payload_contains_only_persona_mood_and_fresh_prompt(self) -> None:
        ns = self.namespace()
        self.assertEqual(call_text(ns, "generate_twitch_redirect"), "Tea time!")
        ns.requests.post.assert_called_once()
        kwargs = call_kwargs(ns.requests.post)
        self.assertEqual(kwargs["timeout"], (2, 8))
        content = str(kwargs["json"])
        self.assertNotIn("PRIVATE", content)
        self.assertIn("witty", content)
        self.assertIn("Supportive", content)
        self.assertIn("chill", content)
        mock_in(ns, "compute_mood").assert_called_once_with({"happiness": 0.4})
        self.assertEqual(ns["_conversation_history"], [{"role": "user", "content": "PRIVATE topic"}])

    def test_model_failures_have_one_attempt_and_no_backend_error_reply(self) -> None:
        for value in (RuntimeError("PRIVATE"), types.SimpleNamespace(status_code=404),
                      types.SimpleNamespace(status_code=200, json=lambda: {"message": {"content": 12}})):
            ns = self.namespace()
            if isinstance(value, Exception):
                ns.requests.post.side_effect = value
            else:
                ns.requests.post.return_value = value
            self.assertIsNone(call_text(ns, "generate_twitch_redirect"))
            ns.requests.post.assert_called_once()

    def test_private_generation_does_not_use_twitch_policy_or_redirect(self) -> None:
        ns = self.namespace()
        ns.update({
            "analyze_sentiment_advanced": neutral_sentiment,
            "_estimate_severity": low_severity,
            "get_emotional_system": lambda: types.SimpleNamespace(process_message=Mock()),
            "check_ollama_health": lambda: True,
            "_build_ollama_messages": Mock(return_value=[]),
            "_call_ollama_chat": desktop_reply,
            "_append_history": Mock(),
        })
        FIXTURE["load_definitions"]("MedlarTV/core/llm_brain.py", ns, names={"generate_response"})
        self.assertEqual(call_text(ns, "generate_response", "private prompt", "Operator", context="desktop"),
                         "violet bridge PRIVATE desktop reply")
        ns.requests.post.assert_not_called()
        self.assertEqual(call_kwargs(mock_in(ns, "_build_ollama_messages"))["context"], "desktop")


if __name__ == "__main__":
    unittest.main()
