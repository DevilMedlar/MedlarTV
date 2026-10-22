"""Offline checks for Twitch feature switches; no project imports or connections."""

import ast
import logging
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fixture_bootstrap import isolate_listener_stores


LISTENER = Path(__file__).resolve().parents[1] / "MedlarTV" / "tools" / "twitch_listener.py"


def load_client_class(namespace):
    """Compile the real class without executing credential or network imports."""
    tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "TwitchIRC")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(module, str(LISTENER), "exec"), namespace)
    return namespace["TwitchIRC"]


class TwitchFeatureToggleTests(unittest.TestCase):
    def setUp(self):
        self.enabled = {"timers": True, "fuzzy_trigger": True, "llm_brain": True}
        self.should_respond = Mock(return_value=True)
        self.generate_response = Mock(return_value="mock reply")
        self.execute_command = Mock(return_value="mock command reply")
        self.log_interaction = Mock()
        self.clock = SimpleNamespace(now=0, sleeps=0)

        def sleep(_seconds):
            self.clock.sleeps += 1
            self.client._timer_stop.set()

        namespace = {
            "threading": threading,
            "time": SimpleNamespace(time=lambda: self.clock.now, sleep=sleep),
            "log": logging.getLogger("test_twitch_feature_toggles"),
            "is_enabled": lambda key: self.enabled.get(key, True),
            "get_memory_control": lambda: SimpleNamespace(enabled=True, revision="fixture"),
            "BOT_NICK": "TestBot",
            "CHANNEL": "#testchannel",
            "_arrived_users": {"viewer"},
            "_apply_event_rules": Mock(side_effect=AssertionError("Unexpected event")),
            "_is_ignored": lambda _username: False,
            "should_lookup_time": Mock(return_value=(False, None)),
            "get_times_for_location": Mock(return_value="mock local time"),
            "get_default_local_time": Mock(return_value="mock local time"),
            "_role_prefix": lambda _username: "",
            "_prefix_for_llm_reply": lambda _username: "",
            "_username_for_model": lambda username: username,
            "_prepare_msg": lambda message: message,
            "_sanitize_reply_text": lambda reply, _username: reply,
            "_get_active_copilots": lambda: [],
            "should_respond": self.should_respond,
            "search_intelligently": Mock(return_value=""),
            "generate_response": self.generate_response,
            "execute_command": self.execute_command,
            "log_interaction": self.log_interaction,
            "detect_raid": lambda _raw: None,
            "detect_subscription": lambda _raw: None,
            "detect_channel_point_redemption": lambda _raw: None,
            "detect_bits": lambda _raw: None,
        }
        self.namespace = namespace
        self.client = load_client_class(namespace)()
        isolate_listener_stores(self.client)
        self.client.send_message = Mock()

    def regular_message(self, text="ordinary message"):
        self.client._handle_regular_message("viewer", text, {}, {})

    def test_recall_and_factual_questions_generate_without_external_search(self):
        recall = "Medlar, what test word did I tell you?"
        factual = "Medlar, latest news about AI?"
        search_context = "CURRENT WEB INFORMATION:\n[Result 1] AI research update"
        search = Mock(return_value=search_context)
        self.namespace["search_intelligently"] = search

        self.regular_message(recall)
        self.regular_message(factual)

        search.assert_not_called()
        self.assertEqual(
            [call.args for call in self.generate_response.call_args_list],
            [(recall, "viewer"), (factual, "viewer")],
        )
        self.assertEqual(
            self.generate_response.call_args_list[0].kwargs.get("search_context", ""),
            "",
        )
        self.assertEqual(
            self.generate_response.call_args_list[1].kwargs.get("search_context"),
            "",
        )

    def test_disabled_chat_timers_neither_send_nor_accumulate_chat_count(self):
        self.client._timers = [{
            "id": "chat timer", "type": "chats", "enabled": True,
            "interval_chats": 1, "message": "scheduled chat message",
        }]
        self.enabled["timers"] = False
        self.enabled["fuzzy_trigger"] = False

        self.regular_message()
        self.regular_message()

        self.client.send_message.assert_not_called()
        self.assertEqual(self.client._chat_count, 0)
        self.assertEqual(self.client._next_chat_due, {})

        self.enabled["timers"] = True
        self.regular_message()
        self.client.send_message.assert_called_once_with("scheduled chat message")
        self.assertEqual(self.client._chat_count, 1)
        self.assertEqual(self.client._next_chat_due["chat timer"], 2)

    def test_disabling_during_chat_timer_scan_prevents_send(self):
        self.client._timers = [{
            "id": "chat timer", "type": "chats", "enabled": True,
            "interval_chats": 1, "message": "scheduled chat message",
        }]
        self.enabled["fuzzy_trigger"] = False
        checks = 0

        def timer_switch(key):
            nonlocal checks
            if key == "timers":
                checks += 1
                return checks == 1
            return self.enabled.get(key, True)

        self.namespace["is_enabled"] = timer_switch
        self.regular_message()
        self.client.send_message.assert_not_called()
        self.assertEqual(self.client._next_chat_due, {})

    def test_existing_time_timer_loop_observes_disable_and_reenable(self):
        self.client._timers = [{
            "id": "time timer", "type": "time", "enabled": True,
            "interval_minutes": 1, "message": "scheduled time message",
        }]
        self.client._next_time_due["time timer"] = 10

        def pause_then_disable(_seconds):
            self.clock.now = 10
            self.enabled["timers"] = False
            self.namespace["time"].sleep = lambda _seconds: self.client._timer_stop.set()

        self.namespace["time"].sleep = pause_then_disable
        self.client._timer_loop()
        self.client.send_message.assert_not_called()
        self.assertEqual(self.client._next_time_due["time timer"], 10)

        self.enabled["timers"] = True
        self.client._timer_stop.clear()
        self.client._timer_loop()
        self.client.send_message.assert_called_once_with("scheduled time message")
        self.assertEqual(self.client._next_time_due["time timer"], 70)

    def test_disabled_fuzzy_skips_detection_and_llm(self):
        self.enabled["fuzzy_trigger"] = False
        self.regular_message("medlar")
        self.should_respond.assert_not_called()
        self.generate_response.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_enabled_fuzzy_still_generates_reply(self):
        self.regular_message("medlar")
        self.should_respond.assert_called_once_with("medlar")
        self.generate_response.assert_called_once()
        self.client.send_message.assert_called_once_with("mock reply")

    def test_disabled_fuzzy_preserves_explicit_command_dispatch(self):
        self.enabled["fuzzy_trigger"] = False
        self.client._learn_emotes_from_message = Mock()
        raw = ":viewer!viewer@local PRIVMSG #testchannel :!help"

        self.client._process_incoming(raw)

        self.execute_command.assert_called_once()
        self.client.send_message.assert_called_once_with("mock command reply")
        self.should_respond.assert_not_called()
        self.generate_response.assert_not_called()

    def test_disabled_fuzzy_preserves_unrelated_time_lookup(self):
        self.enabled["fuzzy_trigger"] = False
        self.namespace["should_lookup_time"] = Mock(return_value=(True, "Paris"))

        self.regular_message("what time is it in Paris")

        self.client.send_message.assert_called_once_with("mock local time")
        self.should_respond.assert_not_called()


if __name__ == "__main__":
    unittest.main()
