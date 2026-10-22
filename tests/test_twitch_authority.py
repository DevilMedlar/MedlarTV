"""B1: real IRC parser -> listener -> dispatcher -> inert primary mutations."""
from __future__ import annotations

import ast
import copy
import logging
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import yaml
from MedlarTV.command_authority import command_authority, copilot_channels, normalize_login
import test_twitch_command_routing as routing
import test_twitch_listener_reliability as transport

ROOT = Path(__file__).resolve().parents[1]
LISTENER = ROOT / "MedlarTV/tools/twitch_listener.py"


class TwitchAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.data = {"copilots": {"active": ["wing", "peer"]}}
        self.counters = {"counters": {"death": 3}}
        self.save_copilots = Mock(side_effect=self._persist_copilots)
        self.save_counters = Mock(side_effect=self._persist_counters)
        self.update_title = Mock(return_value=True)
        self.update_game = Mock(return_value=True)
        self.channel_info = Mock(return_value={"title": "Existing title", "game_name": "Existing game"})
        self.reset_emotions = Mock()
        self.timeout = Mock()
        modules = {}
        for name, values in {
            "MedlarTV.core.stream_management": {
                "update_stream_title": self.update_title, "update_stream_category": self.update_game,
                "get_channel_info": self.channel_info},
            "MedlarTV.core.emotional_system": {
                "get_emotional_system": lambda: types.SimpleNamespace(reset_to_baseline=self.reset_emotions)},
            "MedlarTV.core.moderation": {"execute_timeout": self.timeout},
        }.items():
            module = types.ModuleType(name)
            module.__dict__.update(values)
            modules[name] = module
        patched = patch.dict(sys.modules, modules)
        patched.start()
        self.addCleanup(patched.stop)

        self.engine = routing._command_engine()
        self.engine.update(
            _COUNTERS_LOCK=threading.RLock(),  # Real handler serialization, inert mutations.
            _load_counters=lambda: copy.deepcopy(self.counters), _save_counters=self.save_counters,
            random=types.SimpleNamespace(randint=lambda *_: 1),
        )
        tree = ast.parse(routing.COMMANDS.read_text(encoding="utf-8"))
        for command, function in {
            "title": "handle_title", "game": "handle_game", "resetemotions": "handle_resetemotions",
            "death": "handle_death", "deathreset": "handle_deathreset", "death#": "handle_deathhash",
            "roulette": "handle_roulette",
        }.items():
            routing._load_definition(routing.COMMANDS, tree, function, self.engine)
            self.engine["COMMAND_REGISTRY"][command]["handler"] = self.engine[function]
        self.dispatch = Mock(wraps=self.engine["execute_command"])
        self.ns = {
            "threading": threading, "time": types.SimpleNamespace(time=lambda: 100.0),
            "log": logging.getLogger("test_twitch_authority"), "BOT_NICK": "TestBot", "CHANNEL": "#pilot",
            "_arrived_users": set(), "_apply_event_rules": Mock(), "is_enabled": lambda _: False,
            "detect_channel_point_redemption": lambda _: None, "detect_bits": lambda _: None,
            "_prepare_msg": lambda text: text, "execute_command": self.dispatch,
            "log_interaction": Mock(),
            "_load_copilots": lambda: copy.deepcopy(self.data), "_save_copilots": self.save_copilots,
        }
        tree = ast.parse(LISTENER.read_text(encoding="utf-8"))
        for name in ("_get_active_copilots", "_add_copilot", "_remove_copilot", "_role_prefix", "TwitchIRC"):
            routing._load_definition(LISTENER, tree, name, self.ns)
        self.client = self.ns["TwitchIRC"]()
        self.client.send_message = Mock()
        self.client._learn_emotes_from_message = Mock()
        self.client._socket_for_current_work = Mock(return_value=object())
        self.client._send_immediate = Mock()

    def _persist_copilots(self, value):
        self.data = copy.deepcopy(value)

    def _persist_counters(self, value):
        self.counters = copy.deepcopy(value)

    def incoming(self, text, *, username="pilot", source="#pilot", tags=None):
        values = {"user-id": "202" if username.lower() == "pilot" else "101",
                  "room-id": "202", "mod": "0", "vip": "0"}
        if tags:
            values.update(tags)
        serialized = ";".join(f"{key}={value}" for key, value in values.items() if value is not None)
        self.client._process_incoming(f"@{serialized} :{username}!user@synthetic PRIVMSG {source} :{text}")

    def reset_observers(self):
        for observer in self.mutations() + [self.channel_info, self.dispatch, self.client.send_message]:
            observer.reset_mock()

    def mutations(self):
        return [self.save_copilots, self.save_counters, self.update_title, self.update_game,
                self.reset_emotions, self.timeout]

    def assert_no_mutation(self):
        for observer in self.mutations():
            observer.assert_not_called()
        self.channel_info.assert_not_called()

    def test_foreign_mod_cannot_escalate_global_copilot_then_change_primary_title(self):
        self.incoming("!addcopilot intruder", username="outsider", source="#wing", tags={"mod": "1"})
        self.incoming("!title stolen", username="intruder")
        self.assert_no_mutation()
        self.assertEqual(["wing", "peer"], self.data["copilots"]["active"])
        self.dispatch.assert_called_once()
        self.assertIn("Only Broadcaster", self.client.send_message.call_args.args[0])

    def test_all_primary_effects_reject_foreign_pilot_mod_copilot_and_viewer(self):
        for username, tags in (("pilot", {}), ("moderator", {"mod": "1"}), ("wing", {}), ("viewer", {})):
            for command in ("!addcopilot newwing", "!removecopilot peer", "!title new", "!game new",
                            "!resetemotions", "!death", "!deathreset", "!roulette"):
                with self.subTest(username=username, command=command):
                    self.reset_observers()
                    self.incoming(command, username=username, source="#foreign", tags=tags)
                    self.assert_no_mutation()
                    self.client.send_message.assert_called_once()
                    self.assertIn("primary-channel", self.client.send_message.call_args.args[0])

    def test_primary_pilot_and_mod_keep_add_remove_permissions(self):
        for username, tags in (("pilot", {}), ("moderator", {"mod": "1"})):
            with self.subTest(username=username):
                self.reset_observers()
                self.incoming("!addcopilot @NeW_WiNg", username=username, tags=tags)
                self.assertIn("new_wing", self.data["copilots"]["active"])
                self.save_copilots.assert_called_once()
                self.incoming("!removecopilot @NEW_WING", username=username, tags=tags)
                self.assertNotIn("new_wing", self.data["copilots"]["active"])
                self.assertEqual(2, self.save_copilots.call_count)

    def test_primary_viewer_vip_and_copilot_cannot_add(self):
        for username, tags in (("viewer", {}), ("vip", {"vip": "1"}), ("wing", {})):
            self.reset_observers()
            self.incoming("!addcopilot newwing", username=username, tags=tags)
            self.assert_no_mutation()

    def test_primary_viewer_and_vip_cannot_remove(self):
        for username, tags in (("viewer", {}), ("vip", {"vip": "1"})):
            self.reset_observers()
            self.incoming("!removecopilot peer", username=username, tags=tags)
            self.assert_no_mutation()

    def test_existing_copilot_self_and_peer_removal_rights_are_preserved(self):
        self.incoming("!removecopilot peer", username="wing")
        self.assertEqual(["wing"], self.data["copilots"]["active"])
        self.incoming("!removecopilot wing", username="wing")
        self.assertEqual([], self.data["copilots"]["active"])
        self.assertEqual(2, self.save_copilots.call_count)

    def test_primary_pilot_and_copilot_can_set_title_and_category(self):
        for username in ("pilot", "wing"):
            self.reset_observers()
            self.incoming("!title New title", username=username)
            self.incoming("!game New game", username=username)
            self.update_title.assert_called_once_with("New title")
            self.update_game.assert_called_once_with("New game")
            self.assertEqual(2, self.dispatch.call_count)

    def test_ordinary_mod_title_category_set_remains_denied(self):
        for command in ("!title New title", "!game New game"):
            self.reset_observers()
            self.incoming(command, username="moderator", tags={"mod": "1"})
            self.assert_no_mutation()
            self.assertIn("Only Broadcaster", self.client.send_message.call_args.args[0])

    def test_readonly_queries_remain_available_in_primary_and_foreign_rooms(self):
        for source in ("#pilot", "#foreign"):
            for username, tags in (("viewer", {}), ("moderator", {"mod": "1"}), ("wing", {})):
                self.reset_observers()
                self.incoming("!title", username=username, source=source, tags=tags)
                self.incoming("!game   ", username=username, source=source, tags=tags)
                self.incoming("!death#", username=username, source=source, tags=tags)
                self.assertEqual(2, self.channel_info.call_count)
                for observer in self.mutations():
                    observer.assert_not_called()

    def test_untagged_readonly_query_survives_but_write_is_denied(self):
        self.client._process_incoming(":pilot!user@synthetic PRIVMSG #pilot :!title")
        self.channel_info.assert_called_once()
        self.client._process_incoming(":pilot!user@synthetic PRIVMSG #pilot :!title no")
        self.update_title.assert_not_called()

    def test_pilot_is_not_downgraded_by_mod_or_vip_tags(self):
        for mod, vip in (("0", "0"), ("1", "0"), ("0", "1"), ("1", "1")):
            self.reset_observers()
            self.incoming("!deathreset", username="PiLoT", source="#PiLoT", tags={"mod": mod, "vip": vip})
            self.save_counters.assert_called_once_with({"counters": {"death": 0}})
            self.assertEqual("pilot", self.dispatch.call_args.args[2])

    def test_nonpilot_role_tag_precedence_is_not_broadened(self):
        cases = (("wing", {"mod": "1"}, "mod", False),
                 ("wing", {"vip": "1"}, "copilot", True),
                 ("wing", {"mod": "1", "vip": "1"}, "copilot", True),
                 ("moderator", {"mod": "1", "vip": "1"}, "vip", False))
        for username, tags, role, permitted in cases:
            self.reset_observers()
            self.incoming("!title New title", username=username, tags=tags)
            self.assertEqual(role, self.dispatch.call_args.args[2])
            self.assertEqual(int(permitted), self.update_title.call_count)

    def test_primary_pilot_login_requires_matching_stable_room_owner_identity(self):
        self.incoming("!title forbidden", tags={"user-id": "999"})
        self.incoming("!addcopilot stranger", tags={"user-id": "999"})
        self.assert_no_mutation()

    def test_global_emotion_and_counter_permissions_are_preserved(self):
        for username, tags in (("pilot", {}), ("moderator", {"mod": "1"})):
            self.reset_observers()
            self.incoming("!resetemotions", username=username, tags=tags)
            self.reset_emotions.assert_called_once()
        for username, tags in (("viewer", {}), ("moderator", {"mod": "1"}), ("wing", {})):
            self.reset_observers()
            self.incoming("!deathreset", username=username, tags=tags)
            self.assert_no_mutation()
            self.incoming("!death", username=username, tags=tags)
            self.save_counters.assert_called_once()

    def test_primary_roulette_timeout_stays_inert_and_foreign_one_cannot_reach_it(self):
        self.incoming("!roulette", username="viewer")
        self.timeout.assert_called_once()
        self.assertEqual(("#pilot", "viewer", 30, "roulette"), self.timeout.call_args.args[1:])
        self.reset_observers()
        self.incoming("!roulette", username="viewer", source="#foreign")
        self.assert_no_mutation()

    def test_missing_or_malformed_server_identity_denies_all_writes(self):
        bad_tags = ({"user-id": None}, {"room-id": None}, {"user-id": ""}, {"user-id": "0"},
                    {"user-id": "101x"}, {"room-id": "-1"}, {"mod": "true"}, {"vip": "2"})
        for tags in bad_tags:
            for command in ("!addcopilot stranger", "!title new", "!death", "!resetemotions"):
                self.reset_observers()
                self.incoming(command, tags=tags)
                self.assert_no_mutation()

    def test_missing_and_malformed_source_never_reaches_primary_mutation(self):
        for source in ("pilot", "#", "##pilot", "#pilot extra", "#pilot\t", "#pílót"):
            self.reset_observers()
            self.incoming("!title new", source=source)
            self.assert_no_mutation()
            self.dispatch.assert_not_called()
        for context in ({}, {"channel": None}, {"channel": "#pilot", "login": "wrong"}):
            self.reset_observers()
            self.client._handle_command("pilot", "!title new", context, {"mod": "1"})
            self.assert_no_mutation()

    def test_invalid_login_targets_never_write_or_join(self):
        for target in ("", "@", "@@wing", "two names", "bad-name", "#room", "café", "x" * 26,
                       "wing\r\nJOIN #other"):
            for command in ("addcopilot", "removecopilot"):
                self.reset_observers()
                self.incoming(f"!{command} {target}")
                self.assert_no_mutation()
        self.client._send_immediate.assert_not_called()

    def test_legacy_mentions_and_case_are_normalized_without_duplicate_grants(self):
        self.data["copilots"]["active"] = ["@WiNg", "peer"]
        self.incoming("!title New title", username="WING")
        self.update_title.assert_called_once()
        self.incoming("!addcopilot @wing")
        self.save_copilots.assert_not_called()
        self.incoming("!removecopilot @WING")
        self.assertEqual(["peer"], self.data["copilots"]["active"])

    def test_removed_copilot_loses_authority_before_membership_ends(self):
        self.client.connected = True  # existing transport is still joined
        self.incoming("!title allowed", username="wing")
        self.update_title.assert_called_once_with("allowed")
        self.incoming("!removecopilot wing")
        self.reset_observers()
        self.incoming("!title denied", username="wing")
        self.incoming("!removecopilot peer", username="wing")
        self.assert_no_mutation()
        self.assertTrue(self.client.connected)
        self.client._send_immediate.assert_not_called()  # no implicit PART/reconnect
        self.assertEqual(["#peer"], copilot_channels("#pilot", self.ns["_get_active_copilots"]()))

    def test_removal_revokes_all_legacy_duplicate_grants(self):
        self.data["copilots"]["active"] = ["wing", "@WiNg", "WING", "peer"]
        self.incoming("!removecopilot @WING")
        self.save_copilots.assert_called_once_with({"copilots": {"active": ["peer"]}})
        self.reset_observers()
        self.incoming("!title denied", username="wing")
        self.assert_no_mutation()

    def test_unknown_command_stays_silent_even_with_foreign_privileged_tags(self):
        self.incoming("!not_registered anything", username="moderator", source="#foreign", tags={"mod": "1"})
        self.assert_no_mutation()
        self.client.send_message.assert_not_called()

    def test_dispatcher_does_not_accept_missing_forged_or_mismatched_authority(self):
        valid = command_authority("pilot", {"channel": "#pilot", "login": "pilot", "user_id": "202", "room_id": "202"},
                                  {"user-id": "202", "room-id": "202"}, "#pilot", [])
        self.assertTrue(valid.permits_primary_write("pilot", "pilot"))
        for username, role, context in (("pilot", "pilot", {}),
                                        ("pilot", "pilot", {"command_authority": {"role": "pilot"}}),
                                        ("other", "pilot", {"command_authority": valid}),
                                        ("pilot", "copilot", {"command_authority": valid})):
            self.reset_observers()
            reply = self.engine["execute_command"]("title", username, role, "new", context)
            self.assertIn("primary-channel", reply)
            self.assert_no_mutation()

    def test_context_and_tag_identity_must_agree(self):
        for tags in ({"user-id": "999", "room-id": "202"}, {"user-id": "202", "room-id": "999"},
                     {"user-id": "202", "room-id": "202", "mod": []}):
            authority = command_authority("pilot", {"channel": "#pilot", "login": "pilot", "user_id": "202", "room_id": "202"},
                                          tags, "#pilot", [])
            self.assertFalse(authority.permits_primary_write("pilot", "pilot"))

    def test_copilot_reads_do_not_create_missing_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "copilots.yaml"
            ns = {"_COPILOTS_FILE": path, "yaml": yaml, "_ensure_copilots_file": Mock()}
            tree = ast.parse(LISTENER.read_text(encoding="utf-8"))
            routing._load_definition(LISTENER, tree, "_load_copilots", ns)
            self.assertEqual({"copilots": {"active": []}}, ns["_load_copilots"]())
            ns["_ensure_copilots_file"].assert_not_called()
            self.assertFalse(path.exists())

    def test_malformed_copilot_list_does_not_grant_or_write(self):
        for active in ("wing", {"wing": True}, 1, None):
            self.data = {"copilots": {"active": active}}
            self.assertEqual([], self.ns["_get_active_copilots"]())
            self.assertFalse(self.ns["_add_copilot"]("newwing"))
            self.assertFalse(self.ns["_remove_copilot"]("wing"))
            self.save_copilots.assert_not_called()

    def test_connection_join_plan_refreshes_on_reconnect_with_only_valid_logins(self):
        def connect(active):
            tls = transport.ScriptedSocket([
                b":tmi.twitch.tv 001 TestBot :Welcome\r\n",
                b":TestBot!testbot@local JOIN #testchannel\r\n",
            ])
            harness = transport.ListenerHarness().client_with_socket(tls)
            namespace = type(harness.client).connect.__globals__
            namespace["_get_active_copilots"] = lambda: active
            harness.client.connect()
            self.addCleanup(harness.client.stop)
            return [line.decode().strip() for line in tls.sent if line.startswith(b"JOIN ")]
        first = connect(["@WiNg", "peer", "WING", "testchannel", "bad\r\nJOIN #evil"])
        self.assertEqual(["JOIN #testchannel", "JOIN #peer", "JOIN #wing"], first)
        second = connect(["peer"])
        self.assertEqual(["JOIN #testchannel", "JOIN #peer"], second)


if __name__ == "__main__":
    unittest.main()
