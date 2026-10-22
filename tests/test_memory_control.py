"""Valid-partial default-on, missing/invalid fail-closed memory control and actual feature UI/store methods."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from MedlarTV.app import config_editor_store as editor
from MedlarTV.core.memory_control import read_memory_control
from MedlarTV.core.health_conditions import HealthConditionStore
import test_feature_config_saves as features
import test_twitch_memory_wiring as wiring


class MemoryControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'app_settings.yaml'

    def write(self, text):
        self.path.write_text(text, encoding='utf-8')

    def toggle(self, value):
        _, revision = editor.load_features(self.path)
        return editor.save_features(self.path, {'remember_users':value}, revision)

    def harness(self):
        fixture = wiring.TwitchMemoryWiringTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.namespace['get_memory_control'] = lambda: read_memory_control(self.path)
        return fixture

    def test_missing_control_defaults_on_without_creating_a_file(self):
        """Historical ID retained: missing is now off; valid omission stays on."""
        self.assertFalse(read_memory_control(self.path).enabled)
        self.assertEqual(read_memory_control(self.path).revision, 'missing')
        self.assertFalse(self.path.exists())
        self.write('llm_brain: false\ncustom: keep\n')
        before = self.path.read_bytes()
        self.assertTrue(read_memory_control(self.path).enabled)
        self.assertEqual(before, self.path.read_bytes())

    def test_malformed_or_quoted_values_fail_closed(self):
        for text in ('remember_users: "false"\n', 'remember_users: "true"\n',
                     'remember_users: 1\n', 'remember_users: null\n',
                     'remember_users: true\nremember_users: false\n', '[]\n', '',
                     'remember_users: [', '_memory_revision: bad\n'):
            self.write(text)
            self.assertFalse(read_memory_control(self.path).enabled, text)
            with self.assertRaises(editor.ConfigFormatError):
                editor.load_features(self.path)

    def test_unreadable_control_fails_closed_without_surfacing_payload(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError('synthetic PRIVATE')):
            state = read_memory_control(self.path)
        self.assertFalse(state.enabled)
        self.assertNotIn('PRIVATE', repr(state))

    def test_runtime_is_enabled_uses_strict_memory_reader(self):
        source=Path(__file__).resolve().parents[1]/'MedlarTV/core/settings.py'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'is_enabled','get_memory_control'}]
        namespace={'__name__':'MedlarTV.core.settings', '__package__':'MedlarTV.core',
                   'APP_SETTINGS_FILE':self.path, 'get_settings':lambda: {'remember_users':'false'}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(source),'exec'),namespace)
        self.write('remember_users: "false"\n')
        self.assertFalse(namespace['is_enabled']('remember_users'))
        self.write('remember_users: true\n')
        self.assertTrue(namespace['is_enabled']('remember_users'))

    def test_validated_save_preserves_comments_unknown_fields_and_adds_only_control_revision(self):
        self.write('llm_brain: true # retained\ncustom:\n  note: keep\n')
        self.toggle(False)
        data = yaml.safe_load(self.path.read_text())
        self.assertEqual({'llm_brain','custom','remember_users','_memory_revision'}, set(data))
        self.assertEqual({'note':'keep'}, data['custom'])
        self.assertIn('# retained', self.path.read_text())
        self.assertFalse(read_memory_control(self.path).enabled)
        self.assertRegex(data['_memory_revision'], r'^[0-9a-f]{64}$')

    def test_off_on_cycle_has_new_revision_even_when_listener_misses_intermediate_off(self):
        self.write('remember_users: true\n')
        first = read_memory_control(self.path)
        self.toggle(False)
        off = read_memory_control(self.path)
        self.toggle(True)
        on = read_memory_control(self.path)
        self.assertTrue(on.enabled)
        self.assertEqual(3, len({first.revision,off.revision,on.revision}))
        before = self.path.read_bytes()
        self.toggle(True)
        self.assertEqual(before, self.path.read_bytes())

    def test_unrelated_feature_save_does_not_change_memory_revision(self):
        self.write('llm_brain: true\nremember_users: true\n')
        self.toggle(False)
        before = read_memory_control(self.path)
        _,revision = editor.load_features(self.path)
        editor.save_features(self.path, {'llm_brain':False}, revision)
        self.assertEqual(before, read_memory_control(self.path))

    def test_rejected_save_keeps_effective_control_and_revision(self):
        self.write('remember_users: true\n')
        before = self.path.read_bytes()
        _,revision=editor.load_features(self.path)
        with patch.object(editor, '_atomic_save_locked', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                editor.save_features(self.path, {'remember_users':False}, revision)
        self.assertEqual(before,self.path.read_bytes())
        with self.assertRaises(ValueError):
            editor.save_features(self.path, {'remember_users':'false'}, revision)
        self.assertTrue(read_memory_control(self.path).enabled)

    def test_gui_memory_control_saves_and_reloads_actual_value(self):
        fixture=features.FeatureGuiMethodTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertTrue(fixture.window.chk_memory.checked)
        self.assertTrue(fixture.window.update_toggle('remember_users',False))
        self.assertFalse(read_memory_control(fixture.path).enabled)
        self.assertFalse(fixture.window.chk_memory.checked)
        self.assertTrue(fixture.window.refresh_settings())
        self.assertFalse(fixture.window.chk_memory.checked)

    def test_legacy_personality_preference_is_not_a_second_control(self):
        self.write('personality:\n  learning:\n    remember_users: false\n')
        self.assertTrue(read_memory_control(self.path).enabled)
        self.toggle(False)
        self.assertFalse(read_memory_control(self.path).enabled)

    def test_off_stops_intake_and_recall_keeps_saved_facts_and_stateless_chat(self):
        self.write('remember_users: true\n')
        h=self.harness()
        h.chat('Ada','My favorite color is blue')
        h.chat('Ada','My test word is cobalt')
        before=h.profiles._raw_facts('room-a','100')
        self.toggle(False)
        h.chat('Ada','My favorite color is red')
        self.assertEqual(before,h.profiles._raw_facts('room-a','100'))
        self.assertEqual({},h.conversation._viewers)
        h.generate_response.reset_mock()
        h.chat('Ada','Medlar, what is my favorite color?')
        h.generate_response.assert_not_called()
        self.assertIn('memory is off',h.client.send_message.call_args.args[0])
        self.assertNotIn('blue',h.client.send_message.call_args.args[0])
        h.chat('Ada','Medlar, tell me a joke')
        self.assertEqual([],h.generate_response.call_args.kwargs['history'])
        self.assertEqual('',h.generate_response.call_args.kwargs['memory_context'])
        self.toggle(True)
        h.chat('Ada','Medlar, what is my favorite color?')
        self.assertIn('blue',h.client.send_message.call_args.args[0])
        self.assertNotIn('cobalt',repr(h.conversation._viewers))

    def test_off_keeps_forget_and_exclusion_flags_without_child_profile(self):
        self.write('remember_users: true\n')
        h=self.harness()
        h.chat('Ada','I have HIV')
        self.toggle(False)
        h.chat('Ada','I am 17')
        h.chat('Ada','!forgetme')
        self.assertTrue(h.profiles.is_excluded('room-a','100'))
        self.assertEqual({},h.profiles._raw_facts('room-a','100'))
        self.toggle(True)
        h.chat('Ada','I have lupus')
        self.assertEqual({},h.profiles.get_facts('room-a','100'))

    def test_off_full_birthdate_and_dated_age_record_only_exclusion(self):
        for index, text in enumerate(('My birthday is May 4, 2010','I turned 17 on October 2, 2026')):
            with HealthConditionStore(self.root/f'profile-{index}.sqlite3') as store:
                result=store.observe('room','100',text,intake_enabled=False)
                self.assertEqual('excluded',result.kind)
                self.assertEqual({},store._raw_facts('room','100'))
                self.assertTrue(store.is_excluded('room','100'))

    def test_off_never_creates_profile_storage_for_ordinary_self_reports(self):
        path=self.root/'profiles.sqlite3'
        with HealthConditionStore(path) as store:
            for message in ('My real name is Elara','I have HIV','My favorite food is noodles','I am 30'):
                self.assertEqual('ignored',store.observe('room','100',message,intake_enabled=False).kind)
        self.assertFalse(path.exists())

    def test_disabling_llm_alone_does_not_disable_independent_memory(self):
        self.write('remember_users: true\n')
        h=self.harness()
        h.enabled['llm_brain']=False
        h.chat('Ada','My favorite color is blue')
        self.assertEqual('blue',h.profiles.get_facts('room-a','100')['favorite_color'].value)
        h.generate_response.assert_not_called()
