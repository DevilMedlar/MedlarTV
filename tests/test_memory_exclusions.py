"""Durable exclusion/intake contracts. All IDs, reports and databases are synthetic."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch

from MedlarTV.core import viewer_profiles as profiles
from MedlarTV.core.health_conditions import HealthConditionStore, current_under18_report, health_groups
from MedlarTV.core.viewer_profiles import ViewerProfileStore
import test_twitch_memory_wiring as wiring

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


class MemoryExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.store = HealthConditionStore(self.path)
        self.addCleanup(self.store.close)

    def observe(self, message, channel='alpha', viewer='100', **kwargs):
        return self.store.observe(channel, viewer, message, now=NOW, **kwargs)

    def raw(self, channel='alpha', viewer='100'):
        return self.store._raw_facts(channel, viewer)

    def test_new_under18_report_stores_only_two_identity_columns(self):
        self.assertEqual('excluded', self.observe('I am 17', message_id='must-not-be-stored').kind)
        db = self.store._db(create=False)
        self.assertEqual(['channel_id', 'viewer_id'], [r[1] for r in db.execute('PRAGMA table_info(memory_exclusions)')])
        self.assertEqual([('alpha', '100')], [tuple(r) for r in db.execute('SELECT * FROM memory_exclusions')])
        self.assertEqual({}, self.raw())

    def test_exclusion_survives_restart_forget_and_new_adult_claim(self):
        self.observe('I am 17')
        self.store.close()
        with HealthConditionStore(self.path) as reopened:
            self.assertTrue(reopened.is_excluded('alpha', '100'))
            self.assertEqual(0, reopened.forget('alpha', '100'))
            for message in ('I am 30', 'My real name is Elara', 'I have HIV', 'My favorite color is blue'):
                self.assertEqual('excluded', reopened.observe('alpha', '100', message).kind)
            self.assertEqual({}, reopened.get_facts('alpha', '100'))
        with HealthConditionStore(self.path) as again:
            self.assertTrue(again.is_excluded('alpha', '100'))

    def test_new_exclusion_keeps_existing_facts_inaccessible_until_forget(self):
        self.observe('I have HIV')
        self.observe('My real name is Elara')
        before = self.raw()
        self.observe('I am 17')
        self.assertEqual(before, self.raw())
        self.assertEqual({}, self.store.get_facts('alpha', '100'))
        self.assertIsNone(self.store.get_answer('alpha', '100', 'What is my real name?'))
        self.assertEqual('', self.store.format_profile_context('alpha', '100'))
        self.assertEqual(2, self.store.forget('alpha', '100'))
        self.assertEqual({}, self.raw())
        self.assertTrue(self.store.is_excluded('alpha', '100'))

    def test_exclusion_is_channel_and_stable_viewer_scoped(self):
        self.observe('I am 17')
        self.assertEqual('saved', self.observe('I have HIV', viewer='200').kind)
        self.assertEqual('saved', self.observe('I have lupus', channel='beta').kind)
        self.assertFalse(self.store.is_excluded('alpha', '200'))
        self.assertFalse(self.store.is_excluded('beta', '100'))
        self.assertTrue(self.store.is_excluded('ALPHA', '100'))

    def test_missing_identity_cannot_create_persistent_flag(self):
        for channel, viewer in (('', '100'), ('alpha', None), ('alpha', '')):
            self.assertEqual('excluded', self.observe('I am 17', channel, viewer).kind)
        self.assertFalse(self.path.exists())

    def test_quoted_uncertain_third_party_and_nonuser_reports_do_not_flag(self):
        for text in ('"I am 17"', 'My friend is 17', 'I think I am 17', 'I am 17, just kidding',
                     'I am 17?', 'I am 17\n', 'I am 17' + ' ' * 180 + 'maybe'):
            self.assertFalse(current_under18_report(text))
        for role in ('assistant', 'search', 'timer'):
            self.assertEqual('ignored', self.observe('I am 17', source_role=role).kind)
        self.assertFalse(self.path.exists())

    def test_turning_under18_today_needs_no_timezone_or_birthday_storage(self):
        for text in ('I am turning 17 today', "I'm turning 17 today", 'I turn 17 today'):
            self.assertEqual('excluded', self.observe(text).kind)
        self.assertEqual({}, self.raw())

    def test_existing_accepted_full_birthdate_is_used_only_to_exclude(self):
        self.assertEqual('excluded', self.observe('My birthday is May 4, 2010').kind)
        self.assertEqual({}, self.raw())
        self.assertTrue(self.store.is_excluded('alpha', '100'))

    def test_dated_age_does_not_write_child_birthday_or_derived_year(self):
        self.assertEqual('excluded', self.observe('I turned 17 on October 2, 2026').kind)
        self.assertEqual({}, self.raw())

    def test_adult_and_unknown_age_intake_is_automatic_without_verification_claim(self):
        self.assertEqual('saved', self.observe('I have HIV').kind)
        self.assertNotIn('age', self.raw())
        self.assertEqual('saved', self.observe('I am 30', viewer='200').kind)
        self.assertEqual('saved', self.observe('I have lupus', viewer='200').kind)
        self.assertFalse(self.store.is_excluded('alpha', '100'))
        db = self.store._db(create=False)
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='memory_exclusions'").fetchone())

    def test_lazy_legacy_check_then_forget_preserves_identity_flag(self):
        with ViewerProfileStore(self.path) as legacy:
            legacy.observe('alpha', '100', 'I am 17', now=NOW)
            legacy.observe('alpha', '100', 'I have asthma', now=NOW)
        self.assertEqual(2, self.store.forget('alpha', '100'))
        self.assertEqual({}, self.raw())
        self.store.close()
        with HealthConditionStore(self.path) as reopened:
            self.assertTrue(reopened.is_excluded('alpha', '100'))

    def test_legacy_lookup_creates_flag_only_for_requested_viewer(self):
        with ViewerProfileStore(self.path) as legacy:
            for viewer in ('100', '200'):
                legacy.observe('alpha', viewer, 'I am 17', now=NOW)
        self.assertEqual({}, self.store.get_facts('alpha', '100'))
        db = self.store._db(create=False)
        self.assertEqual([('alpha', '100')], [tuple(r) for r in db.execute('SELECT * FROM memory_exclusions')])
        self.assertIn('age', self.raw(viewer='200'))

    def test_fact_capacity_cannot_prevent_exclusion_or_evict_another_viewer(self):
        self.observe('My favorite color is blue', viewer='200')
        with patch.object(profiles, 'MAX_TOTAL_FACTS', 1), patch.object(profiles, 'MAX_FACTS_PER_VIEWER', 0):
            self.assertEqual('excluded', self.observe('I am 17').kind)
        self.assertEqual('blue', self.raw(viewer='200')['favorite_color'].value)

    def test_flag_storage_failure_surfaces_and_does_not_save_age(self):
        with patch.object(self.store, '_mark_excluded', side_effect=sqlite3.OperationalError('synthetic full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.observe('I am 17')
        self.assertEqual({}, self.raw())

    def test_failed_flag_is_retried_before_later_fact_admission(self):
        with patch.object(self.store, '_mark_excluded', side_effect=sqlite3.OperationalError('synthetic full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.observe('I am 17')
            with self.assertRaises(sqlite3.OperationalError):
                self.observe('My favorite color is blue')
        self.assertEqual('excluded', self.observe('I have HIV').kind)
        self.assertTrue(self.store.is_excluded('alpha', '100'))
        self.assertEqual({}, self.raw())
        self.store.close()
        with HealthConditionStore(self.path) as reopened:
            self.assertTrue(reopened.is_excluded('alpha', '100'))

    def test_sqlite_page_capacity_blocks_flag_safely_then_retries_without_eviction(self):
        self.observe('My favorite color is blue', viewer='200')
        db = self.store._db(create=False)
        maximum = db.execute('PRAGMA max_page_count').fetchone()[0]
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        db.execute(f'PRAGMA max_page_count={pages}')
        with self.assertRaises(sqlite3.OperationalError):
            self.observe('I am 17')
        self.assertEqual({}, self.raw())
        self.assertEqual('blue', self.raw(viewer='200')['favorite_color'].value)
        db.execute(f'PRAGMA max_page_count={maximum}')
        self.assertEqual('excluded', self.observe('I have HIV').kind)
        self.assertTrue(self.store.is_excluded('alpha', '100'))

    def test_natural_forget_and_targeted_health_retraction_preserve_exclusion(self):
        self.observe('I have HIV')
        self.observe('My favorite color is blue')
        self.observe('I am 17')
        self.assertEqual('forgotten', self.observe('I no longer have HIV').kind)
        self.assertEqual({'favorite_color'}, set(self.raw()))
        self.assertEqual('forgotten', self.observe('Forget everything about me').kind)
        self.assertEqual({}, self.raw())
        self.assertTrue(self.store.is_excluded('alpha', '100'))

    def test_old_dated_age_is_not_treated_as_current_under18(self):
        self.assertEqual('saved', self.observe('I turned 17 on October 2, 2000').kind)
        self.assertFalse(self.store.is_excluded('alpha', '100'))
        self.assertEqual('saved', self.observe('I have HIV').kind)

    def test_legacy_full_birthdate_is_excluded_without_bulk_conversion(self):
        with ViewerProfileStore(self.path) as legacy:
            legacy.observe('alpha', '100', 'My birthday is May 4, 2010', now=NOW)
            legacy.observe('alpha', '100', 'I have asthma', now=NOW)
        before = self.raw()
        self.assertEqual({}, self.store.get_facts('alpha', '100'))
        self.assertEqual(before, self.raw())
        self.assertTrue(self.store.is_excluded('alpha', '100'))

    def test_compound_health_and_favorite_use_one_atomic_four_condition_policy(self):
        self.assertEqual(2, self.observe('My favorite food is pizza. I have HIV').count)
        for value in ('asthma', 'lupus', 'bipolar'):
            self.assertEqual('saved', self.observe('I have ' + value).kind)
        before = self.raw()
        self.assertEqual('limit_reached', self.observe('My favorite food is noodles. I have diabetes').kind)
        self.assertEqual(before, self.raw())
        self.assertEqual({'HIV', 'asthma', 'lupus', 'bipolar disorder'}, set(health_groups(self.raw())))

    def test_compound_capacity_failure_rolls_back_favorite_and_health_together(self):
        with patch.object(profiles, 'MAX_FACTS_PER_VIEWER', 1):
            self.assertEqual('limit_reached', self.observe('My favorite food is pizza. I have HIV').kind)
        self.assertEqual({}, self.raw())

    def test_health_and_ordinary_writes_recheck_flag_under_transaction(self):
        peer = HealthConditionStore(self.path)
        self.addCleanup(peer.close)
        self.observe('My favorite color is blue')
        original = self.store.is_excluded
        calls = 0
        def race(channel, viewer):
            nonlocal calls
            calls += 1
            if calls == 1:
                peer.exclude(channel, viewer)
                return False
            return original(channel, viewer)
        with patch.object(self.store, 'is_excluded', side_effect=race):
            self.assertEqual('excluded', self.observe('My favorite color is red').kind)
        self.assertEqual('blue', self.raw()['favorite_color'].value)
        self.assertNotIn('age', self.raw())

    def test_two_connections_excluding_and_health_intake_never_resume_after_flag(self):
        self.observe('My favorite color is blue')
        peer = HealthConditionStore(self.path)
        self.addCleanup(peer.close)
        ready = threading.Barrier(2)
        def exclude():
            ready.wait(timeout=5)
            return peer.observe('alpha', '100', 'I am 17').kind
        def health():
            ready.wait(timeout=5)
            return self.observe('I have HIV').kind
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(exclude), pool.submit(health)
            self.assertEqual('excluded', first.result())
            self.assertIn(second.result(), {'saved', 'excluded'})
        self.assertEqual({}, self.store.get_facts('alpha', '100'))
        self.assertEqual('excluded', self.observe('I have lupus').kind)

    def harness(self):
        harness = wiring.TwitchMemoryWiringTests('runTest')
        harness.setUp()
        self.addCleanup(harness.tearDown)
        return harness

    def test_listener_factory_uses_new_store_without_opening_database(self):
        harness = self.harness()
        harness.client._viewer_profiles = None
        with patch.object(profiles, 'default_profile_path', return_value=self.path):
            store = harness.client._profile_store()
        self.addCleanup(store.close)
        self.assertIsInstance(store, HealthConditionStore)
        self.assertFalse(self.path.exists())

    def test_listener_automatic_four_condition_intake_direct_recall_and_restart(self):
        harness = self.harness()
        for value in ('asthma', 'HIV', 'lupus', 'bipolar'):
            harness.chat('Ada', 'I have ' + value)
        harness.profiles.close()
        reopened = HealthConditionStore(harness.profile_path)
        self.addCleanup(reopened.close)
        harness.client._viewer_profiles = reopened
        harness.generate_response.reset_mock()
        harness.chat('Ada', 'Medlar, what are my health conditions?')
        harness.generate_response.assert_not_called()
        answer = harness.client.send_message.call_args.args[0]
        for value in ('asthma', 'HIV', 'lupus', 'bipolar disorder'):
            self.assertIn(value, answer)
        harness.chat('Ada', 'I have diabetes')
        self.assertEqual(4, len(health_groups(reopened.get_facts('room-a', '100'))))

    def test_listener_excludes_profile_session_public_context_without_ban(self):
        harness = self.harness()
        harness.chat('Ada', 'My test word is cobalt')
        harness.chat('Ada', 'I am 17')
        self.assertTrue(harness.profiles.is_excluded('room-a', '100'))
        for text in ('My real name is Elara', 'I have HIV', 'My favorite color is blue', 'Medlar, hello'):
            harness.chat('Ada', text)
            kwargs = harness.generate_response.call_args.kwargs
            self.assertEqual([], kwargs['history'])
            self.assertEqual('', kwargs['memory_context'])
        self.assertEqual({}, harness.profiles.get_facts('room-a', '100'))
        self.assertEqual({}, harness.conversation._viewers)
        self.assertEqual({}, harness.conversation._public)
        harness.client.send_message.assert_called()
        harness.execute_command.assert_not_called()

    def test_listener_forget_restart_reobserve_keeps_flag_and_other_viewer(self):
        harness = self.harness()
        harness.chat('Ada', 'My favorite color is blue')
        harness.chat('Bex', 'My favorite color is red', user_id='200')
        harness.chat('Ada', 'I am 17')
        harness.chat('Ada', '!forgetme')
        self.assertEqual({}, harness.profiles._raw_facts('room-a', '100'))
        harness.profiles.close()
        reopened = HealthConditionStore(harness.profile_path)
        self.addCleanup(reopened.close)
        harness.client._viewer_profiles = reopened
        harness.chat('Ada', 'I have lupus')
        self.assertTrue(reopened.is_excluded('room-a', '100'))
        self.assertEqual({}, reopened.get_facts('room-a', '100'))
        self.assertEqual('red', reopened.get_facts('room-a', '200')['favorite_color'].value)
        harness.generate_response.reset_mock()
        harness.chat('Ada', 'Medlar, what is my favorite color?')
        harness.generate_response.assert_not_called()
        self.assertNotIn('blue', harness.client.send_message.call_args.args[0])

    def test_listener_sensitive_reports_and_direct_answers_never_enter_general_context(self):
        harness = self.harness()
        harness.chat('Ada', 'My real name is Elara')
        harness.chat('Ada', 'I have HIV')
        harness.chat('Ada', 'Medlar, what are my health conditions?')
        harness.chat('Ada', 'Medlar, how is the game?')
        for value in ('Elara', 'HIV'):
            self.assertNotIn(value, str(harness.generate_response.call_args.kwargs))
        harness.chat('Bex', 'Medlar, what did Ada say?', user_id='200')
        for value in ('Elara', 'HIV'):
            self.assertNotIn(value, str(harness.generate_response.call_args.kwargs))

    def test_listener_flag_failure_disables_memory_and_keeps_chat_available(self):
        harness = self.harness()
        harness.chat('Ada', 'My test word is cobalt')
        with patch.object(harness.profiles, '_mark_excluded', side_effect=sqlite3.OperationalError('synthetic full')):
            harness.chat('Ada', 'I am 17')
        self.assertEqual([], harness.generate_response.call_args.kwargs['history'])
        self.assertEqual('', harness.generate_response.call_args.kwargs['memory_context'])
        self.assertEqual({}, harness.conversation._viewers)
        self.assertEqual({}, harness.profiles._raw_facts('room-a', '100'))
        harness.client.send_message.assert_called()

    def test_listener_missing_id_under18_is_not_retained_for_current_session_message(self):
        harness = self.harness()
        harness.chat('Ada', 'I am 17', user_id='')
        self.assertEqual([], harness.generate_response.call_args.kwargs['history'])
        self.assertEqual('', harness.generate_response.call_args.kwargs['memory_context'])
        self.assertEqual({}, harness.conversation._viewers)
        self.assertFalse(harness.profile_path.exists())

    def test_listener_concurrent_prior_observation_cannot_refill_excluded_session(self):
        harness = self.harness()
        entered, release, attempted, excluded = (threading.Event() for _ in range(4))
        original = harness.conversation.observe_user
        def delayed(*args, **kwargs):
            if args[3] == 'My test word is cobalt':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Synthetic barrier timed out')
            return original(*args, **kwargs)
        def exclude():
            attempted.set()
            harness.chat('Ada', 'I am 17')
            excluded.set()
        with patch.object(harness.conversation, 'observe_user', side_effect=delayed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                old = pool.submit(harness.chat, 'Ada', 'My test word is cobalt')
                self.assertTrue(entered.wait(5))
                new = pool.submit(exclude)
                self.assertTrue(attempted.wait(5))
                excluded.wait(0.15)
                release.set()
                old.result(timeout=5)
                new.result(timeout=5)
        self.assertTrue(harness.profiles.is_excluded('room-a', '100'))
        self.assertEqual({}, harness.conversation._viewers)
        self.assertEqual({}, harness.conversation._public)
