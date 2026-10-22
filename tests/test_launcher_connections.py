"""Offline checks for launcher credential precedence and child inheritance."""

import ast
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "launcher.py"
CONNECTION_KEYS = (
    "MEDLARTV_TWITCH_TOKEN",
    "MEDLARTV_TWITCH_CLIENT_ID",
    "APP_TWITCH_CLIENT_ID",
    "APP_SECRET_ID",
    "DEVILMEDLAR_TWITCH_CLIENT_ID",
    "DEVILMEDLAR_TWITCH_TOKEN",
)


def _source_functions(*names, namespace):
    tree = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"), str(LAUNCHER_PATH))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != set(names):
        raise AssertionError("Launcher connection functions were not found")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(LAUNCHER_PATH), "exec"), namespace)


def _fake_vault_module(apply):
    medlar = types.ModuleType("MedlarTV")
    medlar.__path__ = []
    core = types.ModuleType("MedlarTV.core")
    core.__path__ = []
    vault = types.ModuleType("MedlarTV.core.local_credentials")
    vault.apply_saved_credentials = apply
    vault.CREDENTIAL_FIELDS = dict.fromkeys(CONNECTION_KEYS)
    return {
        "MedlarTV": medlar,
        "MedlarTV.core": core,
        "MedlarTV.core.local_credentials": vault,
    }


class LauncherConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".env").write_text(
            "MEDLARTV_TWITCH_TOKEN=legacy-dummy-token\n"
            "APP_SECRET_ID=legacy-dummy-app-secret\n",
            encoding="utf-8",
        )
        (self.root / "fake-python.exe").write_bytes(b"")
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.env = {"OTHER": "untouched"}
        fake_os = types.SimpleNamespace(
            environ=self.env,
            getenv=lambda key, default=None: self.env.get(key, default),
        )
        self.ns = {
            "os": fake_os,
            "Path": Path,
            "ROOT": self.root,
            "LOGS_DIR": self.logs,
            "VENV_PYTHON": self.root / "fake-python.exe",
            "DEBUG": False,
            "Colors": types.SimpleNamespace(red="", green="", yellow="", cyan="", reset=""),
            "StartupError": type("StartupError", (RuntimeError,), {}),
            "print": lambda *_args, **_kwargs: None,
            "clear_logs": lambda: None,
            "clear_old_sessions": lambda _keep: None,
            "banner": lambda: None,
            # Endpoint alignment has dedicated source-isolated contracts; this
            # fixture checks synthetic credential ordering without service probes.
            "_validate_owned_ollama_endpoint": mock.Mock(),
            "start_libretranslate": lambda _path: None,
            "apply_saved_credentials": None,
            "_avatar_bootstrap_for": mock.Mock(return_value=None),
            "_windows_console_available": mock.Mock(return_value=False),
            "_python_service_command": mock.Mock(side_effect=lambda _name, command: command),
            "tempfile": types.SimpleNamespace(gettempdir=lambda: str(self.root)),
            "uuid": types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="synthetic-core")),
            "CORE_STOP_FILE_ENV": "MEDLARTV_CORE_STOP_FILE",
        }
        _source_functions("_load_env_file", "_load_saved_connections", "_child_environment",
                          "_run_services", "start_process", namespace=self.ns)

    def _vault_patch(self, apply):
        self.ns["apply_saved_credentials"] = apply
        return mock.patch.dict(sys.modules, _fake_vault_module(apply))

    def test_saved_value_precedes_first_service_start(self):
        def apply(environ=None):
            target = self.env if environ is None else environ
            target["MEDLARTV_TWITCH_TOKEN"] = "vault-dummy-token"
            target["MEDLARTV_TWITCH_CLIENT_ID"] = "vault-dummy-bot-client-id"
            return {"MEDLARTV_TWITCH_TOKEN", "MEDLARTV_TWITCH_CLIENT_ID"}

        observed = []
        def start_ollama():
            observed.append(self.env.copy())
            return True

        # First check protects startup before config work; second precedes
        # Ollama start; third ends the mocked launch after credentials apply.
        stop_checks = iter((False, False, True))
        self.ns["_stop_requested"] = lambda: next(stop_checks)
        self.ns["start_ollama"] = start_ollama
        self.ns["start_process"] = mock.Mock(side_effect=AssertionError("child must not start"))
        with self._vault_patch(apply):
            self.ns["_run_services"]()

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["MEDLARTV_TWITCH_TOKEN"], "vault-dummy-token")
        self.assertEqual(observed[0]["MEDLARTV_TWITCH_CLIENT_ID"], "vault-dummy-bot-client-id")
        self.assertEqual(observed[0]["APP_SECRET_ID"], "legacy-dummy-app-secret")
        self.assertEqual(observed[0]["OTHER"], "untouched")
        self.ns["start_process"].assert_not_called()

    def test_only_core_child_inherits_saved_connections(self):
        def apply(environ=None):
            target = self.env if environ is None else environ
            target.update({key: "vault-dummy-" + key.lower() for key in CONNECTION_KEYS})
            return set(CONNECTION_KEYS)

        self.ns["_load_env_file"]()
        with self._vault_patch(apply):
            self.ns["_load_saved_connections"]()

        captured = []
        class FakeProcess:
            pid = 12345
            def poll(self):
                return None

        def fake_popen(command, **kwargs):
            captured.append({"command": command, **kwargs})
            return FakeProcess()

        fake_subprocess = types.SimpleNamespace(
            Popen=fake_popen,
            CREATE_NEW_PROCESS_GROUP=1,
            CREATE_NO_WINDOW=2,
            STDOUT=-2,
            DEVNULL=-3,
        )
        self.ns.update({
            "subprocess": fake_subprocess,
            "platform": types.SimpleNamespace(system=lambda: "Windows"),
            "time": types.SimpleNamespace(sleep=lambda _seconds: None),
            "processes": [],
        })
        try:
            with self._vault_patch(apply):
                self.assertTrue(self.ns["start_process"]("Fake child", ["fake-python", "fake-module"], wait=0))
                self.assertTrue(self.ns["start_process"](
                    "Medlar Core", ["fake-python", "fake-core"], wait=0,
                    include_connections=True,
                ))
            self.assertEqual(len(captured), 2)
            self.assertEqual(captured[0]["command"], ["fake-python", "fake-module"])
            self.assertEqual(captured[1]["command"], ["fake-python", "fake-core"])
            for key in CONNECTION_KEYS:
                self.assertNotIn(key, captured[0]["env"])
                self.assertEqual(captured[1]["env"][key], "vault-dummy-" + key.lower())
            self.assertEqual(captured[0]["env"]["OTHER"], "untouched")
            self.assertEqual(captured[1]["env"]["OTHER"], "untouched")
            self.assertEqual(self.ns["_avatar_bootstrap_for"].call_args_list,
                             [mock.call("Fake child"), mock.call("Medlar Core")])
            self.assertEqual(self.ns["_windows_console_available"].call_count, 2)
            self.assertEqual(self.ns["_python_service_command"].call_count, 2)
            self.assertEqual(captured[1]["env"]["MEDLARTV_CORE_STOP_FILE"],
                             str(self.root / "medlar-core-synthetic-core.stop"))
            self.assertNotIn("MEDLARTV_CORE_STOP_FILE", captured[0]["env"])
            self.assertEqual([item["stdin"] for item in captured], [-3, -3])
            self.assertTrue(all(item["shell"] is False for item in captured))
            self.assertEqual(len(self.ns["processes"]), 2)
        finally:
            for entry in self.ns["processes"]:
                entry["log"].close()

    def test_run_services_explicitly_passes_connections_only_to_core(self):
        tree = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"), str(LAUNCHER_PATH))
        run_services = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                            and node.name == "_run_services")
        calls = [node for node in ast.walk(run_services) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "start_process"]
        core_calls = [node for node in calls if node.args and isinstance(node.args[0], ast.Constant)
                      and node.args[0].value == "Medlar Core"]
        self.assertEqual(len(core_calls), 1)
        for call in calls:
            opt_in = [kw.value.value for kw in call.keywords
                      if kw.arg == "include_connections" and isinstance(kw.value, ast.Constant)]
            self.assertEqual(opt_in, [True] if call is core_calls[0] else [])

    def test_libretranslate_import_probe_receives_sanitized_environment(self):
        self.env.update({key: "dummy-" + key.lower() for key in CONNECTION_KEYS})
        captured = {}
        def fake_run(_command, **kwargs):
            captured.update(kwargs)

        self.ns.update({
            "_local_libretranslate_target": lambda: ("127.0.0.1", 5000),
            "libretranslate_python": lambda _service_dir: ("fake-python", None),
            "socket": types.SimpleNamespace(
                create_connection=mock.Mock(side_effect=OSError("offline")),
            ),
            "subprocess": types.SimpleNamespace(
                run=fake_run,
                DEVNULL=-1,
                CalledProcessError=RuntimeError,
                TimeoutExpired=TimeoutError,
            ),
            "start_process": mock.Mock(return_value=False),
        })
        _source_functions("start_libretranslate", namespace=self.ns)
        with self._vault_patch(lambda: set()):
            self.ns["start_libretranslate"](self.root)
        self.assertIn("env", captured)
        for key in CONNECTION_KEYS:
            self.assertNotIn(key, captured["env"])
        self.assertEqual(captured["env"]["OTHER"], "untouched")

    def test_vault_failure_blocks_all_service_starts(self):
        def apply(_environ=None):
            raise OSError("fake credential store unavailable")

        start_ollama = mock.Mock()
        start_process = mock.Mock()
        self.ns["start_ollama"] = start_ollama
        self.ns["start_process"] = start_process
        self.ns["_stop_requested"] = mock.Mock(return_value=False)
        with self._vault_patch(apply):
            with self.assertRaises(self.ns["StartupError"]):
                self.ns["_run_services"]()
        start_ollama.assert_not_called()
        start_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
