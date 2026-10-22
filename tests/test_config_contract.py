"""Approved B5 behavior on synthetic files, with real pure leaves/AST consumers."""
import ast
import importlib.util
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

from MedlarTV import config_contract as contract, config_state
from MedlarTV.app import config_editor_store as editor
from MedlarTV.core.memory_control import read_memory_control
import test_twitch_outgoing as outgoing

ROOT = Path(__file__).resolve().parents[1]


def settings_module():
    spec = importlib.util.spec_from_file_location('MedlarTV.core.settings', ROOT/'MedlarTV/core/settings.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ControlledLock:
    """Detect re-entry immediately, without risking a deadlocked test thread."""
    def __init__(self):
        self.held = False
    def __enter__(self):
        if self.held:
            raise RuntimeError('synthetic lock re-entry')
        self.held = True
    def __exit__(self, *args):
        self.held = False


class ConfigContractTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='b5-contract-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root/'app_settings.yaml'
        self.state = config_state.FeatureState()

    def write(self, text):
        self.path.write_text(text, encoding='utf-8')

    def module(self):
        module = settings_module()
        module.APP_SETTINGS_FILE = self.path
        module.FEATURE_STATE = self.state
        return module

    def emotion_loader(self, path):
        ns = outgoing.load_definitions('MedlarTV/core/emotional_system.py', {
            'EMOTIONS_CONFIG_PATH': path, 'DEFAULT_DECAY': 0.97,
            'yaml': yaml,
            'log': logging.getLogger('b5-emotion-fixture'),
        }, names={'_load_emotion_config'}, assignments={'DEFAULT_EMOTIONS'})
        return ns, ns['_load_emotion_config']()

    def diagnostic(self):
        ns = outgoing.load_definitions('tests/medlar_diagnostics.py', {
            'REPO_ROOT': self.root, 'READY': 'READY', 'ERROR': 'ERROR', 'SKIPPED': 'SKIPPED',
            'Check': lambda section,name,status,detail: SimpleNamespace(name=name,status=status,detail=detail),
            '_relative': lambda path,root: str(path.relative_to(root)),
        }, names={'_string_list', 'check_config_schemas'})
        return {row.name: row for row in ns['check_config_schemas'](self.root)}

    def test_valid_partial_defaults_and_unknown_fields(self):
        self.write('llm_brain: false\ncustom: retained\n')
        data, flags = contract.parse_features(self.path.read_bytes())
        self.assertFalse(flags['llm_brain'])
        self.assertTrue(flags['remember_users'])
        self.assertEqual(data['custom'], 'retained')
        self.assertEqual(editor.load_features(self.path)[0], flags)
        self.assertEqual(self.module().get_settings()['custom'], 'retained')

    def test_empty_valid_mapping_preserves_defaults(self):
        self.assertEqual(contract.parse_features(b'{}')[1], contract.FEATURE_DEFAULTS)

    def test_invalid_shapes_match_gui_memory_and_runtime(self):
        for raw in ('', '[]', '42', 'null', 'llm_brain: "false"', 'timers: 1',
                    'remember_users: true\nremember_users: false', '_memory_revision: bad',
                    'custom:\n  key: 1\n  key: 2', '[complex, key]: true'):
            with self.subTest(raw=raw):
                self.write(raw)
                with self.assertRaises(contract.ConfigFormatError):
                    contract.parse_features(self.path.read_bytes())
                with self.assertRaises(editor.ConfigFormatError):
                    editor.load_features(self.path)
                self.assertFalse(read_memory_control(self.path).enabled)
                flags, _, status = config_state.FeatureState().read(self.path)
                self.assertFalse(status.ready)
                self.assertFalse(flags['llm_brain'])

    def test_bom_crlf_comments_and_unknown_fields_survive_save(self):
        before = b'\xef\xbb\xbfllm_brain: true # retain\r\ncustom: keep\r\n'
        self.path.write_bytes(before)
        _, revision = editor.load_features(self.path)
        editor.save_features(self.path, {'llm_brain': False}, revision)
        self.assertEqual(self.path.read_bytes(), before.replace(b'true', b'false'))

    def test_fresh_missing_suspends_actions_without_creating_paths(self):
        self.path = self.root/'absent'/'app_settings.yaml'
        module = self.module()
        flags = module.get_settings()
        for key in ('llm_brain','timers','pcg_auto_catch','fuzzy_trigger','remember_users'):
            self.assertFalse(flags[key])
        self.assertTrue(flags['content_filter'])
        self.assertFalse(module.settings_status().ready)
        self.assertFalse(self.path.parent.exists())

    def test_failed_reload_retains_nonprivate_flags_and_disables_memory(self):
        self.write('llm_brain: true\ntimers: false\nremember_users: true\ncustom: private-fixture\n')
        module = self.module()
        self.assertTrue(module.get_settings()['remember_users'])
        self.write('[')
        flags = module.get_settings()
        self.assertTrue(flags['llm_brain'])
        self.assertFalse(flags['timers'])
        self.assertFalse(flags['remember_users'])
        self.assertNotIn('custom', flags)
        self.assertFalse(module.is_enabled('remember_users'))
        self.assertEqual(module.settings_status(), config_state.SettingsStatus(False,True,'invalid',True))

    def test_missing_after_valid_keeps_nonprivate_snapshot(self):
        self.write('timers: false\n')
        self.state.read(self.path)
        self.path.unlink()
        flags, _, status = self.state.read(self.path)
        self.assertFalse(flags['timers'])
        self.assertFalse(flags['remember_users'])
        self.assertTrue(status.ready)
        self.assertEqual(status.reason, 'missing')

    def test_permission_failure_has_fixed_visible_status(self):
        self.write('{}')
        self.state.read(self.path)
        with self.assertLogs('settings', level='WARNING') as logs:
            with patch.object(Path, 'read_bytes', side_effect=PermissionError('PRIVATE-fixture')):
                flags, _, status = self.state.read(self.path)
        self.assertTrue(status.using_previous)
        self.assertFalse(flags['remember_users'])
        self.assertNotIn('PRIVATE-fixture', str(logs.output)+repr(status))

    def test_recovery_replaces_previous_state_and_clears_error(self):
        self.write('[')
        self.state.read(self.path)
        self.write('llm_brain: false\nremember_users: true\n')
        flags, _, status = self.state.read(self.path)
        self.assertTrue(status.healthy)
        self.assertFalse(status.using_previous)
        self.assertFalse(flags['llm_brain'])
        self.assertTrue(flags['remember_users'])

    def test_path_change_cannot_reuse_prior_checkout_state(self):
        self.write('{}')
        self.state.read(self.path)
        flags, _, status = self.state.read(self.root/'other.yaml')
        self.assertFalse(status.ready)
        self.assertFalse(flags['llm_brain'])

    def test_repeated_reads_make_no_writes(self):
        self.write('{}')
        before = self.path.read_bytes()
        with patch.object(Path,'mkdir',side_effect=AssertionError('mkdir')), patch.object(Path,'write_bytes',side_effect=AssertionError('write')):
            for _ in range(4): self.state.read(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_root_resolution_is_explicit_and_does_not_create(self):
        with patch.dict('os.environ', {'MEDLARTV_ROOT': str(self.root/'selected')}):
            self.assertEqual(contract.config_path('app_settings.yaml'), self.root/'selected/MedlarTV/config/app_settings.yaml')
            self.assertEqual(contract.config_path('app_settings.yaml', self.root), self.root/'MedlarTV/config/app_settings.yaml')
        with patch.dict('os.environ', {'MEDLARTV_ROOT': ''}):
            self.assertEqual(contract.selected_root(), ROOT)
        self.assertFalse((self.root/'selected').exists())

    def test_dormant_update_completes_without_lock_reentry(self):
        self.write('llm_brain: true # retained\ncustom: keep\n')
        module = self.module()
        module._lock = ControlledLock()
        try:
            result = module.update_settings({'llm_brain': False})
            outcome = 'completed'
        except RuntimeError:
            result, outcome = {}, 'nested-lock'
        self.assertEqual(outcome, 'completed')
        self.assertFalse(result['llm_brain'])
        self.assertIn('# retained', self.path.read_text())
        self.assertIn('custom: keep', self.path.read_text())

    def test_dormant_update_rejects_nonbooleans_and_unknown_edits(self):
        self.write('{}')
        module = self.module()
        before = self.path.read_bytes()
        for change in ({'llm_brain':'false'},{'timers':1},{'unknown':True},[]):
            with self.assertRaises(ValueError): module.update_settings(change)
            self.assertEqual(self.path.read_bytes(), before)

    def test_dormant_update_never_overwrites_invalid_file_or_creates_missing(self):
        module = self.module()
        self.write('[')
        with self.assertRaises(ValueError): module.update_settings({'llm_brain':False})
        self.assertEqual(self.path.read_text(), '[')
        self.path.unlink()
        with self.assertRaises(FileNotFoundError): module.update_settings({'llm_brain':False})
        self.assertFalse(self.path.exists())

    def test_dormant_api_calls_repaired_writer_without_hosting(self):
        self.write('custom: keep\n')
        module = self.module()
        ns = outgoing.load_definitions('MedlarTV/core/main.py', {
            'APP': SimpleNamespace(post=lambda *args: lambda f:f),
            'update_settings': module.update_settings,
            'JSONResponse': lambda value,**kwargs:value,
        }, names={'api_update_settings'})
        self.assertFalse(ns['api_update_settings']({'timers':False})['timers'])

    def test_missing_memory_control_is_off_but_valid_omission_is_on(self):
        self.assertFalse(read_memory_control(self.path).enabled)
        self.assertFalse(self.path.exists())
        self.write('{}')
        self.assertTrue(read_memory_control(self.path).enabled)

    def test_memory_permission_cannot_use_cached_enabled(self):
        self.write('remember_users: true')
        module = self.module()
        module.get_settings()
        self.path.unlink()
        self.assertFalse(module.get_settings()['remember_users'])
        self.assertFalse(module.is_enabled('remember_users'))

    def test_emotion_forms_normalize_to_same_intended_values(self):
        for raw in ('supportive: 0.4','supportive:\n  baseline: 0.4',
                    'emotions:\n  supportive: 0.4', 'emotions:\n  supportive:\n    baseline: 0.4'):
            self.assertEqual(contract.parse_emotions(raw)[0], {'supportive':{'baseline':0.4}})

    def test_actual_emotion_file_preserves_all_effective_defaults(self):
        path = ROOT/'MedlarTV/data/emotions.yaml'
        intended, inactive = contract.parse_emotions(path.read_bytes())
        ns, effective = self.emotion_loader(path)
        self.assertEqual(effective, ns['DEFAULT_EMOTIONS'])
        self.assertEqual(len(intended), 19)
        self.assertEqual(len(inactive), 19)
        self.assertEqual(effective['supportive']['baseline'], 0.6)
        self.assertEqual(intended['supportive']['baseline'], 0.4)

    def test_previously_effective_wrapped_and_scalar_overrides_remain_effective(self):
        for raw in ('supportive: 0.4', 'emotions:\n  supportive:\n    baseline: 0.4\n    decay: 0.9'):
            self.write(raw)
            _, effective = self.emotion_loader(self.path)
            self.assertEqual(effective['supportive']['baseline'], 0.4)

    def test_emotion_invalid_numbers_ranges_and_roots_are_rejected(self):
        for raw in ('[]','7','emotions: []','anger: true','anger: .nan','anger: .inf',
                    'anger: -0.1','anger: 1.1','anger: "0.4"','anger:\n  baseline: false',
                    'emotions:\n  anger:\n    baseline: 0.1\n    decay: 2',
                    'anger: 0.1\nanger: 0.2', 'anger: {}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError): contract.parse_emotions(raw)
                self.write(raw)
                ns, effective = self.emotion_loader(self.path)
                self.assertEqual(effective, ns['DEFAULT_EMOTIONS'])
        with self.assertRaises(ValueError):
            contract.parse_emotions('anger: ' + '9' * 500)

    def test_strict_mapping_rejects_nontext_input_without_parser_leak(self):
        for raw in (None, [], 4):
            with self.assertRaises(contract.ConfigFormatError):
                contract.parse_mapping(raw)

    def test_personality_root_errors_are_contained_before_traits_access(self):
        self.write('[]')
        ns = outgoing.load_definitions('MedlarTV/core/emotional_system.py', {
            'PERSONALITY_CONFIG_PATH': self.path,
            'yaml': yaml,
            'log': logging.getLogger('b5-trait-fixture'),
        }, names={'_load_personality_traits'})
        self.assertEqual(ns['_load_personality_traits'](), {})

    def test_explicit_root_emotion_path_prefers_config_then_data(self):
        directory = self.root/'MedlarTV/data'
        directory.mkdir(parents=True)
        data = directory/'emotions.yaml'
        data.write_text('anger: 0.1')
        with patch.dict('os.environ', {'MEDLARTV_ROOT': str(self.root)}):
            self.assertEqual(contract.config_path('emotions.yaml',data_fallback=True),data)
            preferred = self.root/'MedlarTV/config/emotions.yaml'
            preferred.parent.mkdir()
            preferred.write_text('anger: 0.2')
            self.assertEqual(contract.config_path('emotions.yaml',data_fallback=True),preferred)

    def test_dormant_writer_preserves_memory_revision_and_ignores_stale_external_edit(self):
        self.write('remember_users: true\ncustom: keep\n')
        module = self.module()
        module.update_settings({'remember_users':False})
        first = read_memory_control(self.path)
        self.assertFalse(first.enabled)
        module.update_settings({'remember_users':True})
        self.assertNotEqual(first.revision,read_memory_control(self.path).revision)
        before = self.path.read_bytes()
        fsync = editor.os.fsync
        external = before+b'# synthetic later edit\n'
        def interleave(fd):
            fsync(fd)
            self.path.write_bytes(external)
        with patch.object(editor.os,'fsync',side_effect=interleave):
            with self.assertRaises(editor.ConfigConflictError):
                module.update_settings({'timers':False})
        self.assertEqual(self.path.read_bytes(),external)

    def test_diagnostic_duplicate_keys_match_feature_reader_rejection(self):
        directory = self.root/'MedlarTV/config'
        directory.mkdir(parents=True)
        path = directory/'app_settings.yaml'
        path.write_text('timers: true\ntimers: false')
        self.assertEqual(self.diagnostic()['Effective app settings'].status,'ERROR')
        with self.assertRaises(editor.ConfigFormatError):
            editor.load_features(path)

    def test_emotion_missing_and_permission_failures_use_visible_defaults(self):
        ns, effective = self.emotion_loader(None)
        self.assertEqual(effective, ns['DEFAULT_EMOTIONS'])
        with patch.object(Path,'read_bytes',side_effect=PermissionError('PRIVATE-fixture')):
            with self.assertLogs('b5-emotion-fixture',level='WARNING') as logs:
                ns, effective = self.emotion_loader(self.path)
        self.assertEqual(effective, ns['DEFAULT_EMOTIONS'])
        self.assertNotIn('PRIVATE-fixture',str(logs.output))

    def test_nonfinite_emotion_override_cannot_enter_effective_state(self):
        self.write('anger: .nan')
        ns, effective = self.emotion_loader(self.path)
        self.assertEqual(effective, ns['DEFAULT_EMOTIONS'])

    def test_diagnostics_uses_runtime_parsers_and_labels_inactive_emotions(self):
        directory = self.root/'MedlarTV/config'
        directory.mkdir(parents=True)
        (directory/'app_settings.yaml').write_text('{}')
        (directory/'emotions.yaml').write_text('supportive:\n  baseline: 0.4')
        checks = self.diagnostic()
        self.assertEqual(checks['Effective app settings'].status,'READY')
        self.assertEqual(checks['Emotions'].status,'READY')
        self.assertIn('inactive',checks['Emotions'].detail)
        (directory/'app_settings.yaml').write_text('remember_users: "false"')
        (directory/'emotions.yaml').write_text('anger: .nan')
        checks = self.diagnostic()
        self.assertEqual(checks['Effective app settings'].status,'ERROR')
        self.assertEqual(checks['Emotions'].status,'ERROR')

    def test_output_policy_fresh_invalid_suspends_chat_but_not_pong(self):
        h = outgoing.TwitchOutgoingTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        h.client._load_output_policy = lambda:h.policy_module.load_policy(self.path)
        self.assertFalse(h.client.send_message('synthetic reply'))
        self.assertTrue(h.client._send_raw('PONG :fixture'))
        self.assertEqual(h.client.sock.sent,[b'PONG :fixture\r\n'])

    def test_output_policy_retains_valid_flags_on_bad_reload_and_recovers(self):
        h = outgoing.TwitchOutgoingTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        self.write('content_filter: false\nllm_brain: false')
        with patch.object(config_state,'FEATURE_STATE',self.state):
            self.assertFalse(h.policy_module.load_policy(self.path).enabled)
            self.write('[')
            self.assertFalse(h.policy_module.load_policy(self.path).enabled)
            self.assertFalse(self.state.status.healthy)
            self.write('content_filter: false\nllm_brain: true')
            self.assertTrue(h.policy_module.load_policy(self.path).llm_enabled)
            self.assertTrue(self.state.status.healthy)
