"""Bounded B2 fixes, using synthetic statements and disposable profile stores."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from MedlarTV.core import conversation_context as context
from MedlarTV.core import interaction_logger as logger
from MedlarTV.core import viewer_profiles as profiles
from MedlarTV.core.conversation_context import ConversationContextStore
from MedlarTV.core.viewer_profiles import ViewerProfileStore
import test_twitch_memory_wiring as wiring


DETAILS = (
    'I have lupus', 'I have HIV', 'I have bipolar disorder', 'I have diabetes',
    'My real name is Elara Voss', 'My email is fictional@example.invalid',
    'My phone number is 202-555-0147', 'My address is 17 Fictional Lane',
)


class MemoryPrivacyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'profiles.sqlite3'
        self.profiles = ViewerProfileStore(self.path)
        self.addCleanup(self.profiles.close)

    def test_known_sensitive_statements_never_enter_session_evidence(self):
        for message in DETAILS:
            with self.subTest(message=message):
                store = ConversationContextStore()
                store.observe_user('alpha', 'id:1', 'Ada', message)
                store.complete_reply('alpha', 'id:1', 'Ada', message, 'Acknowledged')
                self.assertEqual({}, store._viewers)
                self.assertEqual({}, store._public)
                self.assertEqual([], store.history_for('alpha', 'id:1'))

    def test_sensitive_tail_is_checked_before_user_storage_truncation(self):
        for detail in DETAILS:
            store = ConversationContextStore()
            message = 'The dragon is ' + 'bright' * 50 + '. ' + detail
            self.assertGreater(message.index(detail), 240)
            store.observe_user('alpha', 'id:1', 'Ada', message)
            self.assertEqual({}, store._viewers, detail)
            self.assertEqual({}, store._public, detail)

    def test_sensitive_tail_is_checked_after_whitespace_normalization(self):
        store = ConversationContextStore()
        message = ('The dragon is bright ' * 20) + '\nI\thave\nHIV'
        store.observe_user('alpha', 'id:1', 'Ada', message)
        self.assertEqual({}, store._viewers)

    def test_sensitive_answer_tail_is_checked_before_reply_truncation(self):
        for detail in DETAILS:
            store = ConversationContextStore()
            message = 'The dragon is bright'
            store.observe_user('alpha', 'id:1', 'Ada', message)
            reply = 'A fine game. ' * 100 + detail
            store.complete_reply('alpha', 'id:1', 'Ada', message, reply)
            self.assertEqual([], store.history_for('alpha', 'id:1'), detail)
            self.assertIn(message, store.context_for('alpha', 'id:1', 'What about the dragon?'))

    def test_sensitive_completion_cannot_reuse_safe_truncated_prefix(self):
        store = ConversationContextStore()
        prefix = 'x' * 240
        store.observe_user('alpha', 'id:1', 'Ada', prefix)
        store.complete_reply('alpha', 'id:1', 'Ada', prefix + 'I have HIV', 'Acknowledged')
        self.assertEqual([], store.history_for('alpha', 'id:1'))

    def test_public_banter_uses_whole_source_not_a_truncated_prefix(self):
        store = ConversationContextStore()
        message = 'The dragon is ' + 'a' * 250 + ' and my favorite color is blue'
        store.observe_user('alpha', 'id:1', 'Ada', message)
        self.assertTrue(store._viewers)
        self.assertEqual({}, store._public)
        self.assertEqual('', store.context_for('alpha', 'id:2', 'What about the dragon?'))

    def test_retrieval_rechecks_legacy_sensitive_user_evidence(self):
        for detail in DETAILS:
            store = ConversationContextStore()
            with patch.object(context, '_sensitive_session_detail', return_value=False):
                store.observe_user('alpha', 'id:1', 'Ada', detail)
                store.complete_reply('alpha', 'id:1', 'Ada', detail, 'Acknowledged')
            self.assertTrue(store._viewers)
            for query in (None, 'How is the game?', 'What do you remember about me?'):
                self.assertEqual([], store.history_for('alpha', 'id:1', query=query))
            self.assertEqual('', store.context_for('alpha', 'id:1', 'What do you remember about me?'))

    def test_retrieval_rechecks_legacy_sensitive_assistant_answers(self):
        for detail in DETAILS:
            store = ConversationContextStore()
            with patch.object(context, '_sensitive_session_detail', return_value=False):
                store.observe_user('alpha', 'id:1', 'Ada', 'The dragon is bright')
                store.complete_reply('alpha', 'id:1', 'Ada', 'The dragon is bright', detail)
            self.assertEqual([], store.history_for('alpha', 'id:1'))
            self.assertEqual([], store.history_for('alpha', 'id:1', query='How is the game?'))

    def test_retrieval_rechecks_legacy_public_entries_for_other_viewers(self):
        store = ConversationContextStore()
        with patch.object(context, '_sensitive_session_detail', return_value=False), \
                patch.object(ConversationContextStore, '_public_banter', return_value=True):
            store.observe_user('alpha', 'id:1', 'Ada', 'I have HIV')
        self.assertTrue(store._public)
        self.assertEqual('', store.context_for('alpha', 'id:2', 'What did Ada say?'))

    def test_ordinary_game_interest_and_favorite_controls_survive(self):
        store = ConversationContextStore()
        for text in ('The dragon is bright', 'I enjoy playing chess', 'My favorite food is noodles'):
            store.observe_user('alpha', 'id:1', 'Ada', text)
            store.complete_reply('alpha', 'id:1', 'Ada', text, 'That sounds fun')
        self.assertEqual(6, len(store.history_for('alpha', 'id:1')))
        self.assertIn('dragon', store.context_for('alpha', 'id:2', 'How is the dragon?'))
        self.assertNotIn('noodles', store.context_for('alpha', 'id:2', 'What is their favorite food?'))
        self.assertEqual('saved', self.profiles.observe('alpha', '100', 'My favorite food is noodles').kind)

    def test_supported_health_is_still_automatic_persistent_and_direct_only(self):
        self.assertEqual('saved', self.profiles.observe('alpha', '100', 'I have diabetes').kind)
        self.profiles.close()
        with ViewerProfileStore(self.path) as reopened:
            self.assertIn('diabetes', reopened.get_answer('alpha', '100', 'What is my health condition?'))
            for query in (None, 'How are you?', 'What do you remember about me?', 'What is my health condition?'):
                self.assertNotIn('diabetes', reopened.format_profile_context('alpha', '100', query=query))
            self.assertIsNone(reopened.get_answer('alpha', '200', 'What is my health condition?'))
            self.assertIsNone(reopened.get_answer('beta', '100', 'What is my health condition?'))

    def test_direct_name_recall_remains_separate_from_general_context(self):
        self.assertEqual('saved', self.profiles.observe('alpha', '100', 'My real name is Elara Voss').kind)
        self.assertIn('Elara Voss', self.profiles.get_answer('alpha', '100', 'What is my real name?'))
        for query in (None, 'Hello again', 'What do you remember about me?', 'What is my real name?'):
            self.assertNotIn('Elara', self.profiles.format_profile_context('alpha', '100', query=query))
        self.assertIsNone(self.profiles.get_answer('alpha', '200', 'What is my real name?'))

    def test_contact_taxonomy_does_not_enable_contact_collection(self):
        for text in DETAILS[5:]:
            self.assertEqual('ignored', self.profiles.observe('alpha', '100', text).kind)
        self.assertEqual({}, self.profiles.get_facts('alpha', '100'))
        self.assertFalse(self.path.exists())

    def test_sensitive_details_cannot_hide_inside_favorite_values(self):
        for suffix in ('lupus', 'HIV', 'bipolar disorder', 'fictional@example.invalid', '202-555-0147'):
            self.assertEqual('ignored', self.profiles.observe('alpha', '100', 'My favorite food is ' + suffix).kind)
        self.assertFalse(self.path.exists())

    def test_parenthood_article_a_means_one_without_child_details(self):
        for text in ('I have a child', 'I have a kid'):
            self.assertEqual('saved', self.profiles.observe('alpha', '100', text).kind)
            facts = self.profiles.get_facts('alpha', '100')
            self.assertEqual({'parenthood', 'children_count'}, set(facts))
            self.assertEqual('yes', facts['parenthood'].value)
            self.assertEqual('1', facts['children_count'].value)
        self.assertEqual('saved', self.profiles.observe('alpha', '100', 'I have no children').kind)
        self.assertEqual({'parenthood'}, set(self.profiles.get_facts('alpha', '100')))
        self.assertEqual('no', self.profiles.get_facts('alpha', '100')['parenthood'].value)

    def test_parenthood_rejects_child_identity_quotes_jokes_and_third_party(self):
        for text in ('I have a child named Kai', 'I have a child aged 7',
                     'I have a child at Fictional School', 'My friend has a child',
                     '"I have a child"', 'I have a child, just kidding',
                     'If I have a child', 'I have a child. My favorite food is noodles'):
            self.assertEqual('ignored', self.profiles.observe('alpha', '100', text).kind, text)
        self.assertFalse(self.path.exists())

    def test_parenthood_observation_respects_capacity_and_scoped_forget(self):
        with patch.object(profiles, 'MAX_FACTS_PER_VIEWER', 1):
            self.assertEqual('limit_reached', self.profiles.observe('alpha', '100', 'I have a child').kind)
            self.assertEqual({}, self.profiles.get_facts('alpha', '100'))
        self.profiles.observe('alpha', '100', 'I have a child')
        self.profiles.observe('alpha', '200', 'I have two children')
        self.assertEqual(2, self.profiles.forget('alpha', '100', 'parenthood'))
        self.assertEqual({}, self.profiles.get_facts('alpha', '100'))
        self.assertEqual('2', self.profiles.get_facts('alpha', '200')['children_count'].value)

    def test_interaction_log_keeps_no_freeform_sensitive_values(self):
        with patch.object(logger, 'ENABLE_LOGGING', True), patch.object(logger, '_append_jsonl') as append:
            for text in DETAILS:
                logger.log_interaction('Ada', text, text, context={'raw': text})
                entry = append.call_args.args[1]
                self.assertIsNone(entry['message'])
                self.assertIsNone(entry['response'])
                self.assertNotIn(text, json.dumps(entry))

    def test_listener_unrelated_greeting_has_no_retained_sensitive_details(self):
        harness = wiring.TwitchMemoryWiringTests('runTest')
        harness.setUp()
        self.addCleanup(harness.tearDown)
        for text in DETAILS:
            harness.chat('Ada', text)
        harness.generate_response.reset_mock()
        harness.chat('Ada', 'Medlar, how is the game today?')
        kwargs = harness.generate_response.call_args.kwargs
        retained = json.dumps({'history': kwargs['history'], 'context': kwargs['memory_context']})
        for value in ('HIV', 'lupus', 'bipolar', 'diabetes', 'Elara', 'example.invalid', '202-555-0147', 'Fictional Lane'):
            self.assertNotIn(value, retained)
        harness.chat('Bex', 'Medlar, what do you remember about me?', user_id='200')
        self.assertNotIn('Elara', json.dumps(harness.generate_response.call_args.kwargs))
        self.assertNotIn('diabetes', json.dumps(harness.generate_response.call_args.kwargs))


if __name__ == '__main__':
    unittest.main()
