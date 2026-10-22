"""Raw IRC event fixtures; real pure detectors/listener with an inert transport."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import queue
import runpy
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = runpy.run_path(str(ROOT / "tests/test_twitch_outgoing.py"))


class TwitchEventRoutingTests(unittest.TestCase):
    def setUp(self):
        self.f = FIXTURE["TwitchOutgoingTests"]()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.client = self.f.client
        names = {"parse_usernotice", "detect_raid", "detect_subscription",
                 "detect_channel_point_redemption", "detect_bits", "get_sub_response",
                 "get_channel_point_response"}
        self.events = FIXTURE["load_definitions"]("MedlarTV/core/twitch_events.py", {
            "DEBUG": False, "re": FIXTURE["re"], "datetime": datetime,
            "random": types.SimpleNamespace(choice=lambda choices: choices[0]),
            "ENABLE_RAID_DETECTION": True, "ENABLE_SUB_DETECTION": True,
            "ENABLE_CHANNEL_POINTS": True,
        }, names=names)
        for name in names:
            setattr(self.f.event_module, name, self.events[name])
        for name in ("detect_raid", "detect_subscription", "detect_channel_point_redemption", "detect_bits"):
            self.f.ns[name] = self.events[name]
        self.arrivals = Mock(side_effect=AssertionError("Event must not cause viewer-arrival effects"))
        self.f.ns.update({"_arrived_users": {"viewer"}, "_apply_event_rules": self.arrivals})
        self.client._handle_regular_message = Mock()
        self.client._handle_command = Mock()
        self.client._learn_emotes_from_message = Mock()
        self.send = Mock(wraps=self.client.send_message)
        self.client.send_message = self.send

    def notice(self, kind="raid", *, event_id="event-1", channel="#devilmedlar", trailing=None, extra=""):
        tags = {
            "raid": "msg-param-displayName=Raider;msg-param-viewerCount=15",
            "sub": "display-name=Subscriber;msg-param-cumulative-months=1;msg-param-sub-plan=Prime",
            "resub": "display-name=Subscriber;msg-param-cumulative-months=6;msg-param-sub-plan=1000",
            "subgift": "display-name=Gifter;msg-param-recipient-display-name=Recipient;msg-param-months=1",
            "submysterygift": "display-name=Gifter;msg-param-mass-gift-count=5;msg-param-sender-count=99",
        }.get(kind, "display-name=Viewer")
        identifier = f";id={event_id}" if event_id is not None else ""
        raw = f"@msg-id={kind};{tags}{identifier}{extra} :tmi.twitch.tv USERNOTICE {channel}"
        return raw if trailing is None else raw + " :" + trailing

    def body(self):
        return self.f.assert_frame(self.client.sock.sent[-1])

    def no_chat(self):
        self.client._handle_regular_message.assert_not_called()
        self.client._handle_command.assert_not_called()
        self.arrivals.assert_not_called()
        self.client._learn_emotes_from_message.assert_not_called()

    def test_raid_without_trailing_text_is_reachable_once(self):
        raw = self.notice()
        self.assertEqual(self.client._parse_message(raw)[1:], (None, None, None))
        self.client._process_incoming(raw)
        self.assertEqual(self.body(), "⚡ RAID from Raider with 15 viewers! Kappa")
        self.send.assert_called_once()
        self.no_chat()

    def test_sub_resub_and_gifts_allow_missing_empty_or_present_trailing_text(self):
        for kind in ("sub", "resub", "subgift", "submysterygift"):
            for index, trailing in enumerate((None, "", "!help Medlar USERNOTICE text")):
                with self.subTest(kind=kind, trailing=trailing):
                    before = len(self.client.sock.sent)
                    self.client._process_incoming(self.notice(kind, event_id=f"{kind}-{index}", trailing=trailing))
                    self.assertEqual(len(self.client.sock.sent), before + 1)
                    self.assertIn("Subscriber" if kind in {"sub", "resub"} else "Gifter", self.body())
        self.assertEqual(self.send.call_count, 12)
        self.no_chat()

    def test_community_count_uses_this_batch_not_lifetime_total(self):
        info = self.events["detect_subscription"](self.notice("submysterygift"))
        self.assertEqual(info["count"], 5)
        self.client._process_incoming(self.notice("submysterygift"))
        self.assertIn("5 subs", self.body())
        self.assertNotIn("99", self.body())

    def test_exact_notice_types_and_server_command_prevent_body_spoofing(self):
        for raw in (
            self.notice("subgiftpaidupgrade"), self.notice("raid-extra"),
            self.notice("unraid"), self.notice("giftpaidupgrade"),
            self.notice().replace("USERNOTICE", "PRIVMSG"),
            self.notice("sub").replace(":tmi.twitch.tv", ":viewer!viewer@local"),
            ":viewer!viewer@local PRIVMSG #devilmedlar :msg-id=raid USERNOTICE",
        ):
            self.assertIsNone(self.events["detect_raid"](raw))
            self.assertIsNone(self.events["detect_subscription"](raw))
        self.client._process_incoming(":viewer!viewer@local PRIVMSG #devilmedlar :msg-id=raid USERNOTICE")
        self.client._handle_regular_message.assert_called_once()
        self.send.assert_not_called()

    def test_malformed_notices_do_not_emit_or_become_chat(self):
        for raw in (
            "", "@", "USERNOTICE #devilmedlar", ":tmi.twitch.tv USERNOTICE #devilmedlar",
            self.notice().replace("#devilmedlar", "#"), self.notice().replace("#devilmedlar", "viewer"),
            self.notice().replace("#devilmedlar", "#devilmedlar extra"),
            self.notice() + "\r\nPRIVMSG #devilmedlar :injected",
            self.notice() + "\x00", self.notice(extra=";msg-id=sub"),
            self.notice().replace("msg-param-viewerCount=15", "msg-param-viewerCount=oops"),
            self.notice().replace("msg-param-displayName=Raider;", ""),
            self.notice("resub").replace("cumulative-months=6", "cumulative-months=oops"),
        ):
            with self.subTest(raw=raw):
                self.client._process_incoming(raw)
        self.assertEqual(self.client.sock.sent, [])
        self.no_chat()

    def test_notice_tags_decode_ircv3_escapes_before_response_filtering(self):
        raw = self.notice("subgift").replace("display-name=Gifter", "display-name=Gift\\sFriend")
        raw = raw.replace("recipient-display-name=Recipient", "recipient-display-name=Lucky\\:One")
        self.client._process_incoming(raw)
        self.assertIn("Gift Friend", self.body())
        self.assertIn("Lucky;One", self.body())
        self.no_chat()

    def test_flags_disable_only_their_events_without_chat_fallback(self):
        self.events["ENABLE_RAID_DETECTION"] = False
        self.client._process_incoming(self.notice())
        self.events["ENABLE_SUB_DETECTION"] = False
        self.client._process_incoming(self.notice("sub"))
        self.assertEqual(self.client.sock.sent, [])
        self.no_chat()
        self.events["ENABLE_RAID_DETECTION"] = True
        self.client._process_incoming(self.notice())
        self.assertEqual(len(self.client.sock.sent), 1)

    def test_events_do_not_depend_on_fuzzy_or_llm_brain_toggle(self):
        self.f.flags.update(fuzzy_trigger=False, llm_brain=False)
        self.client._process_incoming(self.notice("resub"))
        self.assertEqual(len(self.client.sock.sent), 1)
        self.no_chat()

    def test_repeated_id_in_channel_emits_one_reply_even_after_reconnect(self):
        self.client._process_incoming(self.notice())
        self.client._process_incoming(self.notice(trailing="different optional text"))
        self.client._generation = 2
        self.client._process_incoming(self.notice())
        self.assertEqual(len(self.client.sock.sent), 1)
        self.send.assert_called_once()

    def test_different_ids_and_no_ids_are_not_accidentally_collapsed(self):
        for identifier in ("first", "second", None, None):
            self.client._process_incoming(self.notice("subgift", event_id=identifier))
        self.assertEqual(len(self.client.sock.sent), 4)

    def test_same_id_in_distinct_sources_preserves_channel_scope_and_primary_destination(self):
        for channel in ("#devilmedlar", "#Copilot"):
            self.client._process_incoming(self.notice(channel=channel))
        self.assertEqual(len(self.client.sock.sent), 2)
        for frame in self.client.sock.sent:
            self.f.assert_frame(frame, "#devilmedlar")
        self.assertEqual(set(self.client._seen_usernotices), {("#devilmedlar", "event-1"), ("#copilot", "event-1")})

    def test_cache_is_bounded_and_does_not_claim_invalid_events(self):
        raw = self.notice(event_id="zero").replace("viewerCount=15", "viewerCount=bad")
        self.client._process_incoming(raw)
        self.client._process_incoming(self.notice(event_id="zero"))
        for index in range(256):
            self.client._process_incoming(self.notice(event_id=f"event-{index}"))
        self.assertEqual(len(self.client._seen_usernotices), 256)
        self.assertNotIn(("#devilmedlar", "zero"), self.client._seen_usernotices)
        self.client._process_incoming(self.notice(event_id="zero"))
        self.assertEqual(len(self.client.sock.sent), 258)

    def test_community_and_recipient_notices_each_have_one_existing_response(self):
        self.client._process_incoming(self.notice("submysterygift", event_id="batch"))
        self.client._process_incoming(self.notice("subgift", event_id="recipient"))
        self.client._process_incoming(self.notice("subgift", event_id="recipient"))
        self.assertEqual(len(self.client.sock.sent), 2)
        self.no_chat()

    def test_ordinary_privmsg_keeps_identity_and_command_routing(self):
        raw = "@user-id=1;room-id=2;id=chat :viewer!viewer@local PRIVMSG #copilot :hello"
        self.client._process_incoming(raw)
        args = self.client._handle_regular_message.call_args.args
        self.assertEqual(args[:2], ("viewer", "hello"))
        self.assertEqual(args[2]["channel"], "#copilot")
        self.client._process_incoming(raw.replace(":hello", ":!help"))
        self.client._handle_command.assert_called_once()
        self.send.assert_not_called()

    def test_event_body_cannot_trigger_membership_or_state_effects(self):
        fetch = self.client._fetch_emote_sets = Mock(side_effect=AssertionError("No event-body API action"))
        self.client._process_raw_message(self.notice(
            "sub", trailing="hello! JOIN #other USERSTATE emote-sets=1 GLOBALUSERSTATE done",
        ))
        self.send.assert_called_once()
        fetch.assert_not_called()
        self.no_chat()

    def test_chat_body_protocol_words_remain_regular_chat(self):
        fetch = self.client._fetch_emote_sets = Mock(side_effect=AssertionError("No chat-body API action"))
        self.client._process_raw_message(
            ":viewer!viewer@local PRIVMSG #devilmedlar :JOIN #other USERSTATE emote-sets=1 GLOBALUSERSTATE done"
        )
        self.client._handle_regular_message.assert_called_once()
        self.send.assert_not_called()
        self.arrivals.assert_not_called()
        fetch.assert_not_called()

    def test_actual_privmsg_bits_still_emit_one_existing_cheer_response(self):
        self.client._process_incoming(
            "@bits=500;display-name=Cheerer :viewer!viewer@local PRIVMSG #devilmedlar :Cheer500"
        )
        self.assertIn("Cheerer cheered 500 bits", self.body())
        self.client._handle_regular_message.assert_not_called()
        self.send.assert_called_once()

    def test_channel_points_are_not_claimed_as_general_usernotice_support(self):
        fabricated = self.notice("reward-redeemed", extra=";msg-param-reward-title=Hydrate;custom-reward-id=reward")
        # The legacy detector accepts a synthetic undocumented shape; dispatcher does not advertise it.
        self.assertIsNotNone(self.events["detect_channel_point_redemption"](fabricated))
        self.client._process_incoming(fabricated)
        self.send.assert_not_called()
        self.no_chat()
        limited_chat = "@custom-reward-id=reward;display-name=Viewer :viewer!viewer@local PRIVMSG #devilmedlar :water"
        self.assertIsNone(self.events["detect_channel_point_redemption"](limited_chat))
        self.client._process_incoming(limited_chat)
        self.client._handle_regular_message.assert_called_once()
        self.send.assert_not_called()

    def test_ping_and_other_control_frames_do_not_enter_event_or_chat_handling(self):
        self.client._handle_raw_message("PING :server", 1)
        self.assertEqual(self.client.sock.sent, [b"PONG :server\r\n"])
        for raw in (":tmi.twitch.tv ROOMSTATE #devilmedlar", ":tmi.twitch.tv USERSTATE #devilmedlar",
                    ":tmi.twitch.tv 001 MedlarTV :Welcome", "@msg-id=raid :tmi.twitch.tv NOTICE #devilmedlar :notice"):
            self.client._handle_raw_message(raw, 1)
        self.send.assert_not_called()
        self.no_chat()

    def test_worker_queue_routes_notice_once_and_drops_stale_work(self):
        self.client._workers_started = True
        self.client._work_queue = queue.Queue()
        self.client._handle_raw_message(self.notice(), 0)
        self.assertTrue(self.client._work_queue.empty())
        self.client._handle_raw_message(self.notice(), 1)
        self.assertEqual(self.client._work_queue.qsize(), 1)
        self.client._work_queue.put(None)
        self.client._worker_loop()
        self.assertEqual(len(self.client.sock.sent), 1)
        self.no_chat()

    def test_event_output_still_passes_final_filter_and_utf8_frame_limits(self):
        self.f.config["blocked_topics"] = ["violet bridge"]
        raw = self.notice().replace("displayName=Raider", "displayName=violet\\sbridge")
        self.client._process_incoming(raw)
        self.assertNotIn("violet", self.body())
        self.client._process_incoming(self.notice(event_id="long").replace("displayName=Raider", "displayName=" + "漢" * 600))
        self.f.assert_frame(self.client.sock.sent[-1])
        self.assertLessEqual(len(self.client.sock.sent[-1]), 512)

    def test_failed_event_validation_does_not_retry_or_duplicate_unchecked_output(self):
        self.client._load_output_policy = Mock(side_effect=ValueError("PRIVATE config"))
        self.client._process_incoming(self.notice())
        self.client._process_incoming(self.notice())
        self.assertEqual(self.client.sock.sent, [])
        self.send.assert_called_once()
        self.assertTrue(self.client.connected)


if __name__ == "__main__":
    unittest.main()
