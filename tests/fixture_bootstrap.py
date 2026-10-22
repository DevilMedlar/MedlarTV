"""B0 offline fixture boundary. Import only standard library before install().

This is an accidental-side-effect guard for inspected tests, not a sandbox for
hostile Python. Run via offline_contract_runner.py with -I -S -B. Audit denials
are sticky: catching PermissionError in application code never clears them.
"""
from __future__ import annotations

import importlib.machinery
import os
from pathlib import Path
import sys
import tempfile
import types


class OfflineViolation(PermissionError):
    pass


def isolate_listener_stores(client):
    """Routing-only fixtures do not own persistence; memory suites use temp DBs."""
    from unittest.mock import Mock
    client._conversation_store = Mock(side_effect=RuntimeError("inert routing store"))
    client._profile_store = Mock(side_effect=RuntimeError("inert routing store"))


def _inside(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


class OfflineGuard:
    def __init__(self, root, evidence, dependency_root):
        self.root = os.path.normcase(os.path.realpath(root))
        self.evidence = os.path.normcase(os.path.realpath(evidence))
        self.runtime = os.path.normcase(os.path.realpath(sys.base_prefix))
        self.dependencies = os.path.normcase(os.path.realpath(dependency_root))
        self.denials = []
        self.current_test = "bootstrap"
        self.expected = False
        self.expected_denials = []
        self.owned_descriptors = {}

    def deny(self, event, reason):
        # Never retain arguments, payloads, environment values or private paths.
        entry = {"event": event, "reason": reason, "test": self.current_test}
        (self.expected_denials if self.expected else self.denials).append(entry)
        raise OfflineViolation("B0 blocked " + event + ": " + reason)

    def path(self, value, event, write=False):
        if isinstance(value, int):
            owned = self.owned_descriptors.get(value)
            try:
                info = os.fstat(value)
            except OSError:
                info = None
            if owned and info and (info.st_dev, info.st_ino) == owned:
                return
            self.deny(event, "unowned file descriptor")
        if not isinstance(value, (str, bytes, os.PathLike)):
            self.deny(event, "unsupported path")
        path = os.path.normcase(os.path.realpath(os.fsdecode(value)))
        if _inside(path, self.evidence):
            return
        if write:
            self.deny(event, "write outside disposable evidence")
        if _inside(path, self.dependencies):
            relative = os.path.relpath(path, self.dependencies).replace("\\", "/")
            package = relative.split("/", 1)[0].lower()
            if (package in {"yaml", "tzdata", "regex"}
                    or package.startswith(("pyyaml-", "tzdata-"))
                    or package == "regex-2026.9.29.dist-info"):
                return
            self.deny(event, "dependency outside fixture allowlist")
        if _inside(path, self.root):
            relative = os.path.relpath(path, self.root).replace("\\", "/")
            # Source/config only; no .env, logs, databases, Git or cached bytecode.
            if (relative.startswith(("medlartv/", "tests/"))
                    and Path(path).suffix in {".py", ".yaml"}
                    and relative != "medlartv/data/memory.yaml"
                    and "/logs/" not in relative):
                return
            if relative in {"launcher.py", "requirements.txt"}:
                return
            self.deny(event, "private or unreviewed project artifact")
        if _inside(path, self.runtime):
            return
        self.deny(event, "read outside reviewed source/runtime")

    def audit(self, event, args):
        if event == "open":
            mode = args[1] or ""
            flags = args[2] or 0
            write = any(char in mode for char in "wax+") or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            self.path(args[0], event, write)
        elif event.startswith("socket."):
            self.deny(event, "socket and DNS access forbidden")
        elif event.startswith(("subprocess.", "os.spawn", "os.exec", "winreg.")) or event in {
                "os.system", "os.startfile", "os.startfile/2", "os.fork", "os.forkpty",
                "os.posix_spawn", "os.kill", "os.killpg", "ctypes.dlopen", "ctypes.dlsym",
                "ctypes.dlsym/handle"}:
            self.deny(event, "process, native credential or system access forbidden")
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
            self.path(args[0], event, True)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            self.path(args[0], event, True)
            self.path(args[1], event, True)
            if event != "os.rename":
                self.deny(event, "links are not fixture artifacts")
        elif event == "os.chdir":
            path = os.path.normcase(os.path.realpath(args[0]))
            if not _inside(path, self.evidence):
                self.deny(event, "working directory must remain disposable")
        elif event == "sqlite3.connect":
            if args[0] != ":memory:":
                if str(args[0]).startswith("file:"):
                    self.deny(event, "SQLite URI disallowed")
                self.path(args[0], event, True)
        elif event == "import":
            name = args[0]
            if name.split(".")[0] in {"keyring", "win32cred", "win32api", "win32security", "PySide6"}:
                self.deny(event, "native UI or credential module forbidden")

    def install(self):
        evidence = Path(self.evidence)
        for name in ("tmp", "home", "local", "roaming", "config"):
            (evidence / name).mkdir()
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        os.environ.clear()
        os.environ.update({
            "SYSTEMROOT": system_root, "WINDIR": system_root,
            "PATH": str(Path(sys.executable).parent),
            "HOME": str(evidence / "home"), "USERPROFILE": str(evidence / "home"),
            "LOCALAPPDATA": str(evidence / "local"), "APPDATA": str(evidence / "roaming"),
            "TEMP": str(evidence / "tmp"), "TMP": str(evidence / "tmp"),
            "MEDLARTV_ROOT": str(evidence / "config"), "MEDLARTV_DEBUG": "false",
            "PYTHONDONTWRITEBYTECODE": "1", "QT_QPA_PLATFORM": "offscreen",
        })
        sys.dont_write_bytecode = True
        # Do not even attempt to read an old project __pycache__ entry.
        sys.pycache_prefix = str(evidence / "bytecode-disabled")
        tempfile.tempdir = str(evidence / "tmp")
        os.chdir(evidence)
        sys.addaudithook(self.audit)
        # fdopen is used by atomic-save fixtures. Only descriptors opened beneath
        # this run's evidence root are owned; verify identity against FD reuse.
        real_open, real_close = os.open, os.close
        def owned_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is not None:
                self.deny("os.open", "relative descriptor path forbidden")
            descriptor = real_open(path, flags, mode)
            canonical = os.path.normcase(os.path.realpath(os.fsdecode(path)))
            if _inside(canonical, self.evidence):
                info = os.fstat(descriptor)
                self.owned_descriptors[descriptor] = (info.st_dev, info.st_ino)
            return descriptor
        def owned_close(descriptor):
            self.owned_descriptors.pop(descriptor, None)
            return real_close(descriptor)
        os.open, os.close = owned_open, owned_close
        # Synthetic parents bypass core/__init__.py and its eager runtime imports.
        for name, relative in (("MedlarTV", "MedlarTV"), ("MedlarTV.core", "MedlarTV/core")):
            module = types.ModuleType(name)
            module.__path__ = [str(Path(self.root) / relative)]
            module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
            sys.modules[name] = module
        # These tests only mock requests. Avoid urllib3's import-time IPv6 probe.
        request_module = types.ModuleType("requests")
        sessions = types.ModuleType("requests.sessions")
        guard = self
        class Session:
            def request(self, *_args, **_kwargs):
                guard.deny("requests.request", "HTTP access forbidden")
        sessions.Session = Session
        request_module.sessions = sessions
        request_module.Session = Session
        request_module.get = request_module.post = request_module.patch = Session().request
        sys.modules.update({"requests": request_module, "requests.sessions": sessions})

    def self_check(self):
        """Probe forbidden operations before OS effects; retain expected counts."""
        import socket
        import sqlite3
        import subprocess
        operations = {
            "socket": lambda: socket.socket(),
            "dns": lambda: socket.getaddrinfo("example.invalid", 443),
            "process": lambda: subprocess.Popen([sys.executable, "-c", "raise SystemExit(99)"]),
            "production_env": lambda: open(Path(self.root) / ".env", "rb"),
            "production_log": lambda: open(Path(self.root) / "logs/core.log", "rb"),
            "production_database": lambda: sqlite3.connect(str(Path(self.root) / "private.sqlite3")),
            "outside_write": lambda: open(Path(self.root) / "__b0_forbidden_probe__", "wb"),
            "unowned_descriptor": lambda: self.path(1, "open", True),
            # Audit this boundary directly: no real vault library is ever loaded.
            "credential_native": lambda: sys.audit("ctypes.dlopen", "advapi32.dll"),
        }
        results = {}
        self.current_test = "guard_self_check"
        for name, action in operations.items():
            before = len(self.expected_denials)
            self.expected = True
            try:
                action()
            except OfflineViolation:
                results[name] = len(self.expected_denials) == before + 1
            finally:
                self.expected = False
            if not results.get(name):
                raise AssertionError("Guard did not deny " + name)
        # A caught violation must remain visible to the result gate.
        before = len(self.denials)
        try:
            sys.audit("socket.getaddrinfo", "synthetic.invalid", 443, 0, 0, 0)
        except PermissionError:
            pass
        results["swallowed_denial_is_sticky"] = len(self.denials) == before + 1
        if not results["swallowed_denial_is_sticky"]:
            raise AssertionError("Caught violation was lost")
        self.expected_denials.append(self.denials.pop())
        synthetic = Path(self.evidence) / "tmp/.env"
        synthetic.write_text("B0_SYNTHETIC=true\n", encoding="utf-8")
        results["synthetic_env_allowed"] = synthetic.read_text(encoding="utf-8") == "B0_SYNTHETIC=true\n"
        with sqlite3.connect(str(Path(self.evidence) / "tmp/synthetic.sqlite3")) as connection:
            connection.execute("CREATE TABLE proof(value INTEGER)")
            connection.execute("INSERT INTO proof VALUES (1)")
            results["temp_sqlite_allowed"] = connection.execute("SELECT value FROM proof").fetchone() == (1,)
        connection.close()
        descriptor, name = tempfile.mkstemp()
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"owned synthetic descriptor")
        results["owned_descriptor_allowed"] = Path(name).read_bytes() == b"owned synthetic descriptor"
        self.current_test = "collection"
        return results
