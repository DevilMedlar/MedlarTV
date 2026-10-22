"""Offline checks for bounded, source-attributed Twitch session context."""

import unittest
from datetime import datetime, timedelta, timezone

from MedlarTV.core.conversation_context import (
    ConversationContextStore,
    is_explicit_personal_run_query,
    memory_query_topics,
)
from MedlarTV.core.memory_privacy import has_contact_detail


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        self.store = ConversationContextStore()

    def speak(self, message: str, reply: str = "ack", *, viewer: str = "id:1", name: str = "Ada", channel: str = "alpha") -> None:
        self.store.observe_user(channel, viewer, name, message)
        self.store.complete_reply(channel, viewer, name, message, reply)

    def test_user_evidence_survives_five_exchanges_without_assistant_guesses(self):
        self.speak("My test word is cobalt", "I think the word is garnet")
        for index in range(6):
            self.speak(f"What is happening in round {index}?", f"Round {index} is done")

        history = self.store.history_for("alpha", "id:1")
        self.assertEqual(10, len(history))
        self.assertNotIn("cobalt", repr(history))
        self.store.observe_user("alpha", "id:1", "Ada", "What test word did I tell you?")
        context = self.store.context_for("alpha", "id:1", "What test word did I tell you?")
        self.assertIn("My test word is cobalt", context)
        self.assertNotIn("garnet", context)
        self.assertNotIn('"What test word did I tell you?"', context)

    def test_varied_recall_phrasing_finds_unseen_statement_after_eight_fillers(self):
        self.speak("My puzzle token is bronze", "I think it was silver")
        for index in range(8):
            self.speak(f"Tell me a joke number {index}", "A new joke")

        question = "Could you remind me what detail I asked you to keep in mind?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        self.assertIn("My puzzle token is bronze", context)
        self.assertNotIn("silver", context)
        self.assertNotIn(question, context)
        self.assertIn("bronze", self.store.context_for(
            "alpha", "id:1", "What was my puzzle token?",
        ))
        self.assertIn("bronze", self.store.context_for(
            "alpha", "id:1", "What's my puzzle token?",
        ))

    def test_numbered_temp_list_and_latest_correction_are_source_ordered(self):
        self.speak("Word 1 is lantern", "Maybe the first word is sunflower")
        self.speak("Word 2 is otter")
        self.speak("Word 3 is velvet")
        self.speak("Word 4 is comet")
        self.speak("Correction: word 2 is cedar")
        for index in range(6):
            self.speak(f"Tell a joke {index}")

        self.store.observe_user("alpha", "id:1", "Ada", "Give the words in original order")
        context = self.store.context_for("alpha", "id:1", "Give the words in original order")
        for expected in ("Word 1 is lantern", "Word 2 is otter", "Word 3 is velvet",
                         "Word 4 is comet", "Correction: word 2 is cedar"):
            self.assertIn(expected, context)
        self.assertLess(context.index("Word 2 is otter"), context.index("word 2 is cedar"))
        self.assertIn("Later corrections win", context)
        self.assertNotIn("Tell a joke", context)
        self.assertNotIn("sunflower", context)

    def test_run_labeled_comma_sequence_recall_and_replacement_are_source_backed(self):
        teaching = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "lantern, river, quartz, maple"
        )
        self.speak(teaching)
        for question in (
            "What color is the moon?",
            "How many sides does a triangle have?",
            "What is a short greeting?",
        ):
            self.speak(question, "synthetic filler reply")

        original = "Give me the same four-word sequence in original order."
        self.store.observe_user("alpha", "id:1", "Ada", original)
        context, source_match = self.store.context_for(
            "alpha", "id:1", original, return_source_match=True,
        )
        self.assertTrue(source_match)
        self.assertIn(teaching, context)
        self.assertNotIn("What color is the moon?", context)
        self.assertNotIn(original, context)
        self.store.complete_reply(
            "alpha", "id:1", "Ada", original,
            "I don't remember any sequence.",
        )

        reverse = "Give me the same four-word sequence in reverse order."
        self.store.observe_user("alpha", "id:1", "Ada", reverse)
        reverse_context, reverse_source = self.store.context_for(
            "alpha", "id:1", reverse, return_source_match=True,
        )
        self.assertTrue(reverse_source)
        self.assertIn(teaching, reverse_context)
        self.assertNotIn(original, reverse_context)
        self.assertNotIn("don't remember", reverse_context)
        self.store.complete_reply(
            "alpha", "id:1", "Ada", reverse,
            "I still don't remember it.",
        )

        correction = "Replace lantern with compass in my sequence."
        self.speak(correction)
        corrected = "What is my corrected four-word test sequence in original order?"
        self.store.observe_user("alpha", "id:1", "Ada", corrected)
        corrected_context, corrected_source = self.store.context_for(
            "alpha", "id:1", corrected, return_source_match=True,
        )
        self.assertTrue(corrected_source)
        self.assertIn(teaching, corrected_context)
        self.assertIn(correction, corrected_context)
        self.assertLess(corrected_context.index(teaching), corrected_context.index(correction))
        self.assertIn(
            'Resolved sequence after viewer-authored replacements: '
            '["compass", "river", "quartz", "maple"]',
            corrected_context,
        )
        self.assertNotIn(original, corrected_context)
        self.assertNotIn(reverse, corrected_context)
        self.assertNotIn("don't remember", corrected_context)
        self.assertEqual([], self.store.history_for("alpha", "id:1", corrected))

    def test_verbatim_live_run_sequence_is_source_backed_through_correction(self):
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
        original_denial = (
            "Sorry Pilot, I don't have that information. You didn't share the four words "
            "with me earlier, so I'm not aware of what you're referring to."
        )
        reverse_denial = (
            "Sorry Pilot, I don't have that info. You'll have to give me another chance "
            "to decode that for you."
        )

        self.speak(teaching)
        for filler in fillers:
            self.speak(filler, "synthetic filler reply")

        for question, denial in (
            (original, original_denial),
            (reverse, reverse_denial),
        ):
            with self.subTest(question=question):
                self.assertEqual({"list"}, memory_query_topics(question))
                self.store.observe_user("alpha", "id:1", "Ada", question)
                context, source_match = self.store.context_for(
                    "alpha", "id:1", question, return_source_match=True,
                )
                self.assertTrue(source_match)
                self.assertEqual([], self.store.history_for("alpha", "id:1", question))
                self.assertIn(teaching, context)
                self.assertEqual(1, context.count("- ["))
                for excluded in (*fillers, original_denial, reverse_denial):
                    self.assertNotIn(excluded, context)
                self.assertNotIn(question, context)
                self.store.complete_reply("alpha", "id:1", "Ada", question, denial)

        self.speak(correction, "synthetic correction acknowledgement")
        self.speak(correction_filler, "synthetic filler reply")
        self.assertEqual({"list"}, memory_query_topics(corrected))
        self.store.observe_user("alpha", "id:1", "Ada", corrected)
        context, source_match = self.store.context_for(
            "alpha", "id:1", corrected, return_source_match=True,
        )
        self.assertTrue(source_match)
        self.assertEqual([], self.store.history_for("alpha", "id:1", corrected))
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
            original_denial, reverse_denial, "synthetic correction acknowledgement",
        ):
            self.assertNotIn(excluded, context)

        question = "Give me my sequence in its original order."
        self.assertEqual({"list"}, memory_query_topics(question))
        context, source_match = self.store.context_for(
            "alpha", "id:1", question, return_source_match=True,
        )
        self.assertTrue(source_match)
        self.assertIn(teaching, context)
        self.assertIn(correction, context)

    def test_run_labeled_episodes_stay_isolated_across_correction_orderings(self):
        teaching_a = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune"
        )
        teaching_b = (
            "Remember this four-word sequence for run MM-20261005-B: "
            "ivory, jade, cedar, lime"
        )
        correction_a = (
            "Correction for run MM-20261005-A: "
            "replace cedar with coral in my sequence."
        )
        query_a = (
            "For run mm-20261005-a, what is my corrected four-word sequence "
            "in its original order?"
        )
        query_b = (
            "For run MM-20261005-B, what is my corrected four-word sequence "
            "in its original order?"
        )
        unlabeled_query = "What is my corrected four-word sequence in its original order?"

        for messages in (
            (teaching_a, correction_a, teaching_b),
            (teaching_a, teaching_b, correction_a),
        ):
            with self.subTest(messages=messages):
                store = ConversationContextStore()
                for message in messages:
                    store.observe_user("alpha", "id:1", "Ada", message)
                    store.complete_reply("alpha", "id:1", "Ada", message, "ack")

                for query, expected, excluded, resolved, line_count in (
                    (query_a, (teaching_a, correction_a), (teaching_b,),
                     '["amber", "birch", "coral", "dune"]', 2),
                    (query_b, (teaching_b,), (teaching_a, correction_a),
                     '["ivory", "jade", "cedar", "lime"]', 1),
                    (unlabeled_query, (teaching_b,), (teaching_a, correction_a),
                     '["ivory", "jade", "cedar", "lime"]', 1),
                ):
                    store.observe_user("alpha", "id:1", "Ada", query)
                    context, source_match = store.context_for(
                        "alpha", "id:1", query, return_source_match=True,
                    )
                    self.assertTrue(source_match)
                    self.assertEqual([], store.history_for("alpha", "id:1", query))
                    self.assertEqual(line_count, context.count("- ["))
                    for message in expected:
                        self.assertIn(message, context)
                    for message in excluded:
                        self.assertNotIn(message, context)
                    self.assertIn(resolved, context)

    def test_unlabeled_corrections_and_queries_follow_latest_teaching_anchor(self):
        teaching_a = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune"
        )
        teaching_b = (
            "Remember this four-word sequence for run MM-20261005-B: "
            "ivory, jade, cedar, lime"
        )
        correction = "Replace cedar with coral in my sequence."
        query_a = "For run MM-20261005-A, what is my four-word sequence?"
        query_b = "For run MM-20261005-B, what is my four-word sequence?"
        latest_query = "What is my four-word sequence in original order?"

        for messages, resolved_a, resolved_b in (
            ((teaching_a, correction, teaching_b), "coral", "cedar"),
            ((teaching_a, teaching_b, correction), "cedar", "coral"),
        ):
            with self.subTest(messages=messages):
                store = ConversationContextStore()
                for message in messages:
                    store.observe_user("alpha", "id:1", "Ada", message)

                for query, expected_teaching, expected_value in (
                    (query_a, teaching_a, resolved_a),
                    (query_b, teaching_b, resolved_b),
                    (latest_query, teaching_b, resolved_b),
                ):
                    store.observe_user("alpha", "id:1", "Ada", query)
                    context, source_match = store.context_for(
                        "alpha", "id:1", query, return_source_match=True,
                    )
                    self.assertTrue(source_match)
                    self.assertIn(expected_teaching, context)
                    self.assertIn(expected_value, context)
                    other = teaching_b if expected_teaching == teaching_a else teaching_a
                    self.assertNotIn(other, context)
                latest_context = store.context_for("alpha", "id:1", latest_query)
                if messages[-1] == correction:
                    self.assertIn(correction, latest_context)
                else:
                    self.assertNotIn(correction, latest_context)

    def test_unknown_run_never_falls_back_to_other_source_kinds(self):
        query = (
            "For run MM-20261005-C, what is my corrected four-word sequence "
            "in its original order?"
        )
        seed_sets = (
            ("Remember this four-word sequence for run MM-20261005-A: "
             "amber, birch, cedar, dune",),
            ("Remember this four-word sequence: amber, birch, cedar, dune",),
            ("1 is amber", "2 is birch", "3 is cedar", "4 is dune"),
            ("Remember these four words:", "amber", "birch", "cedar", "dune"),
            ("Tell me a story about a sequence",),
        )
        self.assertEqual({"list"}, memory_query_topics(query))
        self.assertTrue(is_explicit_personal_run_query(query))
        for index, seed_set in enumerate(seed_sets):
            with self.subTest(seed_set=seed_set):
                store = ConversationContextStore()
                for seed in seed_set:
                    store.observe_user("alpha", "id:1", "Ada", seed)
                    reply = ("The saved sequence is amber, birch, cedar, dune"
                             if index == len(seed_sets) - 1 else "ack")
                    store.complete_reply("alpha", "id:1", "Ada", seed, reply)
                store.observe_user("alpha", "id:1", "Ada", query)
                self.assertEqual([], store.history_for("alpha", "id:1", query))
                self.assertEqual(
                    ("", False),
                    store.context_for(
                        "alpha", "id:1", query, return_source_match=True,
                    ),
                )

    def test_count_free_personal_sequence_queries_keep_run_and_latest_semantics(self):
        teaching_a = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune"
        )
        teaching_b = (
            "Remember this four-word sequence for run MM-20261005-B: "
            "ivory, jade, kelp, lime"
        )
        self.store.observe_user("alpha", "id:1", "Ada", teaching_a)
        self.store.observe_user("alpha", "id:1", "Ada", teaching_b)
        for query, expected, excluded in (
            ("For run MM-20261005-A, what is my sequence?", teaching_a, teaching_b),
            ("Give me my sequence for run MM-20261005-A.", teaching_a, teaching_b),
            ("What is my sequence?", teaching_b, teaching_a),
        ):
            with self.subTest(query=query):
                self.assertEqual({"list"}, memory_query_topics(query))
                context, source_match = self.store.context_for(
                    "alpha", "id:1", query, return_source_match=True,
                )
                self.assertTrue(source_match)
                self.assertEqual(1, context.count("- ["))
                self.assertIn(expected, context)
                self.assertNotIn(excluded, context)
                self.assertEqual([], self.store.history_for("alpha", "id:1", query))

        unknown = "For run MM-20261005-C, what is my sequence?"
        self.assertTrue(is_explicit_personal_run_query(unknown))
        self.assertEqual([], self.store.history_for("alpha", "id:1", unknown))
        self.assertEqual(
            ("", False),
            self.store.context_for(
                "alpha", "id:1", unknown, return_source_match=True,
            ),
        )

    def test_ambiguous_run_ids_are_never_treated_as_unlabeled(self):
        teaching_a = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune"
        )
        teaching_b = (
            "Remember this four-word sequence for run MM-20261005-B: "
            "ivory, jade, kelp, lime"
        )
        self.speak(teaching_a)
        self.speak(teaching_b)
        ambiguous_query = (
            "For run MM-20261005-A and run MM-20261005-B, "
            "what is my four-word sequence?"
        )
        self.assertTrue(is_explicit_personal_run_query(ambiguous_query))
        self.assertEqual([], self.store.history_for("alpha", "id:1", ambiguous_query))
        self.assertEqual(
            ("", False),
            self.store.context_for(
                "alpha", "id:1", ambiguous_query, return_source_match=True,
            ),
        )
        single_context_ambiguous_query = (
            "For run MM-20261005-A with reference MM-20261005-B, "
            "what is my four-word sequence?"
        )
        self.assertTrue(is_explicit_personal_run_query(single_context_ambiguous_query))
        self.assertEqual(
            ("", False),
            self.store.context_for(
                "alpha", "id:1", single_context_ambiguous_query,
                return_source_match=True,
            ),
        )

        ambiguous_correction = (
            "Correction for run MM-20261005-A and run MM-20261005-B: "
            "replace cedar with coral in my sequence."
        )
        self.speak(ambiguous_correction)
        query_a = "For run MM-20261005-A, what is my four-word sequence?"
        context_a = self.store.context_for("alpha", "id:1", query_a)
        self.assertIn(teaching_a, context_a)
        self.assertIn('"cedar"', context_a)
        self.assertNotIn(ambiguous_correction, context_a)

        repeated_id_query = (
            "For run MM-20261005-A, reference mm-20261005-a, "
            "what is my four-word sequence?"
        )
        repeated_context, repeated_source = self.store.context_for(
            "alpha", "id:1", repeated_id_query, return_source_match=True,
        )
        self.assertTrue(repeated_source)
        self.assertIn(teaching_a, repeated_context)

        ambiguous_teaching = (
            "Remember this four-word sequence for run MM-20261005-A and "
            "run MM-20261005-B: north, east, south, west"
        )
        fresh = ConversationContextStore()
        fresh.observe_user("alpha", "id:1", "Ada", ambiguous_teaching)
        unlabeled = "What is my four-word sequence?"
        self.assertEqual(
            ("", False),
            fresh.context_for(
                "alpha", "id:1", unlabeled, return_source_match=True,
            ),
        )
        self.assertFalse(is_explicit_personal_run_query(
            "For run MM-20261005-A, what were their four words?",
        ))
        self.assertFalse(is_explicit_personal_run_query(
            "For run MM-20261005-A, what were the four words Ada asked you to remember?",
        ))
        for ordinary in (
            "For run MM-20261005-A, give me four words about space.",
            "For run MM-20261005-A, what are four words that rhyme with cat?",
            "For run MM-20261005-A, list four words.",
            teaching_a,
            ambiguous_correction,
        ):
            with self.subTest(ordinary=ordinary):
                self.assertFalse(is_explicit_personal_run_query(ordinary))

    def test_same_run_id_is_isolated_by_viewer(self):
        ada_teaching = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune"
        )
        bex_teaching = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "ivory, jade, kelp, lime"
        )
        self.store.observe_user("alpha", "id:1", "Ada", ada_teaching)
        self.store.observe_user("alpha", "id:2", "Bex", bex_teaching)
        query = "For run MM-20261005-A, what is my four-word sequence?"
        for viewer, expected, excluded in (
            ("id:1", ada_teaching, bex_teaching),
            ("id:2", bex_teaching, ada_teaching),
        ):
            with self.subTest(viewer=viewer):
                context, source_match = self.store.context_for(
                    "alpha", viewer, query, return_source_match=True,
                )
                self.assertTrue(source_match)
                self.assertEqual(1, context.count("- ["))
                self.assertIn(expected, context)
                self.assertNotIn(excluded, context)
                self.assertEqual([], self.store.history_for("alpha", viewer, query))

    def test_orphan_corrections_never_supply_evidence_or_mutate_future_teaching(self):
        teaching = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune"
        )
        query = "For run MM-20261005-A, what is my four-word sequence?"
        for correction in (
            "Correction for run MM-20261005-A: replace cedar with coral in my sequence.",
            "Replace cedar with coral in my sequence.",
        ):
            with self.subTest(correction=correction):
                store = ConversationContextStore()
                store.observe_user("alpha", "id:1", "Ada", correction)
                broad_context, broad_source = store.context_for(
                    "alpha", "id:1", "What do you remember about me?",
                    return_source_match=True,
                )
                self.assertFalse(broad_source)
                self.assertNotIn(correction, broad_context)
                store.complete_reply(
                    "alpha", "id:1", "Ada", correction, "Okay, coral.",
                )
                for list_query in (
                    "What is my sequence?",
                    "What is my four-word sequence?",
                    "Recall my sequence.",
                ):
                    self.assertEqual(
                        [], store.history_for("alpha", "id:1", list_query),
                    )
                self.assertEqual(
                    ("", False),
                    store.context_for(
                        "alpha", "id:1", query, return_source_match=True,
                    ),
                )
                store.observe_user("alpha", "id:1", "Ada", teaching)
                context, source_match = store.context_for(
                    "alpha", "id:1", query, return_source_match=True,
                )
                self.assertTrue(source_match)
                self.assertEqual(1, context.count("- ["))
                self.assertIn(teaching, context)
                self.assertIn('"cedar"', context)
                self.assertNotIn(correction, context)
                self.assertNotIn('"coral"', context)

    def test_reteaching_same_run_supersedes_old_episode_and_corrections(self):
        old_teaching = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "amber, birch, cedar, dune"
        )
        correction = (
            "Correction for run MM-20261005-A: "
            "replace cedar with coral in my sequence."
        )
        new_teaching = (
            "Remember this four-word sequence for run MM-20261005-A: "
            "ivory, cedar, kelp, lime"
        )
        for message in (old_teaching, correction, new_teaching):
            self.store.observe_user("alpha", "id:1", "Ada", message)
        query = "For run MM-20261005-A, what is my four-word sequence?"
        context, source_match = self.store.context_for(
            "alpha", "id:1", query, return_source_match=True,
        )
        self.assertTrue(source_match)
        self.assertEqual(1, context.count("- ["))
        self.assertIn(new_teaching, context)
        self.assertIn('["ivory", "cedar", "kelp", "lime"]', context)
        self.assertNotIn(old_teaching, context)
        self.assertNotIn(correction, context)
        self.assertNotIn('"coral"', context)

        mismatch = ConversationContextStore()
        for message in (
            old_teaching,
            "Remember this three-word sequence for run MM-20261005-A: "
            "north, east, west",
        ):
            mismatch.observe_user("alpha", "id:1", "Ada", message)
        four_word_query = (
            "For run MM-20261005-A, what is my four-word sequence?"
        )
        self.assertEqual(
            ("", False),
            mismatch.context_for(
                "alpha", "id:1", four_word_query, return_source_match=True,
            ),
        )

    def test_opaque_run_label_is_not_contact_but_real_contact_details_remain_blocked(self):
        allowed_run_messages = (
            "Remember this sequence for run MM-20261005-A: lantern, river, quartz, maple",
            "[Medlar memory test] Medlar, remember this test sequence in this order: "
            "cobalt, fern, lantern, orbit. Run MM-20261005-A.",
            "[Medlar memory test] Medlar, for run MM-20261005-A, what were the four "
            "words I asked you to remember, in their original order?",
            "[Medlar memory test] Medlar, correction for run MM-20261005-A: "
            "replace lantern with compass in my test sequence.",
        )
        for message in allowed_run_messages:
            with self.subTest(message=message):
                self.assertFalse(has_contact_detail(message))
        for contact in (
            "Call me at 5550123",
            "Reach me at 5550123-ext",
            "Reach me at x-5550123",
            "The identifier is MM-20261005-A",
            "Run 5550123-ext.",
            "Run x-5550123.",
            "Run MM-20261005-A5550123.",
            "Run MM-20261005-5550123.",
            "Run MM-55501234-A.",
            "Run MM-20261305-A.",
            "Run MM-20261032-A.",
            "pre-run MM-20261005-A.",
            "@run MM-20261005-A.",
            "Run MM-20261005-A@example.invalid",
            "Run MM-20261005-A.example.invalid",
            "Run MM-20261005-A/path",
            "Run MM-20261005-A-extra",
            "Run MM-20261005-A_suffix",
            "Run MM-20261005-A; call me 5550123",
            "Run MM-20261005-A; email mira@example.invalid",
            "Run MM-20261005-A; visit https://example.invalid/contact",
            "Run MM-20261005-A; meet at 18 Cedar Street",
            "+1 (503) 555-0199",
            "mira@example.invalid",
            "https://example.invalid/contact",
            "18 Cedar Street",
        ):
            with self.subTest(contact=contact):
                self.assertTrue(has_contact_detail(contact))

    def test_hyphenated_contact_numbers_never_enter_any_context(self):
        contacts = ("Reach me at 5550123-ext", "Reach me at x-5550123")
        for contact in contacts:
            self.speak(contact, "synthetic acknowledgement")

        self.assertEqual([], self.store.history_for("alpha", "id:1"))
        own_context = self.store.context_for(
            "alpha", "id:1", "What do you remember about me?",
        )
        public_context = self.store.context_for(
            "alpha", "id:2", "What did Ada say in chat?",
        )
        for contact in contacts:
            self.assertNotIn(contact, own_context)
            self.assertNotIn(contact, public_context)

    def test_order_modifier_pronouns_do_not_unlock_other_owned_lists(self):
        for statement in (
            "Remember these four words:", "tamarind", "kestrel", "violet", "delta",
        ):
            self.speak(statement)
        for statement in (
            "1 is basalt", "2 is oriole", "3 is paprika", "4 is pewter",
        ):
            self.store.observe_user("alpha", "id:2", "Davi", statement)

        self.assertEqual(set(), memory_query_topics("What were their four words?"))
        for question in (
            "What were their four words?",
            "What are the four words in their anthem?",
            "What is his four-word sequence in its original order?",
            "What were their four words in their original order?",
            "What did I ask you to remember about his four-word sequence "
            "in its original order?",
        ):
            with self.subTest(question=question):
                self.assertEqual(set(), memory_query_topics(question))
                self.assertEqual("", self.store.context_for("alpha", "id:1", question))
                self.assertEqual(
                    [], self.store.history_for("alpha", "id:1", query=question),
                )

        self.speak("The crystal shield is ready")
        context = self.store.context_for(
            "alpha", "id:1", "What did I say about her shield?",
        )
        self.assertIn("The crystal shield is ready", context)

    def test_counted_recall_recovers_unlabeled_numbered_words_after_fillers(self):
        for statement in ("1 is quartz", "2 is elm", "3 is saffron", "4 is orbit"):
            self.speak(statement)
        self.speak("My birthday is June 8")
        self.speak("Correction: 2 is willow")
        for index in range(8):
            self.speak(f"Tell me another joke {index}")
        question = "What were the four words?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        for statement in ("1 is quartz", "2 is elm", "3 is saffron", "4 is orbit",
                          "Correction: 2 is willow"):
            self.assertIn(statement, context)
        self.assertLess(context.index("2 is elm"), context.index("2 is willow"))
        self.assertNotIn("another joke", context)
        self.assertNotIn("June 8", context)

    def test_counted_recall_recovers_contiguous_unnumbered_teaching(self):
        for statement in (
            "Remember these four words:", "marigold", "harbor", "badger", "copper",
        ):
            self.speak(statement)
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        self.speak("1 is the score")
        question = "What were the four words?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("marigold", "harbor", "badger", "copper"):
            self.assertIn(word, context)
        self.assertNotIn("story about", context)
        self.assertNotIn("1 is the score", context)

    def test_counted_recall_uses_viewer_turns_when_other_viewers_interleave(self):
        for word in ("marigold", "harbor", "badger", "copper"):
            self.speak(word)
            self.store.observe_user("alpha", "id:2", "Bex", "The dragon moves")
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        question = "What were the four words?"
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("marigold", "harbor", "badger", "copper"):
            self.assertIn(word, context)
        self.assertNotIn("story about", context)

    def test_uncounted_recall_finds_interleaved_teaching_episode_after_fillers(self):
        self.speak("Remember these four words:")
        for word in ("azalea", "isthmus", "gerbil", "ochre"):
            self.speak(word)
            self.store.observe_user("alpha", "id:2", "Bex", "The dragon moves")
        self.speak("My birthday is June 8")
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        question = "What words did I teach you?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("azalea", "isthmus", "gerbil", "ochre"):
            self.assertIn(word, context)
        self.assertNotIn("story about", context)
        self.assertNotIn("June 8", context)

    def test_repeat_my_words_keeps_private_recap_and_other_channel_out(self):
        self.speak("Remember these four words:")
        for word in ("quasar", "tamarind", "ibex", "pewter"):
            self.speak(word)
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        self.speak("My birthday is June 8", "You said June 8")
        self.speak("My favorite color is emerald", viewer="id:2", name="Bex")
        self.store.observe_user("alpha", "id:2", "Bex", "The dragon boss is here")
        self.store.observe_user("beta", "id:2", "Bex", "The crystal gate is open")
        question = "Can you repeat my words?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        combined = context + repr(self.store.history_for("alpha", "id:1", query=question))
        for word in ("quasar", "tamarind", "ibex", "pewter"):
            self.assertIn(word, context)
        for excluded in ("June 8", "emerald", "The crystal gate is open", question):
            self.assertNotIn(excluded, combined)
        self.assertIn("The dragon boss is here", context)

    def test_sent_words_recall_after_history_eviction_is_viewer_scoped(self):
        self.speak("Remember these four words:")
        for word in ("peridot", "cicada", "firn", "lilac"):
            self.speak(word)
        self.speak("My birthday is June 8")
        self.speak("My favorite food is curry", viewer="id:2", name="Bex")
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        question = "What words did I send you?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        combined = context + repr(self.store.history_for("alpha", "id:1", query=question))
        for word in ("peridot", "cicada", "firn", "lilac"):
            self.assertIn(word, context)
        for excluded in ("June 8", "curry", question):
            self.assertNotIn(excluded, combined)
        self.assertNotIn("story about", context)

    def test_those_words_uses_only_requesting_viewers_established_group(self):
        self.speak("Remember these four words:")
        for word in ("amethyst", "marmoset", "willow", "cumin"):
            self.speak(word)
        for statement in ("1 is basalt", "2 is heron", "3 is ginger",
                          "4 is pewter"):
            self.store.observe_user("alpha", "id:2", "Bex", statement)
        self.speak("My birthday is June 8")
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        for question in ("What were those words?", "Medlar, what were those words?"):
            self.store.observe_user("alpha", "id:1", "Ada", question)
            context = self.store.context_for("alpha", "id:1", question)
            self.assertIn("Earlier messages by this viewer", context)
            for word in ("amethyst", "marmoset", "willow", "cumin"):
                self.assertIn(word, context)
            for excluded in ("basalt", "heron", "ginger", "pewter",
                             "June 8", "story about", question):
                self.assertNotIn(excluded, context)
            self.assertEqual("", self.store.context_for("alpha", "id:3", question))

    def test_later_teaching_group_beats_older_numbered_distractors(self):
        for statement in ("1 is the score", "2 is the round", "3 is the level",
                          "4 is the turn"):
            self.speak(statement)
        self.speak("Remember these four words:")
        for word in ("quasar", "tamarind", "ibex", "pewter"):
            self.speak(word)
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        question = "What were the four words?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("quasar", "tamarind", "ibex", "pewter"):
            self.assertIn(word, context)
        for distractor in ("1 is the score", "2 is the round", "3 is the level",
                           "4 is the turn"):
            self.assertNotIn(distractor, context)

    def test_later_complete_numbered_list_beats_older_teaching_group(self):
        self.speak("Remember these four words:")
        for word in ("quasar", "tamarind", "ibex", "pewter"):
            self.speak(word)
        for statement in ("1 is granite", "2 is kestrel", "3 is paprika",
                          "4 is turquoise"):
            self.speak(statement)
        for index in range(8):
            self.speak(f"Tell me a story about round {index}")
        question = "What were the four words?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("granite", "kestrel", "paprika", "turquoise"):
            self.assertIn(word, context)
        for old_word in ("quasar", "tamarind", "ibex", "pewter"):
            self.assertNotIn(old_word, context)

    def test_uncounted_recall_infers_unanchored_word_run(self):
        for word in ("acorn", "mesa", "ibis", "umber"):
            self.speak(word)
        for index in range(8):
            self.speak(f"Tell me a different story {index}")
        question = "Which words did I tell you?"
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("acorn", "mesa", "ibis", "umber"):
            self.assertIn(word, context)
        self.assertNotIn("different story", context)

    def test_counted_group_recall_survives_public_buffer_and_eight_fillers(self):
        for statement in ("1 is quartz", "2 is elm", "3 is saffron", "4 is orbit"):
            self.store.observe_user("alpha", "id:2", "Bex", statement)
        for index in range(8):
            self.store.observe_user("alpha", "id:2", "Bex", f"The dragon moves {index}")
        question = "What were the four words?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("quartz", "elm", "saffron", "orbit"):
            self.assertIn(word, context)
        self.assertNotIn("The dragon moves", context)

    def test_missing_personal_list_does_not_borrow_another_viewers_public_list(self):
        self.speak("How is the raid?", "Your four words were citrine, rook, fern, cobalt")
        for statement in ("1 is citrine", "2 is rook", "3 is fern",
                          "4 is cobalt"):
            self.store.observe_user("alpha", "id:2", "Bex", statement)
        self.store.observe_user("alpha", "id:2", "Bex", "The dragon boss is here")
        question = "What were my four words?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        context = self.store.context_for("alpha", "id:1", question)
        for word in ("citrine", "rook", "fern", "cobalt"):
            self.assertNotIn(word, context)
        self.assertIn("The dragon boss is here", context)
        self.assertEqual([], self.store.history_for("alpha", "id:1", query=question))

    def test_collective_and_natural_named_recall_use_public_speakers(self):
        for statement in ("Remember these four words:", "juniper", "lynx",
                          "amber", "estuary"):
            self.speak(statement)
        for statement in ("1 is basalt", "2 is egret", "3 is saffron",
                          "4 is aurora"):
            self.store.observe_user("alpha", "id:2", "Davi", statement)
        for question in ("What were the four words we said?",
                         "What did we say about the four words in the puzzle?",
                         "What were the four words in the puzzle we said?",
                         "What words did Davi send?",
                         "What were Davi's four words?"):
            with self.subTest(question=question):
                context = self.store.context_for("alpha", "id:1", question)
                for statement in ("1 is basalt", "2 is egret", "3 is saffron",
                                  "4 is aurora"):
                    self.assertIn(f'"Davi" said "{statement}"', context)
                for own in ("juniper", "lynx", "amber", "estuary"):
                    self.assertNotIn(own, context)
                self.assertEqual([], self.store.history_for(
                    "alpha", "id:1", query=question,
                ))

    def test_external_list_question_does_not_surface_private_chat_list(self):
        for statement in ("Remember these four words:", "tamarind", "kestrel",
                          "violet", "delta"):
            self.speak(statement)
        question = "What are the four words in this historic motto?"
        self.assertEqual("", self.store.context_for("alpha", "id:1", question))
        self.assertEqual([], self.store.history_for("alpha", "id:1", query=question))

    def test_own_list_recall_excludes_competing_public_list_and_assistant_guess(self):
        for statement in ("Remember these four words:", "maple", "falcon",
                          "ochre", "fjord"):
            self.speak(statement)
        for statement in ("1 is basalt", "2 is egret", "3 is turmeric",
                          "4 is pewter"):
            self.store.observe_user("alpha", "id:2", "Davi", statement)
        self.store.observe_user("alpha", "id:2", "Davi", "The dragon boss is here")
        self.speak("How is the raid?", "Those words were basalt, egret, turmeric, pewter")
        for index in range(8):
            self.speak(f"Tell me a new story {index}")

        for question in ("What were the four words?",
                         "What were my four words in reverse order please?",
                         "Can you list the four words I gave you in reverse order?"):
            with self.subTest(question=question):
                self.store.observe_user("alpha", "id:1", "Ada", question)
                context = self.store.context_for("alpha", "id:1", question)
                history = self.store.history_for("alpha", "id:1", query=question)
                for word in ("maple", "falcon", "ochre", "fjord"):
                    self.assertIn(word, context)
                for other_word in ("basalt", "egret", "turmeric", "pewter"):
                    self.assertNotIn(other_word, context + repr(history))
                self.assertIn("The dragon boss is here", context)
                self.assertEqual([], history)

        # Named, attributed public recall still works even when Ada has a list.
        question = "What were Davi's four words?"
        context = self.store.context_for("alpha", "id:1", question)
        for statement in ("1 is basalt", "2 is egret", "3 is turmeric",
                          "4 is pewter"):
            self.assertIn(f'"Davi" said "{statement}"', context)
        for own_word in ("maple", "falcon", "ochre", "fjord"):
            self.assertNotIn(own_word, context)
        self.assertEqual("", self.store.context_for(
            "alpha", "id:1", "What were Elio's four words?",
        ))
        self.assertEqual([], self.store.history_for(
            "alpha", "id:1", query="What were Elio's four words?",
        ))
        self.assertIn('"Davi" said "1 is basalt"', self.store.context_for(
            "alpha", "id:1", "What four words did Davi give us?",
        ))
        received = self.store.context_for(
            "alpha", "id:1", "What four words did Davi give me?",
        )
        self.assertIn('"Davi" said "1 is basalt"', received)
        self.assertNotIn("maple", received)
        self.assertEqual([], self.store.history_for(
            "alpha", "id:1", query="What four words did Davi give me?",
        ))
        given = self.store.context_for(
            "alpha", "id:1", "What four words did I give Davi?",
        )
        self.assertIn("maple", given)
        self.assertNotIn("basalt", given)

    def test_group_list_recall_uses_public_sources_not_private_own_list(self):
        for statement in ("Remember these four words:", "lichen", "kestrel",
                          "umber", "delta"):
            self.speak(statement)
        for statement in ("1 is granite", "2 is heron", "3 is paprika",
                          "4 is turquoise"):
            self.store.observe_user("alpha", "id:2", "Davi", statement)
        context = self.store.context_for("alpha", "id:1", "What were the group's four words?")
        for word in ("granite", "heron", "paprika", "turquoise"):
            self.assertIn(word, context)
        for word in ("lichen", "kestrel", "umber", "delta"):
            self.assertNotIn(word, context)
        for question in ("What were my group's four words?",
                         "What were the group words?"):
            with self.subTest(question=question):
                context = self.store.context_for("alpha", "id:1", question)
                for word in ("granite", "heron", "paprika", "turquoise"):
                    self.assertIn(word, context)
                for word in ("lichen", "kestrel", "umber", "delta"):
                    self.assertNotIn(word, context)
                self.assertEqual([], self.store.history_for(
                    "alpha", "id:1", query=question,
                ))

    def test_viewer_channel_isolation_and_public_banter(self):
        self.speak("My favorite food is curry", viewer="id:1", name="Pilot")
        self.speak("My favorite food is noodles", viewer="id:2", name="Pilot")
        self.speak("My favorite food is soup", viewer="id:1", name="Pilot", channel="beta")
        self.store.observe_user("alpha", "id:2", "Bex", "The dragon boss is here")
        self.store.observe_user("beta", "id:3", "Cal", "The beach boss is here")

        self.store.observe_user("alpha", "id:1", "Pilot", "What is my favorite food?")
        context = self.store.context_for("alpha", "id:1", "What is my favorite food?")
        self.assertIn("curry", context)
        self.assertIn("The dragon boss is here", context)
        self.assertNotIn("noodles", context)
        self.assertNotIn("soup", context)
        self.assertNotIn("beach boss", context)
        self.assertEqual(2, len(self.store.history_for("alpha", "id:1")))
        self.assertEqual(2, len(self.store.history_for("alpha", "id:2")))
        self.assertEqual(2, len(self.store.history_for("beta", "id:1")))

    def test_public_context_skips_personal_disclosures_and_explicitly_private_lines(self):
        self.store.observe_user("alpha", "id:2", "Bex", "I am married")
        self.store.observe_user("alpha", "id:2", "Bex", "Jane is married")
        self.store.observe_user("alpha", "id:2", "Bex", "Cal lives at 12 Orchid Lane")
        self.store.observe_user("alpha", "id:2", "Bex", "Cal at 12 Orchid St")
        self.store.observe_user("alpha", "id:2", "Bex", "Ava attends Maple School")
        self.store.observe_user("alpha", "id:2", "Bex", "Ava's name is Nora")
        self.store.observe_user("alpha", "id:2", "Bex", "The purple dragon appears", share_public=False)
        self.store.observe_user("alpha", "id:2", "Bex", "We can fight the boss")
        context = self.store.context_for("alpha", "id:1", "What is happening?")
        self.assertIn("Bex", context)
        self.assertIn("We can fight the boss", context)
        self.assertNotIn("married", context)
        self.assertNotIn("Orchid Lane", context)
        self.assertNotIn("Orchid St", context)
        self.assertNotIn("Maple School", context)
        self.assertNotIn("name is Nora", context)
        self.assertNotIn("purple dragon", context)
        self.assertIn("purple dragon", self.store.context_for("alpha", "id:2", "purple dragon"))

    def test_public_context_excludes_third_party_age_school_and_named_details(self):
        for statement in (
            "Kai is 7", "kai is 7", "Kai is sick", "Nora goes to Silverwood Elementary",
            "Mara's teacher is Jen", "Jules has a new address",
        ):
            self.store.observe_user("alpha", "id:2", "Bex", statement)
        self.store.observe_user("alpha", "id:2", "Bex", "The dragon boss is here")
        context = self.store.context_for("alpha", "id:1", "What is happening?")
        self.assertIn("The dragon boss is here", context)
        for detail in ("Kai", "Nora", "Silverwood", "Mara", "Jules"):
            self.assertNotIn(detail, context)

    def test_sensitive_child_and_third_party_details_never_enter_session(self):
        for statement in (
            "My son Kai is seven",
            "my daughter maya attends silverwood elementary",
            "Our kid's teacher is Jen",
            "my little one Kai likes games",
            "my 7yo Kai likes games",
            "my niece Rowan likes games",
            "Nora is in the hospital",
            "kai is sick",
        ):
            self.speak(statement, "I heard about Kai at Silverwood Elementary")
        self.speak("The dragon boss is here", "The boss is ready")
        self.speak("I have two kids", "ack")
        self.speak("The crystal gate is open", "Your child Kai attends Silverwood Elementary")
        raw_history = repr(self.store.history_for("alpha", "id:1"))
        broad = self.store.context_for("alpha", "id:1", "What do you remember about me?")
        for detail in ("Kai", "maya", "silverwood", "Jen", "Rowan", "Nora", "hospital"):
            self.assertNotIn(detail.casefold(), (raw_history + broad).casefold())
        self.assertIn("I have two kids", raw_history + broad)
        self.assertIn("The dragon boss is here", raw_history + broad)

    def test_bare_contact_and_health_details_do_not_enter_session(self):
        for statement in (
            "mira@example.invalid", "+1 (503) 555-0199", "My number is 5550123",
            "I have asthma",
            "I have celiac disease", "I am diabetic",
            "18 Cedar Street", "https://example.invalid/contact",
            "7 is kai's age", "7 is kai\u2019s age",
        ):
            self.speak(statement, "ack")
        self.speak("The dragon boss is here", "The boss is ready")
        self.speak("I have two kids", "ack")
        self.speak("The crystal gate is open", "Write me at mira@example.invalid")
        combined = repr(self.store.history_for("alpha", "id:1")) + self.store.context_for(
            "alpha", "id:1", "What do you remember about me?",
        )
        for detail in ("mira@example.invalid", "555-0199", "5550123", "asthma", "celiac",
                       "diabetic", "kai",
                       "Cedar Street", "example.invalid/contact"):
            self.assertNotIn(detail, combined.casefold())
        self.assertIn("The dragon boss is here", combined)
        self.assertIn("I have two kids", combined)

    def test_public_context_admits_shared_chat_but_not_lowercase_person_details(self):
        for statement in (
            "kai is sick", "KAI is sick", "kAi is sick",
            "nora goes to Silverwood Elementary",
            "the child maya has a fever",
            "We heard Kai is sick during the boss fight",
            "kai likes pizza",
            "the streamer kai likes pizza",
            "we saw kai eating pizza",
            "The dragon boss is here, kai likes pizza",
            "The dragon boss is here and kai likes pizza",
            "The dragon boss is here. kai likes pizza",
            "The dragon boss is here kai likes pizza",
            "7 is kai's age",
            "7 is kai\u2019s age",
        ):
            self.store.observe_user("alpha", "id:2", "Bex", statement)
        for statement in (
            "The dragon boss is here", "We can fight the boss",
            "The group puzzle token is copper",
        ):
            self.store.observe_user("alpha", "id:2", "Bex", statement)
        context = self.store.context_for("alpha", "id:1", "What is happening?")
        for safe in ("The dragon boss is here", "We can fight the boss",
                     "The group puzzle token is copper"):
            self.assertIn(safe, context)
        for detail in ("kai", "nora", "maya", "Silverwood", "pizza"):
            self.assertNotIn(detail.casefold(), context.casefold())

    def test_public_context_keeps_novel_group_word_without_personal_tail(self):
        for statement in (
            "7 is jasper", "The crystal gate is open", "GG everyone",
            "The crystal gate is open. mira likes tea",
            "The crystal gate is open mira likes tea",
        ):
            self.store.observe_user("alpha", "id:2", "Bex", statement)
        context = self.store.context_for("alpha", "id:1", "What is happening at the gate?")
        self.assertIn("7 is jasper", context)
        self.assertIn("The crystal gate is open", context)
        self.assertIn("GG everyone", context)
        self.assertNotIn("mira", context)
        self.assertNotIn("tea", context)

    def test_public_banter_ranks_relevant_older_line_above_newer_filler(self):
        self.store.observe_user("alpha", "id:1", "Ada", "The group puzzle token is copper")
        for index in range(6):
            self.store.observe_user("alpha", "id:2", "Cal", f"The dragon boss moves {index}")
        question = "What is the group puzzle token?"
        self.store.observe_user("alpha", "id:3", "Bex", question)
        context = self.store.context_for("alpha", "id:3", question)
        self.assertIn("Ada", context)
        self.assertIn("The group puzzle token is copper", context)

    def test_group_note_remains_available_to_its_author(self):
        self.store.observe_user("alpha", "id:1", "Ada", "Remember the group puzzle token is copper")
        question = "What is the group puzzle token?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        self.assertIn("copper", self.store.context_for("alpha", "id:1", question))

    def test_general_topic_omits_own_birthday_from_context_and_role_history(self):
        self.speak("My birthday is May 4", "Your birthday is May 4")
        self.speak("What did I tell you?", "May 4")
        self.speak("The dragon boss is here", "The dragon is ready")
        question = "Medlar, do you like birthday parties?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        self.assertNotIn("May 4", self.store.context_for("alpha", "id:1", question))
        history = self.store.history_for("alpha", "id:1", query=question)
        self.assertNotIn("May 4", repr(history))
        self.assertIn("The dragon boss is here", repr(history))

    def test_color_keyword_in_general_question_does_not_expose_private_favorite(self):
        self.speak("My favorite color is cobalt", "You said cobalt")
        question = "Medlar, what color should the dragon boss be?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        self.assertNotIn("cobalt", self.store.context_for("alpha", "id:1", question))
        self.assertNotIn("cobalt", repr(self.store.history_for("alpha", "id:1", query=question)))
        self.assertIn("cobalt", self.store.context_for(
            "alpha", "id:1", "What is my favorite color?",
        ))

    def test_direct_birth_question_can_use_own_session_evidence(self):
        self.speak("My birthday is June 8, 1996")
        self.assertIn("June 8, 1996", self.store.context_for(
            "alpha", "id:1", "What year was I born?",
        ))

    def test_explicit_birth_year_is_hidden_from_unrelated_context(self):
        self.speak("I was born in 1996", "You said 1996")
        self.speak("The dragon boss is here", "The boss is ready")
        unrelated = "How is the dragon boss?"
        combined = self.store.context_for("alpha", "id:1", unrelated) + repr(
            self.store.history_for("alpha", "id:1", query=unrelated),
        )
        self.assertNotIn("1996", combined)
        self.assertIn("I was born in 1996", self.store.context_for(
            "alpha", "id:1", "What year was I born?",
        ))

    def test_single_status_is_hidden_from_unrelated_context(self):
        self.speak("I am single", "You said single")
        self.speak("The dragon boss is here", "The boss is ready")
        unrelated = "How is the dragon boss?"
        combined = self.store.context_for("alpha", "id:1", unrelated) + repr(
            self.store.history_for("alpha", "id:1", query=unrelated),
        )
        self.assertNotIn("single", combined)
        self.assertIn("I am single", self.store.context_for(
            "alpha", "id:1", "What is my marital status?",
        ))

    def test_numeric_age_stays_private_until_direct_self_lookup(self):
        for report in ("I am 30", "I turn 31 today", "I turned 32 on June 8"):
            self.speak(report, "You gave an age")
        question = "How is the dragon fight?"
        combined = self.store.context_for("alpha", "id:1", question) + repr(
            self.store.history_for("alpha", "id:1", query=question),
        )
        for report in ("I am 30", "I turn 31 today", "I turned 32 on June 8"):
            self.assertNotIn(report, combined)
        self.assertIn("I turned 32 on June 8", self.store.context_for(
            "alpha", "id:1", "How old am I?",
        ))

    def test_recall_about_one_topic_does_not_unlock_other_personal_facts(self):
        self.speak("My birthday is June 8", "You said June 8")
        self.speak("My favorite food is tamales", "You said tamales")
        self.speak("My favorite color is indigo", "You said indigo")
        self.speak("The dragon boss is here", "The dragon is ready")
        question = "What did I say about the dragon boss?"
        self.store.observe_user("alpha", "id:1", "Ada", question)
        combined = self.store.context_for("alpha", "id:1", question) + repr(
            self.store.history_for("alpha", "id:1", query=question),
        )
        self.assertIn("dragon boss", combined)
        for secret in ("June 8", "tamales", "indigo"):
            self.assertNotIn(secret, combined)
        color_question = "What is my favorite color?"
        color_combined = self.store.context_for("alpha", "id:1", color_question) + repr(
            self.store.history_for("alpha", "id:1", query=color_question),
        )
        self.assertIn("indigo", color_combined)
        self.assertNotIn("June 8", color_combined)
        self.assertNotIn("tamales", color_combined)

    def test_general_recall_with_personal_keyword_does_not_unlock_fact(self):
        self.speak("My birthday is June 8", "You said June 8")
        self.speak("My favorite color is indigo", "You said indigo")
        for question in (
            "What did I say about birthday parties?",
            "What did I say about the dragon's favorite color?",
        ):
            combined = self.store.context_for("alpha", "id:1", question) + repr(
                self.store.history_for("alpha", "id:1", query=question),
            )
            self.assertNotIn("June 8", combined, question)
            self.assertNotIn("indigo", combined, question)
        self.assertIn("June 8", self.store.context_for(
            "alpha", "id:1", "What is my birthday?",
        ))

    def test_value_only_assistant_recap_does_not_surface_private_fact(self):
        self.speak("My birthday is June 8", "You said June 8")
        self.speak("Can you help with the dragon?", "June 8")
        self.speak("The dragon boss is here", "Ready for the boss")
        history = self.store.history_for(
            "alpha", "id:1", query="How is the dragon boss?",
        )
        self.assertNotIn("June 8", repr(history))
        self.assertIn("The dragon boss is here", repr(history))

    def test_unrelated_chat_does_not_retrieve_private_self_report(self):
        self.speak("My favorite color is cobalt")
        self.store.observe_user("alpha", "id:1", "Ada", "How is the dragon boss fight?")
        context = self.store.context_for("alpha", "id:1", "How is the dragon boss fight?")
        self.assertNotIn("cobalt", context)
        self.assertIn("cobalt", self.store.context_for(
            "alpha", "id:1", "What favorite color did I tell you?",
        ))

    def test_contact_number_label_with_split_digits_is_never_retained(self):
        message = "My contact number: 55 50123"
        self.store.observe_user("alpha", "id:1", "Ada", message)
        self.assertEqual([], self.store.history_for("alpha", "id:1"))
        self.assertNotIn(message, self.store.context_for(
            "alpha", "id:1", "What did I say?",
        ))

    def test_contact_health_and_name_requests_never_enter_session(self):
        excluded = (
            "Pizza at 17 Oak Place", "Meet at 8 Birch Terrace",
            "Pizza at 10 Maple Row", "Meet near 21 Cedar Mews",
            "Pizza 123 Elm Parade", "Visit example dot app",
            "Parcel for 12 Oak Apt 4",
            "Visit example.com", "My game site is maps.example.net",
            "Visit example dot com",
            "I have a migraine", "I had a headache yesterday",
            "I have high blood pressure", "I have hypertension",
            "I have a heart condition", "I live with a medical condition",
            "I have a respiratory disorder", "I have a chronic disease",
            "I am dealing with vertigo", "I feel dizzy today",
            "I had surgery", "I broke my ankle",
            "My health has been rough", "Call me Vesper",
            "You can call me Rowan",
            "I prefer to be called Quill", "They call me Cedar",
        )
        for message in excluded:
            with self.subTest(message=message):
                self.speak(message)
                self.assertNotIn(message, repr(self.store.history_for(
                    "alpha", "id:1", query="How is the dragon raid?",
                )))
                self.assertNotIn(message, self.store.context_for(
                    "alpha", "id:1", "How is the dragon raid?",
                ))
        self.assertEqual([], self.store.history_for("alpha", "id:1"))
        self.speak("I have the key")
        self.speak("The win condition is clear")
        self.store.observe_user("alpha", "id:2", "Bex", "The dragon boss is here")
        self.assertIn("I have the key", repr(self.store.history_for("alpha", "id:1")))
        self.assertIn("The win condition is clear", repr(self.store.history_for("alpha", "id:1")))
        self.assertIn("The dragon boss is here", self.store.context_for(
            "alpha", "id:1", "How is the dragon boss?",
        ))

    def test_forget_removes_only_viewers_own_public_snippets(self):
        self.speak("My favorite food is curry")
        self.store.observe_user("alpha", "id:1", "Ada", "The red dragon is here")
        self.store.observe_user("alpha", "id:2", "Bex", "The dragon boss is here")
        self.store.forget_viewer("alpha", "id:1")
        self.assertEqual([], self.store.history_for("alpha", "id:1"))
        context = self.store.context_for("alpha", "id:1", "What happened to the dragon?")
        self.assertNotIn("curry", context)
        self.assertNotIn("red dragon", context)
        self.assertIn("The dragon boss is here", context)
        self.store.complete_reply("alpha", "id:1", "Ada", "My favorite food is curry", "late reply")
        self.assertEqual([], self.store.history_for("alpha", "id:1"))
        self.store.observe_user("alpha", "id:2", "Bex", "My favorite color is blue")
        self.assertIn("blue", self.store.context_for("alpha", "id:2", "What is my favorite color?"))

    def test_receive_order_is_kept_if_distinct_replies_complete_out_of_order(self):
        self.store.observe_user("alpha", "id:1", "Ada", "first message")
        self.store.observe_user("alpha", "id:1", "Ada", "second message")
        self.store.complete_reply("alpha", "id:1", "Ada", "second message", "second reply")
        self.store.complete_reply("alpha", "id:1", "Ada", "first message", "first reply")
        self.assertEqual(
            ["Ada: first message", "first reply", "Ada: second message", "second reply"],
            [item["content"] for item in self.store.history_for("alpha", "id:1")],
        )

    def test_bounds_and_ttls(self):
        store = ConversationContextStore(
            max_history_messages=10, max_user_statements=3,
            max_context_chars=300, evidence_ttl=timedelta(hours=1),
            public_ttl=timedelta(minutes=1),
        )
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        store.observe_user("alpha", "id:1", "Ada", "Old private item", now=old)
        store.observe_user("alpha", "id:2", "Bex", "Old public item", now=old)
        for index in range(7):
            message = f"Round {index} token " + "x" * 300
            store.observe_user("alpha", "id:1", "Ada", message)
            store.complete_reply("alpha", "id:1", "Ada", message, "ack")
        self.assertEqual(10, len(store.history_for("alpha", "id:1")))
        context = store.context_for("alpha", "id:1", "round token")
        self.assertLessEqual(len(context), 300)
        self.assertNotIn("Old private item", context)
        self.assertNotIn("Old public item", context)
        self.assertNotIn("Round 0 token", context)

    def test_newer_correction_survives_context_character_budget(self):
        store = ConversationContextStore(max_context_chars=500)
        for index in range(5):
            message = f"My favorite color was shade{index} " + "x" * 150
            store.observe_user("alpha", "id:1", "Ada", message)
            store.complete_reply("alpha", "id:1", "Ada", message, "ack")
        store.observe_user("alpha", "id:1", "Ada", "Correction: my favorite color is cobalt")
        store.observe_user("alpha", "id:1", "Ada", "What is my favorite color?")

        context = store.context_for("alpha", "id:1", "What is my favorite color?")
        self.assertLessEqual(len(context), 500)
        self.assertIn("Correction: my favorite color is cobalt", context)
        self.assertNotIn('"What is my favorite color?"', context)

    def test_missing_scope_and_commands_do_not_create_shared_state(self):
        self.store.observe_user("", "", "Unknown", "My favorite is red")
        self.store.observe_user("alpha", "id:1", "Ada", "!ping")
        self.assertEqual("", self.store.context_for("alpha", "id:1", "favorite?"))
        self.assertEqual([], self.store.history_for("alpha", "id:1"))


if __name__ == "__main__":
    unittest.main()
