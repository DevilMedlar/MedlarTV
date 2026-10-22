"""Offline checks for Twitch memory dispatch with synthetic IRC messages only."""

from __future__ import annotations

import ast
import logging
import tempfile
import threading
import unittest
from chat_pacing_fixture import install_fake_chat_clock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from MedlarTV.core.conversation_context import (
    ConversationContextStore,
    memory_query_topics,
)
from MedlarTV.core.health_conditions import HealthConditionStore as ViewerProfileStore
from MedlarTV.core.memory_control import MemoryControl
from MedlarTV.core.memory_receipts import WrittenReply


LISTENER = Path(__file__).resolve().parents[1] / "MedlarTV" / "tools" / "twitch_listener.py"


def load_client_class(namespace):
    """Compile the listener class without executing its imports or startup code."""
    tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "TwitchIRC")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(module, str(LISTENER), "exec"), namespace)
    return namespace["TwitchIRC"]


def load_address_helpers(namespace):
    tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
    names = {"_role_prefix", "_is_pilot", "_prefix_for_llm_reply", "_sanitize_reply_text"}
    nodes = [item for item in tree.body
             if isinstance(item, ast.FunctionDef) and item.name in names]
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(LISTENER), "exec"), namespace)
    return namespace


class TwitchMemoryWiringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.profile_path = Path(self.temporary.name) / "profiles.sqlite3"
        self.profiles = ViewerProfileStore(self.profile_path)
        self.conversation = ConversationContextStore()
        self.generate_response = Mock(return_value="mock model reply")
        self.search_intelligently = Mock(return_value="")
        self.execute_command = Mock(return_value="Pong")
        self.log_interaction = Mock()
        self.enabled = {"timers": True, "fuzzy_trigger": True, "llm_brain": True}
        self.should_respond = Mock(return_value=True)
        self.namespace = {
            "threading": threading,
            "time": SimpleNamespace(time=lambda: 100.0),
            "log": logging.getLogger("test_twitch_memory_wiring"),
            "BOT_NICK": "MedlarTV",
            "CHANNEL": "#testchannel",
            "_arrived_users": set(),
            "_apply_event_rules": Mock(),
            "_is_ignored": lambda _username: False,
            "is_enabled": lambda key: self.enabled.get(key, True),
            "get_memory_control": lambda: MemoryControl(self.enabled.get("remember_users", True), "fixture"),
            "should_lookup_time": Mock(return_value=(False, None)),
            "get_times_for_location": Mock(return_value="mock local time"),
            "get_default_local_time": Mock(return_value="mock local time"),
            "_role_prefix": lambda _username: "",
            "_prefix_for_llm_reply": lambda _username: "",
            "_username_for_model": lambda username: "Pilot" if username.lower() == "devilmedlar" else username,
            "_prepare_msg": lambda message: message,
            "_sanitize_reply_text": lambda reply, _username: reply,
            "_get_active_copilots": lambda: [],
            "should_respond": self.should_respond,
            "search_intelligently": self.search_intelligently,
            "generate_response": self.generate_response,
            "execute_command": self.execute_command,
            "log_interaction": self.log_interaction,
            "detect_raid": lambda _raw: None,
            "detect_subscription": lambda _raw: None,
            "detect_channel_point_redemption": lambda _raw: None,
            "detect_bits": lambda _raw: None,
        }
        self.client_class = load_client_class(self.namespace)
        self.client = self._new_client()
        self.message_number = 0

    def tearDown(self):
        self.profiles.close()
        self.temporary.cleanup()

    def _new_client(self):
        client = self.client_class()
        install_fake_chat_clock(client)
        client._viewer_profiles = self.profiles
        client._scoped_conversation = self.conversation
        client._learn_emotes_from_message = Mock()
        client.connected = True
        client._generation = 1
        def written(message):
            receipt = getattr(client._work_local, 'memory_receipt', None)
            if receipt is not None:
                client._record_written(WrittenReply(receipt, client._generation, message))
            return True
        # This routing fixture reports an explicit synthetic successful write.
        # The receipt suite exercises the real policy/queue/socket-write path.
        client.send_message = Mock(side_effect=written)
        return client

    def test_address_helpers_keep_handle_and_existing_role_prefixes(self):
        helpers = load_address_helpers({
            "CHANNEL": "#testchannel", "_get_active_copilots": lambda: ["Bex"],
        })
        self.assertEqual("@Ada ", helpers["_prefix_for_llm_reply"]("Ada"))
        self.assertEqual("Co-Pilot @Bex ", helpers["_prefix_for_llm_reply"]("Bex"))
        self.assertEqual("", helpers["_prefix_for_llm_reply"]("testchannel"))
        self.assertEqual("Ready", helpers["_sanitize_reply_text"]("Hi Ada, Ready", "Ada"))

    def test_generated_real_name_address_keeps_reply_body_and_ordinary_month(self):
        helpers = load_address_helpers(self.namespace)
        self.generate_response.return_value = "Hello Elara!"
        self.chat("Ada", "My real name is Elara", user_id="100")
        self.client.send_message.assert_called_with("@Ada Hello!")

        self.generate_response.return_value = "Elara!"
        self.chat("Ada", "Medlar, ready?", user_id="100")
        self.client.send_message.assert_called_with("@Ada Noted.")

        self.generate_response.return_value = "Thanks for joining Elara, let's play!"
        self.chat("Ada", "Medlar, is the dragon ready?", user_id="100")
        self.client.send_message.assert_called_with(
            "@Ada Thanks for joining @Ada, let's play!",
        )

        guard = self.client_class._use_twitch_handle
        self.assertEqual("Meet us in May.", guard("Meet us in May.", "Ada", "May"))
        self.assertEqual("Meet us May 4.", guard("Meet us May 4.", "Ada", "May"))
        self.assertEqual("Meet us in May, then we can play.", guard(
            "Meet us in May, then we can play.", "Ada", "May",
        ))
        self.assertEqual("May the force be with you.", guard(
            "May the force be with you.", "Ada", "May",
        ))
        self.assertEqual("We may win this round.", guard(
            "We may win this round.", "Ada", "May",
        ))
        self.assertEqual("Please, May I join?", guard(
            "Please, May I join?", "Ada", "May",
        ))
        self.assertEqual("Please, may I join?", guard(
            "Please, may I join?", "Ada", "May",
        ))
        self.assertEqual("Thanks, may I join?", guard(
            "Thanks, may I join?", "Ada", "May",
        ))
        self.assertEqual("Hello, May I ask a question?", guard(
            "Hello, May I ask a question?", "Ada", "May",
        ))
        self.assertEqual("The festival is in May; then we play.", guard(
            "The festival is in May; then we play.", "Ada", "May",
        ))
        for calendar in (
            "May, 2026 is when the event starts.",
            "May: 2026 is when the event starts.",
            "May; 2026 is when the event starts.",
            "May, 4th, 2026 is when the event starts.",
            "May, '26 is when the event starts.",
            "May, the month we play, is near.",
        ):
            with self.subTest(calendar=calendar):
                self.assertEqual(calendar, guard(calendar, "Ada", "May"))
        self.assertEqual("April, 2026 is when the event starts.", guard(
            "April, 2026 is when the event starts.", "Ada", "April",
        ))
        self.assertEqual("Great play, @Ada; you nailed it", guard(
            "Great play, may; you nailed it", "Ada", "May",
        ))
        self.assertEqual("I heard @Ada is ready.", guard(
            "I heard @may is ready.", "Ada", "May",
        ))
        self.assertEqual("Thanks for joining @Ada, let's play!", guard(
            "Thanks for joining Elara, let's play!", "Ada", "Elara Voss",
        ))
        self.assertEqual("Keep going @Ada, you got this!", guard(
            "Keep going Elara, you got this!", "Ada", "Elara Voss",
        ))
        self.assertEqual("@Ada will join the raid.", guard(
            "Elara will join the raid.", "Ada", "Elara Voss",
        ))
        self.assertEqual("Nice work!", guard("Nice work, May!", "Ada", "May"))
        self.assertEqual("the gate is open!", guard(
            "May, the gate is open!", "Ada", "May",
        ))
        self.assertEqual("Thanks!", guard("Thanks Elara!", "Ada", "Elara"))
        self.assertEqual("Hey how are you?", guard(
            "Hey Elara how are you?", "Ada", "Elara",
        ))
        self.assertEqual("Great to see you", guard(
            "Great to see you Elara", "Ada", "Elara",
        ))
        self.assertEqual("Great job!", guard("Great job Elara!", "Ada", "Elara"))
        self.assertEqual("That was brilliant!", guard(
            "That was brilliant Elara!", "Ada", "Elara",
        ))

    def test_saved_name_may_does_not_rewrite_ordinary_modal_reply(self):
        self.generate_response.return_value = "May the force be with you."
        self.chat("Ada", "My real name is May", user_id="100")
        self.assertEqual("May", self.profiles.get_facts(
            "room-a", "100",
        )["real_name"].value)
        self.client.send_message.assert_called_with(
            "May the force be with you.",
        )
        self.generate_response.return_value = "Great play, may; you nailed it"
        self.chat("Ada", "Medlar, how was that play?", user_id="100")
        self.client.send_message.assert_called_with(
            "Great play, @Ada; you nailed it",
        )
        self.generate_response.return_value = "May, 2026 is when the event starts."
        self.chat("Ada", "Medlar, when does the event start?", user_id="100")
        self.client.send_message.assert_called_with(
            "May, 2026 is when the event starts.",
        )

    def chat(self, username, message, *, user_id="100", room_id="room-a", channel="#testchannel"):
        self.message_number += 1
        if user_id is None:
            raw = f":{username}!{username.lower()}@local PRIVMSG {channel} :{message}"
        else:
            tags = f"user-id={user_id};room-id={room_id or ''};id=synthetic-{self.message_number}"
            raw = f"@{tags} :{username}!{username.lower()}@local PRIVMSG {channel} :{message}"
        self.client._process_incoming(raw)

    def test_tagged_identity_persists_and_direct_answer_survives_client_restart(self):
        self.chat("DevilMedlar", "My favorite color is cobalt", user_id="100")
        self.assertEqual("cobalt", self.profiles.get_facts("room-a", "100")["favorite_color"].value)
        self.assertEqual("Pilot", self.generate_response.call_args.args[1])
        self.assertTrue(self.profile_path.exists())

        self.profiles.close()
        self.profiles = ViewerProfileStore(self.profile_path)
        self.conversation = ConversationContextStore()
        self.client = self._new_client()
        self.generate_response.reset_mock()
        self.chat("DevilMedlar", "Medlar, what is my favorite color?", user_id="100")

        self.generate_response.assert_not_called()
        self.client.send_message.assert_called_once_with("You told me your favorite color is cobalt.")
        self.assertEqual(2, len(self.conversation.history_for("#testchannel", "100")))

    def test_real_name_is_a_private_fact_and_generated_address_uses_twitch_handle(self):
        self.namespace["_prefix_for_llm_reply"] = lambda username: f"@{username} "
        self.generate_response.return_value = "Hi Elara, welcome back."
        self.chat("Ada", "My real name is Elara Voss", user_id="100")
        self.assertEqual("Elara Voss", self.profiles.get_facts("room-a", "100")["real_name"].value)
        first_reply = self.client.send_message.call_args.args[0]
        self.assertTrue(first_reply.startswith("@Ada "))
        self.assertNotIn("Elara", first_reply)

        self.generate_response.return_value = "Elara Voss, the boss is ready."
        self.chat("Ada", "Medlar, how is the dragon boss?", user_id="100")
        later_reply = self.client.send_message.call_args.args[0]
        self.assertTrue(later_reply.startswith("@Ada "))
        self.assertNotIn("Elara", later_reply)
        self.assertNotIn("Elara", self.generate_response.call_args.kwargs["memory_context"])

        self.generate_response.reset_mock()
        self.chat("Ada", "Medlar, what is my real name?", user_id="100")
        self.generate_response.assert_not_called()
        direct = self.client.send_message.call_args.args[0]
        self.assertTrue(direct.startswith("@Ada You told me your real name is Elara Voss."))
        self.chat("Ada", "!forget name", user_id="100")
        self.assertNotIn("real_name", self.profiles.get_facts("room-a", "100"))

    def test_compound_health_report_is_saved_separately_and_not_in_routine_context(self):
        self.namespace["_prefix_for_llm_reply"] = lambda username: f"@{username} "
        self.chat("Ada", "My favorite food is pizza. I have diabetes", user_id="100")
        facts = self.profiles.get_facts("room-a", "100")
        self.assertEqual(facts["favorite_food"].value, "pizza")
        self.assertEqual(facts["health_condition:diabetes"].value, "diabetes")
        self.generate_response.reset_mock()
        self.chat("Ada", "Medlar, how is the dragon boss?", user_id="100")
        call = self.generate_response.call_args
        self.assertNotIn("diabetes", call.kwargs["memory_context"])
        self.assertNotIn("diabetes", repr(call.kwargs["history"]))
        self.chat("Bex", "Medlar, how is the dragon boss?", user_id="200")
        self.assertNotIn("diabetes", self.generate_response.call_args.kwargs["memory_context"])
        self.generate_response.reset_mock()
        self.chat("Ada", "Medlar, what health condition did I tell you about?", user_id="100")
        self.generate_response.assert_not_called()
        direct = self.client.send_message.call_args.args[0]
        self.assertTrue(direct.startswith("@Ada "))
        self.assertIn("previously reported diabetes", direct)
        self.chat("Ada", "!forget health condition", user_id="100")
        self.assertNotIn("health_condition:diabetes", self.profiles.get_facts("room-a", "100"))
        self.assertEqual(self.profiles.get_facts("room-a", "100")["favorite_food"].value, "pizza")

    def test_generic_health_condition_stays_out_of_profile_and_session(self):
        self.chat("Ada", "I have a heart condition", user_id="100")
        self.chat("Ada", "My favorite food is pizza due to a heart condition",
                  user_id="100")
        self.assertEqual({}, self.profiles.get_facts("room-a", "100"))
        self.assertFalse(self.profile_path.exists())
        self.assertEqual([], self.conversation.history_for("#testchannel", "100"))
        self.generate_response.reset_mock()
        self.chat("Ada", "Medlar, how is the dragon boss?", user_id="100")
        call = self.generate_response.call_args
        self.assertNotIn("heart condition", call.kwargs["memory_context"])
        self.assertNotIn("heart condition", repr(call.kwargs["history"]))
        self.assertNotIn("pizza", call.kwargs["memory_context"])

    def test_untagged_login_has_session_evidence_but_no_saved_profile(self):
        self.chat("Ada", "My favorite food is saffron rice", user_id=None)
        self.assertFalse(self.profile_path.exists())
        self.assertEqual({}, self.profiles.get_facts("#testchannel", "ada"))
        self.generate_response.reset_mock()

        self.chat("Ada", "Medlar, what was my favorite food?", user_id=None)
        self.generate_response.assert_called_once()
        call = self.generate_response.call_args
        self.assertIn("saffron rice", call.kwargs["memory_context"])
        self.assertEqual(2, len(call.kwargs["history"]))
        self.assertFalse(self.profile_path.exists())

    def test_missing_room_id_keeps_session_but_does_not_save_facts(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100", room_id="room-a")
        self.chat("Ada", "My favorite food is saffron rice", user_id="100", room_id=None)
        self.assertEqual("cobalt", self.profiles.get_facts("room-a", "100")["favorite_color"].value)
        self.assertNotIn("favorite_food", self.profiles.get_facts("room-a", "100"))
        self.generate_response.reset_mock()

        self.chat("Ada", "Medlar, what did I say about food?", user_id="100", room_id="room-a")

        call = self.generate_response.call_args
        self.assertEqual(2, len(call.kwargs["history"]))
        self.assertIn("saffron rice", call.kwargs["memory_context"])
        self.assertNotIn("cobalt", repr(call.kwargs["history"]))
        self.assertNotIn("cobalt", call.kwargs["memory_context"])
        self.assertEqual("cobalt", self.profiles.get_facts("room-a", "100")["favorite_color"].value)
        self.assertNotIn("favorite_food", self.profiles.get_facts("room-a", "100"))

    def test_profile_store_failure_does_not_fall_back_to_global_history(self):
        broken_profiles = Mock()
        broken_profiles.observe.side_effect = RuntimeError("synthetic profile store failure")
        self.client._viewer_profiles = broken_profiles

        self.chat("Ada", "Medlar, tell me a joke", user_id="100", room_id="room-a")

        self.generate_response.assert_called_once()
        self.assertEqual([], self.generate_response.call_args.kwargs["history"])
        self.assertEqual("", self.generate_response.call_args.kwargs["memory_context"])
        broken_profiles.format_profile_context.assert_not_called()

        self.generate_response.return_value = "Hi Elara, good to meet you."
        self.chat("Ada", "My real name is Elara Voss", user_id="100", room_id="room-a")
        self.assertNotIn("Elara", self.client.send_message.call_args.args[0])

    def test_contact_chat_is_not_copied_to_listener_debug_logs(self):
        capture_log = Mock()
        self.namespace["log"] = capture_log
        self.chat("Ada", "Write to mira@example.invalid or 18 Cedar Street", user_id="100")
        debug_calls = repr(capture_log.debug.call_args_list)
        self.assertNotIn("mira@example.invalid", debug_calls)
        self.assertNotIn("Cedar Street", debug_calls)

    def test_unrelated_banter_does_not_retrieve_saved_personal_fact(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100")
        self.generate_response.reset_mock()

        self.chat("Ada", "Medlar, how is the dragon boss fight?", user_id="100")

        self.generate_response.assert_called_once()
        context = self.generate_response.call_args.kwargs["memory_context"]
        self.assertNotIn("favorite color", context)
        self.assertNotIn("cobalt", context)

    def test_unrelated_questions_do_not_send_own_personal_fact_in_history_or_context(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100")
        for question in (
            "Medlar, how is the dragon boss fight?",
            "Medlar, what color is the dragon boss?",
        ):
            with self.subTest(question=question):
                self.generate_response.reset_mock()
                self.chat("Ada", question, user_id="100")
                self.generate_response.assert_called_once()
                call = self.generate_response.call_args
                self.assertNotIn("cobalt", repr(call.kwargs["history"]))
                self.assertNotIn("cobalt", call.kwargs["memory_context"])

    def test_source_evidence_survives_history_eviction_without_assistant_guess(self):
        self.generate_response.side_effect = ["I think it was garnet"] + ["ack"] * 8
        self.chat("Ada", "My test word is cobalt")
        for number in range(6):
            self.chat("Ada", f"Tell me a short joke number {number}")

        self.chat("Ada", "Medlar, what test word did I tell you?")
        call = self.generate_response.call_args
        # Source-backed recall deliberately excludes competing assistant history.
        self.assertEqual([], call.kwargs["history"])
        self.assertNotIn("cobalt", repr(call.kwargs["history"]))
        self.assertIn("My test word is cobalt", call.kwargs["memory_context"])
        self.assertNotIn("garnet", call.kwargs["memory_context"])
        self.assertNotIn('"Medlar, what test word did I tell you?"', call.kwargs["memory_context"])
        self.assertEqual({}, self.profiles.get_facts("room-a", "100"))
        self.search_intelligently.assert_not_called()

    def test_ordinary_dialogue_history_keeps_only_the_latest_five_exchanges(self):
        for number in range(7):
            self.generate_response.return_value = f"reply number {number}"
            self.chat("Ada", f"Tell me a short joke number {number}")

        self.generate_response.reset_mock()
        self.chat("Ada", "Tell me another joke")
        self.generate_response.assert_called_once()
        history = self.generate_response.call_args.kwargs["history"]
        self.assertEqual(10, len(history))
        self.assertEqual(["user", "assistant"] * 5, [item["role"] for item in history])
        self.assertNotIn("joke number 0", repr(history))
        self.assertNotIn("joke number 1", repr(history))
        self.assertIn("joke number 2", history[0]["content"])
        self.assertEqual("reply number 6", history[-1]["content"])
        self.assertNotIn("Tell me another joke", repr(history))
        self.search_intelligently.assert_not_called()
        self.assertEqual({}, self.profiles.get_facts("room-a", "100"))

    def test_those_words_recall_reaches_model_from_viewer_scoped_evidence(self):
        self.chat("Ada", "Remember these four words:", user_id="100")
        for word in ("obsidian", "lemur", "thyme", "aurora"):
            self.chat("Ada", word, user_id="100")
        for index in range(8):
            self.chat("Ada", f"Tell me a story about round {index}", user_id="100")
        self.generate_response.reset_mock()
        self.chat("Ada", "Medlar, what were those words?", user_id="100")
        context = self.generate_response.call_args.kwargs["memory_context"]
        for word in ("obsidian", "lemur", "thyme", "aurora"):
            self.assertIn(word, context)
        self.assertNotIn("story about", context)
        self.assertNotIn("what were those words?", context.casefold())

    def test_live_comma_sequence_syntax_suppresses_search_and_applies_replacement(self):
        teaching = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "lantern, river, quartz, maple"
        )
        self.chat("Ada", teaching, user_id="100")
        for question in (
            "What color is the moon?",
            "How many sides does a triangle have?",
            "What is a short greeting?",
        ):
            self.chat("Ada", question, user_id="100")

        for question in (
            "Give me the same four-word sequence in original order.",
            "Give me the same four-word sequence in reverse order.",
        ):
            with self.subTest(question=question):
                self.search_intelligently.reset_mock()
                self.generate_response.reset_mock()
                self.client.send_message.reset_mock()
                self.chat("Ada", question, user_id="100")
                self.search_intelligently.assert_not_called()
                self.generate_response.assert_called_once()
                call = self.generate_response.call_args
                self.assertEqual([], call.kwargs["history"])
                self.assertIn(teaching, call.kwargs["memory_context"])
                self.assertNotIn("What color is the moon?", call.kwargs["memory_context"])
                self.client.send_message.assert_called_once_with("mock model reply")

        correction = "Replace lantern with compass in my sequence."
        self.chat("Ada", correction, user_id="100")
        self.search_intelligently.reset_mock()
        self.generate_response.reset_mock()
        self.client.send_message.reset_mock()
        corrected = "What is my corrected four-word test sequence in original order?"
        self.chat("Ada", corrected, user_id="100")

        self.search_intelligently.assert_not_called()
        self.generate_response.assert_called_once()
        context = self.generate_response.call_args.kwargs["memory_context"]
        self.assertIn(teaching, context)
        self.assertIn(correction, context)
        self.assertLess(context.index(teaching), context.index(correction))
        self.assertIn(
            '["compass", "river", "quartz", "maple"]',
            context,
        )

    def test_verbatim_live_sequence_flow_is_source_backed_and_never_searches(self):
        teaching = (
            "[Medlar memory test] Medlar, remember this test sequence in this order: "
            "cobalt, fern, lantern, orbit. Run MM-20261005-A."
        )
        fillers = (
            "[Medlar memory test] Medlar, what is a cozy way to spend a rainy afternoon?",
            "[Medlar memory test] Medlar, would you choose a telescope or binoculars "
            "for stargazing, and why?",
            "[Medlar memory test] Medlar, invent a funny name for a tiny robot.",
        )
        original = (
            "[Medlar memory test] Medlar, for run MM-20261005-A, what were the four "
            "words I asked you to remember, in their original order?"
        )
        reverse = (
            "[Medlar memory test] Medlar, for run MM-20261005-A, give the same "
            "four-word sequence in reverse order."
        )
        correction = (
            "[Medlar memory test] Medlar, correction for run MM-20261005-A: "
            "replace lantern with compass in my test sequence."
        )
        correction_filler = (
            "[Medlar memory test] Medlar, what makes a simple game fun to replay?"
        )
        corrected = (
            "[Medlar memory test] Medlar, for run MM-20261005-A, what is my corrected "
            "four-word test sequence in its original order?"
        )
        denials = (
            "Sorry Pilot, I don't have that information. You didn't share the four words "
            "with me earlier, so I'm not aware of what you're referring to. Would you "
            "like to share them with me now? VoHiYo",
            "Sorry Pilot, I don't have that info. You'll have to give me another chance "
            "to decode that for you ??. VoHiYo",
        )

        self.chat("Ada", teaching, user_id="100")
        for filler in fillers:
            self.chat("Ada", filler, user_id="100")

        real_context_for = self.conversation.context_for
        observed_context_calls = []
        observed_topic_results = []

        def recording_context_for(*args, **kwargs):
            result = real_context_for(*args, **kwargs)
            if kwargs.get("return_source_match"):
                observed_context_calls.append((args, kwargs, result))
            return result

        self.conversation.context_for = Mock(side_effect=recording_context_for)

        def recording_memory_topics(query):
            result = memory_query_topics(query)
            observed_topic_results.append((query, result))
            return result

        topic_patcher = patch(
            "MedlarTV.core.conversation_context.memory_query_topics",
            side_effect=recording_memory_topics,
        )
        topic_patcher.start()
        self.addCleanup(topic_patcher.stop)
        leak_fragments = (
            "rainy afternoon", "telescope or binoculars", "tiny robot",
            "simple game fun", "don't have that information",
            "don't have that info", "Compass it is",
        )

        for question, denial in zip((original, reverse), denials):
            with self.subTest(question=question):
                self.assertEqual({"list"}, memory_query_topics(question))
                observed_context_calls.clear()
                observed_topic_results.clear()
                self.search_intelligently.reset_mock()
                self.generate_response.reset_mock()
                self.client.send_message.reset_mock()
                self.generate_response.return_value = denial
                self.chat("Ada", question, user_id="100")

                self.search_intelligently.assert_not_called()
                self.generate_response.assert_called_once()
                call = self.generate_response.call_args
                self.assertEqual([], call.kwargs["history"])
                self.assertEqual("", call.kwargs["search_context"])
                self.assertIn(teaching, call.kwargs["memory_context"])
                self.assertEqual(1, call.kwargs["memory_context"].count("- ["))
                for excluded in (*fillers, *denials):
                    self.assertNotIn(excluded, call.kwargs["memory_context"])
                for fragment in leak_fragments:
                    self.assertNotIn(
                        fragment.casefold(), call.kwargs["memory_context"].casefold(),
                    )
                self.assertNotIn(question, call.kwargs["memory_context"])
                listener_calls = [
                    item for item in observed_context_calls
                    if len(item[0]) >= 3 and item[0][2] == question
                ]
                self.assertEqual(1, len(listener_calls))
                listener_context, source_match = listener_calls[0][2]
                self.assertTrue(source_match)
                self.assertEqual(call.kwargs["memory_context"], listener_context)
                topic_results = [
                    result for topic_query, result in observed_topic_results
                    if topic_query == question
                ]
                # One call is the listener's intent classification and another
                # is the scoped context lookup. A source-backed history lookup
                # may return before it needs a third classification.
                self.assertGreaterEqual(len(topic_results), 2)
                self.assertTrue(all(result == {"list"} for result in topic_results))

        correction_reply = (
            "Looks like we've got a correction, Pilot! ?? Thanks for update, I'll make "
            "sure to reflect the change in your test sequence. Compass it is, then! "
            "??? CoolCat"
        )
        self.generate_response.return_value = correction_reply
        self.chat("Ada", correction, user_id="100")
        self.chat("Ada", correction_filler, user_id="100")

        self.assertEqual({"list"}, memory_query_topics(corrected))
        observed_context_calls.clear()
        observed_topic_results.clear()
        self.search_intelligently.reset_mock()
        self.generate_response.reset_mock()
        self.client.send_message.reset_mock()
        self.generate_response.return_value = "mock corrected reply"
        self.chat("Ada", corrected, user_id="100")

        self.search_intelligently.assert_not_called()
        self.generate_response.assert_called_once()
        call = self.generate_response.call_args
        self.assertEqual([], call.kwargs["history"])
        self.assertEqual("", call.kwargs["search_context"])
        context = call.kwargs["memory_context"]
        self.assertIn(teaching, context)
        self.assertIn(correction, context)
        self.assertLess(context.index(teaching), context.index(correction))
        self.assertEqual(2, context.count("- ["))
        self.assertIn(
            'Resolved sequence after viewer-authored replacements: '
            '["cobalt", "fern", "compass", "orbit"]',
            context,
        )
        for excluded in (
            *fillers, correction_filler, original, reverse, corrected,
            *denials, correction_reply,
        ):
            self.assertNotIn(excluded, repr(call.kwargs))
        rendered_call = repr(call.kwargs).casefold()
        for fragment in leak_fragments:
            self.assertNotIn(fragment.casefold(), rendered_call)
        listener_calls = [
            item for item in observed_context_calls
            if len(item[0]) >= 3 and item[0][2] == corrected
        ]
        self.assertEqual(1, len(listener_calls))
        listener_context, source_match = listener_calls[0][2]
        self.assertTrue(source_match)
        self.assertEqual(context, listener_context)
        topic_results = [
            result for topic_query, result in observed_topic_results
            if topic_query == corrected
        ]
        self.assertGreaterEqual(len(topic_results), 2)
        self.assertTrue(all(result == {"list"} for result in topic_results))

    def test_unknown_explicit_run_gets_one_deterministic_reply_without_search_or_model(self):
        for message in (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune",
            "Remember this four-word sequence: north, east, south, west",
            "1 is ivory", "2 is jade", "3 is kelp", "4 is lime",
        ):
            self.conversation.observe_user("#testchannel", "100", "Ada", message)
            self.conversation.complete_reply(
                "#testchannel", "100", "Ada", message, "synthetic acknowledgement",
            )

        question = (
            "[Medlar memory test] Medlar, for run MM-20261005-C, what is my "
            "corrected four-word test sequence in its original order?"
        )
        real_history_for = self.conversation.history_for
        real_context_for = self.conversation.context_for
        observed_histories = []
        observed_contexts = []

        def recording_history_for(*args, **kwargs):
            result = real_history_for(*args, **kwargs)
            if kwargs.get("query") == question:
                observed_histories.append(result)
            return result

        def recording_context_for(*args, **kwargs):
            result = real_context_for(*args, **kwargs)
            if len(args) >= 3 and args[2] == question:
                observed_contexts.append(result)
            return result

        self.conversation.history_for = Mock(side_effect=recording_history_for)
        self.conversation.context_for = Mock(side_effect=recording_context_for)
        self.search_intelligently.reset_mock()
        self.generate_response.reset_mock()
        self.client.send_message.reset_mock()
        self.log_interaction.reset_mock()

        self.chat("Ada", question, user_id="100")

        self.assertEqual([[]], observed_histories)
        self.assertEqual([("", False)], observed_contexts)
        self.search_intelligently.assert_not_called()
        self.generate_response.assert_not_called()
        self.client.send_message.assert_called_once_with(
            "I don't have a saved sequence for that run.",
        )
        self.log_interaction.assert_called_once_with(
            "Ada", question, "I don't have a saved sequence for that run.",
        )

    def test_count_free_unknown_run_gets_deterministic_reply(self):
        self.conversation.observe_user(
            "#testchannel", "100", "Ada",
            "Remember this sequence for run MM-20261005-A: amber, birch, cedar, dune",
        )
        question = "For run MM-20261005-C, what is my sequence?"
        self.search_intelligently.reset_mock()
        self.generate_response.reset_mock()
        self.client.send_message.reset_mock()

        self.chat("Ada", question, user_id="100")

        self.search_intelligently.assert_not_called()
        self.generate_response.assert_not_called()
        self.client.send_message.assert_called_once_with(
            "I don't have a saved sequence for that run.",
        )

    def test_teachings_ordinary_generation_and_third_party_run_prompts_reach_model(self):
        prompts = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune",
            "For run MM-20261005-C, give me four words about space.",
            "For run MM-20261005-C, what were the four words Ada asked you to remember?",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.conversation = ConversationContextStore()
                self.client._scoped_conversation = self.conversation
                self.generate_response.reset_mock()
                self.generate_response.return_value = "mock model reply"
                self.search_intelligently.reset_mock()
                self.client.send_message.reset_mock()

                self.chat("Bex", prompt, user_id="200")

                self.generate_response.assert_called_once()
                self.client.send_message.assert_called_once_with("mock model reply")
                self.assertNotEqual(
                    "I don't have a saved sequence for that run.",
                    self.client.send_message.call_args.args[0],
                )

    def test_deleted_favorite_number_returns_no_memory_without_search_after_restart(self):
        self.chat("Ada", "My favorite number is 37", user_id="100")
        self.chat("Ada", "!forget favorite number", user_id="100")
        self.assertNotIn(
            "favorite_number", self.profiles.get_facts("room-a", "100"),
        )

        self.profiles.close()
        self.profiles = ViewerProfileStore(self.profile_path)
        self.conversation = ConversationContextStore()
        self.client = self._new_client()
        self.search_intelligently.reset_mock()
        self.generate_response.reset_mock()

        self.chat("Ada", "What is my favorite number?", user_id="100")

        self.search_intelligently.assert_not_called()
        self.generate_response.assert_not_called()
        self.client.send_message.assert_called_once_with(
            "I don't have a saved favorite number for you.",
        )
        self.assertNotIn(
            "favorite_number", self.profiles.get_facts("room-a", "100"),
        )

    def test_verbatim_favorite_number_is_missing_before_and_after_restart(self):
        teaching = "Medlar, my favorite number is 731942"
        query = "Medlar, what is my favorite number?"
        self.chat("Ada", teaching, user_id="100")
        self.assertEqual(
            "731942", self.profiles.get_facts("room-a", "100")["favorite_number"].value,
        )

        for restart_before_query in (False, True):
            with self.subTest(saved_restart=restart_before_query):
                if restart_before_query:
                    self.profiles.close()
                    self.profiles = ViewerProfileStore(self.profile_path)
                    self.conversation = ConversationContextStore()
                    self.client = self._new_client()
                self.search_intelligently.reset_mock()
                self.generate_response.reset_mock()
                self.client.send_message.reset_mock()
                self.chat("Ada", query, user_id="100")
                self.search_intelligently.assert_not_called()
                self.generate_response.assert_not_called()
                self.client.send_message.assert_called_once_with(
                    "You told me your favorite number is 731942.",
                )

        self.chat("Ada", "!forget favorite number", user_id="100")
        self.assertNotIn(
            "favorite_number", self.profiles.get_facts("room-a", "100"),
        )

        for restart_before_query in (False, True):
            with self.subTest(missing_restart=restart_before_query):
                if restart_before_query:
                    self.profiles.close()
                    self.profiles = ViewerProfileStore(self.profile_path)
                    self.conversation = ConversationContextStore()
                    self.client = self._new_client()
                self.search_intelligently.reset_mock()
                self.generate_response.reset_mock()
                self.client.send_message.reset_mock()
                self.chat("Ada", query, user_id="100")
                self.search_intelligently.assert_not_called()
                self.generate_response.assert_not_called()
                self.client.send_message.assert_called_once_with(
                    "I don't have a saved favorite number for you.",
                )
                self.assertNotIn(
                    "favorite_number", self.profiles.get_facts("room-a", "100"),
                )

    def test_own_list_model_input_excludes_other_list_and_false_assistant_recap(self):
        for statement in ("Remember these four words:", "cypress", "badger",
                          "saffron", "fjord"):
            self.chat("Ada", statement, user_id="100")
        for statement in ("1 is basalt", "2 is oriole", "3 is paprika",
                          "4 is pewter"):
            self.chat("Davi", statement, user_id="200")
        self.generate_response.return_value = "Those words were basalt, oriole, paprika, pewter"
        self.chat("Ada", "How is the raid?", user_id="100")
        self.generate_response.return_value = "mock model reply"
        for index in range(8):
            self.chat("Ada", f"Tell me a new story {index}", user_id="100")

        self.generate_response.reset_mock()
        self.chat("Ada", "Can you list the four words I gave you in reverse order?",
                  user_id="100")
        call = self.generate_response.call_args
        self.assertEqual([], call.kwargs["history"])
        context = call.kwargs["memory_context"]
        for word in ("cypress", "badger", "saffron", "fjord"):
            self.assertIn(word, context)
        for other in ("basalt", "oriole", "paprika", "pewter"):
            self.assertNotIn(other, context)

        self.generate_response.reset_mock()
        self.chat("Ada", "What were Davi's four words?", user_id="100")
        other_context = self.generate_response.call_args.kwargs["memory_context"]
        self.assertIn('"Davi" said "1 is basalt"', other_context)
        self.assertNotIn("cypress", other_context)

    def test_named_list_uses_channel_source_while_factual_words_stay_conversational(self):
        for statement in ("1 is topaz", "2 is heron", "3 is thyme",
                          "4 is glacier"):
            self.chat("Davi", statement, user_id="200")
        self.search_intelligently.reset_mock()
        self.generate_response.reset_mock()

        self.chat("Ada", "What words did Davi send?", user_id="100")
        self.search_intelligently.assert_not_called()
        context = self.generate_response.call_args.kwargs["memory_context"]
        for statement in ("1 is topaz", "2 is heron", "3 is thyme",
                          "4 is glacier"):
            self.assertIn(f'"Davi" said "{statement}"', context)

        self.chat("Ada", "What words did Shakespeare mention?", user_id="100")
        self.search_intelligently.assert_not_called()
        self.assertEqual("", self.generate_response.call_args.kwargs["search_context"])
        self.assertNotIn("topaz", self.generate_response.call_args.kwargs["memory_context"])

    def test_factual_list_question_generates_without_search_or_private_chat_evidence(self):
        for statement in ("Remember these four words:", "cypress", "badger",
                          "saffron", "fjord"):
            self.chat("Ada", statement, user_id="100")
        self.search_intelligently.reset_mock()
        self.search_intelligently.return_value = "CURRENT WEB INFORMATION: synthetic motto"
        self.generate_response.reset_mock()

        question = "What are the four words in this historic motto?"
        self.chat("Ada", question, user_id="100")
        self.search_intelligently.assert_not_called()
        call = self.generate_response.call_args
        self.assertEqual("", call.kwargs["memory_context"])
        self.assertEqual([], call.kwargs["history"])
        self.assertEqual("", call.kwargs["search_context"])

    def test_two_viewers_and_channels_keep_profiles_separate_with_public_banter(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100")
        self.chat("Bex", "My favorite color is amber", user_id="200")
        self.chat("Bex", "The dragon boss is here", user_id="200")
        self.generate_response.reset_mock()

        self.chat("Ada", "Medlar, how is the boss fight?", user_id="100")
        context = self.generate_response.call_args.kwargs["memory_context"]
        self.assertIn("The dragon boss is here", context)
        self.assertNotIn("amber", context)
        self.assertEqual("amber", self.profiles.get_facts("room-a", "200")["favorite_color"].value)
        self.assertEqual(4, len(self.conversation.history_for("#testchannel", "200")))

        self.generate_response.reset_mock()
        self.chat("Ada", "Medlar, what is my favorite color?", user_id="100",
                  room_id="room-b", channel="#otherchannel")
        self.generate_response.assert_called_once()
        self.assertEqual([], self.generate_response.call_args.kwargs["history"])
        self.assertEqual("", self.generate_response.call_args.kwargs["memory_context"])
        self.assertEqual({}, self.profiles.get_facts("room-b", "100"))

    def test_group_topic_recall_survives_intervening_public_chat(self):
        self.chat("Ada", "The group puzzle token is copper", user_id="100")
        for number in range(6):
            self.chat("Cal", f"Round {number} is ready for the next boss", user_id="300")
        self.generate_response.reset_mock()

        self.chat("Bex", "Medlar, what group puzzle token did Ada mention?", user_id="200")

        self.generate_response.assert_called_once()
        context = self.generate_response.call_args.kwargs["memory_context"]
        self.assertIn('"Ada" said "The group puzzle token is copper"', context)
        self.assertEqual([], self.generate_response.call_args.kwargs["history"])

    def test_forgetting_one_viewer_keeps_other_viewers_public_banter(self):
        self.chat("Ada", "The crystal gate is open", user_id="100")
        self.chat("Bex", "The dragon boss is here", user_id="200")
        self.chat("Ada", "!forgetme", user_id="100")
        self.generate_response.reset_mock()

        self.chat("Cal", "Medlar, what did Bex say about the dragon boss?", user_id="300")

        context = self.generate_response.call_args.kwargs["memory_context"]
        self.assertIn('"Bex" said "The dragon boss is here"', context)
        self.assertNotIn("crystal gate", context)

    def test_private_birthday_and_third_party_location_stay_out_of_public_context(self):
        self.chat("Ada", "My birthday is October 2", user_id="100")
        self.chat("Bex", "Cal lives at 12 Orchid Lane", user_id="200")
        self.generate_response.reset_mock()

        self.chat("Cal", "Medlar, what did Bex say about me?", user_id="300")

        context = self.generate_response.call_args.kwargs["memory_context"]
        self.assertNotIn("October 2", context)
        self.assertNotIn("Orchid Lane", context)
        self.assertEqual("10-02", self.profiles.get_facts("room-a", "100")["birthday"].value)
        self.assertEqual({}, self.profiles.get_facts("room-a", "300"))

    def test_forgetme_deletes_only_requesting_viewers_profile_and_session(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100")
        self.chat("Bex", "My favorite color is amber", user_id="200")
        self.generate_response.reset_mock()
        self.execute_command.reset_mock()

        self.chat("Ada", "!forgetme", user_id="100")

        self.assertEqual({}, self.profiles.get_facts("room-a", "100"))
        self.assertEqual("amber", self.profiles.get_facts("room-a", "200")["favorite_color"].value)
        self.assertEqual([], self.conversation.history_for("#testchannel", "100"))
        self.assertEqual(2, len(self.conversation.history_for("#testchannel", "200")))
        self.generate_response.assert_not_called()
        self.execute_command.assert_not_called()
        self.assertIn("cleared", self.client.send_message.call_args.args[0])

    def test_forget_command_accepts_viewer_facing_fact_name(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100")
        self.chat("Bex", "My favorite color is amber", user_id="200")
        self.generate_response.reset_mock()

        self.chat("Ada", "!forget favorite color", user_id="100")

        self.assertNotIn("favorite_color", self.profiles.get_facts("room-a", "100"))
        self.assertEqual("amber", self.profiles.get_facts("room-a", "200")["favorite_color"].value)
        self.generate_response.assert_not_called()

    def test_profile_limit_notice_keeps_existing_fact_and_skips_model(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100")
        limited_profiles = Mock(wraps=self.profiles)
        limited_profiles.observe.return_value = SimpleNamespace(kind="limit_reached")
        self.client._viewer_profiles = limited_profiles
        self.generate_response.reset_mock()
        self.search_intelligently.reset_mock()
        self.client.send_message.reset_mock()

        self.chat("Ada", "My favorite food is saffron rice", user_id="100")

        self.generate_response.assert_not_called()
        self.search_intelligently.assert_not_called()
        self.client.send_message.assert_called_once()
        self.assertIn("couldn't save", self.client.send_message.call_args.args[0])
        self.assertEqual("cobalt", self.profiles.get_facts("room-a", "100")["favorite_color"].value)
        self.assertNotIn("favorite_food", self.profiles.get_facts("room-a", "100"))

    def test_bare_forget_shows_usage_without_deleting_profile(self):
        self.chat("Ada", "My favorite color is cobalt", user_id="100")
        self.client.send_message.reset_mock()

        self.chat("Ada", "!forget", user_id="100")

        self.assertEqual("cobalt", self.profiles.get_facts("room-a", "100")["favorite_color"].value)
        self.assertIn("Use !forget", self.client.send_message.call_args.args[0])

    def test_untagged_natural_forget_clears_session_without_profile(self):
        self.chat("Ada", "My favorite food is curry", user_id=None)
        self.assertEqual(2, len(self.conversation.history_for("#testchannel", "login:ada")))

        self.chat("Ada", "forget everything about me", user_id=None)

        self.assertEqual([], self.conversation.history_for("#testchannel", "login:ada"))
        self.assertFalse(self.profile_path.exists())

    def test_conversation_command_and_chat_timer_paths_remain_separate(self):
        self.client._timers = [{
            "id": "chat-timer", "type": "chats", "enabled": True,
            "interval_chats": 1, "message": "scheduled timer message",
        }]
        self.search_intelligently.return_value = "CURRENT WEB INFORMATION: synthetic result"
        self.chat("Ada", "Medlar, latest astronomy news?", user_id="100")

        self.search_intelligently.assert_not_called()
        self.assertEqual("", self.generate_response.call_args.kwargs["search_context"])
        self.assertNotIn("CURRENT WEB INFORMATION", self.generate_response.call_args.kwargs["memory_context"])
        self.assertEqual(1, self.client._chat_count)
        self.assertEqual("scheduled timer message", self.client.send_message.call_args_list[0].args[0])
        self.assertEqual("mock model reply", self.client.send_message.call_args_list[1].args[0])

        self.generate_response.reset_mock()
        self.search_intelligently.reset_mock()
        self.client.send_message.reset_mock()
        self.chat("Ada", "!ping", user_id="100")
        self.execute_command.assert_called_once()
        self.generate_response.assert_not_called()
        self.search_intelligently.assert_not_called()
        self.client.send_message.assert_called_once_with("Pong")
        self.assertEqual(1, self.client._chat_count)
        self.assertEqual({}, self.profiles.get_facts("room-a", "100"))


if __name__ == "__main__":
    unittest.main()
