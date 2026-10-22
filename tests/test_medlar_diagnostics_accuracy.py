"""Full private diagnostic workflow and accuracy contracts; synthetic collaborators only."""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
import io
import json
import socket
import tempfile
import unittest
import sys
from types import SimpleNamespace
from http.client import IncompleteRead

import test_medlar_diagnostics as legacy

d = legacy.diagnostics
MARKER = 'synthetic-full-private-evidence-76543'
ROOT = Path(__file__).resolve().parents[1]


def row(run, name):
    return next(c for c in (run.checks if isinstance(run, d.DiagnosticRun) else run) if c.name == name)


def transport_fixture():
    helper = legacy.TwitchExerciseTests('runTest')
    delegate, calls, state = helper._successful_http()
    def transport(method, url, **kwargs):
        result = delegate(method, url, **kwargs)
        if isinstance(result.payload, dict) and 'access_token' in result.payload:
            body = {**result.payload, 'expires_in': 3600, 'token_type': 'bearer'}
            return d.HttpJsonResult(result.status, payload=body, raw_body=json.dumps(body))
        if '/helix/channels?' in url and method == 'GET':
            body = {'data': [{**result.payload['data'][0], 'broadcaster_id': 'channel-42'}]}
            return d.HttpJsonResult(result.status, payload=body, raw_body=json.dumps(body))
        return d.HttpJsonResult(result.status, payload=result.payload, error=result.error,
                                raw_body=json.dumps(result.payload) if result.payload is not None else '',
                                response_headers=(('X-Synthetic', MARKER),))
    return helper, transport, calls, state


LOCAL_CHECKS = (
    'check_source_layout', 'check_source_syntax', 'check_component_contracts', 'check_project_inventory',
    'check_public_twitch_settings', 'check_credential_loader_contract', 'check_configs', 'check_config_schemas',
    'check_event_configuration', 'check_dependencies', 'check_dependency_imports', 'check_legacy_setup_claims',
    'check_launcher_controls', 'check_runtime_writability', 'check_bridge_readiness',
    'check_twitch_source_compatibility', 'check_ollama_executable', 'check_ollama_bind_alignment',
    'check_ollama', 'check_ollama_launch_precondition', 'check_libretranslate', 'check_memory_paths',
)


@contextmanager
def local_fixtures():
    with ExitStack() as stack:
        patches = {}
        for name in LOCAL_CHECKS:
            patches[name] = stack.enter_context(mock.patch.object(d, name, return_value=[
                d.Check('Synthetic local ' + name, name, d.READY, MARKER + name)]))
        stack.enter_context(mock.patch.object(d, 'launcher_python', return_value=None))
        stack.enter_context(mock.patch.object(d, '_scope_catalog', return_value={'chat:read': 'Read', 'chat:edit': 'Write'}))
        yield patches


def synthetic_collect(**overrides):
    helper, http, calls, state = transport_fixture()
    args = dict(root=ROOT, environ=helper._environment(), use_env_file=False, use_vault=False,
                probe_services=True, probe_writes=False, live_twitch=True, include_secret_values=True,
                http_json=http, irc_probe=lambda *a: d.IrcProbeResult(True, True, True))
    args.update(overrides)
    return d.collect_diagnostics(**args), calls, state


@contextmanager
def cli_fixture(run):
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        root = Path(tmp)
        (root / '.gitignore').write_text('/logs/\n', encoding='utf-8')
        stack.enter_context(mock.patch.object(d, 'REPO_ROOT', root))
        stack.enter_context(mock.patch.object(d, 'DEFAULT_REPORT', root / 'logs/medlar_diagnostics.txt'))
        stack.enter_context(mock.patch.object(d, 'reexec_in_launcher_python', return_value=None))
        collect = stack.enter_context(mock.patch.object(d, 'collect_diagnostics', return_value=run))
        editor = stack.enter_context(mock.patch.object(d, 'open_in_editor'))
        stdout = stack.enter_context(redirect_stdout(io.StringIO()))
        stderr = stack.enter_context(redirect_stderr(io.StringIO()))
        yield root, collect, editor, stdout, stderr


