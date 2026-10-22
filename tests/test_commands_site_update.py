"""Commands-site safety checks; subprocess/build/Git are always mocked."""
import importlib.util
import io
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('offline_catalog_update', ROOT / 'MedlarTV/tools/update_commands_site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class FakeCommands:
    """A fake checkout/build driver; never delegates to an actual command."""
    base = 'a' * 40
    new = 'b' * 40
    remote = 'https://github.com/DevilMedlar/medlar-commands-site.git'
    def __init__(self, public):
        self.public = public
        self.calls = []
        self.fetch_urls = self.push_urls = self.remote
        self.top = str(public)
        self.pending = []
        self.unstaged = False
        self.tracked = list(site.GENERATED_PATHS)
        self.branch, self.upstream = 'main', 'origin/main'
        self.head = self.tracking = self.parent = self.base
        self.changed_files = list(site.GENERATED_PATHS)
        self.changed = True
        self.fail = None
        self.build_action = None
        self.stage_action = None
        self.commit_action = None
        self.checkouts = []
    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if kwargs.get('shell') is not False or kwargs.get('cwd') != site.ROOT:
            raise AssertionError('Explicit cwd/shell=False required')
        if any(key.startswith('GIT_') and key != 'GIT_TERMINAL_PROMPT' for key in kwargs['env']):
            raise AssertionError('Inherited Git redirection variables are forbidden')
        if args[0] != 'git':
            operation, command = 'build', args
        else:
            if args[1:3] != ['-C', str(self.public)]:
                raise AssertionError('Every Git call must target the public checkout')
            command = args[3:]
            while command and command[0] == '-c':
                command = command[2:]
            operation = command[0]
        if self.fail == operation:
            return subprocess.CompletedProcess(args, 7, '', 'synthetic private error body')
        output, status = '', 0
        if operation == 'build':
            expected = [site.sys.executable, '-B', str(ROOT / 'MedlarTV/tools/build_commands_site.py'), '--out', str(self.public / 'docs')]
            if args != expected:
                raise AssertionError('Build must use the exact private source and public output')
            if self.build_action:
                self.build_action()
            else:
                (self.public / 'docs').mkdir(exist_ok=True)
                (self.public / 'docs/index.html').write_text('synthetic catalog', encoding='utf-8')
                (self.public / 'docs/commands.json').write_text('{"commands": []}', encoding='utf-8')
        elif operation == 'remote':
            output = self.push_urls if '--push' in command else self.fetch_urls
        elif operation == 'rev-parse':
            if '--show-toplevel' in command:
                output = self.top
            elif '--abbrev-ref' in command:
                output = self.upstream
            elif command[-1] == 'HEAD^':
                output = self.parent
            elif command[-1].startswith('refs/remotes/'):
                output = self.tracking
            else:
                output = self.head
        elif operation == 'symbolic-ref':
            output = self.branch
        elif operation == 'check-ref-format':
            pass
        elif operation == 'ls-files':
            output = '\n'.join(self.tracked)
        elif operation == 'diff':
            if '--cached' in command:
                if '--quiet' in command:
                    status = int(bool(self.pending))
                else:
                    output = '\0'.join(self.pending) + ('\0' if self.pending else '')
            else:
                status = int(self.unstaged)
        elif operation == 'add':
            if command != ['add', '--', *site.GENERATED_PATHS]:
                raise AssertionError('Only the two generated paths may be staged')
            self.pending = list(site.GENERATED_PATHS) if self.changed else []
            if self.stage_action:
                self.stage_action()
        elif operation == 'commit':
            if command != ['commit', '--only', '-m', 'Update commands', '--', *site.GENERATED_PATHS]:
                raise AssertionError('Commit must be path-only')
            self.assert_empty_hooks(args)
            self.head = self.new
            self.pending = []
            if self.commit_action:
                self.commit_action()
        elif operation == 'diff-tree':
            output = '\0'.join(self.changed_files) + '\0'
        elif operation == 'push':
            self.assert_empty_hooks(args)
            if 'push.followTags=false' not in args or command != ['push', 'origin', self.new + ':refs/heads/main']:
                raise AssertionError('Push must select the verified commit and branch without tags')
        else:
            raise AssertionError('Unexpected fake operation: ' + operation)
        return subprocess.CompletedProcess(args, status, output, '')

    def assert_empty_hooks(self, args):
        values = [value.split('=', 1)[1] for value in args if value.startswith('core.hooksPath=')]
        if len(values) != 1 or not Path(values[0]).is_dir() or list(Path(values[0]).iterdir()):
            raise AssertionError('Commit/push must use an empty temporary hook directory')

    def operations(self):
        found = []
        for args, _ in self.calls:
            if args[0] != 'git':
                found.append('build')
            else:
                command = args[3:]
                while command[0] == '-c':
                    command = command[2:]
                found.append(command[0])
        return found


class CatalogSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='catalog-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.public = Path(self.directory.name) / 'public repo'
        self.public.mkdir()
        self.fake = FakeCommands(self.public)
        ctx = patch.object(site.subprocess, 'run', side_effect=self.fake)
        ctx.start()
        self.addCleanup(ctx.stop)
    def execute(self, publish=False):
        site.CatalogUpdate(self.public, publish=publish).execute()
    def refused(self, publish=False, absent=('build', 'add', 'commit', 'push')):
        with self.assertRaises(site.WorkflowError):
            self.execute(publish)
        for operation in absent:
            self.assertNotIn(operation, self.fake.operations())

    def test_missing_destination_stops_before_any_command(self):
        with self.assertRaises(site.WorkflowError):
            site.CatalogUpdate(self.public / 'missing').execute()
        self.assertEqual(self.fake.calls, [])

    def test_private_repository_is_never_destination(self):
        with self.assertRaises(site.WorkflowError):
            site.CatalogUpdate(ROOT).execute()
        self.assertEqual(self.fake.calls, [])

    def test_parent_checkout_trap_is_rejected(self):
        self.fake.top = str(self.public.parent)
        self.refused()

    def test_git_directory_failure_stops_before_build(self):
        self.fake.fail = 'rev-parse'
        self.refused()

    def test_wrong_fetch_identity_is_rejected(self):
        self.fake.fetch_urls = 'https://github.com/other/private-project.git'
        self.refused()

    def test_wrong_push_identity_is_rejected(self):
        self.fake.push_urls = 'https://github.com/other/private-project.git'
        self.refused(True)

    def test_multiple_push_urls_are_rejected(self):
        self.fake.push_urls += '\nhttps://github.com/other/project.git'
        self.refused(True)

    def test_credential_bearing_remote_is_rejected_without_echo(self):
        self.fake.fetch_urls = 'https://synthetic-secret@github.com/DevilMedlar/medlar-commands-site.git'
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(site.main(['--public-repo', str(self.public)]), 1)
        self.assertNotIn('synthetic-secret', output.getvalue())

    def test_remote_lookup_failure_stops_before_build(self):
        self.fake.fail = 'remote'
        self.refused()

    def test_existing_staged_work_is_untouched(self):
        self.fake.pending = ['unrelated.txt']
        self.refused(True)
        self.assertEqual(self.fake.pending, ['unrelated.txt'])

    def test_existing_staged_catalog_work_is_untouched(self):
        self.fake.pending = ['docs/index.html']
        self.refused(True)
        self.assertEqual(self.fake.pending, ['docs/index.html'])

    def test_dirty_generated_files_are_not_overwritten(self):
        self.fake.unstaged = True
        self.refused()

    def test_untracked_or_ignored_generated_files_are_not_overwritten(self):
        (self.public / 'docs').mkdir()
        target = self.public / 'docs/index.html'
        target.write_text('keep original', encoding='utf-8')
        self.fake.tracked = []
        self.refused()
        self.assertEqual(target.read_text(encoding='utf-8'), 'keep original')

    def test_wrong_output_file_type_is_rejected(self):
        (self.public / 'docs').write_text('keep original', encoding='utf-8')
        self.refused()

    def test_redirected_output_is_rejected_without_real_symlinks(self):
        resolve = Path.resolve
        redirected = self.public / 'docs'
        def mocked(path, *args, **kwargs):
            if path == redirected:
                return self.public.parent / 'outside'
            return resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', mocked):
            self.refused()

    def test_build_only_never_mutates_git_state(self):
        self.execute()
        for operation in ('add', 'commit', 'push'):
            self.assertNotIn(operation, self.fake.operations())
        self.assertTrue((self.public / 'docs/commands.json').is_file())

    def test_explicit_publish_scopes_add_commit_push(self):
        self.execute(True)
        operations = self.fake.operations()
        self.assertLess(operations.index('build'), operations.index('add'))
        self.assertLess(operations.index('add'), operations.index('commit'))
        self.assertLess(operations.index('commit'), operations.index('push'))

    def test_caller_directory_cannot_redirect_operations(self):
        previous = Path.cwd()
        try:
            os.chdir(self.public.parent)
            self.execute(True)
        finally:
            os.chdir(previous)
        self.assertTrue(all(kwargs['cwd'] == ROOT for _, kwargs in self.fake.calls))

    def test_git_environment_cannot_redirect_repo_or_index(self):
        with patch.dict(os.environ, {'GIT_DIR': 'fake-caller', 'GIT_WORK_TREE': 'fake-caller',
                                    'GIT_INDEX_FILE': 'fake-index', 'GIT_CONFIG_COUNT': '1'}):
            self.execute(True)

    def test_build_failure_never_stages(self):
        self.fake.fail = 'build'
        self.refused(True, absent=('add', 'commit', 'push'))

    def test_missing_build_output_never_stages(self):
        self.fake.build_action = lambda: None
        self.refused(True, absent=('add', 'commit', 'push'))

    def test_staged_work_added_during_build_is_untouched(self):
        def build():
            (self.public / 'docs').mkdir()
            for relative in site.GENERATED_PATHS:
                (self.public / relative).write_text('synthetic', encoding='utf-8')
            self.fake.pending = ['unrelated.txt']
        self.fake.build_action = build
        self.refused(True, absent=('add', 'commit', 'push'))
        self.assertEqual(self.fake.pending, ['unrelated.txt'])

    def test_index_contamination_after_add_never_commits(self):
        self.fake.stage_action = lambda: self.fake.pending.append('unrelated.txt')
        self.refused(True, absent=('commit', 'push'))
        self.assertIn('unrelated.txt', self.fake.pending)

    def test_stage_failure_never_commits(self):
        self.fake.fail = 'add'
        self.refused(True, absent=('commit', 'push'))

    def test_commit_failure_never_pushes(self):
        self.fake.fail = 'commit'
        self.refused(True, absent=('push',))

    def test_push_failure_leaves_new_local_commit(self):
        self.fake.fail = 'push'
        self.refused(True, absent=())
        self.assertEqual(self.fake.head, self.fake.new)
        self.assertEqual(self.fake.operations()[-1], 'push')

    def test_no_catalog_change_never_commits_or_pushes(self):
        self.fake.changed = False
        self.execute(True)
        self.assertNotIn('commit', self.fake.operations())
        self.assertNotIn('push', self.fake.operations())

    def test_detached_head_is_rejected_before_build(self):
        self.fake.fail = 'symbolic-ref'
        self.refused(True)

    def test_wrong_upstream_is_rejected_before_build(self):
        self.fake.upstream = 'other/main'
        self.refused(True)

    def test_unpushed_commits_are_not_incidentally_uploaded(self):
        self.fake.tracking = 'c' * 40
        self.refused(True)

    def test_changed_head_during_build_never_stages(self):
        original = self.fake.__call__
        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] != 'git':
                self.fake.head = 'c' * 40
            return result
        with patch.object(site.subprocess, 'run', side_effect=run):
            self.refused(True, absent=('add', 'commit', 'push'))

    def test_changed_remote_during_build_never_stages(self):
        original = self.fake.__call__
        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] != 'git':
                self.fake.push_urls = 'https://github.com/other/repository.git'
            return result
        with patch.object(site.subprocess, 'run', side_effect=run):
            self.refused(True, absent=('add', 'commit', 'push'))

    def test_unrelated_commit_content_never_pushes(self):
        self.fake.changed_files.append('unrelated.txt')
        self.refused(True, absent=('push',))

    def test_changed_commit_parent_never_pushes(self):
        self.fake.parent = 'c' * 40
        self.refused(True, absent=('push',))

    def test_changed_remote_after_commit_never_pushes(self):
        self.fake.commit_action = lambda: setattr(self.fake, 'push_urls', 'https://github.com/other/repository.git')
        self.refused(True, absent=('push',))

    def test_subprocess_start_failure_reports_no_private_error_body(self):
        output = io.StringIO()
        with patch.object(site.subprocess, 'run', side_effect=OSError('private detail')), redirect_stderr(output):
            self.assertEqual(site.main(['--public-repo', str(self.public)]), 1)
        self.assertNotIn('private detail', output.getvalue())

    def test_cli_requires_explicit_destination_and_publish_opt_in(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            site.main([])
        self.assertEqual(failure.exception.code, 2)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(site.main(['--public-repo', str(self.public)]), 0)
        self.assertNotIn('push', self.fake.operations())

    def test_invalid_options_never_run_commands(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            site.main(['--public-repo', str(self.public), '--upload'])
        self.assertEqual(self.fake.calls, [])

    def test_batch_wrapper_has_no_pushd_git_or_path_fallback(self):
        text = (ROOT / 'update-commands-site.bat').read_text(encoding='utf-8').lower()
        self.assertIn('%~dp0', text)
        self.assertIn('disabledelayedexpansion', text)
        self.assertIn('if not exist "%py%"', text)
        self.assertIn('exit /b %errorlevel%', text)
        self.assertNotIn('pushd', text)
        self.assertNotIn('popd', text)
        self.assertNotIn('git ', text.replace('no build or git action ran.', ''))
        self.assertNotIn('c:\\users\\znorr', text)
        self.assertIn('update_commands_site.py" %*', text)


class CatalogRemoteTests(unittest.TestCase):
    def test_exact_https_and_ssh_identities(self):
        for value in ('https://github.com/DevilMedlar/medlar-commands-site.git',
                      'https://github.com/devilmedlar/medlar-commands-site',
                      'git@github.com:DevilMedlar/medlar-commands-site.git',
                      'ssh://git@github.com/DevilMedlar/medlar-commands-site.git'):
            with self.subTest(value=value):
                self.assertTrue(site.is_expected_remote(value))

    def test_other_hosts_repositories_and_credentials_rejected(self):
        for value in ('https://example.com/DevilMedlar/medlar-commands-site.git',
                      'https://github.com/other/medlar-commands-site.git',
                      'https://github.com/DevilMedlar/medlar-commands-site-extra.git',
                      'https://github.com/DevilMedlar/medlar-commands-site.git?token=fake',
                      'http://github.com/DevilMedlar/medlar-commands-site.git',
                      'ssh://git:fake@github.com/DevilMedlar/medlar-commands-site.git',
                      'C:/local/repository'):
            with self.subTest(value=value):
                self.assertFalse(site.is_expected_remote(value))

    def test_existing_builder_produces_only_the_two_scoped_files(self):
        builder = runpy.run_path(str(ROOT / 'MedlarTV/tools/build_commands_site.py'), run_name='offline_catalog_builder')
        with tempfile.TemporaryDirectory(prefix='catalog-build-') as folder:
            out = Path(folder) / 'docs'
            function = builder['build_once']
            with patch.dict(function.__globals__, {'load_commands': lambda: [{'name': 'synthetic'}]}):
                function(out)
            self.assertEqual(sorted(path.name for path in out.iterdir()), ['commands.json', 'index.html'])
            self.assertIn('synthetic', (out / 'commands.json').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
