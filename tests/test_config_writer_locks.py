"""Controlled cooperating-editor interleavings on synthetic configurations."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from MedlarTV.app import config_editor_store as store


class ConfigWriterLockTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='config-writer-')
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def fixture(self, kind, suffix=''):
        path = self.folder / (kind + suffix + '.yaml')
        text = ('# fixture comment\npersonality:\n  name: "Fixture"\n  tone: "warm"\n'
                '  trigger_keywords: ["medlar"]\n  custom: keep\ncustom: preserved\n'
                if kind == 'personality' else
                '# fixture comment\nblocked_words:\n  - fixtureword\nblocked_topics:\n'
                '  - fixturetopic\nadvanced:\n  min_blocked_word_length: 4\ncustom: preserved\n')
        path.write_text(text, encoding='utf-8')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def intent(self, kind, value):
        return ({'name': value, 'tone': 'warm', 'trigger_keywords': ['medlar']}
                if kind == 'personality' else
                {'blocked_words': [value], 'blocked_topics': ['fixturetopic']})

    def save(self, kind, path, intent, revision):
        return getattr(store, 'save_' + kind)(path, **intent, expected_revision=revision)

    def clean(self, path):
        self.assertFalse(path.with_name('.' + path.name + '.features.lock').exists())
        self.assertEqual(list(self.folder.glob('.' + path.name + '.*.tmp')), [])

    def test_contender_at_final_replace_conflicts_and_can_retry(self):
        for kind in ('personality', 'moderation'):
            with self.subTest(kind=kind):
                path, revision = self.fixture(kind)
                first, second = self.intent(kind, 'firstword'), self.intent(kind, 'secondword')
                pending = copy.deepcopy(second)
                outcomes = []
                replace = store.os.replace
                entered = False

                def interleave(source, destination):
                    nonlocal entered
                    if not entered:
                        entered = True
                        try:
                            self.save(kind, path, second, revision)
                            outcomes.append('committed')
                        except store.ConfigConflictError:
                            outcomes.append('conflict')
                    return replace(source, destination)

                with patch.object(store.os, 'replace', side_effect=interleave):
                    current = self.save(kind, path, first, revision)
                self.assertEqual(outcomes, ['conflict'])
                self.assertEqual(second, pending)
                after = path.read_bytes()
                self.assertIn(b'firstword', after)
                self.assertNotIn(b'secondword', after)
                self.assertIn(b'# fixture comment', after)
                self.assertIn(b'custom: preserved', after)
                with self.assertRaises(store.ConfigConflictError):
                    self.save(kind, path, second, revision)
                self.assertEqual(path.read_bytes(), after)
                self.assertEqual(second, pending)
                self.save(kind, path, second, current)
                self.assertIn(b'secondword', path.read_bytes())
                self.clean(path)

    def test_foreign_lock_preserved(self):
        for kind in ('personality', 'moderation'):
            with self.subTest(kind=kind):
                path, revision = self.fixture(kind)
                before = path.read_bytes()
                lock = path.with_name('.' + path.name + '.features.lock')
                lock.write_bytes(b'synthetic owner')
                with self.assertRaises(store.ConfigConflictError):
                    self.save(kind, path, self.intent(kind, 'changedword'), revision)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(lock.read_bytes(), b'synthetic owner')
                lock.unlink()

    def test_other_path_can_commit_during_replace(self):
        for kind in ('personality', 'moderation'):
            with self.subTest(kind=kind):
                path, revision = self.fixture(kind)
                other, other_revision = self.fixture(kind, '-other')
                replace = store.os.replace
                entered = False

                def interleave(source, destination):
                    nonlocal entered
                    if not entered:
                        entered = True
                        self.save(kind, other, self.intent(kind, 'otherword'), other_revision)
                    return replace(source, destination)

                with patch.object(store.os, 'replace', side_effect=interleave):
                    self.save(kind, path, self.intent(kind, 'firstword'), revision)
                self.assertIn(b'otherword', other.read_bytes())
                self.clean(path)
                self.clean(other)

    def test_flush_and_replace_failure_preserve_original_and_release_lock(self):
        for kind in ('personality', 'moderation'):
            for operation in ('fsync', 'replace'):
                with self.subTest(kind=kind, operation=operation):
                    path, revision = self.fixture(kind)
                    before = path.read_bytes()
                    with patch.object(store.os, operation, side_effect=OSError('synthetic failure')):
                        with self.assertRaises(OSError):
                            self.save(kind, path, self.intent(kind, 'changedword'), revision)
                    self.assertEqual(path.read_bytes(), before)
                    self.clean(path)

    def test_external_edit_during_flush_is_preserved(self):
        for kind in ('personality', 'moderation'):
            with self.subTest(kind=kind):
                path, revision = self.fixture(kind)
                external = path.read_bytes() + b'# synthetic external edit\n'
                with patch.object(store.os, 'fsync', side_effect=lambda fd: path.write_bytes(external)):
                    with self.assertRaises(store.ConfigConflictError):
                        self.save(kind, path, self.intent(kind, 'changedword'), revision)
                self.assertEqual(path.read_bytes(), external)
                self.clean(path)

    def test_unchanged_payload_releases_lock_without_replacement(self):
        for kind in ('personality', 'moderation'):
            with self.subTest(kind=kind):
                path, revision = self.fixture(kind)
                values, loaded_revision = getattr(store, 'load_' + kind)(path)
                before = path.read_bytes()
                with patch.object(store.os, 'replace', side_effect=AssertionError('unexpected replacement')):
                    self.assertEqual(self.save(kind, path, values, loaded_revision), revision)
                self.assertEqual(path.read_bytes(), before)
                self.clean(path)

    def test_stale_revision_does_not_leave_helpers(self):
        for kind in ('personality', 'moderation'):
            with self.subTest(kind=kind):
                path, revision = self.fixture(kind)
                path.write_bytes(path.read_bytes() + b'# later\n')
                before = path.read_bytes()
                with self.assertRaises(store.ConfigConflictError):
                    self.save(kind, path, self.intent(kind, 'changedword'), revision)
                self.assertEqual(path.read_bytes(), before)
                self.clean(path)

    def test_missing_parent_is_not_created(self):
        for kind in ('personality', 'moderation'):
            with self.subTest(kind=kind):
                path = self.folder / 'absent' / (kind + '.yaml')
                with self.assertRaises(FileNotFoundError):
                    self.save(kind, path, self.intent(kind, 'changedword'), 'missing')
                self.assertFalse(path.parent.exists())
