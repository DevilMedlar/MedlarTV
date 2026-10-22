"""Offline checks for the optional LibreTranslate launcher path."""

import io
import os
import subprocess
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import mock_open, patch

import launcher


class LibreTranslateLauncherTests(unittest.TestCase):
    service_dir = Path("C:/LibreTranslate")

    def setUp(self):
        # Translation launcher checks must never probe or spawn local services.
        for context in (
            patch.dict(os.environ, {
                "LIBRETRANSLATE_HOME": "",
                "LIBRETRANSLATE_PYTHON": "",
                "LIBRETRANSLATE_URL": "http://127.0.0.1:5000/translate",
                "LIBRETRANSLATE_LANGUAGES": "",
            }),
            patch.object(launcher.socket, "create_connection", side_effect=ConnectionRefusedError),
            patch.object(launcher, "_child_environment", return_value={}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def test_home_prefers_repo_install_then_legacy_install(self):
        local = launcher.ROOT / ".libretranslate"
        legacy = Path("C:/LibreTranslate")
        for existing, expected in (
            (set(), local),
            ({legacy}, legacy),
            ({local, legacy}, local),
        ):
            with self.subTest(existing=existing), \
                 patch.object(Path, "is_dir", lambda path: path in existing):
                self.assertEqual(launcher.libretranslate_home(), expected)

    def test_home_override_is_honored(self):
        configured = Path("C:/Custom Translation")
        with patch.dict(os.environ, {"LIBRETRANSLATE_HOME": str(configured)}), \
             patch.object(Path, "is_dir", return_value=False):
            self.assertEqual(launcher.libretranslate_home(), configured)

    def test_local_target_uses_configured_port_and_rejects_remote_url(self):
        for url, expected in (
            ("http://localhost:5061/translate", ("127.0.0.1", 5061)),
            ("http://127.0.0.1:5061/translate/", ("127.0.0.1", 5061)),
            ("https://localhost:5061/translate", None),
            ("http://example.com:5061/translate", None),
            ("http://localhost:5061/other", None),
            ("http://localhost:bad/translate", None),
        ):
            with self.subTest(url=url), patch.dict(os.environ, {"LIBRETRANSLATE_URL": url}):
                self.assertEqual(launcher._local_libretranslate_target(), expected)

    def test_languages_readiness_requires_nonempty_json_list(self):
        for payload, status, expected in (
            (b'[{"code":"en"}]', 200, True),
            (b'[]', 200, False),
            (b'{"message":"busy"}', 200, False),
            (b'[1]', 200, False),
            (b'[{"code":"en"}]', 503, False),
            (b'not json', 200, False),
        ):
            response = io.BytesIO(payload)
            response.status = status
            with self.subTest(payload=payload, status=status), \
                 patch.object(launcher, "urlopen", return_value=response) as open_url:
                self.assertIs(launcher._libretranslate_ready("127.0.0.1", 5061), expected)
                open_url.assert_called_once_with("http://127.0.0.1:5061/languages", timeout=1)

        with patch.object(launcher, "urlopen", side_effect=OSError("offline")):
            self.assertFalse(launcher._libretranslate_ready("127.0.0.1", 5061))

    def test_prefers_service_venv_over_path_python(self):
        python = self.service_dir / ".venv" / "Scripts" / "python.exe"
        with patch.dict(os.environ, {"LIBRETRANSLATE_PYTHON": ""}), \
             patch.object(Path, "is_file", lambda path: path == python), \
             patch.object(launcher.shutil, "which", return_value="system-python"):
            selected, error = launcher.libretranslate_python(self.service_dir)
        self.assertEqual(selected, str(python))
        self.assertIsNone(error)

    def test_explicit_interpreter_override(self):
        python = Path("C:/Custom Python/python.exe")
        with patch.dict(os.environ, {"LIBRETRANSLATE_PYTHON": str(python)}), \
             patch.object(Path, "is_file", lambda path: path == python):
            selected, error = launcher.libretranslate_python(self.service_dir)
        self.assertEqual(selected, str(python))
        self.assertIsNone(error)

    def test_invalid_override_does_not_silently_fall_back(self):
        with patch.dict(os.environ, {"LIBRETRANSLATE_PYTHON": "missing-python"}), \
             patch.object(Path, "is_file", return_value=False), \
             patch.object(launcher.shutil, "which", return_value="system-python"):
            selected, error = launcher.libretranslate_python(self.service_dir)
        self.assertIsNone(selected)
        self.assertIn("does not exist", error)

    def test_legacy_system_python_fallback(self):
        with patch.dict(os.environ, {"LIBRETRANSLATE_PYTHON": ""}), \
             patch.object(Path, "is_file", return_value=False), \
             patch.object(launcher.shutil, "which", return_value="system-python"):
            selected, error = launcher.libretranslate_python(self.service_dir)
        self.assertEqual(selected, "system-python")
        self.assertIsNone(error)

    def test_missing_package_skips_service_with_install_hint(self):
        output = io.StringIO()
        with patch.object(Path, "is_dir", return_value=True), \
             patch.object(launcher, "libretranslate_python", return_value=("chosen-python", None)), \
             patch.object(launcher.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "python")), \
             patch.object(launcher, "start_process") as start, redirect_stdout(output):
            launcher.start_libretranslate(self.service_dir)
        start.assert_not_called()
        self.assertIn("requirements-libretranslate.txt", output.getvalue())

    def test_valid_install_starts_selected_interpreter(self):
        with patch.dict(os.environ, {
                 "LIBRETRANSLATE_URL": "http://localhost:5061/translate",
                 "LIBRETRANSLATE_LANGUAGES": "en,es",
             }), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(launcher, "libretranslate_python", return_value=("chosen-python", None)), \
             patch.object(launcher.subprocess, "run") as check, \
             patch.object(launcher, "_libretranslate_ready", return_value=True) as ready, \
             patch.object(launcher, "start_process", return_value=True) as start:
            launcher.start_libretranslate(self.service_dir)
        self.assertEqual(check.call_args.args[0][0], "chosen-python")
        start.assert_called_once_with(
            "LibreTranslate Server", ["chosen-python", "-m", "libretranslate.main",
                                      "--host", "127.0.0.1", "--port", "5061",
                                      "--load-only", "en,es"],
            cwd=self.service_dir, wait=5, required=False,
        )
        ready.assert_called_once_with("127.0.0.1", 5061)

    def test_existing_ready_endpoint_is_not_adopted_or_stopped(self):
        owned = []
        output = io.StringIO()
        with patch.object(launcher.socket, "create_connection", return_value=io.BytesIO()), \
             patch.object(launcher, "processes", owned), \
             patch.object(launcher, "_libretranslate_ready", return_value=True) as ready, \
             patch.object(launcher, "start_process") as start, redirect_stdout(output):
            launcher.start_libretranslate(self.service_dir)

        ready.assert_called_once_with("127.0.0.1", 5000)
        start.assert_not_called()
        self.assertEqual(owned, [])
        self.assertIn("already available", output.getvalue())

    def test_occupied_non_translation_port_is_not_adopted(self):
        output = io.StringIO()
        with patch.object(launcher.socket, "create_connection", return_value=io.BytesIO()), \
             patch.object(launcher, "_libretranslate_ready", return_value=False), \
             patch.object(launcher, "start_process") as start, redirect_stdout(output):
            launcher.start_libretranslate(self.service_dir)

        start.assert_not_called()
        self.assertIn("occupied by another service", output.getvalue())

    def test_remote_endpoint_skips_local_service_start(self):
        with patch.dict(os.environ, {"LIBRETRANSLATE_URL": "https://example.com/translate"}), \
             patch.object(launcher, "start_process") as start:
            launcher.start_libretranslate(self.service_dir)
        start.assert_not_called()

    def test_missing_directory_skips_service(self):
        with patch.object(Path, "is_dir", return_value=False), \
             patch.object(launcher, "start_process") as start:
            launcher.start_libretranslate(self.service_dir)
        start.assert_not_called()

    def test_argument_list_launch_does_not_use_shell(self):
        fake_process = unittest.mock.Mock(pid=123)
        fake_process.poll.return_value = 0
        with patch("builtins.open", mock_open()), \
             patch.object(launcher, "_child_environment", return_value={}), \
             patch.object(launcher.platform, "system", return_value="Linux"), \
             patch.object(launcher.subprocess, "Popen", return_value=fake_process) as popen, \
             patch.object(launcher.time, "sleep"):
            launcher.start_process("LibreTranslate Server", ["python", "-m", "libretranslate.main"])
        self.assertIs(popen.call_args.kwargs["shell"], False)


class LauncherLifecycleTests(unittest.TestCase):
    def setUp(self):
        # These process tests use synthetic environments and never import
        # application configuration through the launcher's env helper.
        child_env = patch.object(launcher, "_child_environment", return_value={})
        child_env.start()
        self.addCleanup(child_env.stop)

    def test_occupied_ollama_endpoint_is_not_adopted_or_killed(self):
        with patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_URL": ""}, clear=True), \
             patch.object(launcher, "_ollama_endpoint_open", return_value=True), \
             patch.object(launcher, "start_process") as start:
            with self.assertRaises(launcher.StartupError):
                launcher.start_ollama()
        start.assert_not_called()

    def test_owned_process_is_registered_before_startup_wait(self):
        fake_process = unittest.mock.Mock(pid=43210)
        fake_process.poll.return_value = None
        owned = []

        def check_during_wait(_seconds):
            self.assertTrue(any(entry["proc"] is fake_process for entry in owned))

        with patch.object(launcher, "processes", owned), \
             patch("builtins.open", mock_open()), \
             patch.object(launcher.platform, "system", return_value="Linux"), \
             patch.object(launcher.subprocess, "Popen", return_value=fake_process) as popen, \
             patch.object(launcher.time, "sleep", side_effect=check_during_wait):
            launcher.start_process("Ollama Server", ["ollama", "serve"], wait=3)

        self.assertEqual(popen.call_args.args[0], ["ollama", "serve"])
        self.assertIs(popen.call_args.kwargs["shell"], False)
        self.assertEqual(len(owned), 1)

    def test_stop_all_reports_offline_only_after_owned_child_exits(self):
        class GracefulProcess:
            pid = 43210

            def __init__(self):
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.alive = False

            def wait(self, timeout=None):
                if self.alive:
                    raise subprocess.TimeoutExpired("fake child", timeout)
                return 0

        child = GracefulProcess()
        log = io.StringIO()
        owned = [{"proc": child, "name": "Medlar Core", "log": log, "required": False}]
        output = io.StringIO()

        def stop_owned(_process, **_kwargs):
            child.alive = False
            return True

        with patch.object(launcher, "processes", owned), \
             patch.object(launcher, "_stop_owned_process", side_effect=stop_owned), \
             redirect_stdout(output):
            result = launcher.stop_all()

        self.assertIs(result, True)
        self.assertFalse(child.alive)
        self.assertTrue(log.closed)
        self.assertIn("offline", output.getvalue().lower())

    def test_stop_all_does_not_claim_offline_if_owned_child_survives(self):
        class StuckProcess:
            pid = 43211

            def poll(self):
                return None

            def terminate(self):
                raise OSError("refused")

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("fake child", timeout)

            def kill(self):
                raise OSError("refused")

        owned = [{"proc": StuckProcess(), "name": "Medlar Core", "log": io.StringIO(), "required": False}]
        output = io.StringIO()
        with patch.object(launcher, "processes", owned), \
             patch.object(launcher, "_stop_owned_process", return_value=False), \
             redirect_stdout(output):
            result = launcher.stop_all()

        self.assertIs(result, False)
        self.assertNotIn("All systems offline", output.getvalue())

    def test_stop_all_verifies_windows_job_has_only_launcher_left(self):
        for active, expected in ((1, True), (2, False)):
            with self.subTest(active_processes=active):
                output = io.StringIO()
                with patch.object(launcher, "processes", []), \
                     patch.object(launcher, "_windows_job_handle", object()), \
                     patch.object(launcher, "_owned_job_active_processes", return_value=active), \
                     patch.object(launcher.time, "monotonic", side_effect=[0, 21]), \
                     redirect_stdout(output):
                    result = launcher.stop_all()

                self.assertIs(result, expected)
                if not expected:
                    self.assertIn("2 owned job processes remain active", output.getvalue())
                    self.assertNotIn("All owned systems offline", output.getvalue())

    def test_windows_fallback_targets_only_owned_pid_tree(self):
        class OwnedProcess:
            pid = 43212

            def __init__(self):
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def wait(self, timeout=None):
                return 0 if not self.alive else None

        child = OwnedProcess()

        def fake_taskkill(_args, **_kwargs):
            child.alive = False

        with patch.object(launcher.platform, "system", return_value="Windows"), \
             patch.object(launcher, "processes", [{"proc": child, "pid": child.pid, "console_group": True}]), \
             patch.object(launcher, "time", unittest.mock.Mock(monotonic=unittest.mock.Mock(side_effect=[0, 16, 16, 16]), sleep=unittest.mock.Mock())), \
             patch.object(launcher.subprocess, "run", side_effect=fake_taskkill) as run:
            stopped = launcher._stop_owned_process(child)

        self.assertTrue(stopped)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "43212", "/T", "/F"])

    def test_startup_interrupt_still_runs_shutdown(self):
        fake_process = unittest.mock.Mock(pid=43213)
        fake_process.poll.return_value = None
        owned = []

        def checked_shutdown():
            self.assertTrue(any(entry["proc"] is fake_process for entry in owned))
            return True

        with patch.object(launcher, "processes", owned), \
             patch.object(launcher, "_protect_owned_processes", create=True), \
             patch.object(launcher, "clear_logs"), \
             patch.object(launcher, "_load_env_file"), \
             patch.object(launcher, "_load_saved_connections", create=True), \
             patch.object(launcher, "clear_old_sessions"), \
             patch.object(launcher, "banner"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(launcher, "_ollama_endpoint_open", return_value=False), \
             patch("builtins.open", mock_open()), \
             patch.object(launcher.subprocess, "Popen", return_value=fake_process), \
             patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(launcher, "stop_all", side_effect=checked_shutdown) as stop:
            result = launcher.main()

        self.assertEqual(result, 0)
        stop.assert_called_once()

    def test_heartbeat_interrupt_runs_shutdown(self):
        fake_socket = unittest.mock.Mock()
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(launcher, "_protect_owned_processes", create=True), \
             patch.object(launcher, "clear_logs"), \
             patch.object(launcher, "_load_env_file"), \
             patch.object(launcher, "_load_saved_connections", create=True), \
             patch.object(launcher, "clear_old_sessions"), \
             patch.object(launcher, "banner"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(launcher, "start_ollama", return_value=True), \
             patch.object(launcher, "start_libretranslate"), \
             patch.object(launcher, "start_process"), \
             patch.object(launcher, "_stop_requested", return_value=False), \
             patch.object(launcher.socket, "socket", return_value=fake_socket), \
             patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt) as sleep, \
             patch.object(launcher, "stop_all", return_value=True) as stop:
            result = launcher.main()

        self.assertEqual(result, 0)
        sleep.assert_called_once_with(1)
        stop.assert_called_once()

    def test_required_core_start_failure_cleans_up_owned_ollama(self):
        owned = []
        fake_socket = unittest.mock.Mock()
        fake_ollama = unittest.mock.Mock(pid=43214)

        def start_owned_ollama():
            owned.append({"proc": fake_ollama, "name": "Ollama Server", "log": io.StringIO(), "required": False})
            return True

        def start_service(name, *_args, **_kwargs):
            return name != "Medlar Core"

        def checked_shutdown():
            self.assertTrue(any(entry["proc"] is fake_ollama for entry in owned))
            return True

        with patch.dict(os.environ, {}, clear=False), \
             patch.object(launcher, "processes", owned), \
             patch.object(launcher, "_protect_owned_processes", create=True), \
             patch.object(launcher, "clear_logs"), \
             patch.object(launcher, "_load_env_file"), \
             patch.object(launcher, "_load_saved_connections", create=True), \
             patch.object(launcher, "clear_old_sessions"), \
             patch.object(launcher, "banner"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(launcher, "start_ollama", side_effect=start_owned_ollama), \
             patch.object(launcher, "start_libretranslate"), \
             patch.object(launcher, "start_process", side_effect=start_service), \
             patch.object(launcher, "_stop_requested", return_value=False), \
             patch.object(launcher.socket, "socket", return_value=fake_socket), \
             patch.object(launcher.time, "sleep", side_effect=AssertionError("heartbeat reached after Core failed")) as sleep, \
             patch.object(launcher, "stop_all", side_effect=checked_shutdown) as stop:
            result = launcher.main()

        self.assertEqual(result, 1)
        sleep.assert_not_called()
        stop.assert_called_once()

    def test_failed_ollama_spawn_aborts_startup_and_cleans_owned_handle(self):
        owned = []
        fake_ollama = unittest.mock.Mock(pid=43215)
        fake_ollama.poll.return_value = 1

        def checked_shutdown():
            self.assertTrue(any(entry["proc"] is fake_ollama for entry in owned))
            return True

        def startup_sleep(seconds):
            if seconds == 1:
                raise AssertionError("heartbeat reached after Ollama failed")

        with patch.dict(os.environ, {}, clear=False), \
             patch.object(launcher, "processes", owned), \
             patch.object(launcher, "_protect_owned_processes", create=True), \
             patch.object(launcher, "clear_logs"), \
             patch.object(launcher, "_load_env_file"), \
             patch.object(launcher, "_load_saved_connections", create=True), \
             patch.object(launcher, "clear_old_sessions"), \
             patch.object(launcher, "banner"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(launcher, "_ollama_endpoint_open", return_value=False), \
             patch("builtins.open", mock_open()), \
             patch.object(launcher.subprocess, "Popen", return_value=fake_ollama), \
             patch.object(launcher.time, "sleep", side_effect=startup_sleep), \
             patch.object(launcher, "start_libretranslate") as libre, \
             patch.object(launcher, "stop_all", side_effect=checked_shutdown) as stop:
            result = launcher.main()

        self.assertEqual(result, 1)
        libre.assert_not_called()
        stop.assert_called_once()


class LauncherModeTests(unittest.TestCase):
    """Mode checks never launch a real service or inspect local credentials."""

    @staticmethod
    def _mock_bootstrap(stack):
        calls = {}
        for name in (
            "clear_logs", "_load_env_file", "_load_saved_connections",
            "clear_old_sessions", "banner",
        ):
            calls[name] = stack.enter_context(patch.object(launcher, name))
        stack.enter_context(patch.object(Path, "exists", return_value=True))
        stack.enter_context(patch.dict(os.environ, {"ENABLE_AVATAR_CONSOLE": "0"}))
        return calls

    def test_ollama_mode_starts_only_owned_ollama(self):
        started = False

        def start_owned_ollama():
            nonlocal started
            started = True
            return True

        with ExitStack() as stack:
            bootstrap = self._mock_bootstrap(stack)
            start = stack.enter_context(patch.object(
                launcher, "start_ollama", side_effect=start_owned_ollama,
            ))
            core = stack.enter_context(patch.object(launcher, "start_process"))
            translate = stack.enter_context(patch.object(launcher, "start_libretranslate"))
            stack.enter_context(patch.object(launcher, "_stop_requested", side_effect=lambda: started))
            stack.enter_context(patch.object(launcher, "_ollama_endpoint_open", return_value=False))
            launcher._run_services(mode="ollama")

        start.assert_called_once()
        core.assert_not_called()
        translate.assert_not_called()
        bootstrap["_load_env_file"].assert_not_called()
        bootstrap["_load_saved_connections"].assert_not_called()

    def test_twitch_mode_requires_an_existing_ollama_endpoint(self):
        with ExitStack() as stack:
            self._mock_bootstrap(stack)
            stack.enter_context(patch.object(launcher, "ollama_api_ready", return_value=False))
            stack.enter_context(patch.object(launcher, "_ollama_endpoint_open", return_value=True))
            start = stack.enter_context(patch.object(launcher, "start_ollama"))
            core = stack.enter_context(patch.object(launcher, "start_process"))
            translate = stack.enter_context(patch.object(launcher, "start_libretranslate"))
            with self.assertRaises(launcher.StartupError):
                launcher._run_services(mode="twitch")

        start.assert_not_called()
        core.assert_not_called()
        translate.assert_not_called()

    def test_twitch_mode_starts_core_without_owning_ollama(self):
        core_started = False

        def start_service(name, *_args, **_kwargs):
            nonlocal core_started
            if name == "Medlar Core":
                core_started = True
            return True

        with ExitStack() as stack:
            self._mock_bootstrap(stack)
            api = stack.enter_context(patch.object(launcher, "ollama_api_ready", return_value=True))
            stack.enter_context(patch.object(launcher, "_stop_requested", side_effect=lambda: core_started))
            stack.enter_context(patch.object(launcher.socket, "socket", return_value=unittest.mock.Mock()))
            start = stack.enter_context(patch.object(launcher, "start_ollama"))
            stack.enter_context(patch.object(launcher, "start_libretranslate"))
            services = stack.enter_context(patch.object(
                launcher, "start_process", side_effect=start_service,
            ))
            launcher._run_services(mode="twitch")

        start.assert_not_called()
        api.assert_called_once_with()
        self.assertTrue(core_started)
        self.assertIn("Medlar Core", [call.args[0] for call in services.call_args_list])

    def test_twitch_mode_keeps_the_gui_endpoint_after_legacy_env_load(self):
        inherited = {"OLLAMA_URL": "http://localhost:11434", "OLLAMA_HOST": "0.0.0.0:11434"}
        observed = []

        def legacy_env_load():
            os.environ["OLLAMA_URL"] = "http://other.example:12345"
            os.environ["OLLAMA_HOST"] = "other.example:12345"

        def check_api():
            observed.append((os.environ.get("OLLAMA_URL"), os.environ.get("OLLAMA_HOST")))
            return False

        with patch.dict(os.environ, inherited, clear=True), \
             patch.object(launcher, "_stop_requested", return_value=False), \
             patch.object(launcher, "_load_env_file", side_effect=legacy_env_load), \
             patch.object(launcher, "_load_saved_connections"), \
             patch.object(launcher, "clear_old_sessions"), \
             patch.object(launcher, "banner"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(launcher, "ollama_api_ready", side_effect=check_api):
            with self.assertRaises(launcher.StartupError):
                launcher._run_services(mode="twitch")
            self.assertEqual(os.environ["OLLAMA_URL"], inherited["OLLAMA_URL"])
            self.assertEqual(os.environ["OLLAMA_HOST"], inherited["OLLAMA_HOST"])

        self.assertEqual(observed, [(inherited["OLLAMA_URL"], inherited["OLLAMA_HOST"])])

    def test_twitch_mode_does_not_adopt_endpoint_added_by_legacy_env(self):
        def legacy_env_load():
            os.environ["OLLAMA_URL"] = "http://other.example:12345"
            os.environ["OLLAMA_HOST"] = "other.example:12345"

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(launcher, "_stop_requested", return_value=False), \
             patch.object(launcher, "_load_env_file", side_effect=legacy_env_load), \
             patch.object(launcher, "_load_saved_connections"), \
             patch.object(launcher, "clear_old_sessions"), \
             patch.object(launcher, "banner"), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(launcher, "ollama_api_ready", return_value=False):
            with self.assertRaises(launcher.StartupError):
                launcher._run_services(mode="twitch")
            self.assertNotIn("OLLAMA_URL", os.environ)
            self.assertNotIn("OLLAMA_HOST", os.environ)

    def test_owned_ollama_requires_client_and_bind_target_to_match(self):
        with patch.dict(os.environ, {"OLLAMA_HOST": "0.0.0.0:11434",
                                          "OLLAMA_URL": "http://localhost:11434"}, clear=True):
            launcher._validate_owned_ollama_endpoint()

        with patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:11434",
                                          "OLLAMA_URL": "http://other.example:11434"}, clear=True):
            with self.assertRaisesRegex(launcher.StartupError, "OLLAMA_URL"):
                launcher._validate_owned_ollama_endpoint()

    def test_bind_probe_still_checks_tcp_occupancy_not_http_api(self):
        fake_socket = unittest.mock.MagicMock()
        with patch.dict(os.environ, {"OLLAMA_HOST": "0.0.0.0:11434",
                                          "OLLAMA_URL": "http://localhost:11434"}, clear=True), \
             patch.object(launcher.socket, "create_connection", return_value=fake_socket) as connect, \
             patch.object(launcher, "ollama_api_ready") as api:
            self.assertTrue(launcher._ollama_endpoint_open())
        connect.assert_called_once_with(("127.0.0.1", 11434), timeout=0.2)
        api.assert_not_called()

    def test_all_mode_starts_owned_ollama_and_core(self):
        core_started = False

        def start_service(name, *_args, **_kwargs):
            nonlocal core_started
            if name == "Medlar Core":
                core_started = True
            return True

        with ExitStack() as stack:
            self._mock_bootstrap(stack)
            stack.enter_context(patch.object(launcher, "_ollama_endpoint_open", return_value=False))
            stack.enter_context(patch.object(launcher, "_stop_requested", side_effect=lambda: core_started))
            stack.enter_context(patch.object(launcher.socket, "socket", return_value=unittest.mock.Mock()))
            start = stack.enter_context(patch.object(launcher, "start_ollama", return_value=True))
            stack.enter_context(patch.object(launcher, "start_libretranslate"))
            services = stack.enter_context(patch.object(
                launcher, "start_process", side_effect=start_service,
            ))
            launcher._run_services(mode="all")

        start.assert_called_once()
        self.assertTrue(core_started)
        self.assertIn("Medlar Core", [call.args[0] for call in services.call_args_list])

    def test_ctrl_c_cleans_owned_services_in_every_mode(self):
        for mode in ("ollama", "twitch", "all"):
            with self.subTest(mode=mode), \
                 patch.object(launcher, "_protect_owned_processes"), \
                 patch.object(launcher, "_run_services", side_effect=KeyboardInterrupt) as run, \
                 patch.object(launcher, "stop_all", return_value=True) as stop:
                result = launcher.main(mode=mode)

            self.assertEqual(result, 0)
            self.assertEqual(run.call_args.kwargs.get("mode", run.call_args.args[0] if run.call_args.args else None), mode)
            stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