class AccuracyWorkflowTests(unittest.TestCase):
    def test_normal_invocation_saves_one_full_report_with_original_flags(self):
        with local_fixtures():
            run, calls, _ = synthetic_collect()
        with cli_fixture(run) as (root, collect, editor, out, err):
            self.assertEqual(d.main([]), 0)
            files = list((root / 'logs').iterdir())
            self.assertEqual([p.name for p in files], ['medlar_diagnostics.txt'])
            text = files[0].read_text(encoding='utf-8')
            self.assertIn('PRIVATE / FULL / SENSITIVE', text)
            self.assertIn('synthetic-bot-token', text)
            self.assertIn('synthetic-app-secret', text)
            self.assertIn('HTTP REQUEST/RESPONSE TRANSCRIPT', text)
            self.assertIn(MARKER, text)
            self.assertNotIn('synthetic-bot-token', out.getvalue() + err.getvalue())
            self.assertTrue(collect.call_args.kwargs['live_twitch'])
            self.assertTrue(collect.call_args.kwargs['probe_services'])
            self.assertTrue(collect.call_args.kwargs['include_secret_values'])
            editor.assert_not_called()
            self.assertIn('WARNING', err.getvalue())
        self.assertFalse(any(method == 'PATCH' for method, *_ in calls))

    def test_open_remains_explicit_and_runs_after_save(self):
        run = d.DiagnosticRun([d.Check('Original section', 'Original row', d.READY, MARKER)])
        with cli_fixture(run) as (root, collect, editor, out, err):
            editor.side_effect = lambda p: self.assertIn(MARKER, p.read_text(encoding='utf-8'))
            self.assertEqual(d.main(['--open']), 0)
            editor.assert_called_once_with(root / 'logs/medlar_diagnostics.txt')

    def test_interpreter_relaunch_failure_is_saved_without_collection(self):
        with cli_fixture(d.DiagnosticRun()) as (root, collect, editor, out, err), \
                mock.patch.object(d, 'reexec_in_launcher_python', side_effect=OSError(MARKER)):
            self.assertEqual(d.main([]), 2)
            collect.assert_not_called()
            text = (root / 'logs/medlar_diagnostics.txt').read_text(encoding='utf-8')
            self.assertIn('interpreter relaunch', text)
            self.assertIn('OSError: ' + MARKER, text)
            self.assertNotIn(MARKER, out.getvalue() + err.getvalue())

    def test_interpreter_relaunch_preserves_underlying_exception_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); executable = root / 'python.exe'; executable.write_bytes(b'')
            with mock.patch.object(d, 'launcher_python', return_value=executable), \
                    mock.patch.object(d, '_same_path', return_value=False):
                with self.assertRaises(RuntimeError) as failed:
                    d.reexec_in_launcher_python([], root=root, environ={}, runner=mock.Mock(side_effect=OSError(MARKER)))
            self.assertIn('OSError: ' + MARKER, d._exception_evidence(failed.exception))

    def test_static_metadata_and_shareable_cli_modes_are_absent(self):
        with cli_fixture(d.DiagnosticRun()) as (_, collect, _, _, _):
            for argv in (['--profile', 'static'], ['--private-capture'], ['--share-safe']):
                with self.assertRaises(SystemExit): d.main(argv)
            collect.assert_not_called()

    def test_all_original_sections_rows_credentials_and_http_bytes_are_retained(self):
        with local_fixtures(): run, _, _ = synthetic_collect()
        run.failures.append({'stage': 'Synthetic failed stage', 'exception': 'RuntimeError', 'traceback': MARKER})
        text = d.render_report(run)
        for c in run.checks:
            self.assertIn(c.section.upper(), text)
            self.assertIn(c.name, text)
            self.assertIn(c.detail, text)
        for exchange in run.http_exchanges:
            self.assertIn(exchange.url, text)
            if exchange.response_body: self.assertIn(exchange.response_body, text)
            if exchange.request_body: self.assertIn(exchange.request_body, text)
        for key, value in run.sensitive_values.items():
            self.assertIn(key, text)
            if value: self.assertIn(value, text)
        for heading in ('CONFIGURATION', 'PRIVATE CREDENTIAL VALUES USED', 'HTTP REQUEST/RESPONSE TRANSCRIPT',
                        'BOUNDARIES', 'ORIGINAL-SCRIPT PARITY', 'DIAGNOSTIC FAILURES AND TRACEBACKS'):
            self.assertIn(heading, text)
        self.assertGreater(len(text), 1000)

    def test_no_service_probes_skips_every_local_network_or_bind_collaborator(self):
        with local_fixtures() as checks:
            run, _, _ = synthetic_collect(probe_services=False)
            checks['check_legacy_setup_claims'].assert_not_called()
            checks['check_ollama_launch_precondition'].assert_not_called()
            self.assertFalse(checks['check_ollama'].call_args.kwargs['probe_services'])
            self.assertFalse(checks['check_libretranslate'].call_args.kwargs['probe_services'])
            self.assertFalse(checks['check_bridge_readiness'].call_args.kwargs['probe_services'])
        self.assertEqual(row(run, 'Launcher precondition').status, d.SKIPPED)

    def test_offline_keeps_original_local_collection_without_twitch_io(self):
        http = mock.Mock(side_effect=AssertionError('Twitch I/O forbidden'))
        with local_fixtures() as checks:
            run, _, _ = synthetic_collect(live_twitch=False, http_json=http)
            checks['check_configs'].assert_called_once()
            checks['check_dependency_imports'].assert_called_once()
        http.assert_not_called()
        self.assertTrue(run.offline)
        self.assertFalse(run.complete)

    def test_independent_check_exception_preserves_traceback_and_continues(self):
        with local_fixtures() as checks:
            checks['check_configs'].side_effect = RuntimeError(MARKER)
            run, _, _ = synthetic_collect()
            checks['check_memory_paths'].assert_called_once()
        text = d.render_report(run)
        self.assertTrue(run.collection_failed)
        self.assertFalse(run.complete)
        self.assertIn('check_configs', text)
        self.assertIn('Traceback (most recent call last)', text)
        self.assertIn('RuntimeError: ' + MARKER, text)
        self.assertIn('synthetic-bot-token', text)

    def test_outer_collection_exception_preserves_completed_checks_and_skipped_coverage(self):
        with local_fixtures(), mock.patch.object(d, 'effective_public_environment', side_effect=ValueError(MARKER)):
            run, _, _ = synthetic_collect()
        self.assertTrue(run.collection_failed)
        self.assertIn(MARKER + 'check_source_layout', d.render_report(run))
        self.assertEqual(row(run, 'Bot token validation').status, d.SKIPPED)
        self.assertIn('ValueError: ' + MARKER, d.render_report(run))

    def test_live_stage_exception_keeps_resolved_credentials_and_completed_live_rows(self):
        helper, http, calls, _ = transport_fixture()
        def fail(method, url, **kwargs):
            if '/helix/channels?' in url: raise RuntimeError(MARKER)
            return http(method, url, **kwargs)
        with local_fixtures(): run, _, _ = synthetic_collect(http_json=fail)
        text = d.render_report(run)
        self.assertEqual(row(run, 'Bot token validation').status, d.READY)
        self.assertIn('synthetic-bot-token', text)
        self.assertIn(MARKER, text)
        self.assertFalse(run.complete)

    def test_unexpected_live_exception_retains_partial_live_rows_and_credentials(self):
        with local_fixtures(), mock.patch.object(d, '_read_only_twitch_api_checks', side_effect=RuntimeError(MARKER)):
            run, _, _ = synthetic_collect()
        self.assertTrue(run.collection_failed)
        self.assertEqual(row(run, 'Bot token validation').status, d.READY)
        self.assertEqual(row(run, 'Broadcaster token validation').status, d.READY)
        text = d.render_report(run)
        self.assertIn('synthetic-bot-token', text)
        self.assertIn('synthetic-app-token', text)
        self.assertIn('RuntimeError: ' + MARKER, text)
        self.assertIn('HTTP REQUEST/RESPONSE TRANSCRIPT', text)

    def test_source_loader_registers_dataclasses_and_restores_failed_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'synthetic_loader.py'
            source.write_text('from __future__ import annotations\nfrom dataclasses import dataclass\n@dataclass\nclass Entry:\n    value: str\n', encoding='utf-8')
            name = '_synthetic_diagnostic_loader'
            try:
                module = d._load_source_module(source, name)
                self.assertIs(sys.modules[name], module)
                self.assertEqual(module.Entry(MARKER).value, MARKER)
                source.write_text('raise RuntimeError("synthetic loader failure")\n', encoding='utf-8')
                with self.assertRaises(RuntimeError): d._load_source_module(source, name)
                self.assertIs(sys.modules[name], module)
            finally:
                sys.modules.pop(name, None)

    def test_render_failure_serializes_all_private_fields_to_same_file(self):
        with local_fixtures(): run, _, _ = synthetic_collect()
        with cli_fixture(run) as (root, _, _, _, _), mock.patch.object(d, 'render_report', side_effect=RuntimeError(MARKER)):
            self.assertEqual(d.main([]), 2)
            text = (root / 'logs/medlar_diagnostics.txt').read_text(encoding='utf-8')
            self.assertIn('RENDERING FAILURE', text)
            self.assertIn('synthetic-bot-token', text)
            self.assertIn(run.http_exchanges[0].response_body, text)
            self.assertIn('Traceback', text)
            self.assertEqual(len(list((root / 'logs').iterdir())), 1)

    def test_failure_tracebacks_do_not_dump_locals(self):
        private_local = 'synthetic-local-not-diagnostic-evidence'
        try:
            raise RuntimeError('plain failure')
        except RuntimeError as exc:
            text = d._exception_evidence(exc)
        self.assertIn('RuntimeError: plain failure', text)
        self.assertNotIn(private_local, text)

    def test_save_failure_keeps_stale_sentinel_and_creates_no_fallback(self):
        with cli_fixture(d.DiagnosticRun()) as (root, _, editor, out, err):
            p = root / 'logs/medlar_diagnostics.txt'; p.parent.mkdir(); p.write_text('OLD SYNTHETIC REPORT')
            with mock.patch.object(d, 'write_report', side_effect=PermissionError(13, MARKER)):
                self.assertEqual(d.main(['--open']), 2)
            self.assertEqual(p.read_text(), 'OLD SYNTHETIC REPORT')
            self.assertEqual(len(list(p.parent.iterdir())), 1)
            editor.assert_not_called()
            self.assertIn('report save', err.getvalue())
            self.assertIn('PermissionError', err.getvalue())
            self.assertIn('errno=13', err.getvalue())
            self.assertIn('stale', err.getvalue())
            self.assertNotIn(MARKER, out.getvalue() + err.getvalue())

    def test_atomic_replace_failure_preserves_existing_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'report.txt'; p.write_text('OLD SYNTHETIC REPORT')
            with mock.patch.object(d.os, 'replace', side_effect=PermissionError(13, MARKER)):
                with self.assertRaises(PermissionError): d.write_report(p, MARKER, default_report=p)
            self.assertEqual(p.read_text(), 'OLD SYNTHETIC REPORT')
            self.assertEqual([q.name for q in p.parent.iterdir()], ['report.txt'])

    def test_editor_failure_is_appended_to_same_private_report(self):
        run = d.DiagnosticRun([d.Check('Original section', 'Original row', d.READY, MARKER)])
        with cli_fixture(run) as (root, _, editor, out, err):
            editor.side_effect = OSError('synthetic-editor-exception')
            self.assertEqual(d.main(['--open']), 3)
            text = (root / 'logs/medlar_diagnostics.txt').read_text(encoding='utf-8')
            self.assertIn('synthetic-editor-exception', text)
            self.assertIn(MARKER, text)
            self.assertIn('report editor', text)
            self.assertNotIn('synthetic-editor-exception', out.getvalue() + err.getvalue())

    def test_sensitive_destination_and_reparse_guards_remain_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.gitignore').write_text('/logs/\n')
            with self.assertRaises(ValueError): d.ensure_sensitive_output_allowed(root / 'outside.txt', root)
            (root / '.gitignore').write_text('')
            with self.assertRaises(ValueError): d.ensure_sensitive_output_allowed(root / 'logs/report.txt', root)
            with mock.patch.object(d, '_is_link_or_reparse_point', return_value=True):
                with self.assertRaises(ValueError): d.ensure_output_allowed(root / 'logs/report.txt')

    def test_sensitive_report_must_be_effectively_ignored_and_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.gitignore').write_text('/logs/\n'); (root / '.git').mkdir()
            with mock.patch.object(d.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as git:
                with self.assertRaises(ValueError): d.ensure_sensitive_output_allowed(root / 'logs/report.txt', root)
            self.assertIn('check-ignore', git.call_args.args[0])


class AccuracyVerdictTests(unittest.TestCase):
    def test_valid_fixture_is_complete_only_when_all_mandatory_rows_pass(self):
        with local_fixtures(): run, _, _ = synthetic_collect()
        self.assertTrue(run.complete)
        self.assertEqual(row(run, 'Run completeness').status, d.READY)
        for key in d._required_live_rows():
            rows = [c for c in run.checks if (c.section, c.name) == key]
            self.assertEqual(len(rows), 1, key)
            self.assertEqual(rows[0].status, d.READY, key)

    def test_missing_duplicate_or_failed_rows_prevent_completeness(self):
        required = [('Section', 'Required')]
        ready = d.Check('Section', 'Required', d.READY, MARKER)
        for rows in ([], [ready, ready], [d.Check('Section', 'Required', d.SKIPPED, MARKER)]):
            self.assertFalse(d._mandatory_pass(rows, required))

    def test_malformed_token_fields_fail_without_losing_raw_response(self):
        helper, transport, _, _ = transport_fixture()
        base = {'login': 'diagnostic_bot', 'client_id': 'synthetic-bot-client', 'user_id': 'bot-1',
                'scopes': ['chat:read', 'chat:edit'], 'expires_in': 3600}
        invalid = [('login', None), ('client_id', ''), ('user_id', 12), ('scopes', MARKER),
                   ('scopes', ['chat:read', None]), ('expires_in', True), ('expires_in', -1)]
        for key, value in invalid:
            body = {**base, key: value, 'retained_invalid_evidence': MARKER}
            def http(method, url, **kwargs):
                if kwargs.get('headers', {}).get('Authorization') == 'OAuth synthetic-bot-token':
                    return d.HttpJsonResult(200, payload=body, raw_body=json.dumps(body))
                return transport(method, url, **kwargs)
            with self.subTest(field=key, value=value), local_fixtures(): run, _, _ = synthetic_collect(http_json=http)
            self.assertEqual(row(run, 'Bot token validation').status, d.UNAVAILABLE)
            self.assertNotEqual(row(run, 'Bot authentication').status, d.READY)
            self.assertIn(json.dumps(body), d.render_report(run))

    def test_identity_mismatch_prevents_aggregate_authentication_pass(self):
        for role in ('Bot', 'Broadcaster'):
            helper, transport, _, _ = transport_fixture()
            token = 'OAuth synthetic-bot-token' if role == 'Bot' else 'OAuth synthetic-broadcaster-token'
            def http(method, url, **kwargs):
                result = transport(method, url, **kwargs)
                if url.endswith('/validate') and kwargs.get('headers', {}).get('Authorization') == token:
                    body = {**result.payload, 'login': 'wrong_account'}
                    return d.HttpJsonResult(200, payload=body, raw_body=json.dumps(body))
                return result
            with self.subTest(role=role), local_fixtures(): run, _, _ = synthetic_collect(http_json=http)
            self.assertEqual(row(run, role + ' token validation').status, d.READY)
            self.assertEqual(row(run, role + ' identity').status, d.UNAVAILABLE)
            self.assertEqual(row(run, role + ' authentication').status, d.UNAVAILABLE)
            self.assertFalse(run.complete)

    def test_app_exchange_schema_rejects_invalid_fields_with_full_evidence(self):
        for key, value in [('access_token', ''), ('expires_in', True), ('expires_in', 0), ('token_type', 'wrong')]:
            helper, transport, _, _ = transport_fixture()
            body = {'access_token': MARKER, 'expires_in': 3600, 'token_type': 'bearer', key: value}
            def http(method, url, **kwargs):
                return d.HttpJsonResult(200, payload=body, raw_body=json.dumps(body)) if method == 'POST' else transport(method, url, **kwargs)
            with self.subTest(field=key), local_fixtures(): run, _, _ = synthetic_collect(http_json=http)
            self.assertEqual(row(run, 'App credential exchange').status, d.UNAVAILABLE)
            for name in (*d.READ_ONLY_TWITCH_API_NAMES, 'Account-age source data'):
                self.assertEqual(row(run, name).status, d.SKIPPED)
            self.assertIn(json.dumps(body), d.render_report(run))

    def test_app_identity_correlation_is_required_for_aggregate_pass(self):
        helper, transport, _, _ = transport_fixture()
        def http(method, url, **kwargs):
            result = transport(method, url, **kwargs)
            if '/helix/users?' in url:
                body = {'data': [{**result.payload['data'][0], 'id': 'different-user'}]}
                return d.HttpJsonResult(200, payload=body, raw_body=json.dumps(body))
            return result
        with local_fixtures(): run, _, _ = synthetic_collect(http_json=http)
        self.assertEqual(row(run, 'App/API authentication').status, d.UNAVAILABLE)
        self.assertFalse(run.complete)

    def test_incomplete_parseable_body_retains_payload_and_cannot_pass(self):
        body = b'{"id":"synthetic-partial-evidence"}'
        class Partial(legacy._FakeResponse):
            def read(self, size=None): raise IncompleteRead(body, 100)
        result = d.request_json('GET', 'https://synthetic.invalid', opener=lambda *a, **k: Partial(body))
        self.assertEqual(result.payload, json.loads(body))
        self.assertEqual(result.raw_body, body.decode())
        self.assertTrue(result.body_truncated)
        self.assertIsNotNone(result.error)
        helper, transport, _, _ = transport_fixture()
        def http(method, url, **kwargs):
            current = transport(method, url, **kwargs)
            if url.endswith('/validate'):
                return d.HttpJsonResult(200, payload=current.payload, raw_body=current.raw_body, body_truncated=True)
            return current
        with local_fixtures(): run, _, _ = synthetic_collect(http_json=http)
        self.assertNotEqual(row(run, 'Bot token validation').status, d.READY)
        self.assertIn('INCOMPLETE', d.render_report(run))

    def test_complete_response_is_not_bounded_or_shortened(self):
        body = json.dumps({'evidence': MARKER * 50000}).encode()
        response = legacy._FakeResponse(body)
        result = d.request_json('GET', 'https://synthetic.invalid', opener=lambda *a, **k: response)
        self.assertEqual(response.read_limits, [None])
        self.assertEqual(result.raw_body.encode(), body)
        self.assertFalse(result.body_truncated)

    def test_transport_exception_text_and_traceback_are_full_private_evidence(self):
        exchanges = []
        transport = d._recording_http_json(mock.Mock(side_effect=OSError(MARKER)), exchanges)
        result = d._safe_http_call(transport, 'GET', 'https://synthetic.invalid', headers={'Authorization': MARKER})
        self.assertIn('OSError: ' + MARKER, result.error)
        text = d.render_report(d.DiagnosticRun(http_exchanges=exchanges))
        self.assertIn('OSError: ' + MARKER, text)
        self.assertIn('Traceback', text)

    def test_malformed_api_rows_and_channel_id_cannot_pass(self):
        helper, transport, _, _ = transport_fixture()
        for endpoint, body in [('/helix/channels?', {'data': [{}]}), ('/helix/streams?', {'data': [MARKER]}),
                               ('/helix/search/categories?', {'data': [{'id': 123, 'name': MARKER}]}),
                               ('/helix/users?', {'data': [{'login': 'target_channel', 'id': 123}]})]:
            def http(method, url, **kwargs):
                return d.HttpJsonResult(200, payload=body, raw_body=json.dumps(body)) if endpoint in url else transport(method, url, **kwargs)
            with self.subTest(endpoint=endpoint), local_fixtures(): run, _, _ = synthetic_collect(http_json=http)
            self.assertFalse(run.complete)
            self.assertIn(json.dumps(body), d.render_report(run))


class AccuracyGateAndBridgeTests(unittest.TestCase):
    def test_direct_exercise_requires_validated_context_before_any_io(self):
        helper, _, _, _ = transport_fixture(); http = mock.Mock(); irc = mock.Mock()
        checks = d.run_live_twitch_checks(helper._environment(), exercise=helper._exercise(), http_json=http, irc_sender=irc)
        http.assert_not_called(); irc.assert_not_called()
        self.assertEqual(row(checks, 'Full exercise result').status, d.UNAVAILABLE)

    def test_changed_title_before_write_preserved_and_no_patch_or_chat_sent(self):
        helper, delegate, calls, state = transport_fixture(); reads = 0
        def transport(method, url, **kwargs):
            nonlocal reads
            if method == 'GET' and '/helix/channels?' in url:
                reads += 1
                if reads == 3: state['title'] = MARKER
            return delegate(method, url, **kwargs)
        irc = mock.Mock()
        with mock.patch.object(d, '_scope_catalog', return_value={}):
            checks = d.run_live_twitch_checks(helper._environment(), root=ROOT, exercise=helper._exercise(), http_json=transport,
                                              irc_sender=irc, gate_context=legacy.synthetic_gate())
        self.assertEqual(state['title'], MARKER)
        self.assertFalse(any(method == 'PATCH' for method, *_ in calls))
        irc.assert_not_called()
        self.assertNotEqual(row(checks, 'Full exercise result').status, d.READY)

    def test_endpoint_transport_errors_are_unknown_except_refusal(self):
        for error, expected in [(PermissionError(), None), (socket.gaierror(), None), (TimeoutError(), None),
                                (OSError(), None), (ConnectionRefusedError(), True)]:
            actual, _ = d.probe_ollama_endpoint_stopped({}, ROOT, connector=mock.Mock(side_effect=error))
            self.assertIs(actual, expected)

    def test_process_ownership_module_adapter_and_sibling_paths(self):
        root = Path(r'C:\synthetic\Medlar')
        for command in [r'python C:\synthetic\Medlar\launcher.py', 'python -m MedlarTV.core.main',
                        'python MedlarTV/tools/python_service_entry.py --module MedlarTV.core.main']:
            rows = [{'pid': 2, 'parent': 1, 'name': 'python.exe', 'command': command,
                     'executable': r'C:\synthetic\Medlar\.venv\Scripts\python.exe'}]
            actual = d.classify_process_snapshot(rows, root)
            self.assertTrue(actual.probe_completed)
            self.assertEqual(actual.medlar_process_count, 1)
        for executable in [r'C:\synthetic\Medlar-copy\.venv\python.exe', r'C:\Python\python.exe']:
            actual = d.classify_process_snapshot([{'pid': 2, 'parent': 1, 'name': 'python.exe',
                                                   'command': 'python -m MedlarTV.core.main', 'executable': executable}], root)
            self.assertFalse(actual.probe_completed)

    def test_irc_disconnection_is_not_claimed_without_ownership_sampling(self):
        root = Path(r'C:\synthetic\Medlar')
        processes = [{'pid': 2, 'parent': 1, 'name': 'python.exe', 'command': 'python -m MedlarTV.core.main',
                      'executable': r'C:\synthetic\Medlar\.venv\Scripts\python.exe'}]
        baseline = d.classify_process_snapshot(processes, root)
        rows, passed = d.required_stopped_baseline_checks(baseline, True, 'synthetic refusal')
        self.assertEqual(row(rows, 'Medlar Twitch disconnected').status, d.SKIPPED)
        self.assertFalse(passed)
        baseline = d.classify_process_snapshot(processes, root, [{'pid': 2, 'port': 6697}, {'pid': 9, 'port': 6697}])
        self.assertEqual(baseline.medlar_irc_connection_count, 1)
        self.assertTrue(baseline.irc_probe_completed)

    def test_bridge_without_existing_session_is_unavailable_and_does_not_probe(self):
        probe = mock.Mock()
        checks = d.check_bridge_readiness(probe=probe)
        self.assertEqual(row(checks, 'Avatar bridge handshake').status, d.UNAVAILABLE)
        probe.assert_not_called()
        self.assertIn('per-launch subscriber capability', checks[0].detail)

    def test_bridge_validates_existing_session_without_generating_keys(self):
        session = {'version': 1, 'role': 'subscriber', 'url': 'ws://127.0.0.1:8765', 'channel': '#synthetic', 'key': 'a' * 64}
        probe = mock.Mock(return_value=True)
        checks = d.check_bridge_readiness(root=ROOT, session_context=session, probe=probe)
        self.assertEqual(row(checks, 'Avatar bridge handshake').status, d.READY)
        self.assertEqual(probe.call_args.args[0].key, 'a' * 64)
        for bad in ({**session, 'key': ''}, {**session, 'role': 'publisher'}, {**session, 'url': 'ws://external.invalid:8765'}):
            probe.reset_mock()
            self.assertEqual(d.check_bridge_readiness(root=ROOT, session_context=bad, probe=probe)[0].status, d.UNAVAILABLE)
            probe.assert_not_called()

    def test_bridge_wire_contract_authenticates_only_and_checks_role_and_channel(self):
        auth = d._load_source_module(ROOT / 'MedlarTV/avatar/bridge/auth.py', '_synthetic_bridge_auth')
        session = auth.parse_bootstrap({'version': 1, 'role': 'subscriber', 'url': 'ws://127.0.0.1:8765',
                                        'channel': '#synthetic', 'key': 'a' * 64}, 'subscriber')
        sent = []
        response = {'event': 'handshake', 'msg': 'connected', 'role': 'subscriber', 'channel': '#synthetic'}
        class Socket:
            async def send(self, payload): sent.append(json.loads(payload))
            async def recv(self): return json.dumps(response)
        class Connection:
            async def __aenter__(self): return Socket()
            async def __aexit__(self, *args): return False
        async def immediate(awaitable, timeout): return await awaitable
        connect = mock.Mock(return_value=Connection())
        with mock.patch.dict(sys.modules, {'websockets': SimpleNamespace(connect=connect)}), \
                mock.patch.object(d.asyncio, 'wait_for', side_effect=immediate):
            coroutine = d._bridge_handshake(session)
            try:
                with self.assertRaises(StopIteration) as done: coroutine.send(None)
                self.assertIs(done.exception.value, True)
            finally: coroutine.close()
            self.assertEqual(sent, [{'event': 'authenticate', 'token': 'a' * 64}])
            response['channel'] = '#wrong'
            coroutine = d._bridge_handshake(session)
            try:
                with self.assertRaises(ValueError): coroutine.send(None)
            finally: coroutine.close()
        self.assertEqual(connect.call_args.args[0], session.url)

    def test_publisher_wiring_is_source_evidence_with_runtime_caveat(self):
        checks = d.check_component_contracts(ROOT)
        found = row(checks, 'Core-to-Bridge event publishing')
        self.assertEqual(found.status, d.READY)
        self.assertIn('static evidence only', found.detail)


if __name__ == '__main__':
    unittest.main()
