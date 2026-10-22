"""Offline startup diagnostics: AST-only loading, fake HTTP and fake children."""

import ast
from contextlib import redirect_stdout
import io
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SECRET = "STAGE-SECRET-SENTINEL-7193"
STREAM_NAMES = ("_safe_exception_class", "_ssl_exception_details", "_log_verification_stage", "_ensure_app_token", "_app_headers",
                "get_broadcaster_id", "verify_twitch_tokens")


def definitions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in names]
    if {node.name for node in nodes} != set(names):
        raise AssertionError("Expected source functions not found")
    future = ast.ImportFrom(module="__future__",
                            names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


class Response:
    def __init__(self, status=200, body=None, error=None):
        self.status_code = status
        self.json = Mock(return_value=body, side_effect=error)
        self.text = SECRET
        self.headers = {"Authorization": SECRET}
        self.url = "https://invalid.example/?token=" + SECRET

    def __repr__(self):
        raise AssertionError("Response objects must never be formatted")


def stream_fixture(debug=False):
    output = io.StringIO()
    logger = logging.Logger("isolated-stage-test", level=logging.DEBUG)
    logger.addHandler(logging.StreamHandler(output))
    validate = Response(body={"scopes": ["channel:manage:broadcast", SECRET],
                              "login": SECRET, "client_id": SECRET})
    app = Response(body={"access_token": SECRET, "expires_in": 3600, "extra": SECRET})
    channel = Response(body={"data": [{"id": SECRET, "login": SECRET}]})
    requests = SimpleNamespace(get=Mock(side_effect=[validate, channel]),
                               post=Mock(return_value=app))
    namespace = definitions(ROOT / "MedlarTV/core/stream_management.py", STREAM_NAMES, {
        "DEBUG": debug, "log": logger, "requests": requests,
        "time": SimpleNamespace(time=lambda: 100),
        "APP_CLIENT_ID": SECRET, "APP_CLIENT_SECRET": SECRET,
        "BROADCASTER_TOKEN": SECRET, "CHANNEL_NAME": SECRET,
        "TWITCH_AUTH_BASE": "https://invalid.example/auth",
        "TWITCH_API_BASE": "https://invalid.example/api",
        "_app_token": None, "_app_token_expiry": 0, "_broadcaster_id": None,
    })
    return namespace, output, validate, app, channel


class VerificationStageTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.output, self.validate, self.app, self.channel = stream_fixture()

    def tearDown(self):
        self.assertNotIn(SECRET, self.output.getvalue())

    def verify(self):
        with redirect_stdout(self.output):
            return self.ns["verify_twitch_tokens"]()

    def stage(self, name, outcome, status=None, exception=None):
        expected = f"[TwitchVerify] stage={name} outcome={outcome}"
        if status is not None:
            expected += f" http_status={status}"
        if exception is not None:
            expected += f" exception={exception}"
        self.assertIn(expected + "\n", self.output.getvalue())

    def test_success_emits_all_stages_without_values_or_return_objects(self):
        self.assertIs(self.verify(), True)
        for name in ("broadcaster_validation", "broadcaster_scope", "app_token", "channel_lookup"):
            self.stage(name, "ok", 200)
        self.stage("complete", "ok")
        self.assertEqual(self.ns["requests"].get.call_count, 2)
        self.assertEqual(self.ns["requests"].post.call_count, 1)
        self.assertEqual(self.ns["requests"].get.call_args_list[0].kwargs["timeout"], 15)
        self.assertEqual(self.ns["requests"].post.call_args.kwargs["timeout"], 15)
        self.assertEqual(self.ns["_app_token"], SECRET)
        self.assertEqual(self.ns["_broadcaster_id"], SECRET)

    def test_broadcaster_early_failure_keeps_short_circuit(self):
        self.ns["BROADCASTER_TOKEN"] = ""
        self.assertIs(self.verify(), False)
        self.stage("broadcaster_validation", "failed")
        self.stage("complete", "failed")
        self.ns["requests"].get.assert_not_called()
        self.ns["requests"].post.assert_not_called()

    def test_validation_http_failure_still_checks_app_and_channel(self):
        self.validate.status_code = 401
        self.assertIs(self.verify(), False)
        self.stage("broadcaster_validation", "failed", 401)
        self.stage("app_token", "ok", 200)
        self.stage("channel_lookup", "ok", 200)
        self.stage("complete", "failed")
        self.validate.json.assert_not_called()

    def test_validation_exception_records_only_class_and_returns_false(self):
        self.ns["requests"].get.side_effect = TimeoutError(SECRET)
        self.assertIs(self.verify(), False)
        self.stage("broadcaster_validation", "failed", exception="TimeoutError")
        self.stage("complete", "failed")
        self.ns["requests"].post.assert_not_called()

    def test_scope_failure_keeps_existing_criterion_and_app_checks(self):
        self.validate.json.return_value = {"scopes": [SECRET, "chat:read"]}
        self.assertIs(self.verify(), False)
        self.stage("broadcaster_scope", "failed", 200)
        self.stage("channel_lookup", "ok", 200)
        self.stage("complete", "failed")

    def test_scope_parse_exception_is_logged_and_reraised_unchanged(self):
        error = ValueError(SECRET)
        self.validate.json.side_effect = error
        with self.assertRaises(ValueError) as caught:
            self.verify()
        self.assertIs(caught.exception, error)
        self.stage("broadcaster_scope", "failed", 200, "ValueError")
        self.ns["requests"].post.assert_not_called()

    def test_scope_malformed_shapes_still_raise_existing_exception_classes(self):
        for body, error_type in ((None, AttributeError), ({"scopes": None}, TypeError)):
            with self.subTest(error_type=error_type):
                ns, output, validate, _, _ = stream_fixture()
                validate.json.return_value = body
                with redirect_stdout(output), self.assertRaises(error_type):
                    ns["verify_twitch_tokens"]()
                self.assertIn("stage=broadcaster_scope outcome=failed http_status=200 "
                              f"exception={error_type.__name__}", output.getvalue())
                self.assertNotIn(SECRET, output.getvalue())

    def test_app_configuration_failure_keeps_no_post_and_false_result(self):
        for key in ("APP_CLIENT_ID", "APP_CLIENT_SECRET"):
            with self.subTest(key=key):
                self.ns[key] = ""
                self.assertIs(self.verify(), False)
                self.stage("app_token", "failed")
                self.stage("channel_lookup", "failed")
                self.ns["requests"].post.assert_not_called()
                self.ns[key] = SECRET
                self.ns["requests"].get.side_effect = [self.validate, self.channel]

    def test_app_http_failure_preserves_result_without_parsing_body(self):
        self.app.status_code = 403
        self.assertIs(self.verify(), False)
        self.stage("app_token", "failed", 403)
        self.stage("channel_lookup", "failed")
        self.app.json.assert_not_called()

    def test_app_request_exception_preserves_false_result(self):
        self.ns["requests"].post.side_effect = ConnectionError(SECRET)
        self.assertIs(self.verify(), False)
        self.stage("app_token", "failed", exception="ConnectionError")
        self.stage("complete", "failed")

    def test_app_parse_exception_preserves_identity_and_partial_state(self):
        error = ValueError(SECRET)
        self.app.json.side_effect = error
        with self.assertRaises(ValueError) as caught:
            self.verify()
        self.assertIs(caught.exception, error)
        self.stage("app_token", "failed", 200, "ValueError")
        self.assertIsNone(self.ns["_app_token"])

    def test_app_missing_result_and_malformed_expiry_keep_existing_behavior(self):
        # B6 validates the entire response before publishing either cache field.
        # Startup diagnostics must retain that fail-closed contract and stage.
        for body in ({"expires_in": 3600, "extra": SECRET},
                     {"access_token": SECRET, "expires_in": SECRET}):
            with self.subTest(body_fields=tuple(body)):
                ns, output, _, app, channel = stream_fixture()
                ns["_app_token"], ns["_app_token_expiry"] = "old-synthetic", 99
                app.json.return_value = body
                with redirect_stdout(output), self.assertRaisesRegex(ValueError, "Invalid app-token response fields"):
                    ns["verify_twitch_tokens"]()
                self.assertEqual((ns["_app_token"], ns["_app_token_expiry"]), ("old-synthetic", 99))
                self.assertIn("stage=app_token outcome=failed http_status=200 exception=ValueError\n", output.getvalue())
                ns["requests"].post.assert_called_once()
                self.assertEqual(ns["requests"].get.call_count, 1)
                channel.json.assert_not_called()
                self.assertNotIn(SECRET, output.getvalue())

    def test_channel_http_failure_preserves_false_result(self):
        self.channel.status_code = 503
        self.assertIs(self.verify(), False)
        self.stage("channel_lookup", "failed", 503)
        self.channel.json.assert_not_called()

    def test_channel_request_exception_preserves_false_result(self):
        self.ns["requests"].get.side_effect = [self.validate, ConnectionError(SECRET)]
        self.assertIs(self.verify(), False)
        self.stage("channel_lookup", "failed", exception="ConnectionError")

    def test_channel_empty_result_preserves_false_result(self):
        self.channel.json.return_value = {"data": [], "extra": SECRET}
        self.assertIs(self.verify(), False)
        self.stage("channel_lookup", "failed", 200)

    def test_channel_parse_and_shape_exceptions_are_reraised(self):
        for body, error, expected in ((None, ValueError(SECRET), ValueError),
                                      (None, None, AttributeError),
                                      ({"data": [{}]}, None, ValueError),
                                      ({"data": [{"id": ""}]}, None, ValueError),
                                      ({"data": [{"id": 1}]}, None, ValueError)):
            with self.subTest(expected=expected):
                ns, output, _, _, channel = stream_fixture()
                channel.json.return_value = body
                channel.json.side_effect = error
                with redirect_stdout(output), self.assertRaises(expected) as caught:
                    ns["verify_twitch_tokens"]()
                if error is not None:
                    self.assertIs(caught.exception, error)
                # Invalid identities must never reach the broadcaster cache.
                self.assertIsNone(ns["_broadcaster_id"])
                self.assertEqual(ns["_app_token_expiry"], 3700)
                self.assertIn("stage=channel_lookup outcome=failed http_status=200 "
                              f"exception={expected.__name__}", output.getvalue())
                self.assertNotIn(SECRET, output.getvalue())

    def test_channel_debug_shape_failure_records_only_exception_class(self):
        self.ns["DEBUG"] = True
        self.channel.json.return_value = {"data": None, "extra": SECRET}
        with self.assertRaises(TypeError):
            self.verify()
        self.stage("channel_lookup", "failed", 200, "TypeError")

    def test_cache_paths_do_not_add_http_requests(self):
        self.ns["_app_token"] = SECRET
        self.ns["_app_token_expiry"] = 1000
        self.assertEqual(self.ns["_ensure_app_token"](), SECRET)
        self.stage("app_token", "ok")
        self.ns["requests"].post.assert_not_called()
        self.ns["_broadcaster_id"] = SECRET
        self.assertEqual(self.ns["get_broadcaster_id"](), SECRET)
        self.stage("channel_lookup", "ok")
        self.ns["requests"].get.assert_not_called()

    def test_formatter_rejects_nonfixed_stage_outcome_and_nonnumeric_status(self):
        emit = self.ns["_log_verification_stage"]
        emit(SECRET, "failed")
        emit("app_token", SECRET)
        self.assertEqual(self.output.getvalue(), "")
        emit("app_token", "failed", status=SECRET)
        emit("app_token", "failed", status=True)
        self.assertEqual(self.output.getvalue(),
                         "[TwitchVerify] stage=app_token outcome=failed\n" * 2)

    def test_debug_mode_keeps_secret_values_out_of_output(self):
        self.ns["DEBUG"] = True
        self.assertIs(self.verify(), True)
        self.stage("complete", "ok")


class ConsoleStageTests(unittest.TestCase):
    def fixture(self, flag=None, bridge=True, result=True, error=None):
        env = {} if flag is None else {"ENABLE_AVATAR_CONSOLE": flag}
        env["UNRELATED_SECRET"] = SECRET
        output = io.StringIO()
        events = []

        def start(name, *args, **kwargs):
            events.append(name)
            if name == "Avatar Console Client":
                if error is not None:
                    raise error
                return result
            return True

        class PythonPath:
            def exists(self): return True
            def __str__(self): return "mock-python"

        ns = definitions(ROOT / "launcher.py", ("_run_services",), {
            "os": SimpleNamespace(environ=env, getenv=lambda key, default=None: env.get(key, default)),
            "VENV_PYTHON": PythonPath(), "StartupError": RuntimeError,
            "Colors": SimpleNamespace(red="", green="", yellow="", cyan="", reset=""),
            "_stop_requested": Mock(return_value=False), "_load_env_file": Mock(),
            "_load_saved_connections": Mock(), "clear_logs": Mock(),
            "clear_old_sessions": Mock(), "banner": Mock(),
            "ollama_api_ready": Mock(return_value=True),
            "libretranslate_home": Mock(return_value="mock-translate"),
            "start_libretranslate": Mock(), "start_avatar_bridge": Mock(return_value=bridge),
            "start_process": Mock(side_effect=start), "_monitor_processes": Mock(),
        })
        return ns, output, events

    def test_absent_off_and_other_values_skip_optional_console(self):
        for flag in (None, "0", "false", "true", " 1", SECRET):
            with self.subTest(flag=flag):
                ns, output, events = self.fixture(flag=flag)
                with redirect_stdout(output): ns["_run_services"](mode="twitch")
                self.assertIn("[AvatarConsole] enabled=False\n", output.getvalue())
                self.assertIn("[AvatarConsole] launch=skipped_disabled\n", output.getvalue())
                self.assertNotIn("Avatar Console Client", events)
                self.assertNotIn(SECRET, output.getvalue())
                ns["_monitor_processes"].assert_called_once()

    def test_enabled_console_with_missing_bridge_is_informational_skip(self):
        ns, output, events = self.fixture(flag="1", bridge=False)
        with redirect_stdout(output): ns["_run_services"](mode="twitch")
        self.assertIn("[AvatarConsole] enabled=True\n", output.getvalue())
        self.assertIn("[AvatarConsole] launch=skipped_bridge\n", output.getvalue())
        self.assertEqual(events, ["Medlar Core"])
        self.assertNotIn(SECRET, output.getvalue())

    def test_enabled_attempt_records_success_or_failure_without_changing_monitoring(self):
        for result, label in ((True, "started"), (False, "failed")):
            with self.subTest(result=result):
                ns, output, events = self.fixture(flag="1", result=result)
                with redirect_stdout(output): ns["_run_services"](mode="twitch")
                self.assertIn("[AvatarConsole] launch=attempt\n", output.getvalue())
                self.assertIn(f"[AvatarConsole] launch={label}\n", output.getvalue())
                self.assertEqual(events, ["Medlar Core", "Avatar Console Client"])
                self.assertEqual(ns["start_process"].call_args.kwargs, {"use_venv": True, "wait": 2})
                ns["_monitor_processes"].assert_called_once()
                self.assertNotIn(SECRET, output.getvalue())

    def test_console_exception_logs_fixed_error_and_reraises_same_exception(self):
        error = RuntimeError(SECRET)
        ns, output, _ = self.fixture(flag="1", error=error)
        with redirect_stdout(output), self.assertRaises(RuntimeError) as caught:
            ns["_run_services"](mode="twitch")
        self.assertIs(caught.exception, error)
        self.assertIn("[AvatarConsole] launch=error\n", output.getvalue())
        self.assertNotIn(SECRET, output.getvalue())
        ns["_monitor_processes"].assert_not_called()

    def test_requested_stop_prevents_optional_attempt_as_before(self):
        ns, output, events = self.fixture(flag="1")
        ns["_stop_requested"].side_effect = lambda: "Medlar Core" in events
        with redirect_stdout(output): ns["_run_services"](mode="twitch")
        self.assertEqual(events, ["Medlar Core"])
        self.assertNotIn("[AvatarConsole]", output.getvalue())
        ns["_monitor_processes"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
