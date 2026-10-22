"""Offline checks that IRC identity reaches chat handlers without aliasing."""

import ast
import logging
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


LISTENER = Path(__file__).resolve().parents[1] / "MedlarTV" / "tools" / "twitch_listener.py"


def _load_client(namespace):
    """Use the real class without importing credentials or running startup code."""
    tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "TwitchIRC"
    )
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    selected = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(selected, str(LISTENER), "exec"), namespace)
    return namespace["TwitchIRC"]


class TwitchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.arrivals = Mock(side_effect=AssertionError("Unexpected arrival event"))
        namespace = {
            "threading": threading,
            "time": SimpleNamespace(time=lambda: 42.0),
            "log": logging.getLogger("test_twitch_identity"),
            "BOT_NICK": "MedlarTV",
            "_arrived_users": {"devilmedlar", "viewer"},
            "_apply_event_rules": self.arrivals,
            "detect_raid": lambda _raw: None,
            "detect_subscription": lambda _raw: None,
            "detect_channel_point_redemption": lambda _raw: None,
            "detect_bits": lambda _raw: None,
        }
        self.client = _load_client(namespace)()
        self.client._learn_emotes_from_message = Mock()
        self.client._handle_regular_message = Mock()
        self.client._handle_command = Mock()

    def test_tagged_message_retains_source_channel_and_raw_ids(self):
        raw = (
            "@user-id=12345;room-id=67890;id=message-1;display-name=Pilot "
            ":DevilMedlar!devilmedlar@local PRIVMSG #OtherChan :hello"
        )

        tags, username, message, channel = self.client._parse_message(raw)
        self.assertEqual((username, message, channel), ("DevilMedlar", "hello", "#otherchan"))
        self.assertEqual(tags["user-id"], "12345")
        self.assertEqual(tags["room-id"], "67890")

        self.client._process_incoming(raw)

        self.client._handle_regular_message.assert_called_once()
        args = self.client._handle_regular_message.call_args.args
        self.assertEqual(args[0:2], ("DevilMedlar", "hello"))
        self.assertEqual(
            {key: args[2][key] for key in (
                "channel", "login", "user_id", "room_id", "message_id", "message_timestamp"
            )},
            {
                "channel": "#otherchan",
                "login": "devilmedlar",
                "user_id": "12345",
                "room_id": "67890",
                "message_id": "message-1",
                "message_timestamp": 42.0,
            },
        )
        self.assertEqual(args[3], tags)
        self.client._handle_command.assert_not_called()

    def test_untagged_command_keeps_session_login_but_no_durable_ids(self):
        self.client._process_incoming(
            ":Viewer!viewer@local PRIVMSG #MainChan :!help"
        )

        self.client._handle_command.assert_called_once()
        username, message, ctx, tags = self.client._handle_command.call_args.args
        self.assertEqual((username, message), ("Viewer", "!help"))
        self.assertEqual(ctx["channel"], "#mainchan")
        self.assertEqual(ctx["login"], "viewer")
        self.assertEqual((ctx["user_id"], ctx["room_id"], ctx["message_id"]), ("", "", ""))
        self.assertEqual(tags, {})
        self.client._handle_regular_message.assert_not_called()

    def test_distinct_source_channels_and_viewer_ids_do_not_collapse(self):
        for channel, user_id in (("#First", "123"), ("#Second", "456")):
            self.client._process_incoming(
                f"@user-id={user_id};room-id=888 :Viewer!viewer@local "
                f"PRIVMSG {channel} :same words"
            )

        contexts = [call.args[2] for call in self.client._handle_regular_message.call_args_list]
        self.assertEqual([ctx["channel"] for ctx in contexts], ["#first", "#second"])
        self.assertEqual([ctx["user_id"] for ctx in contexts], ["123", "456"])
        self.assertEqual([ctx["login"] for ctx in contexts], ["viewer", "viewer"])

    def test_non_channel_message_is_not_dispatched(self):
        self.client._process_incoming(
            ":Viewer!viewer@local PRIVMSG Viewer :private message"
        )
        self.client._handle_regular_message.assert_not_called()
        self.client._handle_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
