"""Isolated safety and behavior tests for the consolidated diagnostic utility."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tests" / "medlar_diagnostics.py"
MODULE_NAME = "medlar_diagnostics_under_test"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
diagnostics = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = diagnostics
assert SPEC.loader is not None
SPEC.loader.exec_module(diagnostics)


SECRET = "synthetic-private-token-42@example.invalid"


def synthetic_gate():
    """Synthetic gate for stateful action mocks; never probes the host."""
    return diagnostics.ExerciseGate(True, True, diagnostics._EXERCISE_GATE_MARKER)


def setUpModule():
    global _catalog_patch
    _catalog_patch = mock.patch.object(diagnostics, "_scope_catalog", return_value={"chat:read": "Read chat", "chat:edit": "Write chat", "channel:manage:broadcast": "Update channel", "moderator:manage:shoutouts": "Shoutouts"})
    _catalog_patch.start()


def tearDownModule():
    _catalog_patch.stop()


FAKE_YAML = types.SimpleNamespace(
    YAMLError=ValueError,
    safe_load=lambda handle: (
        ["item"] if handle.read().lstrip().startswith("-") else {"ok": True}
    ),
)


def by_name(checks, name):
    return next(item for item in checks if item.name == name)


@contextmanager
def without_host_probes():
    """Keep schema fixtures off host effects and real dependency metadata."""
    names = (
        "check_dependencies",
        "check_dependency_imports",
        "check_runtime_writability",
        "check_ollama_executable",
        "check_ollama_launch_precondition",
        "check_legacy_setup_claims",
        "check_libretranslate",
    )
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(mock.patch.object(diagnostics, name, return_value=[]))
        yield


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers=None):
        self.body = body
        self.status = status
        self.headers = {} if headers is None else headers
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=None):
        self.read_limits.append(size)
        return self.body if size is None else self.body[:size]


class ImportAndPathTests(unittest.TestCase):
    def test_import_from_unrelated_cwd_has_no_output_or_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            spec = importlib.util.spec_from_file_location("isolated_diagnostic", MODULE_PATH)
            module = importlib.util.module_from_spec(spec)
            with mock.patch.object(Path, "cwd", return_value=Path(temporary)), redirect_stdout(output):
                sys.modules[spec.name] = module
                try: spec.loader.exec_module(module)
                finally: sys.modules.pop(spec.name)
            self.assertEqual(module.REPO_ROOT, ROOT)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_paths_are_derived_from_utility_location(self):
        self.assertEqual(diagnostics.REPO_ROOT, ROOT)
        self.assertEqual(
            diagnostics.DEFAULT_REPORT,
            ROOT / "logs" / "medlar_diagnostics.txt",
        )
        with tempfile.TemporaryDirectory() as temporary:
            fake_root = Path(temporary)
            self.assertEqual(
                diagnostics.resolve_output_path(None, fake_root),
                (fake_root / "logs" / "medlar_diagnostics.txt").resolve(),
            )
            self.assertEqual(
                diagnostics.resolve_output_path("reports/result.md", fake_root),
                (fake_root / "reports" / "result.md").resolve(),
            )

    def test_non_app_python_reexecs_once_before_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_python = root / ".venv" / "Scripts" / "python.exe"
            other_python = root / "outside" / "python.exe"
            app_python.parent.mkdir(parents=True)
            other_python.parent.mkdir(parents=True)
            app_python.write_bytes(b"")
            other_python.write_bytes(b"")
            runner = mock.Mock(return_value=types.SimpleNamespace(returncode=7))
            with mock.patch.object(diagnostics.sys, "executable", str(other_python)):
                result = diagnostics.reexec_in_launcher_python(
                    ["--output", "result.md", "--open"], root=root,
                    environ={"SAFE_SYNTHETIC": "1"}, runner=runner,
                )
        self.assertEqual(result, 7)
        command = runner.call_args.args[0]
        self.assertEqual(Path(command[0]), app_python)
        self.assertEqual(command[1:3], ["-B", str(MODULE_PATH)])
        self.assertEqual(command[3:], ["--output", "result.md", "--open"])
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertEqual(runner.call_args.kwargs["env"][diagnostics.REEXEC_MARKER], "1")

    def test_reexec_marker_prevents_loop_without_running_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_python = root / ".venv" / "Scripts" / "python.exe"
            other_python = root / "outside" / "python.exe"
            app_python.parent.mkdir(parents=True)
            other_python.parent.mkdir(parents=True)
            app_python.write_bytes(b"")
            other_python.write_bytes(b"")
            runner = mock.Mock()
            stderr = io.StringIO()
            with mock.patch.object(diagnostics.sys, "executable", str(other_python)), \
                    redirect_stderr(stderr):
                with self.assertRaises(RuntimeError):
                    diagnostics.reexec_in_launcher_python(
                        [], root=root, environ={diagnostics.REEXEC_MARKER: "1"},
                        runner=runner,
                    )
        runner.assert_not_called()
        self.assertNotIn(SECRET, stderr.getvalue())


class EnvironmentAndCredentialTests(unittest.TestCase):
    def test_env_parser_uses_exact_assignments_and_retains_no_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "# APP_SECRET_ID=comment-secret\n"
                "XAPP_SECRET_ID=wrong-key-secret\n"
                f"APP_SECRET_ID={SECRET}\n"
                "MEDLARTV_TWITCH_TOKEN=\n"
                "TWITCH_CHANNEL=#first\n"
                "TWITCH_CHANNEL=#last\n",
                encoding="utf-8",
            )
            snapshot = diagnostics.inspect_env_file(env_file)
        self.assertTrue(snapshot.exists)
        self.assertEqual(snapshot.credential_presence, {
            "APP_SECRET_ID": True,
            "MEDLARTV_TWITCH_TOKEN": False,
        })
        self.assertEqual(snapshot.public_values, {"TWITCH_CHANNEL": "#last"})
        self.assertNotIn(SECRET, repr(snapshot))
        self.assertNotIn("wrong-key-secret", repr(snapshot))

    def test_public_environment_matches_launcher_mode_endpoint_precedence(self):
        snapshot = diagnostics.EnvInspection(
            path=Path("synthetic.env"), exists=True,
            public_values={
                "OLLAMA_URL": "http://env.invalid:11434",
                "MODEL_NAME": "from-env",
            },
        )
        inherited = {
            "OLLAMA_URL": "http://process.invalid:11434",
            "MODEL_NAME": "from-process",
        }
        self.assertEqual(
            diagnostics.effective_public_environment(inherited, snapshot, "all"),
            {"OLLAMA_URL": "http://env.invalid:11434", "MODEL_NAME": "from-env"},
        )
        self.assertEqual(
            diagnostics.effective_public_environment(inherited, snapshot, "twitch"),
            {"OLLAMA_URL": "http://process.invalid:11434", "MODEL_NAME": "from-env"},
        )
        self.assertEqual(
            diagnostics.effective_public_environment(inherited, snapshot, "ollama"),
            inherited,
        )

    def test_no_implicit_twitch_identity_in_public_live_or_exercise_paths(self):
        public = diagnostics.effective_public_environment({}, None, "all")
        self.assertNotIn("TWITCH_NICK", public)
        self.assertNotIn("TWITCH_CHANNEL", public)
        settings = diagnostics.check_public_twitch_settings(public, "all")
        for name in ("Bot account (TWITCH_NICK)", "Target channel (TWITCH_CHANNEL)"):
            row = by_name(settings, name)
            self.assertEqual(row.status, diagnostics.UNAVAILABLE)
            self.assertIn("not explicitly configured", row.detail)

        live, resolution = diagnostics._effective_live_environment(
            {}, root=ROOT, use_env_file=False, use_vault=False,
        )
        self.assertEqual(resolution.status, diagnostics.READY)
        self.assertEqual(live, {})

        live_calls = []

        def app_only_http(method, url, **kwargs):
            live_calls.append((method, url, kwargs))
            if method == "POST" and url.endswith("/token"):
                return diagnostics.HttpJsonResult(
                    200, payload={"access_token": "synthetic-app-token", "expires_in": 3600, "token_type": "bearer"},
                    raw_body='{"access_token":"synthetic-app-token", "expires_in": 3600, "token_type": "bearer"}',
                )
            raise AssertionError((method, url, kwargs))

        irc_probe = mock.Mock(side_effect=AssertionError("IRC probe called"))
        live_checks = diagnostics.run_live_twitch_checks(
            {
                "APP_TWITCH_CLIENT_ID": "synthetic-app-client",
                "APP_SECRET_ID": "synthetic-app-secret",
            },
            root=ROOT, http_json=app_only_http, irc_probe=irc_probe,
        )
        self.assertEqual(len(live_calls), 1)
        self.assertEqual(live_calls[0][0], "POST")
        self.assertFalse(any("/helix/users?" in url for _method, url, _kwargs in live_calls))
        self.assertEqual(by_name(live_checks, "Read-only channel API").status,
                         diagnostics.SKIPPED)
        self.assertIn("TWITCH_CHANNEL is empty", by_name(
            live_checks, "Read-only channel API",
        ).detail)
        irc_probe.assert_not_called()

        http = mock.Mock(side_effect=AssertionError("exercise HTTP called"))
        irc_sender = mock.Mock(side_effect=AssertionError("exercise IRC called"))
        bot_facts = diagnostics.TokenFacts(
            "diagnostic_bot", "synthetic-bot-client", "bot-id",
            frozenset({"chat:read", "chat:edit"}),
        )
        broadcaster_facts = diagnostics.TokenFacts(
            "target_channel", "synthetic-broadcaster-client", "channel-id",
            frozenset({"channel:manage:broadcast"}),
        )
        exercise_checks, failed = diagnostics.run_twitch_exercise(
            {
                "MEDLARTV_TWITCH_TOKEN": "synthetic-bot-token",
                "DEVILMEDLAR_TWITCH_TOKEN": "synthetic-broadcaster-token",
                "DEVILMEDLAR_TWITCH_CLIENT_ID": "synthetic-broadcaster-client",
            },
            exercise=diagnostics.TwitchExercise(
                "target_channel", "[Medlar diagnostics] temporary title",
                "Science & Technology",
                "[Medlar diagnostics] synthetic send; safe to ignore.",
            ),
            bot_facts=bot_facts, broadcaster_facts=broadcaster_facts,
            app_client="synthetic-app-client", app_token="synthetic-app-token",
            resolved_channel_id="channel-id", http_json=http,
            irc_sender=irc_sender,
         gate_context=synthetic_gate())
        self.assertTrue(failed)
        preflight = by_name(exercise_checks, "Exercise preflight")
        self.assertEqual(preflight.status, diagnostics.UNAVAILABLE)
        self.assertIn("TWITCH_CHANNEL is not explicitly configured", preflight.detail)
        self.assertIn("TWITCH_NICK is not explicitly configured", preflight.detail)
        http.assert_not_called()
        irc_sender.assert_not_called()

    def test_credential_source_precedence_uses_presence_only(self):
        snapshot = diagnostics.EnvInspection(
            path=Path("synthetic.env"), exists=True,
            credential_presence={
                "MEDLARTV_TWITCH_TOKEN": True,
                "APP_SECRET_ID": False,
            },
        )
        vault = {key: False for key in diagnostics.CREDENTIAL_FIELDS}
        vault["MEDLARTV_TWITCH_TOKEN"] = True
        checks = diagnostics.credential_checks(
            {"MEDLARTV_TWITCH_TOKEN": SECRET, "APP_SECRET_ID": SECRET},
            snapshot, vault, None, "all",
        )
        bot = by_name(checks, "Bot IRC token")
        app_secret = by_name(checks, "Twitch app client secret")
        self.assertEqual(bot.status, diagnostics.READY)
        self.assertIn("Credential Manager override", bot.detail)
        self.assertEqual(app_secret.status, diagnostics.UNAVAILABLE)
        self.assertIn("empty override", app_secret.detail)
        self.assertNotIn(SECRET, repr(checks))

    def test_live_overlay_is_atomic_and_uses_synthetic_values(self):
        values = {"MEDLARTV_TWITCH_TOKEN": "process-bot"}

        def overlay(target):
            target["MEDLARTV_TWITCH_TOKEN"] = "vault-bot"
            target["APP_SECRET_ID"] = "vault-secret"

        resolved, result = diagnostics._effective_live_environment(
            values, root=ROOT, use_env_file=False, use_vault=True,
            credential_overlay=overlay,
        )
        self.assertEqual(result.status, diagnostics.READY)
        self.assertEqual(resolved["MEDLARTV_TWITCH_TOKEN"], "vault-bot")
        self.assertEqual(resolved["APP_SECRET_ID"], "vault-secret")
        self.assertNotIn("vault-bot", result.detail)
        self.assertNotIn("vault-secret", result.detail)

        def fail(_target):
            raise OSError(SECRET)

        resolved, result = diagnostics._effective_live_environment(
            values, root=ROOT, use_env_file=False, use_vault=True,
            credential_overlay=fail,
        )
        self.assertIsNone(resolved)
        self.assertEqual(result.status, diagnostics.ERROR)
        self.assertNotIn(SECRET, result.detail)

    def test_live_precedence_process_then_temp_env_then_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "MEDLARTV_TWITCH_TOKEN=env-bot\n"
                "APP_SECRET_ID=env-app-secret\n"
                "TWITCH_CHANNEL=#env-channel\n"
                "OLLAMA_URL=http://env.invalid:11434\n",
                encoding="utf-8",
            )

            def overlay(target):
                target["MEDLARTV_TWITCH_TOKEN"] = "vault-bot"
                target["DEVILMEDLAR_TWITCH_TOKEN"] = "vault-broadcaster"

            resolved, result = diagnostics._effective_live_environment(
                {
                    "MEDLARTV_TWITCH_TOKEN": "process-bot",
                    "APP_SECRET_ID": "process-app-secret",
                    "TWITCH_CHANNEL": "#process-channel",
                    "OLLAMA_URL": "http://process.invalid:11434",
                },
                root=root, use_env_file=True, use_vault=True,
                credential_overlay=overlay,
            )
        self.assertEqual(result.status, diagnostics.READY)
        self.assertEqual(resolved["MEDLARTV_TWITCH_TOKEN"], "vault-bot")
        self.assertEqual(resolved["DEVILMEDLAR_TWITCH_TOKEN"], "vault-broadcaster")
        self.assertEqual(resolved["APP_SECRET_ID"], "env-app-secret")
        self.assertEqual(resolved["TWITCH_CHANNEL"], "#env-channel")
        self.assertEqual(resolved["OLLAMA_URL"], "http://process.invalid:11434")
        self.assertNotIn("vault-bot", result.detail)

    def test_live_resolution_failure_keeps_complete_skipped_schema(self):
        with without_host_probes():
            run = diagnostics.collect_diagnostics(
                root=ROOT, environ={}, use_env_file=False, use_vault=True,
                probe_services=False, live_twitch=True,
                http_json=mock.Mock(side_effect=AssertionError("HTTP called")),
                vault_probe=lambda _root: ({key: False for key in diagnostics.CREDENTIAL_FIELDS}, None),
                credential_overlay=mock.Mock(side_effect=OSError(SECRET)))
        required = {
            ("Live Twitch", "Bot token validation"),
            ("Live Twitch", "Bot identity"),
            ("Live Twitch", "Bot raw scopes"),
            ("Live Twitch", "Bot scope classification"),
            ("Live Twitch", "Bot feature scopes"),
            ("Live Twitch — Bot granted scopes", "Detailed granted scopes"),
            ("Live Twitch", "Broadcaster token validation"),
            ("Live Twitch", "Broadcaster identity"),
            ("Live Twitch", "Broadcaster raw scopes"),
            ("Live Twitch", "Broadcaster scope classification"),
            ("Live Twitch", "Broadcaster feature scopes"),
            ("Live Twitch — Broadcaster granted scopes", "Detailed granted scopes"),
            ("Live Twitch", "App credential exchange"),
            ("Live Twitch", "Read-only channel API"),
            ("Live Twitch", "Broadcaster/API identity correlation"),
            ("Full-live Twitch exercise", "Full exercise result"),
        }
        observed = {(item.section, item.name) for item in run.checks}
        self.assertTrue(required <= observed)
        for section, name in required:
            self.assertEqual(
                next(item for item in run.checks if item.section == section and item.name == name).status,
                diagnostics.SKIPPED,
            )
        self.assertNotIn(SECRET, repr(run.checks))


class PrivacyAndHttpTests(unittest.TestCase):
    def test_redaction_removes_values_headers_and_url_userinfo(self):
        text = (
            f"raw={SECRET} Authorization: Bearer {SECRET} oauth:{SECRET} "
            f"client_secret={SECRET} https://name:{SECRET}@example.invalid/path"
        )
        cleaned = diagnostics.redact_text(text, [SECRET])
        self.assertNotIn(SECRET, cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_redaction_handles_quoted_mapping_keys(self):
        text = (
            "{'Authorization': 'Bearer " + SECRET + "', "
            "\"access_token\": \"" + SECRET + "\", "
            "'client_secret': '" + SECRET + "'}"
        )
        cleaned = diagnostics.redact_text(text)
        self.assertNotIn(SECRET, cleaned)
        self.assertGreaterEqual(cleaned.count("[REDACTED]"), 3)

    def test_safe_url_withholds_endpoint_path_values(self):
        displayed = diagnostics._safe_url(
            f"http://127.0.0.1:11434/{SECRET}?token={SECRET}#fragment"
        )
        self.assertEqual(
            displayed,
            "http://127.0.0.1:11434 (custom path withheld)",
        )
        self.assertNotIn(SECRET, displayed)

    def test_http_errors_retain_complete_raw_body_without_repr_leak(self):
        body = io.BytesIO(SECRET.encode("utf-8"))
        error = HTTPError(
            "https://example.invalid", 403, "synthetic rejection", {"X-Test": "yes"}, body,
        )
        result = diagnostics.request_json(
            "GET", "https://example.invalid",
            opener=mock.Mock(side_effect=error))
        self.assertEqual(result.status, 403)
        self.assertEqual(result.raw_body, SECRET)
        self.assertEqual(result.response_headers, (("X-Test", "yes"),))
        self.assertEqual(result.body_bytes_observed, len(SECRET.encode("utf-8")))
        self.assertFalse(result.body_truncated)
        self.assertNotIn(SECRET, repr(result))
        self.assertTrue(body.closed)

    def test_incomplete_http_body_retains_partial_bytes_and_status(self):
        class IncompleteResponse(_FakeResponse):
            def read(self, size=None):
                self.read_limits.append(size)
                raise IncompleteRead(b'{"partial":true}', 20)

        response = IncompleteResponse(b"", status=200, headers={"X-Test": "partial"})
        result = diagnostics.request_json(
            "GET", "https://example.invalid/incomplete",
            opener=lambda *_args, **_kwargs: response)
        self.assertEqual(result.status, 200)
        self.assertTrue(result.body_truncated)
        self.assertEqual(result.raw_body, '{"partial":true}')
        self.assertEqual(result.response_headers, (("X-Test", "partial"),))
        self.assertIn("IncompleteRead", result.error)
        report = diagnostics.render_report(diagnostics.DiagnosticRun(
            http_exchanges=[diagnostics.HttpExchangeEvidence(
                sequence=1, method="GET", url="https://example.invalid/incomplete",
                request_headers=(), request_body=None, response_status=result.status,
                response_headers=result.response_headers, response_body=result.raw_body,
                response_body_source="raw transport body",
                response_body_truncated=result.body_truncated,
                response_body_bytes_observed=result.body_bytes_observed,
                error=result.error,
            )],
        ))
        self.assertIn("Response body completeness: INCOMPLETE", report)
        self.assertIn('{"partial":true}', report)

    def test_http_json_rejects_invalid_json_and_retains_complete_large_payloads(self):
        invalid = _FakeResponse(b"not-json")
        result = diagnostics.request_json(
            "GET", "http://127.0.0.1/", opener=lambda *_args, **_kwargs: invalid)
        self.assertEqual(result.status, 200)
        self.assertIn("not valid", result.error)

        large_payload = {"blob": "x" * 1_100_000, "tail": "complete-tail-marker"}
        complete_body = json.dumps(large_payload, separators=(",", ":")).encode("utf-8")
        large_response = _FakeResponse(complete_body)
        result = diagnostics.request_json(
            "GET", "http://127.0.0.1/",
            opener=lambda *_args, **_kwargs: large_response)
        self.assertIsNone(result.error)
        self.assertFalse(result.body_truncated)
        self.assertEqual(result.body_bytes_observed, len(complete_body))
        self.assertEqual(result.raw_body, complete_body.decode("utf-8"))
        self.assertEqual(result.payload, large_payload)
        self.assertEqual(large_response.read_limits, [None])

    def test_non_2xx_response_body_is_read_once_and_retained(self):
        response = _FakeResponse(
            SECRET.encode("utf-8"), status=503,
            headers={"Retry-After": "30"},
        )
        result = diagnostics.request_json(
            "GET", "http://127.0.0.1/",
            opener=lambda *_args, **_kwargs: response)
        self.assertEqual(result.status, 503)
        self.assertEqual(result.error, "HTTP request was rejected")
        self.assertEqual(result.raw_body, SECRET)
        self.assertEqual(result.response_headers, (("Retry-After", "30"),))
        self.assertEqual(response.read_limits, [None])
        self.assertNotIn(SECRET, repr(result))

    def test_http_json_supports_empty_204_and_exclusive_json_body(self):
        captured = []
        response = _FakeResponse(b"", status=204)

        def opener(request, **_kwargs):
            captured.append(request)
            return response

        result = diagnostics.request_json(
            "PATCH", "https://example.invalid/channels",
            json_body={"title": "temporary"}, allow_empty=True,
            opener=opener)
        self.assertEqual(result.status, 204)
        self.assertIsNone(result.error)
        self.assertEqual(captured[0].data, b'{"title": "temporary"}')
        self.assertEqual(captured[0].headers["Content-type"], "application/json")
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            diagnostics.request_json(
                "POST", "https://example.invalid/", form={"a": "b"},
                json_body={"a": "b"}, opener=opener)

    def test_private_report_preserves_and_share_safe_report_redacts_synthetic_values(self):
        run = diagnostics.DiagnosticRun(
            checks=[diagnostics.Check("Synthetic", "Leak test", diagnostics.ERROR,
                                      f"failure contained {SECRET}")],
            secrets=(SECRET,),
            sensitive_values={"APP_SECRET_ID": SECRET},
        )
        private_report = diagnostics.render_report(
            run, generated_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertIn("Report fidelity: PRIVATE / FULL / SENSITIVE", private_report)
        self.assertIn(SECRET, private_report)

        share_safe = diagnostics.render_report(
            run, generated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            share_safe=True,
        )
        self.assertIn("SHARE-SAFE / REDACTED / INCOMPLETE", share_safe)
        self.assertNotIn(SECRET, share_safe)
        self.assertIn("WITHHELD", share_safe)


class TwitchChecksTests(unittest.TestCase):
    def test_default_collection_never_calls_live_auth_or_mutation(self):
        calls = []

        def no_http(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("HTTP must not run when service probes are disabled")

        with without_host_probes():
            run = diagnostics.collect_diagnostics(
                root=ROOT, environ={}, use_env_file=False, use_vault=False,
                probe_services=False, live_twitch=False, http_json=no_http)
        self.assertEqual(calls, [])
        self.assertEqual(by_name(run.checks, "Bot authentication").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(run.checks, "Broadcaster authentication").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(run.checks, "App/API authentication").status,
                         diagnostics.SKIPPED)

    def test_ollama_mode_never_opens_env_or_probes_vault(self):
        vault_probe = mock.Mock(side_effect=AssertionError("ollama mode probed vault"))
        with without_host_probes(), mock.patch.object(
            diagnostics, "inspect_env_file",
            side_effect=AssertionError("ollama mode opened .env"),
        ):
            run = diagnostics.collect_diagnostics(
                root=ROOT, environ={}, launcher_mode="ollama",
                use_env_file=True, use_vault=True, probe_services=False,
                vault_probe=vault_probe,
            )
        vault_probe.assert_not_called()
        self.assertEqual(by_name(run.checks, "Repository .env").status,
                         diagnostics.SKIPPED)
        self.assertIn("not opened", by_name(run.checks, "Repository .env").detail)
        vault = by_name(run.checks, "Windows credential-store presence")
        self.assertEqual(vault.status, diagnostics.SKIPPED)
        self.assertIn("no records", vault.detail)

    def test_bot_broadcaster_and_app_results_are_independent(self):
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append((method, url, kwargs))
            authorization = kwargs.get("headers", {}).get("Authorization", "")
            if url.endswith("/validate") and authorization == "OAuth bot-bad":
                return diagnostics.HttpJsonResult(401, error="rejected")
            if url.endswith("/validate") and authorization == "OAuth broadcaster-good":
                return diagnostics.HttpJsonResult(200, payload={
                    "login": "channel_name", "client_id": "broadcaster-client",
                    "user_id": "42",
                    "expires_in": 3600, "scopes": [
                        "channel:manage:broadcast", "moderator:manage:shoutouts",
                        "future:scope",
                    ],
                })
            if method == "POST" and url.endswith("/token"):
                return diagnostics.HttpJsonResult(200, payload={"access_token": "ephemeral-app", "expires_in": 3600, "token_type": "bearer"})
            if method == "GET" and "/helix/users?" in url:
                return diagnostics.HttpJsonResult(200, payload={
                    "data": [{
                        "login": "channel_name", "id": "42",
                        "created_at": "2020-01-02T03:04:05Z",
                    }],
                })
            if method == "GET" and "/helix/channels?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": [{"id": "42"}]})
            if method == "GET" and "/helix/streams?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": []})
            if method == "GET" and "/helix/search/categories?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": []})
            raise AssertionError((method, url))

        environment = {
            "TWITCH_NICK": "bot_name",
            "TWITCH_CHANNEL": "#channel_name",
            "MEDLARTV_TWITCH_TOKEN": "bot-bad",
            "MEDLARTV_TWITCH_CLIENT_ID": "bot-client",
            "DEVILMEDLAR_TWITCH_TOKEN": "broadcaster-good",
            "DEVILMEDLAR_TWITCH_CLIENT_ID": "broadcaster-client",
            "APP_TWITCH_CLIENT_ID": "app-client",
            "APP_SECRET_ID": "app-secret",
        }
        checks = diagnostics.run_live_twitch_checks(
            environment, root=ROOT, http_json=fake_http,
        )
        self.assertEqual(by_name(checks, "Bot token validation").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(checks, "Broadcaster token validation").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Broadcaster feature scopes").status,
                         diagnostics.READY)
        self.assertIn(
            "3 raw scope entries in API order (3 unique): "
            "channel:manage:broadcast, moderator:manage:shoutouts, future:scope.",
            by_name(checks, "Broadcaster raw scopes").detail,
        )
        classification = by_name(checks, "Broadcaster scope classification").detail
        self.assertIn("future:scope", classification)
        self.assertIn("does not make it invalid", classification)
        self.assertEqual(by_name(checks, "App credential exchange").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Read-only channel API").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Broadcaster/API identity correlation").status,
                         diagnostics.READY)
        self.assertFalse(any(method in {"PATCH", "DELETE", "PUT"} for method, *_ in calls))
        self.assertFalse(any("/helix/chat/" in url.casefold() for _method, url, _kwargs in calls))

    def test_scope_classification_does_not_alias_or_invalidate(self):
        catalog = {"chat:read": "IRC chat"}
        self.assertIn("legacy raw scope", diagnostics.classify_scope("channel_read", catalog))
        self.assertIn("OpenID Connect", diagnostics.classify_scope("openid", catalog))
        self.assertIn("obsolete", diagnostics.classify_scope("whispers:edit", catalog))
        self.assertIn("currently supported", diagnostics.classify_scope("user:read:whispers", catalog))
        self.assertIn("does not make it invalid", diagnostics.classify_scope("future:scope", catalog))

    def test_live_missing_app_credentials_does_not_erase_user_token_results(self):
        def fake_http(method, url, **kwargs):
            authorization = kwargs.get("headers", {}).get("Authorization", "")
            token_role = "bot" if authorization == "OAuth bot" else "channel"
            client = "bot-client" if token_role == "bot" else "broadcaster-client"
            scopes = ["chat:read", "chat:edit"] if token_role == "bot" else [
                "channel:manage:broadcast", "moderator:manage:shoutouts",
            ]
            return diagnostics.HttpJsonResult(200, payload={
                "login": token_role, "client_id": client,
                "user_id": "1", "expires_in": 3600, "scopes": scopes,
            })

        checks = diagnostics.run_live_twitch_checks({
            "TWITCH_NICK": "bot", "TWITCH_CHANNEL": "#channel",
            "MEDLARTV_TWITCH_TOKEN": "bot",
            "DEVILMEDLAR_TWITCH_TOKEN": "channel",
            "DEVILMEDLAR_TWITCH_CLIENT_ID": "broadcaster-client",
        }, root=ROOT, http_json=fake_http)
        self.assertEqual(by_name(checks, "Bot token validation").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Broadcaster token validation").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "App credential exchange").status,
                         diagnostics.UNAVAILABLE)

    def test_broadcaster_failure_does_not_erase_bot_result(self):
        def fake_http(_method, url, **kwargs):
            authorization = kwargs.get("headers", {}).get("Authorization", "")
            if url.endswith("/validate") and authorization == "OAuth bot-good":
                return diagnostics.HttpJsonResult(200, payload={
                    "login": "bot", "client_id": "bot-client", "user_id": "1",
                    "expires_in": 3600, "scopes": ["chat:read", "chat:edit"],
                })
            if url.endswith("/validate"):
                return diagnostics.HttpJsonResult(401, error="rejected")
            raise AssertionError("app request should not run without app credentials")

        checks = diagnostics.run_live_twitch_checks({
            "TWITCH_NICK": "bot", "TWITCH_CHANNEL": "#channel",
            "MEDLARTV_TWITCH_TOKEN": "bot-good",
            "MEDLARTV_TWITCH_CLIENT_ID": "bot-client",
            "DEVILMEDLAR_TWITCH_TOKEN": "broadcaster-bad",
            "DEVILMEDLAR_TWITCH_CLIENT_ID": "broadcaster-client",
        }, root=ROOT, http_json=fake_http)
        self.assertEqual(by_name(checks, "Bot token validation").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Bot identity").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Bot feature scopes").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Broadcaster token validation").status,
                         diagnostics.UNAVAILABLE)


class TwitchExerciseTests(unittest.TestCase):
    def _environment(self):
        return {
            "TWITCH_NICK": "diagnostic_bot",
            "TWITCH_CHANNEL": "#target_channel",
            "MEDLARTV_TWITCH_TOKEN": "synthetic-bot-token",
            "MEDLARTV_TWITCH_CLIENT_ID": "synthetic-bot-client",
            "DEVILMEDLAR_TWITCH_TOKEN": "synthetic-broadcaster-token",
            "DEVILMEDLAR_TWITCH_CLIENT_ID": "synthetic-broadcaster-client",
            "APP_TWITCH_CLIENT_ID": "synthetic-app-client",
            "APP_SECRET_ID": "synthetic-app-secret",
        }

    def _exercise(self):
        return diagnostics.TwitchExercise(
            target_channel="target_channel",
            title="[Medlar diagnostics] temporary title",
            category="Science & Technology",
            chat_message="[Medlar diagnostics] Chat send test; safe to ignore.",
        )

    def _assert_complete_exercise_rows(self, checks):
        expected = {
            "Action plan",
            "Exercise preflight",
            "Original channel-state snapshot",
            "Category resolution",
            "Broadcaster offline before mutation",
            "Title update request",
            "Title update verification",
            "Category update request",
            "Category update verification",
            "Restoration request",
            "Title restoration verification",
            "Category restoration verification",
            "Broadcaster offline before chat",
            "IRC connection and authentication",
            "Diagnostic chat command write",
            "IRC post-send acknowledgement",
            "Independent chat delivery observation",
            "Full exercise result",
        }
        exercise_rows = [
            item for item in checks
            if item.section == "Full-live Twitch exercise"
        ]
        self.assertEqual({item.name for item in exercise_rows}, expected)
        self.assertEqual(len(exercise_rows), len(expected))
        for name in expected:
            with self.subTest(exercise_row=name):
                self.assertEqual(
                    sum(item.name == name for item in exercise_rows), 1,
                    f"expected exactly one exercise row named {name!r}",
                )

    def _successful_http(
        self, *, fail_restore=False, ambiguous_title=False,
        reject_title=False, reject_category=False, server_error_title=False,
    ):
        state = {
            "title": "Original title",
            "game_id": "10",
            "game_name": "Original category",
        }
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append((method, url, kwargs))
            authorization = kwargs.get("headers", {}).get("Authorization", "")
            if url.endswith("/validate") and authorization == "OAuth synthetic-bot-token":
                return diagnostics.HttpJsonResult(200, payload={
                    "login": "diagnostic_bot", "client_id": "synthetic-bot-client",
                    "user_id": "bot-1", "expires_in": 3600, "scopes": ["chat:read", "chat:edit"],
                })
            if url.endswith("/validate") and authorization == "OAuth synthetic-broadcaster-token":
                return diagnostics.HttpJsonResult(200, payload={
                    "login": "target_channel", "client_id": "synthetic-broadcaster-client",
                    "user_id": "channel-42",
                    "expires_in": 3600, "scopes": ["channel:manage:broadcast", "moderator:manage:shoutouts"],
                })
            if method == "POST" and url.endswith("/token"):
                return diagnostics.HttpJsonResult(200, payload={"access_token": "synthetic-app-token", "expires_in": 3600, "token_type": "bearer"})
            if method == "GET" and "/helix/users?" in url:
                return diagnostics.HttpJsonResult(200, payload={
                    "data": [{
                        "login": "target_channel", "id": "channel-42",
                        "created_at": "2020-01-02T03:04:05Z",
                    }],
                })
            if method == "GET" and "/helix/streams?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": []})
            if method == "GET" and "/helix/search/categories?" in url:
                return diagnostics.HttpJsonResult(200, payload={
                    "data": [
                        {"id": "wrong", "name": "Science"},
                        {"id": "509670", "name": "Science & Technology"},
                    ],
                })
            if method == "GET" and "/helix/channels?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": [{**state, "broadcaster_id": "channel-42"}]})
            if method == "PATCH" and "/helix/channels?" in url:
                body = kwargs.get("json_body", {})
                patch_number = sum(1 for item in calls if item[0] == "PATCH")
                if patch_number == 1 and ambiguous_title:
                    state["title"] = body.get("title", state["title"])
                    return diagnostics.HttpJsonResult(None, error="synthetic timeout")
                if patch_number == 1 and server_error_title:
                    state["title"] = body.get("title", state["title"])
                    return diagnostics.HttpJsonResult(500, error="synthetic server error")
                if patch_number == 1 and reject_title:
                    return diagnostics.HttpJsonResult(403, error="synthetic rejection")
                if patch_number == 2 and reject_category and set(body) == {"game_id"}:
                    return diagnostics.HttpJsonResult(400, error="synthetic rejection")
                if patch_number == 3 and fail_restore:
                    return diagnostics.HttpJsonResult(500, error="synthetic rejection")
                if "title" in body:
                    state["title"] = body["title"]
                if "game_id" in body:
                    state["game_id"] = body["game_id"]
                    state["game_name"] = (
                        "Science & Technology" if body["game_id"] == "509670"
                        else "Original category"
                    )
                return diagnostics.HttpJsonResult(204)
            raise AssertionError((method, url, kwargs))

        return fake_http, calls, state

    def test_full_exercise_changes_verifies_restores_and_sends_exactly_once(self):
        fake_http, calls, state = self._successful_http()

        def send_chat(*_args):
            calls.append(("IRC", "irc.chat.twitch.tv", {}))
            return diagnostics.IrcSendResult(
                connected=True, authenticated=True, joined=True,
                written=True, acknowledged=True,
            )

        chat_sender = mock.Mock(side_effect=send_chat)
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
            exercise=self._exercise(), irc_sender=chat_sender,
         gate_context=synthetic_gate())
        self.assertEqual(by_name(checks, "Title update request").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Title update verification").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Category update request").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Category update verification").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Title restoration verification").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Category restoration verification").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Diagnostic chat command write").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "IRC post-send acknowledgement").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Independent chat delivery observation").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(checks, "Full exercise result").status, diagnostics.READY)
        patches = [item for item in calls if item[0] == "PATCH"]
        self.assertEqual([item[2]["json_body"] for item in patches], [
            {"title": self._exercise().title},
            {"game_id": "509670"},
            {"title": "Original title", "game_id": "10"},
        ])
        self.assertEqual(state["title"], "Original title")
        self.assertEqual(state["game_id"], "10")
        self.assertEqual(calls[-1][0], "IRC")
        restore_index = max(
            index for index, item in enumerate(calls)
            if item[0] == "PATCH" and set(item[2]["json_body"]) == {"title", "game_id"}
        )
        self.assertEqual(calls[restore_index + 1][0], "GET")
        self.assertIn("/helix/channels?", calls[restore_index + 1][1])
        self.assertGreater(len(calls) - 1, restore_index + 1)
        chat_sender.assert_called_once_with(
            "diagnostic_bot", "synthetic-bot-token", "target_channel",
            self._exercise().chat_message,
        )
        self.assertFalse(any(
            marker in url for _method, url, _kwargs in calls
            for marker in ("/moderation", "/bans", "/commercial", "/rewards", "/raids")
        ))

    def test_ambiguous_title_stops_category_and_chat_but_restores(self):
        fake_http, calls, state = self._successful_http(ambiguous_title=True)
        chat_sender = mock.Mock()
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
            exercise=self._exercise(), irc_sender=chat_sender,
         gate_context=synthetic_gate())
        patches = [item for item in calls if item[0] == "PATCH"]
        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[-1][2]["json_body"], {
            "title": "Original title",
        })
        self.assertEqual(state["title"], "Original title")
        self.assertEqual(by_name(checks, "Category update request").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(checks, "Title restoration verification").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Diagnostic chat command write").status,
                         diagnostics.SKIPPED)
        chat_sender.assert_not_called()

    def test_server_error_after_title_is_treated_ambiguous_and_restored(self):
        fake_http, calls, state = self._successful_http(server_error_title=True)
        chat_sender = mock.Mock()
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
            exercise=self._exercise(), irc_sender=chat_sender,
         gate_context=synthetic_gate())
        patches = [item for item in calls if item[0] == "PATCH"]
        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[-1][2]["json_body"], {
            "title": "Original title",
        })
        self.assertEqual(state["title"], "Original title")
        self.assertEqual(by_name(checks, "Title update request").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(checks, "Restoration request").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Title restoration verification").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Full exercise result").status,
                         diagnostics.UNAVAILABLE)
        chat_sender.assert_not_called()

    def test_restore_failure_is_loud_and_stops_chat(self):
        fake_http, _calls, state = self._successful_http(fail_restore=True)
        chat_sender = mock.Mock()
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
            exercise=self._exercise(), irc_sender=chat_sender,
         gate_context=synthetic_gate())
        self.assertEqual(by_name(checks, "Restoration request").status, diagnostics.ERROR)
        self.assertEqual(by_name(checks, "Title restoration verification").status,
                         diagnostics.ERROR)
        self.assertEqual(by_name(checks, "Category restoration verification").status,
                         diagnostics.ERROR)
        self.assertEqual(by_name(checks, "Full exercise result").status, diagnostics.ERROR)
        self.assertNotEqual(state["title"], "Original title")
        chat_sender.assert_not_called()

    def test_definite_title_rejection_stops_later_actions_without_stale_restore(self):
        fake_http, calls, state = self._successful_http(reject_title=True)
        chat_sender = mock.Mock()
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
            exercise=self._exercise(), irc_sender=chat_sender,
         gate_context=synthetic_gate())
        patches = [item for item in calls if item[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][2]["json_body"], {"title": self._exercise().title})
        self.assertEqual(by_name(checks, "Category update request").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(checks, "Restoration request").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(checks, "Title restoration verification").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Category restoration verification").status,
                         diagnostics.READY)
        self.assertEqual(state["title"], "Original title")
        chat_sender.assert_not_called()

    def test_definite_category_rejection_stops_chat_then_restores(self):
        fake_http, calls, state = self._successful_http(reject_category=True)
        chat_sender = mock.Mock()
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
            exercise=self._exercise(), irc_sender=chat_sender,
         gate_context=synthetic_gate())
        patches = [item for item in calls if item[0] == "PATCH"]
        self.assertEqual(len(patches), 3)
        self.assertEqual(by_name(checks, "Category update request").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(checks, "Diagnostic chat command write").status,
                         diagnostics.SKIPPED)
        self.assertEqual(state["title"], "Original title")
        self.assertEqual(state["game_id"], "10")
        chat_sender.assert_not_called()

    def test_target_or_scope_preflight_failure_makes_zero_mutations(self):
        fake_http, calls, _state = self._successful_http()
        exercise = diagnostics.TwitchExercise(
            target_channel="different_channel",
            title=self._exercise().title,
            category=self._exercise().category,
            chat_message=self._exercise().chat_message,
        )
        chat_sender = mock.Mock()
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
            exercise=exercise, irc_sender=chat_sender,
         gate_context=synthetic_gate())
        self.assertEqual(by_name(checks, "Exercise preflight").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(checks, "Full exercise result").status,
                         diagnostics.UNAVAILABLE)
        self.assertFalse(any(method == "PATCH" for method, _url, _kwargs in calls))
        chat_sender.assert_not_called()

    def test_snapshot_and_category_preflight_failures_make_zero_mutations(self):
        cases = (
            ("snapshot unavailable", diagnostics.HttpJsonResult(
                None, error="synthetic timeout",
            )),
            ("snapshot malformed", diagnostics.HttpJsonResult(
                200, payload={"data": []},
            )),
            ("exact category missing", diagnostics.HttpJsonResult(
                200, payload={"data": [{"id": "wrong", "name": "Science"}]},
            )),
        )
        for branch, failure in cases:
            with self.subTest(branch=branch):
                fake_http, calls, _state = self._successful_http()
                channel_gets = 0

                def routed_http(method, url, **kwargs):
                    nonlocal channel_gets
                    if method == "GET" and "/helix/channels?" in url:
                        channel_gets += 1
                        if branch.startswith("snapshot") and channel_gets == 2:
                            calls.append((method, url, kwargs))
                            return failure
                    if (
                        branch == "exact category missing"
                        and method == "GET"
                        and "/helix/search/categories?" in url
                    ):
                        calls.append((method, url, kwargs))
                        return failure
                    return fake_http(method, url, **kwargs)

                chat_sender = mock.Mock()
                checks = diagnostics.run_live_twitch_checks(
                    self._environment(), root=ROOT, http_json=routed_http,
                    exercise=self._exercise(), irc_sender=chat_sender,
                 gate_context=synthetic_gate())
                failed_row = (
                    "Original channel-state snapshot"
                    if branch.startswith("snapshot") else "Category resolution"
                )
                self.assertEqual(by_name(checks, failed_row).status,
                                 diagnostics.UNAVAILABLE)
                self.assertEqual(by_name(checks, "Exercise preflight").status,
                                 diagnostics.UNAVAILABLE)
                self.assertEqual(by_name(checks, "Full exercise result").status,
                                 diagnostics.UNAVAILABLE)
                self.assertFalse(any(
                    method == "PATCH" for method, _url, _kwargs in calls
                ))
                chat_sender.assert_not_called()
                self._assert_complete_exercise_rows(checks)

    def test_non_reversible_original_or_temporary_state_blocks_all_actions(self):
        cases = (
            ("empty original title", {"title": ""}),
            ("unchanged title", {"title": self._exercise().title}),
            ("unchanged category", {
                "game_id": "509670", "game_name": "Science & Technology",
            }),
        )
        for branch, state_update in cases:
            with self.subTest(branch=branch):
                fake_http, calls, state = self._successful_http()
                state.update(state_update)
                chat_sender = mock.Mock()
                checks = diagnostics.run_live_twitch_checks(
                    self._environment(), root=ROOT, http_json=fake_http,
                    exercise=self._exercise(), irc_sender=chat_sender,
                 gate_context=synthetic_gate())
                self.assertEqual(by_name(checks, "Exercise preflight").status,
                                 diagnostics.UNAVAILABLE)
                self.assertEqual(by_name(checks, "Title update request").status,
                                 diagnostics.SKIPPED)
                self.assertEqual(by_name(checks, "Full exercise result").status,
                                 diagnostics.UNAVAILABLE)
                self.assertFalse(any(
                    method == "PATCH" for method, _url, _kwargs in calls
                ))
                chat_sender.assert_not_called()
                self._assert_complete_exercise_rows(checks)

    def test_accepted_but_unverified_action_restores_and_stops(self):
        for branch, failing_channel_get in (
            ("title", 4),
            ("category", 6),
        ):
            with self.subTest(branch=branch):
                fake_http, calls, state = self._successful_http()
                channel_gets = 0

                def routed_http(method, url, **kwargs):
                    nonlocal channel_gets
                    if method == "GET" and "/helix/channels?" in url:
                        channel_gets += 1
                        if channel_gets == failing_channel_get:
                            calls.append((method, url, kwargs))
                            mismatch = dict(state)
                            if branch == "title":
                                mismatch["title"] = "different title"
                            else:
                                mismatch["game_id"] = "different-game"
                                mismatch["game_name"] = "Different category"
                            return diagnostics.HttpJsonResult(
                                200, payload={"data": [mismatch]},
                            )
                    return fake_http(method, url, **kwargs)

                chat_sender = mock.Mock()
                checks = diagnostics.run_live_twitch_checks(
                    self._environment(), root=ROOT, http_json=routed_http,
                    exercise=self._exercise(), irc_sender=chat_sender,
                 gate_context=synthetic_gate())
                verification = (
                    "Title update verification"
                    if branch == "title" else "Category update verification"
                )
                self.assertEqual(by_name(checks, verification).status,
                                 diagnostics.UNAVAILABLE)
                if branch == "title":
                    self.assertEqual(by_name(checks, "Category update request").status,
                                     diagnostics.SKIPPED)
                self.assertEqual(by_name(checks, "Restoration request").status,
                                 diagnostics.READY)
                self.assertEqual(by_name(checks, "Title restoration verification").status,
                                 diagnostics.READY)
                self.assertEqual(by_name(checks, "Category restoration verification").status,
                                 diagnostics.READY)
                self.assertEqual(by_name(checks, "Full exercise result").status,
                                 diagnostics.UNAVAILABLE)
                self.assertEqual(state["title"], "Original title")
                self.assertEqual(state["game_id"], "10")
                chat_sender.assert_not_called()
                self._assert_complete_exercise_rows(checks)

    def test_accepted_restore_without_matching_readback_is_hard_failure(self):
        for branch in ("mismatch", "unavailable"):
            with self.subTest(branch=branch):
                fake_http, calls, state = self._successful_http()
                restore_accepted = False

                def routed_http(method, url, **kwargs):
                    nonlocal restore_accepted
                    body = kwargs.get("json_body", {})
                    if (
                        method == "PATCH"
                        and "/helix/channels?" in url
                        and set(body) == {"title", "game_id"}
                    ):
                        response = fake_http(method, url, **kwargs)
                        restore_accepted = True
                        return response
                    if (
                        restore_accepted
                        and method == "GET"
                        and "/helix/channels?" in url
                    ):
                        calls.append((method, url, kwargs))
                        if branch == "unavailable":
                            return diagnostics.HttpJsonResult(
                                None, error="synthetic timeout",
                            )
                        return diagnostics.HttpJsonResult(200, payload={
                            "data": [{
                                "title": self._exercise().title,
                                "game_id": "509670",
                                "game_name": "Science & Technology",
                            }],
                        })
                    return fake_http(method, url, **kwargs)

                chat_sender = mock.Mock()
                checks = diagnostics.run_live_twitch_checks(
                    self._environment(), root=ROOT, http_json=routed_http,
                    exercise=self._exercise(), irc_sender=chat_sender,
                 gate_context=synthetic_gate())
                self.assertEqual(by_name(checks, "Restoration request").status,
                                 diagnostics.READY)
                self.assertEqual(by_name(checks, "Title restoration verification").status,
                                 diagnostics.ERROR)
                self.assertEqual(by_name(checks, "Category restoration verification").status,
                                 diagnostics.ERROR)
                self.assertEqual(by_name(checks, "Full exercise result").status,
                                 diagnostics.ERROR)
                self.assertEqual(state["title"], "Original title")
                self.assertEqual(state["game_id"], "10")
                chat_sender.assert_not_called()
                self._assert_complete_exercise_rows(checks)

    def test_exercise_validation_rejects_commands_mentions_and_control_chars(self):
        base = self._exercise()
        invalid_messages = (
            "[Medlar diagnostics] hello @everyone",
            "[Medlar diagnostics] line one\nline two",
            "[Medlar diagnostics] bad\x00value",
        )
        for message in invalid_messages:
            with self.subTest(message=repr(message)), self.assertRaises(ValueError):
                diagnostics.validate_twitch_exercise(diagnostics.TwitchExercise(
                    base.target_channel, base.title, base.category, message,
                ))

    def test_direct_invalid_exercise_makes_zero_http_or_irc_calls(self):
        exercise = diagnostics.TwitchExercise(
            "target_channel", "temporary", "Science & Technology",
            "[Medlar diagnostics] invalid @mention",
        )
        http = mock.Mock(side_effect=AssertionError("HTTP called"))
        irc = mock.Mock(side_effect=AssertionError("IRC called"))
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=http,
            exercise=exercise, irc_sender=irc,
         gate_context=synthetic_gate())
        http.assert_not_called()
        irc.assert_not_called()
        self.assertEqual(by_name(checks, "Exercise preflight").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(checks, "Full exercise result").status,
                         diagnostics.UNAVAILABLE)

    def test_scope_rows_are_complete_categorized_and_do_not_prove_mutations(self):
        fake_http, _calls, _state = self._successful_http()
        checks = diagnostics.run_live_twitch_checks(
            self._environment(), root=ROOT, http_json=fake_http,
        )
        bot_rows = [item for item in checks if item.section == "Live Twitch - Bot granted scopes"]
        broadcaster_rows = [
            item for item in checks
            if item.section == "Live Twitch — Broadcaster granted scopes"
        ]
        self.assertEqual(len(bot_rows), 2)
        broadcaster_rows = [
            item for item in checks
            if item.section.endswith("Broadcaster granted scopes")
        ]
        self.assertEqual(len(broadcaster_rows), 2)
        self.assertTrue(any("Bot & Chat - chat:edit" == item.name for item in bot_rows))
        broadcast = next(item for item in broadcaster_rows if "channel:manage:broadcast" in item.name)
        self.assertEqual(broadcast.status, diagnostics.SKIPPED)
        self.assertIn("Grant=GRANTED", broadcast.detail)
        self.assertIn("Functional=UNTESTED", broadcast.detail)
        self.assertIn("No title/category mutation was attempted", broadcast.detail)
        shoutout = next(item for item in broadcaster_rows if "moderator:manage:shoutouts" in item.name)
        self.assertEqual(shoutout.status, diagnostics.SKIPPED)
        self.assertIn("Grant=GRANTED", shoutout.detail)
        self.assertIn("Functional=UNTESTED", shoutout.detail)
        self.assertIn("outside the diagnostic mutation allowlist", shoutout.detail)
        self.assertEqual(by_name(checks, "Title update request").status, diagnostics.SKIPPED)

    def test_real_irc_sender_uses_documented_two_phase_ack_and_one_privmsg(self):
        class FakeTlsSocket:
            def __init__(self, chunks):
                self.chunks = list(chunks)
                self.sent = []
                self.closed = False

            def settimeout(self, _value):
                return None

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, _size):
                if self.chunks:
                    return self.chunks.pop(0)
                raise diagnostics.socket.timeout()

            def close(self):
                self.closed = True

        handshake = (
            b":tmi.twitch.tv 001 Diagnostic_Bot :Welcome\r\n"
            b":diagnostic_bot!u@h JOIN #TARGET_CHANNEL\r\n"
            b"@badge-info=;badges= :tmi.twitch.tv USERSTATE #target_channel\r\n"
        )
        post_send = b"@badge-info=;badges= :tmi.twitch.tv USERSTATE #TARGET_CHANNEL\r\n"
        tls = FakeTlsSocket([handshake, post_send])
        raw = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(diagnostics.socket, "create_connection", return_value=raw), \
                mock.patch.object(diagnostics.ssl, "create_default_context", return_value=context), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = diagnostics.send_irc_diagnostic(
                "Diagnostic_Bot", "oauth:synthetic-token", "Target_Channel",
                "[Medlar diagnostics] protocol test; safe to ignore.", timeout=0.1,
            )
        sent = [item.decode("utf-8") for item in tls.sent]
        self.assertTrue(result.connected and result.authenticated and result.joined)
        self.assertTrue(result.written and result.acknowledged)
        self.assertFalse(result.rejected)
        self.assertEqual(sum(line.startswith("PRIVMSG ") for line in sent), 1)
        self.assertTrue(any("twitch.tv/membership" in line for line in sent if line.startswith("CAP REQ")))
        self.assertTrue(tls.closed)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(any("synthetic-token" in line for line in (stdout.getvalue(), stderr.getvalue())))

    def test_real_irc_sender_requires_initial_userstate_before_privmsg(self):
        class FakeTlsSocket:
            def __init__(self):
                self.sent = []
                self.first = True

            def settimeout(self, _value):
                return None

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, _size):
                if self.first:
                    self.first = False
                    return (
                        b":tmi.twitch.tv 001 diagnostic_bot :Welcome\r\n"
                        b":tmi.twitch.tv 366 diagnostic_bot #target_channel :End of /NAMES list\r\n"
                    )
                raise diagnostics.socket.timeout()

            def close(self):
                return None

        tls = FakeTlsSocket()
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        with mock.patch.object(diagnostics.socket, "create_connection", return_value=mock.Mock()), \
                mock.patch.object(diagnostics.ssl, "create_default_context", return_value=context):
            result = diagnostics.send_irc_diagnostic(
                "diagnostic_bot", "synthetic-token", "target_channel",
                "[Medlar diagnostics] protocol test; safe to ignore.", timeout=0.01,
            )
        self.assertTrue(result.authenticated and result.joined)
        self.assertFalse(result.written)
        self.assertFalse(any(item.startswith(b"PRIVMSG ") for item in tls.sent))

    def test_real_irc_sender_auth_rejection_and_post_send_notice(self):
        class FakeTlsSocket:
            def __init__(self, chunks):
                self.chunks = list(chunks)
                self.sent = []

            def settimeout(self, _value):
                return None

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, _size):
                if self.chunks:
                    return self.chunks.pop(0)
                raise diagnostics.socket.timeout()

            def close(self):
                return None

        auth_reject = FakeTlsSocket([
            b":tmi.twitch.tv NOTICE * :Login authentication failed\r\n",
        ])
        success_then_reject = FakeTlsSocket([
            b":tmi.twitch.tv 001 diagnostic_bot :Welcome\r\n"
            b"@x=y :tmi.twitch.tv USERSTATE #target_channel\r\n",
            b"@msg-id=msg_rejected :tmi.twitch.tv NOTICE #target_channel :rejected\r\n",
        ])
        success_then_channel_blocked = FakeTlsSocket([
            b":tmi.twitch.tv 001 diagnostic_bot :Welcome\r\n"
            b"@x=y :tmi.twitch.tv USERSTATE #target_channel\r\n",
            b"@msg-id=msg_channel_blocked :tmi.twitch.tv NOTICE #target_channel :blocked\r\n",
        ])
        for tls, expected_written in (
            (auth_reject, False),
            (success_then_reject, True),
            (success_then_channel_blocked, True),
        ):
            context = mock.Mock()
            context.wrap_socket.return_value = tls
            with mock.patch.object(diagnostics.socket, "create_connection", return_value=mock.Mock()), \
                    mock.patch.object(diagnostics.ssl, "create_default_context", return_value=context):
                result = diagnostics.send_irc_diagnostic(
                    "diagnostic_bot", "synthetic-token", "target_channel",
                    "[Medlar diagnostics] protocol test; safe to ignore.", timeout=0.05,
                )
            self.assertEqual(result.written, expected_written)
            self.assertTrue(result.rejected)
            self.assertFalse(result.acknowledged)
            self.assertEqual(sum(item.startswith(b"PRIVMSG ") for item in tls.sent),
                             1 if expected_written else 0)

    def test_real_irc_sender_ignores_benign_notice_and_accepts_userstate_without_id(self):
        class FakeTlsSocket:
            def __init__(self, chunks):
                self.chunks = list(chunks)
                self.sent = []

            def settimeout(self, _value):
                return None

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, _size):
                if self.chunks:
                    return self.chunks.pop(0)
                raise diagnostics.socket.timeout()

            def close(self):
                return None

        handshake = (
            b":tmi.twitch.tv 001 diagnostic_bot :Welcome\r\n"
            b"@badge-info= :tmi.twitch.tv USERSTATE #target_channel\r\n"
        )
        cases = (
            ([
                handshake,
                b"@msg-id=room_mods :tmi.twitch.tv NOTICE #target_channel :benign\r\n"
                b"@badge-info= :tmi.twitch.tv USERSTATE #target_channel\r\n",
            ], True),
            ([
                handshake,
                b"@msg-id=room_mods :tmi.twitch.tv NOTICE #target_channel :benign\r\n",
            ], False),
        )
        for chunks, expected_ack in cases:
            tls = FakeTlsSocket(chunks)
            context = mock.Mock()
            context.wrap_socket.return_value = tls
            with mock.patch.object(diagnostics.socket, "create_connection", return_value=mock.Mock()), \
                    mock.patch.object(diagnostics.ssl, "create_default_context", return_value=context):
                result = diagnostics.send_irc_diagnostic(
                    "diagnostic_bot", "synthetic-token", "target_channel",
                    "[Medlar diagnostics] protocol test; safe to ignore.", timeout=0.02,
                )
            self.assertEqual(result.acknowledged, expected_ack)
            self.assertFalse(result.rejected)
            self.assertTrue(result.written)

    def test_irc_login_channel_and_token_crlf_injection_fail_before_socket(self):
        message = "[Medlar diagnostics] synthetic injection test; safe to ignore."
        cases = (
            ("login", "diagnostic_bot\r\nJOIN #other", "synthetic-token", "target_channel"),
            ("channel", "diagnostic_bot", "synthetic-token", "target\r\nJOIN #other"),
            ("token", "diagnostic_bot", "synthetic-token\r\nNICK attacker", "target_channel"),
        )
        for field, login, token, channel in cases:
            with self.subTest(sender_field=field), \
                    mock.patch.object(diagnostics.socket, "create_connection") as connect:
                if field == "channel":
                    with self.assertRaises(ValueError):
                        diagnostics.send_irc_diagnostic(
                            login, token, channel, message, timeout=0.01,
                        )
                else:
                    result = diagnostics.send_irc_diagnostic(
                        login, token, channel, message, timeout=0.01,
                    )
                    self.assertFalse(result.connected)
                    self.assertIsNotNone(result.error)
                    self.assertNotIn("attacker", result.error)
                    self.assertNotIn("other", result.error)
                connect.assert_not_called()

            with self.subTest(read_only_field=field), \
                    mock.patch.object(diagnostics.socket, "create_connection") as connect:
                result = diagnostics.probe_irc_readiness(
                    login, token, channel, timeout=0.01,
                )
                self.assertFalse(result.connected)
                self.assertIsNotNone(result.error)
                self.assertNotIn("attacker", result.error)
                self.assertNotIn("other", result.error)
                connect.assert_not_called()


class ServiceAndConfigTests(unittest.TestCase):
    def test_ollama_executable_and_service_results_are_independent(self):
        completed = types.SimpleNamespace(
            returncode=1, stdout="ollama version is 0.35.0\n", stderr="server unavailable",
        )
        checks = diagnostics.check_ollama_executable(
            which=lambda _name: "C:/synthetic/ollama.exe",
            runner=mock.Mock(return_value=completed),
        )
        self.assertEqual(by_name(checks, "Command-line executable").status,
                         diagnostics.READY)
        detail = by_name(checks, "Command-line executable").detail
        self.assertIn("client version 0.35.0 (exit status 1)", detail)
        self.assertIn('Exact stdout="ollama version is 0.35.0\\n"', detail)
        self.assertIn('exact stderr="server unavailable"', detail)

    def test_ollama_bind_alignment_matches_launcher_loopback_rules(self):
        equivalent = diagnostics.check_ollama_bind_alignment({
            "OLLAMA_HOST": "localhost:11434",
            "OLLAMA_URL": "http://127.0.0.1:11434",
        }, "all", root=ROOT)
        mismatch = diagnostics.check_ollama_bind_alignment({
            "OLLAMA_HOST": "localhost:11434",
            "OLLAMA_URL": "http://127.0.0.1:11435",
        }, "all", root=ROOT)
        twitch = diagnostics.check_ollama_bind_alignment({}, "twitch", root=ROOT)
        self.assertEqual(by_name(equivalent, "Launcher bind/client alignment").status,
                         diagnostics.READY)
        self.assertEqual(by_name(mismatch, "Launcher bind/client alignment").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(twitch, "Launcher bind/client alignment").status,
                         diagnostics.SKIPPED)

    def test_ollama_checks_selected_model_without_inference(self):
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return diagnostics.HttpJsonResult(200, payload={
                "models": [{"name": "llama3:latest"}],
            })

        with mock.patch.object(
            diagnostics, "resolve_ollama_base_url",
            return_value="http://127.0.0.1:11434",
        ):
            checks = diagnostics.check_ollama(
                {"MODEL_NAME": "llama3"}, root=ROOT, http_json=fake_http,
            )
        self.assertEqual(by_name(checks, "Selected model").status, diagnostics.READY)
        self.assertEqual([item[0] for item in calls], ["GET"])
        self.assertTrue(calls[0][1].endswith("/api/tags"))

    def test_ollama_invalid_endpoint_withholds_synthetic_secret(self):
        with mock.patch.object(
            diagnostics, "resolve_ollama_base_url",
            side_effect=ValueError(SECRET),
        ):
            checks = diagnostics.check_ollama(
                {"OLLAMA_URL": f"http://name:{SECRET}@localhost"}, root=ROOT,
            )
        self.assertEqual(by_name(checks, "Endpoint configuration").status,
                         diagnostics.ERROR)
        self.assertNotIn(SECRET, repr(checks))

    def test_unavailable_local_services_are_honest_and_read_only(self):
        calls = []

        def unavailable(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return diagnostics.HttpJsonResult(None, error="request could not be completed")

        with mock.patch.object(
            diagnostics, "resolve_ollama_base_url",
            return_value="http://127.0.0.1:11434",
        ):
            ollama = diagnostics.check_ollama(
                {"MODEL_NAME": "synthetic-model"}, root=ROOT, http_json=unavailable,
            )
        with tempfile.TemporaryDirectory() as temporary:
            libre = diagnostics.check_libretranslate(
                {}, root=Path(temporary), http_json=unavailable,
             runner=mock.Mock(return_value=types.SimpleNamespace(returncode=1)))
        self.assertEqual(by_name(ollama, "Service readiness").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(ollama, "Selected model").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(libre, "Service readiness").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual([method for method, _url, _kwargs in calls], ["GET", "GET"])
        self.assertTrue(calls[0][1].endswith("/api/tags"))
        self.assertTrue(calls[1][1].endswith("/languages"))

    def test_libretranslate_environment_and_service_are_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".libretranslate"
            python = home / ".venv" / "Scripts" / "python.exe"
            site = home / ".venv" / "Lib" / "site-packages"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            for distribution, version in (("libretranslate", "1.9.6"), ("chardet", "5.2.0")):
                metadata = site / f"{distribution}-{version}.dist-info" / "METADATA"
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(
                    f"Name: {distribution}\nVersion: {version}\n", encoding="utf-8",
                )
            (root / "requirements-libretranslate.txt").write_text(
                "libretranslate==1.9.6\nchardet==5.2.0\n", encoding="utf-8",
            )
            calls = []

            def fake_http(method, url, **kwargs):
                calls.append((method, url, kwargs))
                return diagnostics.HttpJsonResult(200, payload=[{"code": "en"}, {"code": "es"}])

            checks = diagnostics.check_libretranslate(
                {}, root=root, http_json=fake_http,
             runner=mock.Mock(return_value=types.SimpleNamespace(returncode=1)))
        self.assertEqual(by_name(checks, "Separate environment").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Service readiness").status,
                         diagnostics.READY)
        self.assertEqual([item[0] for item in calls], ["GET"])
        self.assertTrue(calls[0][1].endswith("/languages"))

    def test_relative_libretranslate_overrides_are_repo_relative(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            caller = base / "caller"
            home = root / "service-home"
            python = root / "tools" / "python.exe"
            home.mkdir(parents=True)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            old_cwd = Path.cwd()
            try:
                caller.mkdir()
                os.chdir(caller)
                self.assertEqual(
                    diagnostics._libretranslate_home(
                        {"LIBRETRANSLATE_HOME": "service-home"}, root,
                    ),
                    home,
                )
                chosen, _source = diagnostics._libretranslate_python(
                    {"LIBRETRANSLATE_PYTHON": "tools/python.exe"}, home, root,
                )
                self.assertEqual(chosen, python)
            finally:
                os.chdir(old_cwd)

    def test_libretranslate_path_fallback_is_not_called_separate_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".libretranslate"
            home.mkdir()
            (root / "requirements-libretranslate.txt").write_text(
                "libretranslate==1.9.6\nchardet==5.2.0\n", encoding="utf-8",
            )
            with mock.patch.object(diagnostics.shutil, "which", return_value=sys.executable), \
                    mock.patch.object(
                        diagnostics, "_metadata_version_near_python",
                        side_effect=lambda _python, name: {
                            "libretranslate": "1.9.6", "chardet": "5.2.0",
                        }[name],
                    ):
                checks = diagnostics.check_libretranslate(
                    {}, root=root, probe_services=False,
                 runner=mock.Mock(return_value=types.SimpleNamespace(returncode=1)))
        environment = by_name(checks, "Separate environment")
        self.assertEqual(environment.status, diagnostics.UNAVAILABLE)
        self.assertIn("not a dedicated", environment.detail)

    def test_dependency_check_targets_launcher_venv_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / ".venv" / "Scripts" / "python.exe"
            metadata = (
                root / ".venv" / "Lib" / "site-packages" /
                "example_pkg-1.2.3.dist-info" / "METADATA"
            )
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            metadata.parent.mkdir(parents=True)
            metadata.write_text("Name: example-pkg\nVersion: 1.2.3\n", encoding="utf-8")
            (root / "requirements.txt").write_text(
                "example-pkg==1.2.3\n", encoding="utf-8",
            )
            with mock.patch.object(
                diagnostics.importlib.metadata, "version",
                side_effect=AssertionError("must not inspect the current interpreter"),
            ):
                checks = diagnostics.check_dependencies(root)
        self.assertEqual(by_name(checks, "Launcher interpreter").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "example-pkg").status, diagnostics.READY)

    def test_config_checks_report_missing_and_invalid_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / diagnostics.CONFIG_FILES[0][1]
            invalid = root / diagnostics.CONFIG_FILES[1][1]
            valid.parent.mkdir(parents=True)
            valid.write_text("enabled: true\n", encoding="utf-8")
            invalid.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
            before = {path.relative_to(root) for path in root.rglob("*")}
            with mock.patch.dict(sys.modules, {"yaml": FAKE_YAML}):
                checks = diagnostics.check_configs(root)
            after = {path.relative_to(root) for path in root.rglob("*")}
        self.assertEqual(before, after)
        self.assertEqual(by_name(checks, "App settings").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Commands").status, diagnostics.ERROR)
        self.assertEqual(by_name(checks, "Content filter").status,
                         diagnostics.UNAVAILABLE)

    def test_config_checks_follow_active_emotions_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preferred = root / "MedlarTV" / "config" / "emotions.yaml"
            fallback = root / "MedlarTV" / "data" / "emotions.yaml"
            preferred.parent.mkdir(parents=True)
            fallback.parent.mkdir(parents=True)
            preferred.write_text("- invalid\n- active\n", encoding="utf-8")
            fallback.write_text("emotions: {}\n", encoding="utf-8")
            with mock.patch.dict(sys.modules, {"yaml": FAKE_YAML}):
                checks = diagnostics.check_configs(root)
        active = by_name(checks, "Emotions (active)")
        self.assertEqual(active.status, diagnostics.ERROR)
        self.assertIn(str(Path("MedlarTV/config/emotions.yaml")), active.detail)

    def test_event_semantics_check_assets_by_metadata_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "MedlarTV" / "config" / "events.yaml"
            config.parent.mkdir(parents=True)
            sounds = root / "Sounds"
            sounds.mkdir()
            (sounds / "present.mp3").write_bytes(b"synthetic")
            config.write_text(
                "events:\n"
                "  - name: sound_rule\n"
                "    effects:\n"
                "      - type: play_sound\n"
                "        path: C:/missing/present.mp3\n"
                "      - type:\n"
                "        path:\n"
                "ignored_users: []\n",
                encoding="utf-8",
            )
            fake_yaml = types.SimpleNamespace(
                YAMLError=ValueError,
                safe_load=lambda _handle: {
                    "events": [{
                        "name": "sound_rule",
                        "effects": [
                            {"type": "play_sound", "path": "C:/missing/present.mp3"},
                            {"type": None, "path": None},
                        ],
                    }],
                    "ignored_users": [],
                },
            )
            with mock.patch.dict(sys.modules, {"yaml": fake_yaml}), \
                    mock.patch.object(Path, "read_bytes", side_effect=AssertionError("media read")):
                checks = diagnostics.check_event_configuration(root)
        sound = next(item for item in checks if item.name.endswith("sound_rule / present.mp3"))
        blank = next(item for item in checks if item.name.endswith("sound_rule #2"))
        self.assertEqual(sound.status, diagnostics.UNAVAILABLE)
        self.assertIn("same-named file exists", sound.detail)
        self.assertEqual(blank.status, diagnostics.UNAVAILABLE)

    def test_source_layout_requires_active_memory_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in diagnostics.REQUIRED_SOURCE_FILES:
                if relative == "MedlarTV/core/memory.py":
                    continue
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            checks = diagnostics.check_source_layout(root)
        self.assertEqual(checks[0].status, diagnostics.UNAVAILABLE)
        self.assertIn("MedlarTV/core/memory.py", checks[0].detail)

    def test_dependency_metadata_exception_is_reported_without_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            (root / "requirements.txt").write_text(
                "example-pkg==1.2.3\n", encoding="utf-8",
            )
            with mock.patch.object(
                diagnostics, "_metadata_version_near_python",
                side_effect=RuntimeError(SECRET),
            ):
                checks = diagnostics.check_dependencies(root)
        package = by_name(checks, "example-pkg")
        self.assertEqual(package.status, diagnostics.ERROR)
        self.assertNotIn(SECRET, package.detail)

    def test_memory_checks_never_open_memory_or_database_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.json").write_text(SECRET, encoding="utf-8")
            local = root / "local"
            database = local / "MedlarTV" / "viewer_profiles.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_text(SECRET, encoding="utf-8")
            with mock.patch.object(Path, "open", side_effect=AssertionError("content read")), \
                    mock.patch.object(Path, "read_text", side_effect=AssertionError("content read")):
                checks = diagnostics.check_memory_paths(
                    {"LOCALAPPDATA": str(local)}, root,
                )
        self.assertEqual(by_name(checks, "Mood memory").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Durable viewer profiles").status,
                         diagnostics.READY)
        self.assertNotIn(SECRET, repr(checks))


class ReportAndCliTests(unittest.TestCase):
    def test_main_returns_reexec_child_code_before_any_work(self):
        with mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=7) as relaunch, \
                mock.patch.object(diagnostics, "collect_diagnostics") as collect:
            self.assertEqual(diagnostics.main([]), 7)
        relaunch.assert_called_once_with([])
        collect.assert_not_called()

    def test_write_report_uses_exact_utf8_bytes_and_lf(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "café-report.txt"
            payload = "café 🫐\nsecond line\n"
            diagnostics.write_report(path, payload, default_report=path)
            self.assertEqual(path.read_bytes(), payload.encode("utf-8"))

    def test_owned_default_updates_but_other_existing_file_needs_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default = root / "logs" / "medlar_diagnostics.txt"
            diagnostics.write_report(default, "first café\n", default_report=default)
            diagnostics.write_report(default, "second café\n", default_report=default)
            self.assertEqual(default.read_text(encoding="utf-8"), "second café\n")

            other = root / "notes.md"
            other.write_text("keep me", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                diagnostics.write_report(other, "replacement", default_report=default)
            self.assertEqual(other.read_text(encoding="utf-8"), "keep me")
            diagnostics.write_report(
                other, "replacement ✓", overwrite=True, default_report=default,
            )
            self.assertEqual(other.read_text(encoding="utf-8"), "replacement ✓")

    def test_output_rejects_non_text_extensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "txt or .md"):
                diagnostics.resolve_output_path("report.json", Path(temporary))

    def test_output_rejects_ads_and_reserved_windows_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("existing.txt:diagnostics.md", "NUL.md", "CON.txt"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    diagnostics.resolve_output_path(name, root)

    def test_output_path_metadata_failure_is_fail_closed_and_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "report.txt"
            with mock.patch.object(
                Path, "lstat", side_effect=PermissionError(SECRET),
            ):
                with self.assertRaisesRegex(
                    ValueError, "safety could not be verified",
                ) as raised:
                    diagnostics.ensure_output_allowed(target)
            self.assertNotIn(SECRET, str(raised.exception))

    def test_output_rejects_symlink_or_reparse_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "logs" / "medlar_diagnostics.txt"
            target.parent.mkdir()
            target.write_text("unrelated", encoding="utf-8")
            with mock.patch.object(
                diagnostics, "_is_link_or_reparse_point",
                side_effect=lambda candidate: candidate == target,
            ):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    diagnostics.ensure_output_allowed(
                        target, default_report=target,
                    )
            self.assertEqual(target.read_text(encoding="utf-8"), "unrelated")

    def test_editor_is_called_only_explicitly_and_after_success(self):
        with mock.patch.object(diagnostics, "ensure_sensitive_output_allowed"):
            report = ROOT / "logs" / "synthetic-editor-test.txt"
            with mock.patch.object(diagnostics.os, "name", "nt"), \
                    mock.patch.object(diagnostics.os, "startfile", create=True) as startfile:
                diagnostics.open_in_editor(report)
            startfile.assert_called_once_with(str(report))

            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "result.txt"
                with mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                        mock.patch.object(diagnostics, "collect_diagnostics") as collect, \
                        mock.patch.object(diagnostics, "write_report", side_effect=OSError("write failed")), \
                        mock.patch.object(diagnostics, "open_in_editor") as editor, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    collect.return_value = diagnostics.DiagnosticRun([])
                    code = diagnostics.main([
                        "--output", str(output), "--open", "--no-env-file",
                        "--no-credential-store", "--no-service-probes",

                    ])
                self.assertEqual(code, 2)
                editor.assert_not_called()

    def test_successful_cli_opens_only_when_requested(self):
        with mock.patch.object(diagnostics, "ensure_sensitive_output_allowed"):
            with tempfile.TemporaryDirectory() as temporary:
                first = Path(temporary) / "first.txt"
                second = Path(temporary) / "second.txt"
                synthetic = diagnostics.DiagnosticRun([
                    diagnostics.Check("Synthetic", "ok", diagnostics.READY, "safe"),
                ])
                with mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                        mock.patch.object(diagnostics, "collect_diagnostics", return_value=synthetic), \
                        mock.patch.object(diagnostics, "write_report") as write, \
                        mock.patch.object(diagnostics, "open_in_editor") as editor, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code_without = diagnostics.main([
                        "--output", str(first),
                    ])
                    code_with = diagnostics.main([
                        "--output", str(second),  "--open",
                    ])
            self.assertEqual(code_without, 0)
            self.assertEqual(code_with, 0)
            self.assertEqual(write.call_count, 2)
            editor.assert_called_once_with(second)

    def test_critical_exercise_failure_has_priority_after_write_or_editor_error(self):
        with mock.patch.object(diagnostics, "ensure_sensitive_output_allowed"):
            critical = diagnostics.DiagnosticRun([
                diagnostics.Check("Full-live Twitch exercise", "Full exercise result",
                                  diagnostics.ERROR, "synthetic failure"),
            ], critical_failure=True)
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "critical.txt"
                stderr = io.StringIO()
                with mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                        mock.patch.object(diagnostics, "collect_diagnostics", return_value=critical), \
                        mock.patch.object(diagnostics, "write_report") as write, \
                        mock.patch.object(diagnostics, "open_in_editor", side_effect=OSError("editor")), \
                        redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    code = diagnostics.main([
                        "--output", str(output),  "--open",
                    ])
                self.assertEqual(code, 4)
                self.assertEqual(write.call_count, 2)
                self.assertIn("report editor", write.call_args.args[1])
                self.assertIn("requested exercise was blocked or incomplete", stderr.getvalue())

                stderr = io.StringIO()
                with mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                        mock.patch.object(diagnostics, "collect_diagnostics", return_value=critical), \
                        mock.patch.object(diagnostics, "write_report", side_effect=OSError("write")), \
                        redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    code = diagnostics.main([
                        "--output", str(output),
                    ])
                self.assertEqual(code, 4)
                self.assertIn("ERROR", stderr.getvalue())

    def test_report_write_exception_redacts_resolved_secret(self):
        with mock.patch.object(diagnostics, "ensure_sensitive_output_allowed"):
            run = diagnostics.DiagnosticRun([], secrets=(SECRET,))
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "report.txt"
                stderr = io.StringIO()
                with mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                        mock.patch.object(diagnostics, "collect_diagnostics", return_value=run), \
                        mock.patch.object(
                            diagnostics, "write_report",
                            side_effect=OSError(f"synthetic write failure {SECRET}"),
                        ), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    code = diagnostics.main([
                        "--output", str(output),
                    ])
            self.assertEqual(code, 2)
            self.assertNotIn(SECRET, stderr.getvalue())
            self.assertIn("stage=report save", stderr.getvalue())
            self.assertIn("stale", stderr.getvalue())

    def test_exercise_cli_requires_full_explicit_gate(self):
        with mock.patch.object(diagnostics, "ensure_sensitive_output_allowed"):
            cases = (
                ["--exercise-twitch"],
                ["--live-twitch", "--exercise-twitch"],
                ["--live-twitch", "--test-title", "temporary"],
            )
            for argv in cases:
                with self.subTest(argv=argv), \
                        mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                        mock.patch.object(diagnostics, "collect_diagnostics") as collect, \
                        redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        diagnostics.main(argv)
                    collect.assert_not_called()

    def test_cli_is_cwd_independent_and_writes_full_markdown_utf8(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.md"
            with mock.patch.object(Path, "cwd", return_value=Path(temporary)), \
                    mock.patch.object(diagnostics, "collect_diagnostics", return_value=diagnostics.DiagnosticRun([
                        diagnostics.Check("Synthetic section", "Synthetic detail", diagnostics.READY, SECRET)])), \
                    mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                    mock.patch.object(diagnostics, "ensure_sensitive_output_allowed"), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(diagnostics.main(["--output", str(output)]), 0)
            text = output.read_text(encoding="utf-8")
            self.assertIn("PRIVATE / FULL / SENSITIVE", text)
            self.assertIn(SECRET, text)
            self.assertIn("Synthetic section", text)


class RevisedDiagnosticContractTests(unittest.TestCase):
    def test_cli_defaults_to_full_read_only_report_and_offline_is_explicit(self):
        synthetic = diagnostics.DiagnosticRun([])
        with mock.patch.object(diagnostics, "collect_diagnostics", return_value=synthetic) as collect, \
                mock.patch.object(diagnostics, "ensure_sensitive_output_allowed"), \
                mock.patch.object(diagnostics, "write_report") as write, \
                mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None) as relaunch, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(diagnostics.main([]), 0)
        self.assertTrue(collect.call_args.kwargs["live_twitch"])
        self.assertTrue(collect.call_args.kwargs["include_secret_values"])
        write.assert_called_once()
        self.assertEqual(write.call_args.args[0], diagnostics.DEFAULT_REPORT)
        self.assertIn("PRIVATE / FULL / SENSITIVE", write.call_args.args[1])
        relaunch.assert_called_once_with([])

    def test_cli_rejects_static_and_shareable_modes_before_collection(self):
        with mock.patch.object(diagnostics, "reexec_in_launcher_python", return_value=None), \
                mock.patch.object(diagnostics, "collect_diagnostics") as collect, redirect_stderr(io.StringIO()):
            for argv in (["--share-safe"], ["--profile", "static"], ["--private-capture"]):
                with self.assertRaises(SystemExit): diagnostics.main(argv)
            collect.assert_not_called()

    def test_offline_mode_retains_local_checks_and_marks_twitch_incomplete(self):
        from test_medlar_diagnostics_accuracy import local_fixtures
        http = mock.Mock(side_effect=AssertionError("Twitch HTTP called"))
        with local_fixtures() as checks:
            run = diagnostics.collect_diagnostics(root=ROOT, environ={}, use_env_file=False,
                                                  use_vault=False, probe_writes=False, live_twitch=False, http_json=http)
            checks["check_configs"].assert_called_once()
            checks["check_dependency_imports"].assert_called_once()
        http.assert_not_called()
        self.assertEqual(by_name(run.checks, "Run completeness").status, diagnostics.SKIPPED)
        self.assertFalse(run.complete)
        self.assertTrue(run.offline)

    def test_required_stopped_baseline_rows_are_independent(self):
        rows, passed = diagnostics.required_stopped_baseline_checks(
            diagnostics.StoppedBaseline(
                True, medlar_process_count=1, ollama_process_count=0,
                medlar_irc_connection_count=0,
                medlar_kinds=("Command Center GUI",),
            ),
            True, "configured Ollama endpoint refused the TCP connection",
        )
        self.assertFalse(passed)
        self.assertEqual(by_name(rows, "Medlar closed").status, diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(rows, "Medlar Twitch disconnected").status,
                         diagnostics.READY)
        self.assertEqual(by_name(rows, "Ollama processes stopped").status,
                         diagnostics.READY)
        self.assertEqual(by_name(rows, "Ollama endpoint stopped").status,
                         diagnostics.READY)
        self.assertEqual(by_name(rows, "Local stopped-state gate").status,
                         diagnostics.UNAVAILABLE)
        self.assertIn("Command Center GUI", by_name(rows, "Medlar closed").detail)

    def test_local_stopped_baseline_failure_blocks_credentials_and_all_twitch_io(self):
        http = mock.Mock(side_effect=AssertionError("Twitch HTTP called"))
        overlay = mock.Mock(side_effect=AssertionError("credential overlay called"))
        irc_sender = mock.Mock(side_effect=AssertionError("IRC sender called"))
        irc_probe = mock.Mock(side_effect=AssertionError("IRC probe called"))
        exercise = TwitchExerciseTests("runTest")._exercise()
        with without_host_probes():
            run = diagnostics.collect_diagnostics(
                root=ROOT, environ={}, use_env_file=False, use_vault=True,
                probe_services=False, probe_writes=False, live_twitch=True,
                twitch_exercise=exercise, http_json=http,
                irc_sender=irc_sender, irc_probe=irc_probe,
                vault_probe=lambda _root: (
                    {key: False for key in diagnostics.CREDENTIAL_FIELDS}, None,
                ),
                credential_overlay=overlay,
                baseline_probe=lambda _root: diagnostics.StoppedBaseline(
                    True, medlar_process_count=1, medlar_kinds=("Core",),
                ),
                ollama_endpoint_probe=lambda _environment, _root: (
                    True, "configured Ollama endpoint refused the TCP connection",
                ))
        overlay.assert_not_called()
        http.assert_not_called()
        irc_sender.assert_not_called()
        irc_probe.assert_not_called()
        self.assertEqual(by_name(run.checks, "Local stopped-state gate").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(
            by_name(run.checks, "Saved credential decode/apply viability").status,
            diagnostics.SKIPPED,
        )
        full = by_name(run.checks, "Full exercise result")
        self.assertEqual(full.status, diagnostics.UNAVAILABLE)
        self.assertIn("no credential was resolved", full.detail)
        self.assertFalse(run.sensitive_values_resolved)
        blocked_report = diagnostics.render_report(run)
        self.assertIn("MEDLARTV_TWITCH_TOKEN: NOT RESOLVED", blocked_report)
        self.assertNotIn("MEDLARTV_TWITCH_TOKEN: exact effective value=null", blocked_report)

    def test_remote_live_stream_gate_uses_app_only_and_blocks_user_tokens_and_irc(self):
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "POST" and url.endswith("/token"):
                return diagnostics.HttpJsonResult(
                    200, payload={"access_token": "synthetic-baseline-app-token", "expires_in": 3600, "token_type": "bearer"},
                    raw_body='{"access_token":"synthetic-baseline-app-token", "expires_in": 3600, "token_type": "bearer"}',
                )
            if method == "GET" and "/helix/users?" in url:
                return diagnostics.HttpJsonResult(
                    200, payload={"data": [{"login": "target_channel", "id": "42"}]},
                    raw_body='{"data":[{"login":"target_channel","id":"42"}]}',
                )
            if method == "GET" and "/helix/streams?" in url:
                return diagnostics.HttpJsonResult(
                    200, payload={"data": [{"id": "live-stream"}]},
                    raw_body='{"data":[{"id":"live-stream"}]}',
                )
            raise AssertionError((method, url, kwargs))

        environment = TwitchExerciseTests("runTest")._environment()
        irc_sender = mock.Mock(side_effect=AssertionError("IRC sender called"))
        irc_probe = mock.Mock(side_effect=AssertionError("IRC probe called"))
        with without_host_probes():
            run = diagnostics.collect_diagnostics(
                root=ROOT, environ=environment, use_env_file=False, use_vault=False,
                probe_services=False, probe_writes=False, live_twitch=True,
                twitch_exercise=TwitchExerciseTests("runTest")._exercise(),
                http_json=fake_http, irc_sender=irc_sender, irc_probe=irc_probe,
                baseline_probe=lambda _root: diagnostics.StoppedBaseline(True),
                ollama_endpoint_probe=lambda _environment, _root: (
                    True, "configured Ollama endpoint refused the TCP connection",
                ))
        irc_sender.assert_not_called()
        irc_probe.assert_not_called()
        self.assertEqual(len(calls), 3)
        self.assertEqual([method for method, _url, _kwargs in calls], ["POST", "GET", "GET"])
        self.assertFalse(any(url.endswith("/validate") for _method, url, _kwargs in calls))
        self.assertFalse(any(method == "PATCH" for method, _url, _kwargs in calls))
        authorizations = [
            kwargs.get("headers", {}).get("Authorization", "")
            for _method, _url, kwargs in calls
        ]
        self.assertFalse(any(value.startswith("OAuth ") for value in authorizations))
        self.assertEqual(
            by_name(run.checks, "Broadcaster offline before diagnostic").status,
            diagnostics.UNAVAILABLE,
        )
        self.assertIn(
            "1 active stream row", by_name(
                run.checks, "Broadcaster offline before diagnostic",
            ).detail,
        )
        self.assertEqual(by_name(run.checks, "Remote baseline gate").status,
                         diagnostics.UNAVAILABLE)
        self.assertEqual(by_name(run.checks, "Bot token validation").status,
                         diagnostics.SKIPPED)
        self.assertEqual(by_name(run.checks, "Full exercise result").status,
                         diagnostics.UNAVAILABLE)

    def test_safe_live_surface_has_all_rows_and_no_mutation(self):
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append((method, url, kwargs))
            authorization = kwargs.get("headers", {}).get("Authorization", "")
            if url.endswith("/validate"):
                if authorization == "OAuth bot-token":
                    return diagnostics.HttpJsonResult(200, payload={
                        "login": "bot", "client_id": "bot-client",
                        "user_id": "bot-id", "expires_in": 3600, "scopes": ["chat:read", "chat:edit"],
                    })
                return diagnostics.HttpJsonResult(200, payload={
                    "login": "channel", "client_id": "broadcaster-client",
                    "user_id": "channel-id",
                    "expires_in": 3600, "scopes": ["channel:manage:broadcast"],
                })
            if method == "POST" and url.endswith("/token"):
                return diagnostics.HttpJsonResult(200, payload={"access_token": "app-token", "expires_in": 3600, "token_type": "bearer"})
            if "/helix/users?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": [{
                    "login": "channel", "id": "channel-id",
                    "created_at": "2021-02-03T04:05:06Z",
                }]})
            if "/helix/channels?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": [{"broadcaster_id": "channel-id", "title": "Synthetic", "game_id": "1", "game_name": "Synthetic category"}]})
            if "/helix/streams?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": []})
            if "/helix/search/categories?" in url:
                return diagnostics.HttpJsonResult(200, payload={"data": []})
            raise AssertionError((method, url))

        irc = mock.Mock(return_value=diagnostics.IrcProbeResult(True, True, True))
        checks = diagnostics.run_live_twitch_checks({
            "TWITCH_NICK": "bot", "TWITCH_CHANNEL": "#channel",
            "MEDLARTV_TWITCH_TOKEN": "bot-token",
            "MEDLARTV_TWITCH_CLIENT_ID": "bot-client",
            "DEVILMEDLAR_TWITCH_TOKEN": "broadcaster-token",
            "DEVILMEDLAR_TWITCH_CLIENT_ID": "broadcaster-client",
            "APP_TWITCH_CLIENT_ID": "app-client", "APP_SECRET_ID": "app-secret",
        }, root=ROOT, http_json=fake_http, irc_probe=irc)
        for name in diagnostics.READ_ONLY_TWITCH_API_NAMES:
            row = next(item for item in checks if item.name == name)
            self.assertEqual(row.status, diagnostics.READY)
            self.assertIn("HTTP 200", row.detail)
        self.assertEqual(by_name(checks, "Account-age source data").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, "Read-only IRC authentication and join").status,
                         diagnostics.READY)
        self.assertFalse(any(method in {"PATCH", "PUT", "DELETE"} for method, *_ in calls))
        self.assertFalse(any(
            method == "POST" and not url.endswith("/token")
            for method, url, _kwargs in calls
        ))
        self.assertFalse(any("/users/follows" in url for _method, url, _kwargs in calls))
        self.assertFalse(any("/helix/chat/" in url for _method, url, _kwargs in calls))

    def test_validation_rejection_and_network_unknown_are_distinct(self):
        common = dict(
            role="Bot", raw_token="synthetic", expected_login="bot",
            configured_client_id="client", required_scopes=diagnostics.BOT_REQUIRED_SCOPES,
            client_id_required=False, catalog={},
        )
        rejected, _ = diagnostics._validate_user_token(
            **common, http_json=lambda *_a, **_k: diagnostics.HttpJsonResult(401, error="rejected"),
        )
        unknown, _ = diagnostics._validate_user_token(
            **common, http_json=lambda *_a, **_k: diagnostics.HttpJsonResult(None, error="offline"),
        )
        self.assertEqual(by_name(rejected, "Bot token validation").status,
                         diagnostics.UNAVAILABLE)
        self.assertIn("Validation=FAIL", by_name(rejected, "Bot token validation").detail)
        self.assertEqual(by_name(unknown, "Bot token validation").status,
                         diagnostics.SKIPPED)
        self.assertIn("Validation=UNTESTED", by_name(unknown, "Bot token validation").detail)

    def test_raw_scope_inventory_preserves_api_order_duplicates_and_unknown_names(self):
        returned = ["future:scope", "chat:edit", "future:scope", "channel_read"]
        checks, facts = diagnostics._validate_user_token(
            role="Bot", raw_token="synthetic-bot-token", expected_login="bot",
            configured_client_id="synthetic-client",
            required_scopes=diagnostics.BOT_REQUIRED_SCOPES,
            client_id_required=False, catalog={},
            http_json=lambda *_args, **_kwargs: diagnostics.HttpJsonResult(
                200, payload={
                    "login": "bot", "client_id": "synthetic-client",
                    "user_id": "synthetic-user", "expires_in": 3600,
                    "scopes": returned,
                }, raw_body=json.dumps({"scopes": returned}),
            ),
        )
        self.assertIsNotNone(facts)
        self.assertEqual(facts.raw_scopes, tuple(returned))
        raw = by_name(checks, "Bot raw scopes")
        self.assertEqual(
            raw.detail,
            "Twitch returned 4 raw scope entries in API order (3 unique): "
            "future:scope, chat:edit, future:scope, channel_read.",
        )
        classification = by_name(checks, "Bot scope classification").detail
        self.assertIn(
            "future:scope = UNKNOWN/UNMAPPED: not described by the embedded or local catalog; "
            "this does not make it invalid",
            classification,
        )
        self.assertIn(
            "channel_read = LEGACY-DECOMMISSIONED: legacy raw scope name; "
            "not treated as an alias for a modern required permission",
            classification,
        )

    def test_every_granted_scope_row_has_full_independent_fields_and_unshortened_ability(self):
        long_ability = "synthetic complete ability description " + ("detail-" * 300)
        outcomes = {
            "chat:edit": diagnostics.ScopeFunctionalOutcome(
                "PASS", "synthetic message acknowledgement passed",
            ),
            "future:scope": diagnostics.ScopeFunctionalOutcome(
                "FAIL", "synthetic endpoint rejected the operation",
                failure_status=diagnostics.ERROR,
            ),
        }
        checks = diagnostics.detailed_scope_checks(
            "Bot", ["chat:edit", "future:scope", "channel_read"],
            {"chat:edit": long_ability}, outcomes,
        )
        self.assertEqual(len(checks), 3)
        by_scope = {item.name.rsplit(" - ", 1)[-1]: item for item in checks}
        self.assertEqual(by_scope["chat:edit"].status, diagnostics.READY)
        self.assertEqual(by_scope["future:scope"].status, diagnostics.ERROR)
        self.assertEqual(by_scope["channel_read"].status, diagnostics.SKIPPED)
        for scope, row in by_scope.items():
            with self.subTest(scope=scope):
                for field in (
                    "Grant=GRANTED", "Classification=", "Ability=",
                    "Functional=", "Reason=",
                ):
                    self.assertIn(field, row.detail)
        self.assertIn(f"Ability={long_ability}", by_scope["chat:edit"].detail)
        self.assertIn("Functional=PASS", by_scope["chat:edit"].detail)
        self.assertIn("Functional=FAIL", by_scope["future:scope"].detail)
        self.assertIn("Functional=UNTESTED", by_scope["channel_read"].detail)
        self.assertIn(
            "No per-scope behavior is inferred", by_scope["channel_read"].detail,
        )
        report = diagnostics.render_report(diagnostics.DiagnosticRun(checks=checks))
        self.assertIn(f"Ability={long_ability}; Functional=PASS", report)

    def test_successful_exercise_promotes_only_exercised_scopes(self):
        helper = TwitchExerciseTests(
            "test_full_exercise_changes_verifies_restores_and_sends_exactly_once"
        )
        fake_http, _calls, _state = helper._successful_http()
        checks = diagnostics.run_live_twitch_checks(
            helper._environment(), root=ROOT, http_json=fake_http,
            exercise=helper._exercise(),
            irc_sender=lambda *_args: diagnostics.IrcSendResult(
                connected=True, authenticated=True, joined=True,
                written=True, acknowledged=True,
            ),
         gate_context=synthetic_gate())
        granted = {
            item.name: item for item in checks if "granted scopes" in item.section
        }
        self.assertEqual(granted["Bot & Chat - chat:edit"].status, diagnostics.READY)
        self.assertIn("Functional=PASS", granted["Bot & Chat - chat:edit"].detail)
        self.assertEqual(granted["Bot & Chat - chat:read"].status, diagnostics.SKIPPED)
        self.assertIn("Functional=UNTESTED", granted["Bot & Chat - chat:read"].detail)
        broadcast = next(value for key, value in granted.items() if "channel:manage:broadcast" in key)
        self.assertEqual(broadcast.status, diagnostics.READY)
        self.assertIn("Functional=PASS", broadcast.detail)
        shoutout = next(value for key, value in granted.items() if "moderator:manage:shoutouts" in key)
        self.assertEqual(shoutout.status, diagnostics.SKIPPED)
        self.assertIn("Functional=UNTESTED", shoutout.detail)

    def test_chat_failure_does_not_erase_successful_broadcast_function(self):
        helper = TwitchExerciseTests(
            "test_full_exercise_changes_verifies_restores_and_sends_exactly_once"
        )
        fake_http, _calls, _state = helper._successful_http()
        checks = diagnostics.run_live_twitch_checks(
            helper._environment(), root=ROOT, http_json=fake_http,
            exercise=helper._exercise(),
            irc_sender=lambda *_args: diagnostics.IrcSendResult(
                connected=True, authenticated=True, joined=True,
                written=True, acknowledged=False,
            ),
         gate_context=synthetic_gate())
        broadcast = next(
            item for item in checks
            if item.section.endswith("Broadcaster granted scopes")
            and "channel:manage:broadcast" in item.name
        )
        chat_edit = next(
            item for item in checks
            if item.section.endswith("Bot granted scopes")
            and "chat:edit" in item.name
        )
        self.assertEqual(broadcast.status, diagnostics.READY)
        self.assertIn("Functional=PASS", broadcast.detail)
        self.assertEqual(chat_edit.status, diagnostics.UNAVAILABLE)
        self.assertIn("Functional=FAIL", chat_edit.detail)

    def test_private_report_lists_exact_credentials_prefixes_lengths_and_wire_values(self):
        values = {
            "MEDLARTV_TWITCH_TOKEN": "oauth:synthetic-bot-token-0123456789",
            "MEDLARTV_TWITCH_CLIENT_ID": "synthetic-bot-client-id-0123456789",
            "APP_TWITCH_CLIENT_ID": "synthetic-app-client-id-0123456789",
            "APP_SECRET_ID": "synthetic-app-secret-0123456789",
            "DEVILMEDLAR_TWITCH_CLIENT_ID": "synthetic-broadcaster-client-0123456789",
            "DEVILMEDLAR_TWITCH_TOKEN": "oauth:synthetic-broadcaster-token-0123456789",
            "TWITCH_BASELINE_APP_ACCESS_TOKEN": "synthetic-baseline-app-token-0123456789",
            "TWITCH_EPHEMERAL_APP_ACCESS_TOKEN": "synthetic-ephemeral-app-token-0123456789",
        }
        run = diagnostics.DiagnosticRun(
            [diagnostics.Check("Synthetic", "full evidence", diagnostics.READY,
                               "synthetic private report")],
            secrets=tuple(value for value in values.values()),
            sensitive_values=values,
        )
        report = diagnostics.render_report(run)
        self.assertIn("Report fidelity: PRIVATE / FULL / SENSITIVE", report)
        self.assertIn("PRIVATE CREDENTIAL VALUES USED", report)
        for key, value in values.items():
            with self.subTest(field=key):
                self.assertIn(
                    f"- {key}: exact effective value={json.dumps(value, ensure_ascii=False)}",
                    report,
                )
                prefix = value[:20] + ("..." if len(value) > 20 else "")
                self.assertIn(
                    f"configured prefix (first 20 characters, diagnostic derivative)="
                    f"{json.dumps(prefix)}; configured length={len(value)}",
                    report,
                )
        for key in (
            "MEDLARTV_TWITCH_TOKEN", "DEVILMEDLAR_TWITCH_TOKEN",
            "TWITCH_BASELINE_APP_ACCESS_TOKEN", "TWITCH_EPHEMERAL_APP_ACCESS_TOKEN",
        ):
            normalized = diagnostics._normalized_oauth_token(values[key])
            wire_prefix = normalized[:20] + ("..." if len(normalized) > 20 else "")
            self.assertIn(
                f"exact normalized wire value={json.dumps(normalized)}; "
                f"wire prefix={json.dumps(wire_prefix)}; wire length={len(normalized)}",
                report,
            )
        broadcaster = values["DEVILMEDLAR_TWITCH_TOKEN"]
        self.assertIn(
            "source=DEVILMEDLAR_TWITCH_TOKEN only (no bot-token fallback); "
            f"exact value={json.dumps(broadcaster)}; "
            f"prefix={json.dumps(broadcaster[:20] + '...')}; length={len(broadcaster)}",
            report,
        )

    def test_fail_closed_private_report_marks_credentials_not_resolved(self):
        run = diagnostics.DiagnosticRun(
            checks=[diagnostics.Check(
                "Required execution baseline", "Local stopped-state gate",
                diagnostics.UNAVAILABLE,
                "Synthetic gate blocked credential resolution.",
            )],
            sensitive_values_resolved=False,
            exercise_requested=True,
        )
        report = diagnostics.render_report(run)
        credential_block = report.split("PRIVATE CREDENTIAL VALUES USED", 1)[1].split(
            "HTTP REQUEST/RESPONSE TRANSCRIPT", 1,
        )[0]
        for key in (
            *diagnostics.CREDENTIAL_FIELDS,
            "TWITCH_BASELINE_APP_ACCESS_TOKEN",
            "TWITCH_EPHEMERAL_APP_ACCESS_TOKEN",
        ):
            with self.subTest(field=key):
                self.assertIn(f"- {key}: NOT RESOLVED", credential_block)
        self.assertIn(
            "Effective broadcaster token selection (legacy TWITCH_USER_TOKEN listing): "
            "NOT RESOLVED",
            credential_block,
        )
        self.assertNotIn("exact effective value=null", credential_block)
        self.assertNotIn("configured length=0", credential_block)

    def test_exercise_report_mode_distinguishes_completed_from_blocked(self):
        completed = diagnostics.DiagnosticRun(
            checks=[diagnostics.Check(
                "Full-live Twitch exercise", "Full exercise result",
                diagnostics.READY, "Synthetic exercise completed.",
            )],
            exercise_requested=True,
        )
        blocked = diagnostics.DiagnosticRun(
            checks=[diagnostics.Check(
                "Full-live Twitch exercise", "Full exercise result",
                diagnostics.UNAVAILABLE, "Synthetic exercise was blocked.",
            )],
            exercise_requested=True,
        )
        completed_report = diagnostics.render_report(completed)
        blocked_report = diagnostics.render_report(blocked)
        self.assertIn(
            "Diagnostic mode: AUTOMATED TWITCH EXERCISE / COMPLETED",
            completed_report,
        )
        self.assertIn(
            "Diagnostic mode: AUTOMATED TWITCH EXERCISE / BLOCKED OR INCOMPLETE",
            blocked_report,
        )
        self.assertNotIn(
            "Diagnostic mode: AUTOMATED TWITCH EXERCISE / COMPLETED",
            blocked_report,
        )

    def test_private_http_transcript_keeps_headers_forms_json_and_raw_responses(self):
        exchanges = []
        results = iter((
            diagnostics.HttpJsonResult(
                400, payload={"message": "synthetic rejection"},
                error="HTTP request was rejected",
                raw_body='{"message":"synthetic rejection","trace":"raw-one"}',
                response_headers=(("X-Diagnostic", "private-header-value"),),
                body_bytes_observed=52,
            ),
            diagnostics.HttpJsonResult(
                204, raw_body="", response_headers=(("X-Request-Id", "request-two"),),
                body_bytes_observed=0,
            ),
        ))
        recorded = diagnostics._recording_http_json(
            lambda *_args, **_kwargs: next(results), exchanges)
        recorded(
            "POST", "https://id.twitch.tv/oauth2/token",
            headers={"Client-ID": "synthetic-full-client-id",
                     "Authorization": "Bearer synthetic-full-token"},
            form={"client_id": "synthetic-full-client-id",
                  "client_secret": "synthetic-full-secret",
                  "grant_type": "client_credentials"},
        )
        recorded(
            "PATCH", "https://api.twitch.tv/helix/channels?broadcaster_id=42",
            headers={"Client-ID": "synthetic-broadcaster-client",
                     "Authorization": "Bearer synthetic-broadcaster-token"},
            json_body={"title": "synthetic-private-temporary-title-9482"},
            allow_empty=True,
        )
        secrets = (
            "synthetic-full-client-id", "synthetic-full-token",
            "synthetic-full-secret", "synthetic-broadcaster-client",
            "synthetic-broadcaster-token", "private-header-value",
        )
        run = diagnostics.DiagnosticRun(
            checks=[], secrets=secrets,
            sensitive_values={"APP_SECRET_ID": "synthetic-full-secret"},
            http_exchanges=exchanges,
        )
        private = diagnostics.render_report(run)
        for exact in (
            "POST https://id.twitch.tv/oauth2/token",
            "Client-ID: synthetic-full-client-id",
            "Authorization: Bearer synthetic-full-token",
            "Content-Type: application/x-www-form-urlencoded",
            "client_id=synthetic-full-client-id&client_secret=synthetic-full-secret&grant_type=client_credentials",
            "Response status: 400",
            "X-Diagnostic: private-header-value",
            '{"message":"synthetic rejection","trace":"raw-one"}',
            'PATCH https://api.twitch.tv/helix/channels?broadcaster_id=42',
            '{"title": "synthetic-private-temporary-title-9482"}',
            "Response status: 204",
            "X-Request-Id: request-two",
            "Response body: [EMPTY BODY]",
        ):
            with self.subTest(exact=exact):
                self.assertIn(exact, private)

        share_safe = diagnostics.render_report(run, share_safe=True)
        self.assertIn("SHARE-SAFE / REDACTED / INCOMPLETE", share_safe)
        for secret in secrets:
            self.assertNotIn(secret, share_safe)
        self.assertNotIn("synthetic rejection", share_safe)
        self.assertNotIn("synthetic-private-temporary-title-9482", share_safe)
        self.assertIn("WITHHELD", share_safe)
        self.assertIn(
            "Response body: [REDACTED IN SHARE-SAFE MODE", share_safe,
        )

    def test_complete_response_body_is_not_silently_truncated_in_report(self):
        complete_body = "synthetic-start-" + ("response-segment-" * 70000) + "complete-tail"
        observed = len(complete_body.encode("utf-8"))
        run = diagnostics.DiagnosticRun(
            http_exchanges=[diagnostics.HttpExchangeEvidence(
                sequence=1, method="GET", url="https://example.invalid/large",
                request_headers=(), request_body=None, response_status=200,
                response_headers=(("Content-Type", "application/json"),),
                response_body=complete_body,
                response_body_source="raw transport body",
                response_body_truncated=False,
                response_body_bytes_observed=observed,
                error=None,
            )],
        )
        report = diagnostics.render_report(run)
        self.assertIn(
            "Response body completeness: COMPLETE (no diagnostic truncation)",
            report,
        )
        self.assertIn(f"Response bytes observed: {observed}", report)
        self.assertIn(complete_body, report)
        self.assertIn("complete-tail", report)
        self.assertNotIn("retained the first", report)

    def test_private_output_requires_root_logs_and_allows_owned_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            (root / ".gitignore").write_text("/logs/\n", encoding="utf-8")
            accepted = diagnostics.resolve_output_path("logs/private.md", root)
            diagnostics.ensure_sensitive_output_allowed(accepted, root)
            owned_default = diagnostics.resolve_output_path(None, root)
            diagnostics.ensure_sensitive_output_allowed(owned_default, root)
            for value in (
                root / "logs2" / "private.txt",
                root / "outside.txt",
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    diagnostics.ensure_sensitive_output_allowed(value, root)
            (root / ".gitignore").write_text("logs2/\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ignore rule"):
                diagnostics.ensure_sensitive_output_allowed(accepted, root)

    def test_private_output_requires_effective_git_ignore_when_repository_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            (root / ".git").mkdir()
            (root / ".gitignore").write_text("/logs/\n", encoding="utf-8")
            target = root / "logs" / "private-diagnostics.txt"
            with mock.patch.object(diagnostics.subprocess, "run") as git:
                git.return_value = types.SimpleNamespace(returncode=0)
                diagnostics.ensure_sensitive_output_allowed(target, root)
                git.assert_called_once()
                command = git.call_args.args[0]
                self.assertEqual(command[:5], [
                    "git", "-C", str(root), "check-ignore", "--quiet",
                ])
                self.assertEqual(command[5], "--")
                self.assertEqual(Path(command[6]), Path("logs") / target.name)
                self.assertIs(git.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertFalse(git.call_args.kwargs["check"])

                git.reset_mock()
                git.return_value = types.SimpleNamespace(returncode=1)
                with self.assertRaisesRegex(ValueError, "effectively Git-ignored"):
                    diagnostics.ensure_sensitive_output_allowed(target, root)
                git.assert_called_once()

    def test_transient_write_probe_cleans_up_and_disabled_mode_calls_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            before = {item.name for item in directory.iterdir()}
            success, cleanup = diagnostics.transient_write_probe(directory)
            after = {item.name for item in directory.iterdir()}
            self.assertTrue(success)
            self.assertTrue(cleanup)
            self.assertEqual(before, after)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

            probe = mock.Mock(side_effect=AssertionError("probe called"))
            checks = diagnostics.check_runtime_writability(
                {"LOCALAPPDATA": str(directory)}, directory,
                enabled=False, probe=probe,
            )
            probe.assert_not_called()
            self.assertTrue(all(item.status == diagnostics.SKIPPED for item in checks))

    def test_runtime_writability_probes_exact_session_and_viewer_database_parents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            local = Path(temporary) / "local-app-data"
            observed = []

            def probe(path):
                observed.append(path)
                return True, True

            checks = diagnostics.check_runtime_writability(
                {"LOCALAPPDATA": str(local)}, root, probe=probe,
            )
        self.assertEqual(by_name(checks, "Session parent").status, diagnostics.READY)
        self.assertEqual(by_name(checks, "Viewer-profile parent").status,
                         diagnostics.READY)
        self.assertIn(root / "db" / "sessions", observed)
        self.assertIn(local / "MedlarTV", observed)
        self.assertNotIn(local / "MedlarTV" / "viewer_profiles.sqlite3", observed)
        for name in ("Session parent", "Viewer-profile parent"):
            detail = by_name(checks, name).detail
            self.assertIn("unique disposable file was created", detail)
            self.assertIn("atomically replaced, and deleted", detail)

    def test_source_syntax_failure_is_localized_without_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / diagnostics.REQUIRED_SOURCE_FILES[0]
            bad = root / diagnostics.REQUIRED_SOURCE_FILES[1]
            good.parent.mkdir(parents=True, exist_ok=True)
            bad.parent.mkdir(parents=True, exist_ok=True)
            good.write_text("def ok():\n    return 1\n", encoding="utf-8")
            bad.write_text("def broken(:\n", encoding="utf-8")
            checks = diagnostics.check_source_syntax(root)
        self.assertEqual(by_name(checks, f"Syntax - {diagnostics.REQUIRED_SOURCE_FILES[0]}").status,
                         diagnostics.READY)
        self.assertEqual(by_name(checks, f"Syntax - {diagnostics.REQUIRED_SOURCE_FILES[1]}").status,
                         diagnostics.ERROR)

    def test_ollama_launcher_mode_precondition_is_not_service_health(self):
        fake_socket = mock.Mock()
        fake_socket.bind.side_effect = OSError("occupied")
        with mock.patch.object(diagnostics.socket, "socket", return_value=fake_socket):
            all_mode = diagnostics.check_ollama_launch_precondition(
                {}, "all", root=ROOT, service_ready=True,
            )[0]
        twitch_mode = diagnostics.check_ollama_launch_precondition(
            {}, "twitch", root=ROOT, service_ready=True,
        )[0]
        self.assertEqual(all_mode.status, diagnostics.UNAVAILABLE)
        self.assertIn("healthy existing API does not satisfy", all_mode.detail)
        self.assertEqual(twitch_mode.status, diagnostics.READY)

    def test_original_script_parity_uses_real_outcomes(self):
        observed = [
            diagnostics.Check("Live Twitch", "App credential exchange",
                              diagnostics.UNAVAILABLE, "failed"),
            diagnostics.Check("Live Twitch", "Read-only channel API",
                              diagnostics.SKIPPED, "dependency failed"),
        ]
        parity = diagnostics.consolidation_coverage_checks(observed)
        app = by_name(parity, "test_api_direct.py - app exchange and broadcaster lookup")
        self.assertEqual(app.status, diagnostics.UNAVAILABLE)
        self.assertFalse(any(item.status == diagnostics.READY for item in parity[:1]))


if __name__ == "__main__":
    unittest.main()
