"""Feature-store and actual GUI-method tests with files/widgets entirely fake."""
import ast
import hashlib
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import yaml
from MedlarTV.app import config_editor_store as store

ROOT = Path(__file__).resolve().parents[1]


class Widget:
    def __init__(self):
        self.checked = False
        self.enabled = True
        self.blocked = False
        self.message = ''
    def blockSignals(self, value):
        previous, self.blocked = self.blocked, value
        return previous
    def setChecked(self, value):
        if type(value) is not bool:
            raise AssertionError('Only validated booleans may reach widgets')
        self.checked = value
    def isChecked(self):
        return self.checked
    def setEnabled(self, value):
        self.enabled = value
    def setText(self, value):
        self.message = value


class Dialog:
    AcceptRole, DestructiveRole, RejectRole = range(3)
    choice = 'Cancel'
    def __init__(self, *args):
        self.buttons = {}
    def setWindowTitle(self, *args):
        pass
    def setText(self, *args):
        pass
    def addButton(self, text, role):
        self.buttons[text] = object()
        return self.buttons[text]
    def setDefaultButton(self, button):
        pass
    def exec(self):
        pass
    def clickedButton(self):
        return self.buttons[self.choice]


def feature_window_type():
    tree = ast.parse((ROOT / 'MedlarTV/app/medlar_app.py').read_text(encoding='utf-8'))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ControlCenter')
    names = {'refresh_settings', '_feature_request_summary', '_apply_feature_values', 'retry_feature_changes',
             'request_feature_reload', 'update_toggle', '_settings_path', '_load_settings', '_save_settings'}
    nodes = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'Path': Path, 'os': os, 'QMessageBox': Dialog,
                 '__file__': str(ROOT / 'MedlarTV/app/medlar_app.py')}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(ROOT / 'MedlarTV/app/medlar_app.py'), 'exec'), namespace)
    return type('FeatureWindow', (), {name: namespace[name] for name in names})


Window = feature_window_type()


class FeatureStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='feature-store-')
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.path = self.folder / 'app_settings.yaml'
        self.path.write_text('llm_brain: true # keep comment\ncontent_filter: false\ncustom:\n  description: "fixture café"\n  number: 7\n', encoding='utf-8')
        self.before = self.path.read_bytes()
        self.values, self.revision = store.load_features(self.path)
    def save(self, changes=None):
        return store.save_features(self.path, changes if changes is not None else {'llm_brain': False}, self.revision)
    def assert_clean_helpers(self):
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ['app_settings.yaml'])

    def test_preserves_unknown_fields_comments_and_utf8(self):
        updated = self.save()
        after = self.path.read_bytes()
        self.assertEqual(after, self.before.replace(b'llm_brain: true', b'llm_brain: false'))
        self.assertEqual(updated, hashlib.sha256(after).hexdigest())
        self.assert_clean_helpers()

    def test_actual_false_loads_as_false(self):
        self.assertFalse(self.values['content_filter'])
        self.assertIs(type(self.values['content_filter']), bool)

    def test_omitted_switches_use_existing_defaults_without_writing(self):
        self.assertTrue(self.values['timers'])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_missing_whole_file_is_not_created(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            store.load_features(self.path)
        with self.assertRaises(FileNotFoundError):
            self.save()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_missing_parent_is_not_created(self):
        target = self.folder / 'missing' / 'app_settings.yaml'
        with self.assertRaises(FileNotFoundError):
            store.save_features(target, {'timers': False}, self.revision)
        self.assertFalse(target.parent.exists())

    def test_corrupt_and_non_mapping_data_never_changes(self):
        for payload in (b'llm_brain: [', b'', b'null\n', b'- true\n', b'1: true\n', b'\xff'):
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                with self.assertRaises((store.ConfigFormatError, store.ConfigConflictError)):
                    store.load_features(self.path)
                with self.assertRaises((store.ConfigFormatError, store.ConfigConflictError)):
                    self.save()
                self.assertEqual(self.path.read_bytes(), payload)
                self.assert_clean_helpers()

    def test_quoted_booleans_numbers_and_null_rejected(self):
        for value in ('"false"', '"true"', '0', '1', 'null', '[]', '{}'):
            with self.subTest(value=value):
                payload = ('llm_brain: ' + value + '\n').encode()
                self.path.write_bytes(payload)
                with self.assertRaises(store.ConfigFormatError):
                    store.load_features(self.path)
                self.assertEqual(self.path.read_bytes(), payload)

    def test_duplicate_known_and_unknown_keys_rejected(self):
        for text in ('llm_brain: true\nllm_brain: false\n', 'custom: 1\ncustom: 2\n', 'custom:\n  a: 1\n  a: 2\n'):
            self.path.write_text(text, encoding='utf-8')
            with self.assertRaises(store.ConfigFormatError):
                store.load_features(self.path)
            self.assertEqual(self.path.read_text(encoding='utf-8'), text)

    def test_invalid_update_keys_and_types_never_write(self):
        for changes in ({'unknown': False}, {'llm_brain': 'false'}, {'timers': 0}, None, []):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                store.save_features(self.path, changes, self.revision)
            self.assertEqual(self.path.read_bytes(), self.before)
            self.assert_clean_helpers()

    def test_unchanged_save_does_not_replace_or_fsync(self):
        with patch.object(store.os, 'replace') as replace, patch.object(store.os, 'fsync') as flush:
            self.assertEqual(self.save({'llm_brain': True, 'timers': True}), self.revision)
        replace.assert_not_called()
        flush.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_clean_helpers()

    def test_adds_only_explicit_missing_switch(self):
        self.save({'timers': False})
        newline = b'\r\n' if b'\r\n' in self.before else b'\n'
        self.assertEqual(self.path.read_bytes(), self.before + b'timers: false' + newline)
        self.assertNotIn('fuzzy_trigger:', self.path.read_text(encoding='utf-8'))

    def test_bom_crlf_and_final_newline_preserved(self):
        before = b'\xef\xbb\xbfllm_brain: true # comment\r\ncustom: "value"'
        self.path.write_bytes(before)
        _, revision = store.load_features(self.path)
        store.save_features(self.path, {'llm_brain': False}, revision)
        self.assertEqual(self.path.read_bytes(), before.replace(b'true', b'false'))

    def test_stale_revision_preserves_external_edit(self):
        external = self.before + b'new_field: "external"\n'
        self.path.write_bytes(external)
        with self.assertRaises(store.ConfigConflictError):
            self.save()
        self.assertEqual(self.path.read_bytes(), external)
        self.assert_clean_helpers()

    def test_second_writer_with_old_revision_is_rejected(self):
        first = self.save()
        with self.assertRaises(store.ConfigConflictError):
            self.save({'content_filter': True})
        self.assertEqual(store.load_features(self.path)[1], first)

    def test_existing_lock_is_not_removed_or_overwritten(self):
        lock = self.path.with_name('.app_settings.yaml.features.lock')
        lock.write_text('belongs to another writer', encoding='utf-8')
        with self.assertRaises(store.ConfigConflictError):
            self.save()
        self.assertEqual(lock.read_text(encoding='utf-8'), 'belongs to another writer')
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_competing_writer_during_flush_is_rejected(self):
        seen = []
        def flush(descriptor):
            try:
                self.save({'content_filter': True})
            except store.ConfigConflictError:
                seen.append('conflict')
        with patch.object(store.os, 'fsync', side_effect=flush):
            self.save()
        self.assertEqual(seen, ['conflict'])
        self.assertFalse(store.load_features(self.path)[0]['content_filter'])
        self.assert_clean_helpers()

    def test_external_edit_during_flush_is_not_overwritten(self):
        external = b'llm_brain: false\ncustom: "external during save"\n'
        with patch.object(store.os, 'fsync', side_effect=lambda fd: self.path.write_bytes(external)), \
             patch.object(store.os, 'replace') as replace:
            with self.assertRaises(store.ConfigConflictError):
                self.save()
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), external)
        self.assert_clean_helpers()

    def test_failed_fsync_keeps_original_and_cleans_temporary_files(self):
        with patch.object(store.os, 'fsync', side_effect=OSError('synthetic flush failure')):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_clean_helpers()

    def test_failed_stream_write_keeps_original_and_cleans_temporary_files(self):
        class FailedStream:
            def __init__(self, descriptor, mode):
                self.descriptor = descriptor
            def __enter__(self):
                return self
            def write(self, payload):
                raise OSError('synthetic full disk on write')
            def __exit__(self, *args):
                os.close(self.descriptor)
        with patch.object(store.os, 'fdopen', side_effect=FailedStream):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_clean_helpers()

    def test_failed_replace_keeps_original_and_cleans_temporary_files(self):
        with patch.object(store.os, 'replace', side_effect=PermissionError('synthetic replace failure')):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_clean_helpers()

    def test_post_commit_lock_cleanup_error_reports_committed_revision(self):
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name.endswith('.features.lock'):
                raise PermissionError('synthetic lock cleanup failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink), self.assertRaises(store.FeatureSaveCleanupError) as result:
            self.save()
        values, revision = store.load_features(self.path)
        self.assertFalse(values['llm_brain'])
        self.assertEqual(result.exception.revision, revision)
        self.assertTrue(self.path.with_name('.app_settings.yaml.features.lock').exists())

    def test_failed_temp_creation_keeps_original_and_releases_lock(self):
        with patch.object(store.tempfile, 'mkstemp', side_effect=OSError('synthetic full disk')):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_clean_helpers()

    def test_unreadable_load_is_not_treated_as_defaults(self):
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('synthetic read failure')):
            with self.assertRaises(PermissionError):
                store.load_features(self.path)

    def test_unsupported_flow_layout_fails_without_rewriting(self):
        text = '{llm_brain: true, custom: 7}\n'
        self.path.write_text(text, encoding='utf-8')
        _, revision = store.load_features(self.path)
        with self.assertRaises(store.ConfigFormatError):
            store.save_features(self.path, {'llm_brain': False}, revision)
        self.assertEqual(self.path.read_text(encoding='utf-8'), text)

    def test_multi_switch_patch_keeps_unexposed_flag(self):
        self.path.write_text('llm_brain: true\nignore_viewer_pokecatch: false\n', encoding='utf-8')
        _, revision = store.load_features(self.path)
        store.save_features(self.path, {'llm_brain': False, 'timers': False}, revision)
        self.assertFalse(store.load_features(self.path)[0]['ignore_viewer_pokecatch'])

    def test_other_editor_stores_keep_their_existing_behavior(self):
        personality = self.folder / 'personality.yaml'
        personality.write_text('personality:\n  name: "Fixture"\n  tone: "calm"\n  trigger_keywords: ["medlar"]\nother: 7\n', encoding='utf-8')
        fields, revision = store.load_personality(personality)
        store.save_personality(personality, 'Changed', fields['tone'], fields['trigger_keywords'], revision)
        self.assertEqual(yaml.safe_load(personality.read_text(encoding='utf-8'))['other'], 7)
        moderation = self.folder / 'moderation.yaml'
        moderation.write_text('blocked_words:\n  - fixtureword\nblocked_topics:\n  - fixturetopic\nother: 7\n', encoding='utf-8')
        _, revision = store.load_moderation(moderation)
        store.save_moderation(moderation, ['secondword'], ['second topic'], revision)
        self.assertEqual(yaml.safe_load(moderation.read_text(encoding='utf-8'))['other'], 7)


class FeatureGuiMethodTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='feature-gui-fake-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'app_settings.yaml'
        self.path.write_text('llm_brain: true\ncontent_filter: true\nunknown: "keep"\n', encoding='utf-8')
        self.window = Window()
        for name in ('chk_llm', 'chk_pcg', 'chk_filter', 'chk_timers', 'chk_fuzzy', 'chk_memory', 'features_status', 'btn_features_retry'):
            setattr(self.window, name, Widget())
        self.window._settings_path = lambda: self.path
        self.assertTrue(self.window.refresh_settings())
        Dialog.choice = 'Cancel'
    def draft_failure(self):
        with patch.object(self.window, '_save_settings', side_effect=OSError('synthetic write failure')):
            self.window.chk_llm.checked = False
            self.assertFalse(self.window.update_toggle('llm_brain', False))

    def test_successful_toggle_saves_actual_value_and_preserves_unknown(self):
        self.assertTrue(self.window.update_toggle('llm_brain', False))
        self.assertFalse(store.load_features(self.path)[0]['llm_brain'])
        self.assertFalse(self.window.chk_llm.checked)
        self.assertEqual(self.window._feature_pending, {})
        self.assertEqual(yaml.safe_load(self.path.read_text())['unknown'], 'keep')

    def test_failed_save_shows_confirmed_value_and_keeps_request(self):
        self.draft_failure()
        self.assertTrue(self.window.chk_llm.checked)
        self.assertTrue(store.load_features(self.path)[0]['llm_brain'])
        self.assertEqual(self.window._feature_pending, {'llm_brain': False})
        self.assertTrue(self.window.btn_features_retry.enabled)
        self.assertIn('Not saved', self.window.features_status.message)

    def test_explicit_retry_persists_retained_request(self):
        self.draft_failure()
        self.assertTrue(self.window.retry_feature_changes())
        self.assertFalse(store.load_features(self.path)[0]['llm_brain'])
        self.assertFalse(self.window.btn_features_retry.enabled)

    def test_retained_request_is_visible_with_its_requested_value(self):
        self.draft_failure()
        self.assertIn('LLM Brain: off', self.window.features_status.message)
        Dialog.choice = 'Keep requests and reload'
        self.assertTrue(self.window.request_feature_reload())
        self.assertIn('LLM Brain: off', self.window.features_status.message)
        self.assertTrue(self.window.chk_llm.checked)

    def test_conflict_requires_reload_and_review_before_retry(self):
        self.path.write_text('llm_brain: true\ncontent_filter: false\nunknown: "external"\n', encoding='utf-8')
        self.assertFalse(self.window.update_toggle('llm_brain', False))
        self.assertTrue(self.window.chk_filter.checked)
        self.assertFalse(self.window.retry_feature_changes())
        Dialog.choice = 'Keep requests and reload'
        self.assertTrue(self.window.request_feature_reload())
        self.assertFalse(self.window.chk_filter.checked)
        self.assertEqual(self.window._feature_pending, {'llm_brain': False})
        self.assertTrue(self.window.retry_feature_changes())
        self.assertEqual(yaml.safe_load(self.path.read_text())['unknown'], 'external')

    def test_reload_cancel_keeps_revision_and_draft(self):
        self.draft_failure()
        revision = self.window._feature_revision
        self.assertFalse(self.window.request_feature_reload())
        self.assertEqual(self.window._feature_revision, revision)
        self.assertEqual(self.window._feature_pending, {'llm_brain': False})

    def test_discard_only_clears_requests_after_successful_reload(self):
        self.draft_failure()
        Dialog.choice = 'Discard requests and reload'
        self.assertTrue(self.window.request_feature_reload())
        self.assertEqual(self.window._feature_pending, {})
        self.assertTrue(self.window.chk_llm.checked)

    def test_failed_discard_reload_preserves_last_good_state_and_draft(self):
        self.draft_failure()
        revision = self.window._feature_revision
        self.path.write_text('llm_brain: [', encoding='utf-8')
        Dialog.choice = 'Discard requests and reload'
        self.assertFalse(self.window.request_feature_reload())
        self.assertEqual(self.window._feature_pending, {'llm_brain': False})
        self.assertEqual(self.window._feature_revision, revision)
        self.assertTrue(self.window.chk_llm.checked)
        self.assertFalse(self.window.chk_llm.enabled)

    def test_post_save_reload_failure_reports_saved_without_inventing_defaults(self):
        with patch.object(self.window, '_load_settings', side_effect=OSError('synthetic reload failure')):
            self.assertFalse(self.window.update_toggle('llm_brain', False))
        self.assertFalse(store.load_features(self.path)[0]['llm_brain'])
        self.assertFalse(self.window.chk_llm.checked)
        self.assertEqual(self.window._feature_pending, {})
        self.assertIn('saved, but reload failed', self.window.features_status.message)

    def test_post_commit_lock_failure_shows_saved_value_and_warning(self):
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name.endswith('.features.lock'):
                raise PermissionError('synthetic lock cleanup failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            self.assertTrue(self.window.update_toggle('llm_brain', False))
        self.assertFalse(self.window.chk_llm.checked)
        self.assertEqual(self.window._feature_pending, {})
        self.assertIn('saved, but the save lock remains', self.window.features_status.message)

    def test_startup_missing_or_invalid_file_does_not_show_enabled_defaults(self):
        del self.window._feature_values
        del self.window._feature_revision
        self.path.unlink()
        self.assertFalse(self.window.refresh_settings())
        self.assertFalse(self.window.chk_llm.checked)
        self.assertFalse(self.window.chk_llm.enabled)
        self.assertFalse(self.window.update_toggle('llm_brain', True))
        self.assertFalse(self.path.exists())

    def test_quoted_false_does_not_enable_filter_or_overwrite_file(self):
        payload = 'llm_brain: true\ncontent_filter: "false"\n'
        self.path.write_text(payload, encoding='utf-8')
        self.assertFalse(self.window.refresh_settings())
        self.assertTrue(self.window.chk_filter.checked)
        self.assertFalse(self.window.update_toggle('timers', False))
        self.assertEqual(self.path.read_text(), payload)

    def test_invalid_toggle_input_is_rejected_without_coercion(self):
        before = self.path.read_bytes()
        for key, value in (('timers', 'false'), ('unknown', False), ('timers', 0)):
            self.assertFalse(self.window.update_toggle(key, value))
        self.assertEqual(self.path.read_bytes(), before)

    def test_load_failure_is_visible_and_does_not_clear_request(self):
        self.draft_failure()
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('synthetic unreadable')):
            self.assertFalse(self.window.refresh_settings())
        self.assertEqual(self.window._feature_pending, {'llm_brain': False})
        self.assertIn('Could not load', self.window.features_status.message)
        self.assertTrue(self.window.chk_llm.checked)

    def test_changed_config_location_requires_explicit_reload(self):
        other = self.path.with_name('other_settings.yaml')
        other.write_bytes(self.path.read_bytes())
        self.window._settings_path = lambda: other
        self.assertFalse(self.window.update_toggle('llm_brain', False))
        self.assertTrue(store.load_features(other)[0]['llm_brain'])
        self.assertIn('location changed', self.window.features_status.message)

    def test_path_resolution_uses_source_or_explicit_root_without_creation(self):
        window = Window()
        with patch.dict(os.environ, {'MEDLARTV_ROOT': ''}), patch.object(Path, 'cwd', side_effect=AssertionError('Caller cwd must not choose config')):
            self.assertEqual(window._settings_path(), ROOT / 'MedlarTV/config/app_settings.yaml')
        missing = self.path.parent / 'nonexistent-project'
        with patch.dict(os.environ, {'MEDLARTV_ROOT': str(missing)}):
            self.assertEqual(window._settings_path(), missing / 'MedlarTV/config/app_settings.yaml')
        self.assertFalse(missing.exists())

    def test_refresh_restores_prior_signal_block_state(self):
        self.window.chk_llm.blocked = True
        self.assertTrue(self.window.refresh_settings())
        self.assertTrue(self.window.chk_llm.blocked)
        self.assertFalse(self.window.chk_filter.blocked)


if __name__ == '__main__':
    unittest.main()
