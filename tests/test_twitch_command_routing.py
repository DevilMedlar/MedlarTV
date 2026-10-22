"""Offline regression checks for Twitch bang-command routing."""

import ast
import logging
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fixture_bootstrap import isolate_listener_stores


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ROOT / "MedlarTV" / "core" / "command_handlers.py"
LISTENER = ROOT / "MedlarTV" / "tools" / "twitch_listener.py"


def _load_definition(path, tree, name, namespace):
    """Compile a real definition without running its module imports or startup code."""
    node = next(
        item for item in tree.body
        if isinstance(item, (ast.ClassDef, ast.FunctionDef)) and item.name == name
    )
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    selected = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(selected, str(path), "exec"), namespace)


def _command_engine():
    """Load the actual registry and selected handlers with harmless dependencies."""
    tree = ast.parse(COMMANDS.read_text(encoding="utf-8"), filename=str(COMMANDS))
    registry_node = next(
        item for item in tree.body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.target.id == "COMMAND_REGISTRY"
    )
    help_entry = next(
        value for key, value in zip(registry_node.value.keys, registry_node.value.values)
        if ast.literal_eval(key) == "help"
    )
    help_handler = next(
        value.id for key, value in zip(help_entry.keys, help_entry.values)
        if ast.literal_eval(key) == "handler" and isinstance(value, ast.Name)
    )

    namespace = {
        "log": logging.getLogger("test_twitch_command_routing.commands"),
        "log_command": Mock(),
        "os": SimpleNamespace(getenv=lambda key, default=None: (
            "https://example.invalid/commands" if key == "COMMANDS_URL" else default
        )),
        "time": SimpleNamespace(time=lambda: 100.0),
        "TRANSLATE_HELP": "test translation help",
        "PERMISSION_EVERYONE": "everybody",
        "PERMISSION_PILOT": "pilot",
        "PERMISSION_MOD": "mod",
        "PERMISSION_COPILOT": "copilot",
        "PERMISSION_VIP": "vip",
    }
    # Other registered handlers are inert. Their registry metadata remains real.
    for node in ast.walk(registry_node.value):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            namespace.setdefault(node.id, Mock(name=node.id))
    for name in {
        "check_permission", "execute_command", "get_command_help",
        "handle_ping", "handle_commands", help_handler,
    }:
        _load_definition(COMMANDS, tree, name, namespace)
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    selected = ast.fix_missing_locations(ast.Module(
        body=[future, registry_node], type_ignores=[]
    ))
    exec(compile(selected, str(COMMANDS), "exec"), namespace)
    return namespace


class TwitchCommandRoutingTests(unittest.TestCase):
    def setUp(self):
        self.commands = _command_engine()
        self.enabled = {"timers": False, "fuzzy_trigger": True, "llm_brain": True}
        self.should_respond = Mock(return_value=True)
        self.generate_response = Mock(return_value="mock conversation reply")
        self.log_interaction = Mock()
        namespace = {
            "threading": threading,
            "time": SimpleNamespace(time=lambda: 100.0),
            "log": logging.getLogger("test_twitch_command_routing.listener"),
            "is_enabled": lambda key: self.enabled.get(key, True),
            "BOT_NICK": "TestBot",
            "CHANNEL": "#testchannel",
            "_arrived_users": {"viewer"},
            "_apply_event_rules": Mock(side_effect=AssertionError("Unexpected event")),
            "_is_ignored": lambda _username: False,
            "should_lookup_time": Mock(return_value=(False, None)),
            "_get_active_copilots": lambda: [],
            "_role_prefix": lambda _username: "",
            "_prefix_for_llm_reply": lambda _username: "",
            "_username_for_model": lambda username: username,
            "_prepare_msg": lambda message: message,
            "_sanitize_reply_text": lambda reply, _username: reply,
            "should_respond": self.should_respond,
            "search_intelligently": Mock(return_value=""),
            "generate_response": self.generate_response,
            "execute_command": self.commands["execute_command"],
            "log_interaction": self.log_interaction,
            "detect_raid": lambda _raw: None,
            "detect_subscription": lambda _raw: None,
            "detect_channel_point_redemption": lambda _raw: None,
            "detect_bits": lambda _raw: None,
        }
        tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
        _load_definition(LISTENER, tree, "TwitchIRC", namespace)
        self.client = namespace["TwitchIRC"]()
        isolate_listener_stores(self.client)
        self.client.send_message = Mock()
        self.client._learn_emotes_from_message = Mock()

    def incoming(self, message):
        self.client._process_incoming(
            f":viewer!viewer@local PRIVMSG #testchannel :{message}"
        )

    def test_help_uses_registered_command_description(self):
        self.incoming("!help ping")

        self.client.send_message.assert_called_once()
        reply = self.client.send_message.call_args.args[0]
        self.assertIn("!ping", reply)
        self.assertIn("latency", reply.lower())
        self.generate_response.assert_not_called()

    def test_help_without_argument_explains_command_discovery(self):
        self.incoming("!help")

        self.client.send_message.assert_called_once()
        reply = self.client.send_message.call_args.args[0]
        self.assertIn("!help", reply)
        self.assertIn("!commands", reply)
        self.assertIn("!forget", reply)
        self.generate_response.assert_not_called()

    def test_help_describes_viewer_forget_commands(self):
        self.incoming("!help forget")
        self.incoming("!help forgetme")

        replies = [call.args[0] for call in self.client.send_message.call_args_list]
        self.assertIn("!forget <fact>", replies[0])
        self.assertIn("!forgetme", replies[1])
        self.generate_response.assert_not_called()

    def test_registered_commands_and_public_catalog_still_work(self):
        self.incoming("!ping")
        self.incoming("!commands")

        replies = [call.args[0] for call in self.client.send_message.call_args_list]
        self.assertEqual(len(replies), 2)
        self.assertIn("Pong!", replies[0])
        self.assertIn("https://example.invalid/commands", replies[1])
        self.generate_response.assert_not_called()

    def test_unknown_leading_commands_are_silent_with_llm_on_or_off(self):
        for enabled in (True, False):
            for message in ("!doesnotexist", "!medlarunknown", "!medlarunknown hello"):
                with self.subTest(enabled=enabled, message=message):
                    self.enabled["llm_brain"] = enabled
                    self.client.send_message.reset_mock()
                    self.generate_response.reset_mock()
                    self.should_respond.reset_mock()
                    self.log_interaction.reset_mock()

                    self.incoming(message)

                    self.client.send_message.assert_not_called()
                    self.generate_response.assert_not_called()
                    self.should_respond.assert_not_called()
                    self.log_interaction.assert_not_called()

    def test_embedded_exclamation_remains_conversationally_eligible(self):
        for message in ("Hi! how are you?", "Hi !medlarunknown"):
            with self.subTest(message=message):
                self.client.send_message.reset_mock()
                self.generate_response.reset_mock()
                self.should_respond.reset_mock()

                self.incoming(message)

                self.should_respond.assert_called_once_with(message)
                self.generate_response.assert_called_once()
                self.client.send_message.assert_called_once_with(
                    "mock conversation reply"
                )


if __name__ == "__main__":
    unittest.main()
