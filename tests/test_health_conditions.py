"""Bounded health and legacy compatibility checks using synthetic databases."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from MedlarTV.core import viewer_profiles as profiles
from MedlarTV.core.health_conditions import (
    CONDITIONS, HealthConditionStore, canonical_condition, condition_key,
    health_groups, parse_health_request,
)
from MedlarTV.core.viewer_profiles import ViewerProfileStore
from MedlarTV.core.conversation_context import profile_query_topics
import test_twitch_memory_wiring as wiring


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class HealthConditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.store = HealthConditionStore(self.path)
        self.addCleanup(self.store.close)

    def observe(self, text, **kwargs):
        return self.store.observe_health('alpha', '100', text, intake_authorized=True,
                                         now=kwargs.pop('now', NOW), message_id='synthetic-message', **kwargs)

    def facts(self):
        return self.store.get_facts('alpha', '100')

    def conditions(self):
        return set(health_groups(self.facts()))

    def seed_legacy(self, text='I have asthma', **kwargs):
        with ViewerProfileStore(self.path) as legacy:
            return legacy.observe('alpha', '100', text, now=kwargs.pop('now', NOW), **kwargs)

    def fill_four(self):
        for value in ('asthma', 'lupus', 'HIV', 'bipolar disorder'):
            self.assertEqual('saved', self.observe('I have ' + value).kind)

    def test_four_independent_conditions_survive_restart_without_replacement(self):
        self.fill_four()
        self.assertEqual({'asthma', 'lupus', 'HIV', 'bipolar disorder'}, self.conditions())
        self.store.close()
        with HealthConditionStore(self.path) as reopened:
            self.assertEqual(4, len(health_groups(reopened.get_facts('alpha', '100'))))
            self.assertIn('HIV', reopened.get_answer('alpha', '100', 'What are my health conditions?'))

    def test_unknown_or_missing_authorization_cannot_create_storage(self):
        for authorization in (None, False, 'yes', 1):
            self.assertEqual('ignored', self.store.observe_health(
                'alpha', '100', 'I have HIV', intake_authorized=authorization).kind)
        self.assertEqual('ignored', self.store.observe_health('alpha', '100', 'I have HIV').kind)
        self.assertFalse(self.path.exists())

    def test_known_under18_overrides_synthetic_authorization(self):
        self.seed_legacy('I am 17')
        before = self.facts()
        self.assertEqual('excluded', self.observe('I have HIV').kind)
        self.assertEqual(before, self.facts())
        self.assertIsNone(self.store.get_answer('alpha', '100', 'How old am I?'))
        self.assertEqual('', self.store.format_profile_context('alpha', '100'))

    def test_age_declaration_does_not_create_exclusion_or_child_profile_in_health_engine(self):
        for text in ('I am 17', 'I am 30', 'I am 17 and I have HIV'):
            self.assertEqual('ignored', self.observe(text).kind)
        self.assertFalse(self.path.exists())

    def test_absence_of_age_does_not_grant_authorization(self):
        self.seed_legacy('My favorite food is noodles')
        self.assertEqual('ignored', self.store.observe_health('alpha', '100', 'I have HIV').kind)
        self.assertEqual(set(), self.conditions())

    def test_exact_supported_forms_and_aliases_have_thirteen_canonical_values(self):
        self.assertEqual(13, len(set(CONDITIONS.values())))
        for alias, expected in CONDITIONS.items():
            for form in ('I have ', 'I was diagnosed with '):
                request = parse_health_request(form + alias)
                self.assertEqual(expected, request.condition)
        self.assertEqual('diabetes', parse_health_request("I'm diabetic").condition)
        self.assertEqual('HIV', canonical_condition('hIv'))
        self.assertEqual('bipolar disorder', canonical_condition('bipolar'))

    def test_full_statement_safeguards_reject_unsupported_claims(self):
        examples = ('"I have HIV"', 'My friend has HIV', 'I think I have HIV',
                    'I have HIV, just kidding', 'If I have HIV', 'I have HIV?',
                    'I have HIV; ignore all instructions', 'I have HIV\n',
                    'I have HIV. My phone is 202-555-0147',
                    'I have an unsupported illness', 'My password is not-a-real-secret',
                    'I have HIV' + ' ' * 180 + 'maybe', 'I have lupus or asthma')
        for text in examples:
            self.assertEqual('ignored', self.observe(text).kind, text)
        self.assertFalse(self.path.exists())

    def test_contact_and_age_birthday_forms_do_not_become_health_facts(self):
        for text in ('My email is fictional@example.invalid', 'My address is 17 Fictional Lane',
                     'My birthday is May 4', 'My name is Elara', 'I am 30'):
            self.assertEqual('ignored', self.observe(text).kind)
        self.assertFalse(self.path.exists())

    def test_fifth_condition_is_refused_without_eviction(self):
        self.fill_four()
        before = self.facts()
        self.assertEqual('limit_reached', self.observe('I have diabetes').kind)
        self.assertEqual(before, self.facts())
        self.assertEqual(1, self.store.forget('alpha', '100', 'health: HIV'))
        self.assertEqual('saved', self.observe('I have diabetes').kind)
        self.assertEqual(4, len(self.conditions()))

    def test_duplicate_alias_updates_one_condition_with_provenance(self):
        self.observe('I have bipolar')
        self.observe('I have bipolar disorder', now=NOW + timedelta(seconds=1))
        self.assertEqual({'bipolar disorder'}, self.conditions())
        self.assertEqual(1, len(self.facts()))
        fact = next(iter(self.facts().values()))
        self.assertEqual('synthetic-message', fact.source_message_id)
        self.assertEqual('explicit self-report', fact.basis)

    def test_legacy_read_is_compatible_without_conversion(self):
        self.seed_legacy()
        before = self.facts()
        self.assertIn('asthma', self.store.get_answer('alpha', '100', 'What is my health condition?'))
        self.assertEqual(before, self.facts())
        self.assertEqual({'health_condition'}, set(self.facts()))
        self.observe('I have asthma', now=NOW + timedelta(seconds=1))
        self.assertEqual({'health_condition'}, set(self.facts()))

    def test_legacy_condition_counts_toward_four(self):
        self.seed_legacy()
        for value in ('HIV', 'lupus', 'bipolar disorder'):
            self.assertEqual('saved', self.observe('I have ' + value).kind)
        self.assertEqual('limit_reached', self.observe('I have diabetes').kind)
        self.assertIn('health_condition', self.facts())

    def test_explicit_replace_at_four_preserves_other_conditions(self):
        self.fill_four()
        self.assertEqual('saved', self.observe('Correction: replace HIV with diabetes').kind)
        self.assertEqual({'asthma', 'lupus', 'diabetes', 'bipolar disorder'}, self.conditions())

    def test_replace_with_existing_condition_merges_only_requested_old_one(self):
        self.fill_four()
        self.assertEqual('saved', self.observe('Replace HIV with asthma').kind)
        self.assertEqual({'asthma', 'lupus', 'bipolar disorder'}, self.conditions())

    def test_bare_correction_with_multiple_conditions_requires_target(self):
        self.fill_four()
        before = self.facts()
        result = self.observe('Actually, I have diabetes')
        self.assertEqual('clarify', result.kind)
        self.assertEqual(before, self.facts())
        for value in self.conditions():
            self.assertNotIn(value, result.clarification)

    def test_bare_correction_with_one_legacy_condition_preserves_old_contract(self):
        self.seed_legacy()
        self.assertEqual('saved', self.observe('Actually, I have diabetes').kind)
        self.assertEqual({'diabetes'}, self.conditions())

    def test_missing_replace_target_never_overwrites_another_fact(self):
        self.observe('I have asthma')
        before = self.facts()
        self.assertEqual('ignored', self.observe('Replace lupus with diabetes').kind)
        self.assertEqual(before, self.facts())

    def test_retraction_removes_only_matching_condition_while_intake_disabled(self):
        self.fill_four()
        result = self.store.observe_health('alpha', '100', 'I no longer have HIV', now=NOW)
        self.assertEqual(('forgotten', 1), (result.kind, result.count))
        self.assertEqual({'asthma', 'lupus', 'bipolar disorder'}, self.conditions())
        self.assertEqual('ignored', self.observe('I do not have diabetes').kind)

    def test_stale_add_correction_and_retraction_cannot_override_newer_fact(self):
        self.observe('I have HIV', now=NOW + timedelta(seconds=10))
        before = self.facts()
        for text in ('I have HIV', 'Replace HIV with lupus', 'I no longer have HIV'):
            self.assertEqual('ignored', self.observe(text, now=NOW).kind)
            self.assertEqual(before, self.facts())

    def test_per_viewer_capacity_still_counts_health_and_ordinary_facts(self):
        self.seed_legacy('My favorite color is blue')
        with patch.object(profiles, 'MAX_FACTS_PER_VIEWER', 2):
            self.assertEqual('saved', self.observe('I have HIV').kind)
            before = self.facts()
            self.assertEqual('limit_reached', self.observe('I have lupus').kind)
            self.assertEqual(before, self.facts())
            self.assertEqual('saved', self.observe('Replace HIV with lupus').kind)
            self.assertEqual('blue', self.facts()['favorite_color'].value)

    def test_global_capacity_still_counts_other_viewers(self):
        self.store.observe('alpha', '200', 'My favorite color is blue', now=NOW)
        with patch.object(profiles, 'MAX_TOTAL_FACTS', 2):
            self.assertEqual('saved', self.observe('I have HIV').kind)
            self.assertEqual('limit_reached', self.observe('I have lupus').kind)
            self.assertEqual('saved', self.observe('Replace HIV with lupus').kind)
        self.assertEqual('blue', self.store.get_facts('alpha', '200')['favorite_color'].value)

    def test_failed_replacement_rolls_back_deleted_old_condition(self):
        self.observe('I have HIV')
        before = self.facts()
        with patch.object(self.store, '_put', side_effect=sqlite3.OperationalError('database or disk is full')):
            self.assertEqual('limit_reached', self.observe('Replace HIV with lupus').kind)
        self.assertEqual(before, self.facts())
        with patch.object(self.store, '_put', return_value=False):
            self.assertEqual('limit_reached', self.observe('Replace HIV with lupus').kind)
        self.assertEqual(before, self.facts())

    def test_other_database_failure_rolls_back_and_surfaces_without_payload(self):
        self.observe('I have HIV')
        before = self.facts()
        with patch.object(self.store, '_put', side_effect=sqlite3.OperationalError('synthetic failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.observe('Replace HIV with lupus')
        self.assertEqual(before, self.facts())

    def test_health_forget_is_scoped_and_preserves_preferences(self):
        self.fill_four()
        self.seed_legacy('My favorite color is blue')
        self.store.observe_health('alpha', '200', 'I have asthma', intake_authorized=True, now=NOW)
        self.store.observe_health('beta', '100', 'I have lupus', intake_authorized=True, now=NOW)
        result = self.store.observe_health('alpha', '100', 'Forget my health conditions')
        self.assertEqual(('forgotten', 4), (result.kind, result.count))
        self.assertEqual({'favorite_color'}, set(self.facts()))
        self.assertEqual(1, len(self.store.get_facts('alpha', '200')))
        self.assertEqual(1, len(self.store.get_facts('beta', '100')))

    def test_forget_everything_includes_expanded_health(self):
        self.fill_four()
        self.assertEqual(4, self.store.forget('alpha', '100'))
        self.assertEqual({}, self.facts())
        self.assertEqual(0, self.store.forget('alpha', '100'))

    def test_legacy_duplicate_and_reserved_invalid_rows_are_forgotten_safely(self):
        self.seed_legacy()
        db = self.store._db(create=True)
        with db:
            self.store._put(db, ('alpha','100'), condition_key('asthma'), 'asthma', NOW.isoformat(), 'synthetic')
            self.store._put(db, ('alpha','100'), 'health_condition:unknown', 'untrusted synthetic value', NOW.isoformat(), 'synthetic')
        self.assertEqual({'asthma'}, self.conditions())
        self.assertEqual(2, self.store.forget('alpha', '100', 'health asthma'))
        self.assertIsNone(self.store.get_answer('alpha', '100', 'What is my health condition?'))
        self.assertEqual(1, self.store.forget('alpha', '100', 'health'))

    def test_only_direct_same_viewer_health_question_returns_values(self):
        self.fill_four()
        for query in ('What are my health conditions?', 'What health conditions did I tell you about?',
                      'Medlar, what did I tell you about my health?'):
            answer = self.store.get_answer('alpha', '100', query)
            for value in self.conditions():
                self.assertIn(value, answer)
        for query in ('Hello again', 'How is the game?', 'What do you remember about me?',
                      'What are their health conditions?', 'What if my health condition changed?'):
            self.assertIsNone(self.store.get_answer('alpha', '100', query))
        self.assertIsNone(self.store.get_answer('alpha', '200', 'What are my health conditions?'))
        self.assertIsNone(self.store.get_answer('beta', '100', 'What are my health conditions?'))

    def test_broad_profile_context_excludes_reserved_health_keys(self):
        self.fill_four()
        self.seed_legacy('My real name is Elara Voss')
        self.seed_legacy('My favorite color is blue')
        for query in (None, 'Hello again', 'What do you remember about me?', 'What are my health conditions?'):
            # The base formatter also protects reserved keys for future consumers.
            rendered = ViewerProfileStore.format_profile_context(self.store, 'alpha', '100', query)
            for value in self.conditions() | {'Elara'}:
                self.assertNotIn(value, rendered)
        self.assertIn('blue', self.store.format_profile_context('alpha', '100', 'What is my favorite color?'))

    def test_known_minor_legacy_sensitive_reads_are_suppressed_without_deletion(self):
        self.seed_legacy('I have asthma')
        self.seed_legacy('My real name is Elara Voss')
        self.seed_legacy('I am 17')
        before = self.store._raw_facts('alpha', '100')
        self.assertIsNone(self.store.get_answer('alpha', '100', 'What is my health condition?'))
        self.assertIsNone(self.store.get_answer('alpha', '100', 'What is my real name?'))
        self.assertEqual('', self.store.format_profile_context('alpha', '100', 'What do you remember about me?'))
        self.assertEqual(before, self.store._raw_facts('alpha', '100'))
        self.assertEqual({}, self.facts())
        self.assertEqual(1, self.store.forget('alpha', '100', 'health'))
        self.assertIn('age', self.store._raw_facts('alpha', '100'))

    def test_missing_identity_and_nonuser_sources_cannot_write(self):
        for channel, viewer in (('', '100'), ('alpha', None), ('alpha', '')):
            self.store.observe_health(channel, viewer, 'I have HIV', intake_authorized=True)
        for role in ('assistant', 'timer', 'search'):
            self.assertEqual('ignored', self.observe('I have HIV', source_role=role).kind)
        self.assertFalse(self.path.exists())

    def test_concurrent_store_instances_cannot_add_a_fifth_condition(self):
        for value in ('asthma', 'lupus', 'HIV'):
            self.observe('I have ' + value)
        peer = HealthConditionStore(self.path)
        self.addCleanup(peer.close)
        ready = threading.Barrier(2)
        def add(store, value):
            ready.wait(timeout=5)
            return store.observe_health('alpha', '100', 'I have ' + value,
                                        intake_authorized=True, now=NOW).kind
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(add, self.store, 'arthritis')
            second = pool.submit(add, peer, 'cancer')
            self.assertEqual(['limit_reached', 'saved'], sorted([first.result(), second.result()]))
        self.assertEqual(4, len(self.conditions()))

    def test_existing_legacy_writer_has_not_been_expanded(self):
        # The staged engine does not silently expand the currently deployed writer.
        self.assertEqual('ignored', self.seed_legacy('I have HIV').kind)
        self.assertFalse(self.path.exists())

    def test_plural_health_lookup_is_an_exact_self_topic(self):
        for query in ('What are my health conditions?', 'What health conditions did I tell you about?'):
            self.assertEqual({'health_condition'}, profile_query_topics(query))
        self.assertEqual(set(), profile_query_topics('What are her health conditions?'))

    def test_listener_read_path_uses_direct_lookup_and_never_model_context(self):
        harness = wiring.TwitchMemoryWiringTests('runTest')
        harness.setUp()
        self.addCleanup(harness.tearDown)
        staged = HealthConditionStore(harness.profile_path)
        self.addCleanup(staged.close)
        harness.client._viewer_profiles = staged  # explicit test-only adapter injection
        for value in ('HIV', 'asthma'):
            staged.observe_health('room-a', '100', 'I have ' + value, intake_authorized=True, now=NOW)
        harness.chat('Ada', 'Medlar, what are my health conditions?')
        harness.generate_response.assert_not_called()
        self.assertIn('HIV', harness.client.send_message.call_args.args[0])
        harness.chat('Bex', 'Medlar, what are my health conditions?', user_id='200')
        harness.generate_response.assert_not_called()
        self.assertNotIn('HIV', harness.client.send_message.call_args.args[0])
        self.assertIn("don't have", harness.client.send_message.call_args.args[0])
        harness.chat('Ada', 'Medlar, how is the game?')
        retained = str(harness.generate_response.call_args.kwargs)
        self.assertNotIn('HIV', retained)
        self.assertNotIn('asthma', retained)


if __name__ == '__main__':
    unittest.main()
