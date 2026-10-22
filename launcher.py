#!/usr/bin/env python3
"""
MedlarTV Unified Launcher (Mixed venv + system processes)
Runs Ollama and LibreTranslate from system, Medlar from .venv
"""

import os
import sys
import time
import argparse
import platform
import subprocess
import socket
import ipaddress
import signal
import shutil
import json
import tempfile
import uuid
from typing import TYPE_CHECKING, Callable, ClassVar, Protocol, TextIO, TypedDict, cast, runtime_checkable

if TYPE_CHECKING:
    from MedlarTV.avatar.bridge.auth import Bootstrap
from urllib.parse import urlsplit
from urllib.request import urlopen
from pathlib import Path
from MedlarTV.core.ollama_endpoint import ollama_api_ready, ollama_probe_target, ollama_base_url
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"

# ──────────────────────────────────────────────────────────────
# Colors for pretty output
class Colors:
    red: ClassVar[str] = '\033[0;31m'
    green: ClassVar[str] = '\033[0;32m'
    yellow: ClassVar[str] = '\033[1;33m'
    cyan: ClassVar[str] = '\033[0;36m'
    reset: ClassVar[str] = '\033[0m'

    @classmethod
    def init(cls) -> None:
        if platform.system() == 'Windows':
            try:
                import colorama
                colorama.just_fix_windows_console()
            except ImportError:
                cls.red = cls.green = cls.yellow = cls.cyan = cls.reset = ''
Colors.init()
# ──────────────────────────────────────────────────────────────

@runtime_checkable
class _ReconfigurableStream(Protocol):
    def reconfigure(self, *, encoding: str, errors: str) -> None: ...


def _configure_output_stream(stream: object) -> None:
    """Support compatible stream wrappers without coupling stdout and stderr."""
    if isinstance(stream, _ReconfigurableStream):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_output_stream(sys.stdout)
_configure_output_stream(sys.stderr)

ROOT = Path(__file__).parent.resolve()
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

class ProcessEntry(TypedDict):
    proc: subprocess.Popen[bytes]
    name: str
    log: TextIO
    required: bool
    stop_file: Path | None
    pid: int
    console_group: bool


processes: list[ProcessEntry] = []
STOP_FILE_ENV = "MEDLARTV_STOP_FILE"
OWNER_PID_ENV = "MEDLARTV_OWNER_PID"
CORE_STOP_FILE_ENV = "MEDLARTV_CORE_STOP_FILE"
SHUTDOWN_GRACE_SECONDS = 15.0
SHUTDOWN_TOTAL_SECONDS = 20.0
_windows_job_handle: int | None = None
_owner_pid: int | None = None
_owner_handle: int | None = None
_owner_missing = False
_avatar_session: "Bootstrap | None" = None


class StartupError(RuntimeError):
    """A service conflict prevents this launcher from owning its processes."""


def _protect_owned_processes() -> None:
    """Put this launcher and its future children in a kill-on-close Windows job."""
    global _windows_job_handle
    if os.name != "nt" or _windows_job_handle is not None:
        return

    import ctypes
    from ctypes import wintypes

    class _BasicJobLimitInformation(ctypes.Structure):
        LimitFlags: int
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(field, ctypes.c_ulonglong) for field in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimitInformation(ctypes.Structure):
        BasicLimitInformation: _BasicJobLimitInformation
        _fields_ = [
            ("BasicLimitInformation", _BasicJobLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetHandleInformation.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise StartupError(f"Could not create owned process job: {ctypes.WinError(ctypes.get_last_error())}")
    try:
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        handle_flags = wintypes.DWORD()
        if not kernel.GetHandleInformation(job, ctypes.byref(handle_flags)):
            raise ctypes.WinError(ctypes.get_last_error())
        if handle_flags.value & 1:  # HANDLE_FLAG_INHERIT
            raise OSError("Owned process job handle must not be inherited by children")
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as exc:
        kernel.CloseHandle(job)
        raise StartupError(f"Could not protect owned child processes: {exc}") from exc
    # Keep the only job handle in this process. Closing it would also kill the launcher.
    _windows_job_handle = cast(int, job)


def _owned_job_active_processes() -> int | None:
    """Count live members of the launcher's Windows job, including descendants."""
    if _windows_job_handle is None:
        return None
    import ctypes
    from ctypes import wintypes

    class BasicAccounting(ctypes.Structure):
        ActiveProcesses: int
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    counts = BasicAccounting()
    if not kernel.QueryInformationJobObject(_windows_job_handle, 1, ctypes.byref(counts),
                                             ctypes.sizeof(counts), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return counts.ActiveProcesses


def _init_owner_watch() -> None:
    """Keep a noninheritable handle to the Command Center that launched us."""
    global _owner_pid, _owner_handle, _owner_missing
    if _owner_pid is not None:
        return
    raw_pid = os.getenv(OWNER_PID_ENV, "").strip()
    if not raw_pid:
        return
    try:
        owner_pid = int(raw_pid)
    except ValueError as exc:
        raise StartupError("MEDLARTV_OWNER_PID must be a process ID") from exc
    if owner_pid <= 0 or owner_pid > 0xFFFFFFFF or owner_pid == os.getpid():
        raise StartupError("MEDLARTV_OWNER_PID must identify another running process")

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x00100000, False, owner_pid)  # SYNCHRONIZE; never inherited
        if not handle:
            error = ctypes.get_last_error()
            if error not in (87, 1168):  # Parent may have exited before we acquired it.
                raise StartupError(f"Could not watch Command Center process: {ctypes.WinError(error)}")
            _owner_missing = True
        else:
            _owner_handle = cast(int, handle)
    _owner_pid = owner_pid


def _owner_process_exited() -> bool:
    if _owner_pid is None:
        _init_owner_watch()
    if _owner_pid is None:
        return False
    if os.name == "nt":
        if _owner_missing:
            return True
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        result = kernel.WaitForSingleObject(_owner_handle, 0)
        if result == 0:  # WAIT_OBJECT_0: owner exited.
            return True
        if result == 258:  # WAIT_TIMEOUT: owner remains active.
            return False
        raise StartupError(f"Could not check Command Center process: {ctypes.WinError(ctypes.get_last_error())}")

    # On POSIX the launcher is a direct child, so reparenting detects exit even
    # if the original PID is later reused. Signal 0 only checks liveness.
    if os.getppid() != _owner_pid:
        return True
    try:
        os.kill(_owner_pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError as exc:
        raise StartupError(f"Could not check Command Center process: {exc}") from exc
    return False


def _close_owner_watch() -> None:
    global _owner_pid, _owner_handle, _owner_missing
    if _owner_handle is not None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        if not kernel.CloseHandle(_owner_handle):
            print(f"{Colors.yellow}[WARN] Could not release Command Center watch handle.{Colors.reset}")
    _owner_handle = None
    _owner_pid = None
    _owner_missing = False


def _stop_requested() -> bool:
    stop_file = os.getenv(STOP_FILE_ENV, "").strip()
    return bool(stop_file and Path(stop_file).is_file()) or _owner_process_exited()


def _ollama_endpoint_open() -> bool:
    """Check the Ollama serve bind port, even if its occupant is not Ollama."""
    host, port = ollama_probe_target(_ollama_bind_environment())
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _ollama_bind_environment() -> dict[str, str]:
    # ollama serve binds via OLLAMA_HOST; OLLAMA_URL configures its clients.
    return {"OLLAMA_HOST": os.getenv("OLLAMA_HOST", "127.0.0.1:11434")}


def _endpoint_host_key(host: str) -> str:
    if host.casefold().rstrip(".") == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.casefold()
    return "loopback" if address.is_loopback else address.compressed


def _validate_owned_ollama_endpoint() -> None:
    """Require local app-owned Ollama to serve the endpoint used by chat."""
    try:
        bind_host, bind_port = ollama_probe_target(_ollama_bind_environment())
        client_host, client_port = ollama_probe_target()
        # Raw owned serve has no TLS terminator or proxy base path. Client-only
        # split-mode endpoints retain the resolver's HTTPS/path acceptance.
        bind_url = urlsplit(ollama_base_url(_ollama_bind_environment()))
        client_url = urlsplit(ollama_base_url())
        if any(url.scheme != "http" or url.path for url in (bind_url, client_url)):
            raise StartupError("App-owned Ollama requires HTTP without a base path")
    except ValueError as exc:
        raise StartupError(f"Invalid Ollama endpoint configuration: {exc}") from exc
    if (_endpoint_host_key(bind_host), bind_port) != (_endpoint_host_key(client_host), client_port):
        raise StartupError("OLLAMA_URL must target the OLLAMA_HOST bind address and port for app-owned Ollama")


def start_ollama() -> bool:
    if _ollama_endpoint_open():
        print(f"{Colors.red}[ERROR] Ollama's configured endpoint is already in use. "
              f"Stop the unmanaged listener before starting Medlar; it will not be killed automatically.{Colors.reset}")
        raise StartupError("Ollama endpoint is already occupied")
    started = start_process("Ollama Server", ["ollama", "serve"], wait=3, required=True)
    if not started and _ollama_endpoint_open():
        print(f"{Colors.red}[ERROR] Another Ollama instance took the endpoint during startup. "
              f"Medlar will not manage or kill it.{Colors.reset}")
        raise StartupError("Ollama endpoint was claimed by another process")
    return started

def _load_env_file() -> None:
    env_path = ROOT / ".env"
    try:
        if env_path.exists():
            with env_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k:
                        os.environ[k] = v
    except Exception:
        pass

def _load_saved_connections() -> None:
    """Apply user-saved local credentials after the legacy .env values."""
    from MedlarTV.core.local_credentials import apply_saved_credentials

    try:
        apply_saved_credentials()
    except (OSError, ValueError) as exc:
        raise StartupError("Could not apply saved local connections; no services started") from exc


def _child_environment(*, include_connections: bool = False) -> dict[str, str]:
    """Give Twitch credentials only to the Medlar Core child that uses them."""
    env = os.environ.copy()
    env.pop("MEDLARTV_AVATAR_BOOTSTRAP", None)
    if not include_connections:
        from MedlarTV.core.local_credentials import CREDENTIAL_FIELDS

        for key in CREDENTIAL_FIELDS:
            env.pop(key, None)
    return env

# Historical cleanup utilities are not called by startup. Existing files stay
# intact; any deliberate cleanup is a separate, explicitly authorized operation.
def clear_logs() -> None:
    try:
        for p in LOGS_DIR.glob("*"):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass

def clear_old_sessions(keep: int = 1) -> None:
    try:
        sessions_dir = ROOT / "db" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        files = []
        try:
            files = [p for p in sessions_dir.iterdir() if p.is_file()]
        except Exception:
            files = []
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        for p in files[keep:]:
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass

def banner() -> None:
    print(f"""{Colors.cyan}
╔══════════════════════════════════════════════════════════╗
║                    MEDLAR  TACTICAL  AI                 ║
╚══════════════════════════════════════════════════════════╝
{Colors.reset}""")

def libretranslate_home() -> Path:
    """Use an isolated repo-local install, while honoring existing installs."""
    configured = os.getenv("LIBRETRANSLATE_HOME")
    if configured:
        return Path(configured).expanduser()
    local = ROOT / ".libretranslate"
    legacy = Path("C:/LibreTranslate")
    return local if local.is_dir() or not legacy.is_dir() else legacy


def libretranslate_python(service_dir: Path) -> tuple[str | None, str | None]:
    """Choose the interpreter for the separately installed translation service."""
    configured = os.getenv("LIBRETRANSLATE_PYTHON")
    if configured:
        python = Path(configured).expanduser()
        if not python.is_file():
            return None, f"LIBRETRANSLATE_PYTHON does not exist: {python}"
        return str(python), None

    for python in (
        service_dir / ".venv" / "Scripts" / "python.exe",
        service_dir / "venv" / "Scripts" / "python.exe",
    ):
        if python.is_file():
            return str(python), None

    python = shutil.which("python")
    if python:
        return python, None
    return None, "No Python interpreter found for LibreTranslate"


def _local_libretranslate_target() -> tuple[str, int] | None:
    """Return the loopback host and port for an app-owned LibreTranslate."""
    url = urlsplit(os.getenv("LIBRETRANSLATE_URL", "http://127.0.0.1:5000/translate"))
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"} or url.path.rstrip("/") != "/translate":
        return None
    try:
        return "127.0.0.1", url.port or 80
    except ValueError:
        return None


def _libretranslate_ready(host: str, port: int) -> bool:
    try:
        with urlopen(f"http://{host}:{port}/languages", timeout=1) as response:
            languages: object = json.load(response)
            if response.status != 200 or not isinstance(languages, list):
                return False
            for item in cast(list[object], languages):
                if isinstance(item, dict):
                    code = cast(dict[str, object], item).get("code")
                    if isinstance(code, str) and code.strip():
                        return True
            return False
    except (OSError, ValueError, TypeError):
        return False


def start_libretranslate(service_dir: Path) -> None:
    target = _local_libretranslate_target()
    if target is None:
        print(f"{Colors.yellow}[WARN] LIBRETRANSLATE_URL is not a local /translate endpoint; "
              f"using the configured external service without starting one.{Colors.reset}")
        return
    host, port = target
    try:
        with socket.create_connection((host, port), timeout=0.2):
            occupied = True
    except OSError:
        occupied = False
    if occupied:
        if _libretranslate_ready(host, port):
            print(f"{Colors.green}[OK] LibreTranslate already available at {host}:{port}{Colors.reset}")
        else:
            print(f"{Colors.yellow}[WARN] Port {port} is occupied by another service; "
                  f"LibreTranslate was not started.{Colors.reset}")
        return

    if not service_dir.is_dir():
        print(f"{Colors.yellow}[WARN] LibreTranslate is not installed at {service_dir}. "
              f"See README.md for the separate environment setup.{Colors.reset}")
        return

    python, error = libretranslate_python(service_dir)
    if error or python is None:
        print(f"{Colors.yellow}[WARN] {error}. Set LIBRETRANSLATE_PYTHON to its installed Python executable.{Colors.reset}")
        return
    try:
        subprocess.run(
            [python, "-c", "import importlib.metadata; importlib.metadata.version('libretranslate')"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            env=_child_environment(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(f"{Colors.yellow}[WARN] LibreTranslate is not installed in {python}; "
              "install requirements-libretranslate.txt in that interpreter's environment. "
              f"Skipping optional service.{Colors.reset}")
        return

    command = [python, "-m", "libretranslate.main", "--host", host, "--port", str(port)]
    languages = os.getenv("LIBRETRANSLATE_LANGUAGES", "").strip()
    if languages:
        command.extend(["--load-only", languages])
    if os.getenv("LIBRETRANSLATE_UPDATE_MODELS", "").strip().lower() in {"1", "true", "yes"}:
        command.append("--update-models")
    if start_process("LibreTranslate Server", command, cwd=service_dir, wait=5, required=False):
        if not _libretranslate_ready(host, port):
            print(f"{Colors.yellow}[WARN] LibreTranslate is still loading language models; "
                  f"translation commands will be unavailable until /languages responds.{Colors.reset}")

def _windows_console_available() -> bool:
    """Read console association without attaching or changing any console."""
    try:
        import ctypes
        return bool(ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleCP())
    except (OSError, AttributeError):
        return False


def _python_service_command(name: str, command: list[str]) -> list[str]:
    """Wrap owned Python services that need SIGBREAK cleanup."""
    adapter = str(ROOT / "MedlarTV" / "tools" / "python_service_entry.py")
    if name == "LibreTranslate Server" and list(command[1:3]) == ["-m", "libretranslate.main"]:
        return [command[0], adapter, "libretranslate", *command[3:]]
    if name == "Avatar Bridge Server" and len(command) >= 2 and Path(command[1]).as_posix() == "MedlarTV/avatar/bridge/server.py":
        return [command[0], adapter, "avatar", *command[2:]]
    if name == "Avatar Console Client" and len(command) >= 2 and Path(command[1]).as_posix() == "MedlarTV/avatar_client/console_client.py":
        return [command[0], adapter, "avatar-console", *command[2:]]
    return command


def _avatar_bootstrap_for(name: str) -> dict[str, str | int] | None:
    if _avatar_session is None:
        return None
    from MedlarTV.avatar.bridge.auth import child_payload
    role = {"Avatar Bridge Server": "server", "Medlar Core": "publisher",
            "Avatar Console Client": "subscriber"}.get(name)
    return child_payload(_avatar_session, role) if role else None


def _write_avatar_bootstrap(proc: "subprocess.Popen[bytes]", payload: dict[str, str | int]) -> None:
    from MedlarTV.avatar.bridge.auth import encode_bootstrap
    stream = proc.stdin
    try:
        if stream is None:
            raise StartupError("Avatar child bootstrap unavailable")
        stream.write(encode_bootstrap(payload))
        stream.flush()
    except Exception:
        raise StartupError("Avatar child bootstrap failed") from None
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                raise StartupError("Avatar child bootstrap close failed") from None


def start_process(name: str, command: list[str], wait: float = 2, cwd: Path | None = None,
                  use_venv: bool = False, required: bool = False,
                  include_connections: bool = False) -> bool:
    print(f"{Colors.cyan}[START] {name}...{Colors.reset}")
    log_handle = None
    proc = None
    stop_file = None
    try:
        if isinstance(command, str):
            raise ValueError("Services must use argument lists so their PIDs can be owned directly")
        env = _child_environment(include_connections=include_connections)
        bootstrap = _avatar_bootstrap_for(name)
        if bootstrap is not None:
            env["MEDLARTV_AVATAR_BOOTSTRAP"] = "1"
        try:
            env["MEDLARTV_ROOT"] = str(ROOT)
        except Exception:
            pass
        if use_venv:
            env["PYTHONPATH"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        if name == "Medlar Core":
            stop_file = Path(tempfile.gettempdir()) / f"medlar-core-{uuid.uuid4().hex}.stop"
            env[CORE_STOP_FILE_ENV] = str(stop_file)

        if DEBUG:
            print(f"[DEBUG] Launching process '{name}' with command: {command}")
            print(f"[DEBUG] Working directory: {cwd or ROOT}")
            print(f"[DEBUG] Using venv: {use_venv}")

        log_file_name = name.lower().replace(" ", "_").replace("(", "").replace(")", "") + ".log"
        log_path = LOGS_DIR / log_file_name
        log_handle = open(log_path, "a", encoding="utf-8")

        flags = 0
        console_group = False
        if platform.system() == "Windows":
            console_group = _windows_console_available()
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            if not console_group:
                flags |= subprocess.CREATE_NO_WINDOW
                print(f"{Colors.yellow}[WARN] No launcher console; owned services will use bounded force fallback.{Colors.reset}")
            command = _python_service_command(name, command)
        proc = subprocess.Popen(
            command,
            cwd=cwd or ROOT,
            env=env,
            shell=False,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if bootstrap is not None else subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=platform.system() != "Windows",
        )
        # Record the child before waiting: Ctrl+C or a startup error must still clean it up.
        processes.append({"proc": proc, "name": name, "log": log_handle,
                          "required": required, "stop_file": stop_file,
                          "pid": proc.pid, "console_group": console_group})
        if bootstrap is not None:
            _write_avatar_bootstrap(proc, bootstrap)

        if DEBUG:
            print(f"[DEBUG] Process object created. PID pending...")

        time.sleep(wait)
        if proc.poll() is None:
            print(f"{Colors.green}[OK] {name} running (PID {proc.pid}){Colors.reset}")
            return True
        else:
            print(f"{Colors.red}[FAIL] {name} failed to start{Colors.reset}")
            return False

    except Exception as e:
        print(f"{Colors.red}[ERROR] {name}: {e}{Colors.reset}")
        if proc is not None:
            raise
        return False
    finally:
        if proc is None and log_handle is not None:
            log_handle.close()

def _owned_entry(proc: "subprocess.Popen[bytes]") -> "ProcessEntry | None":
    """Use the recorded process object and PID; never adopt a discovered service."""
    # Process boundaries may receive dynamically supplied or mocked objects.
    pid = cast(object, proc.pid)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        return None
    return next((p for p in processes if p["proc"] is proc and p.get("pid") == pid), None)


def _request_owned_stop(entry: "ProcessEntry") -> None:
    """Core uses file IPC; native services use their private console group."""
    proc = entry["proc"]
    if proc.poll() is not None:
        return
    if _owned_entry(proc) is not entry:
        print(f"{Colors.yellow}[WARN] Stop rejected: process ownership does not match.{Colors.reset}")
        return
    try:
        stop_file = entry.get("stop_file")
        if stop_file is not None:
            stop_file.write_text("stop", encoding="utf-8")
        elif platform.system() == "Windows":
            if not entry.get("console_group"):
                print(f"{Colors.yellow}[WARN] Cooperative stop unavailable for PID {proc.pid}: no private console group.{Colors.reset}")
                return
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            killpg = cast(Callable[[int, int], None], getattr(os, "killpg"))
            killpg(proc.pid, signal.SIGTERM)
        print(f"{Colors.cyan}[STOP] Cooperative request sent for owned PID {proc.pid}.{Colors.reset}")
    except (OSError, AttributeError, ValueError) as exc:
        print(f"{Colors.yellow}[WARN] Cooperative stop unavailable for PID {proc.pid}: {type(exc).__name__}{Colors.reset}")


def _stop_owned_process(proc: "subprocess.Popen[bytes]", *, grace_deadline: float | None = None,
                        deadline: float | None = None) -> bool:
    if proc.poll() is not None:
        return True
    entry = _owned_entry(proc)
    if entry is None:
        print(f"{Colors.yellow}[WARN] Stop rejected: process ownership does not match.{Colors.reset}")
        return False
    if grace_deadline is None:
        now = time.monotonic()
        grace_deadline = now + SHUTDOWN_GRACE_SECONDS
        if deadline is None:
            deadline = now + SHUTDOWN_TOTAL_SECONDS
        _request_owned_stop(entry)
    elif deadline is None:
        # Preserve the supplied grace period and allow the usual force window.
        deadline = grace_deadline + (SHUTDOWN_TOTAL_SECONDS - SHUTDOWN_GRACE_SECONDS)
    # An explicit total deadline bounds the cooperative wait as well as force.
    grace_deadline = min(grace_deadline, deadline)
    while proc.poll() is None:
        remaining_grace = grace_deadline - time.monotonic()
        if remaining_grace <= 0:
            break
        time.sleep(min(0.1, remaining_grace))
    if proc.poll() is not None:
        return True
    try:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return False
        if _owned_entry(proc) is not entry:
            return False
        if platform.system() == "Windows":
            # Escalate only this owned PID tree after the shared cleanup deadline.
            print(f"{Colors.yellow}[STOP] Force fallback for owned PID {proc.pid}.{Colors.reset}")
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=remaining,
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           env=_child_environment())
        else:
            killpg = cast(Callable[[int, int], None], getattr(os, "killpg"))
            sigkill = cast(int, getattr(signal, "SIGKILL"))
            killpg(proc.pid, sigkill)
        remaining = max(0.0, deadline - time.monotonic())
        if proc.poll() is None and remaining:
            proc.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{Colors.red}[ERROR] Could not stop PID {proc.pid}: {type(exc).__name__}{Colors.reset}")
    return proc.poll() is not None


def stop_all() -> bool:
    print(f"\n{Colors.yellow}Shutting down Medlar systems...{Colors.reset}")
    owned_ollama = any(p["name"] == "Ollama Server" for p in processes)
    stopped = True
    now = time.monotonic()
    grace_deadline = now + SHUTDOWN_GRACE_SECONDS
    deadline = now + SHUTDOWN_TOTAL_SECONDS
    for p in reversed(processes):
        _request_owned_stop(p)
    for p in reversed(processes):
        if not _stop_owned_process(p["proc"], grace_deadline=grace_deadline, deadline=deadline):
            stopped = False
            print(f"{Colors.red}[ERROR] {p['name']} (PID {p['proc'].pid}) is still running.{Colors.reset}")
        try:
            p["log"].close()
        except OSError:
            pass
        stop_file = p.get("stop_file")
        if p["proc"].poll() is not None and stop_file is not None:
            try:
                stop_file.unlink(missing_ok=True)
            except OSError:
                pass
    processes[:] = [p for p in processes if p["proc"].poll() is None]
    if _windows_job_handle is not None:
        try:
            active = _owned_job_active_processes()
            while active != 1 and time.monotonic() < deadline:
                time.sleep(0.1)
                active = _owned_job_active_processes()
            if active != 1:
                stopped = False
                print(f"{Colors.red}[ERROR] {active} owned job processes remain active (expected only launcher).{Colors.reset}")
        except OSError as exc:
            stopped = False
            print(f"{Colors.red}[ERROR] Could not verify owned child processes: {exc}{Colors.reset}")
    if owned_ollama and _ollama_endpoint_open():
        print(f"{Colors.yellow}[WARN] Ollama's endpoint is still active after owned cleanup. "
              f"Another listener may be using it.{Colors.reset}")
    if stopped:
        print(f"{Colors.green}All owned systems offline.{Colors.reset}")
    else:
        print(f"{Colors.red}Shutdown incomplete; an owned process is still active.{Colors.reset}")
    return stopped

def _monitor_processes() -> None:
    heartbeat = 0
    reported_exits: set[int] = set()
    while not _stop_requested():
        time.sleep(1)
        heartbeat += 1
        if heartbeat % 10 == 0 and DEBUG:
            print("[DEBUG] Launcher heartbeat: processes running okay...")
        for p in processes:
            if p["proc"].poll() is not None and p["proc"].pid not in reported_exits:
                reported_exits.add(p["proc"].pid)
                print(f"{Colors.yellow}[WARN] Process exited: {p['name']} ({p['proc'].args}){Colors.reset}")
                if DEBUG:
                    print(f"[DEBUG] Return code: {p['proc'].returncode}")
                if p.get("required", False):
                    print(f"{Colors.red}[ALERT] Required process '{p['name']}' exited. Shutting down...{Colors.reset}")
                    raise StartupError(f"Required process {p['name']} exited")


def _find_free_bridge_port(host: str, start_port: int = 8765, end_port: int = 8775) -> int | None:
    """Return a candidate port, or None; this does not reserve the port."""
    for port in range(start_port, end_port + 1):
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind((host, port))
        except OSError:
            continue
        finally:
            candidate.close()
        return port
    return None


def start_avatar_bridge() -> bool:
    """Start the optional bridge without pointing clients at a failed endpoint."""
    global _avatar_session
    _avatar_session = None
    bridge_host = "127.0.0.1"
    os.environ["BRIDGE_HOST"] = bridge_host
    os.environ["BRIDGE_PORT"] = ""
    os.environ["BRIDGE_URL"] = ""
    bridge_port = _find_free_bridge_port(bridge_host)
    if bridge_port is None:
        print(f"{Colors.yellow}[WARN] Avatar Bridge unavailable: no free port in 8765-8775; "
              f"continuing without the optional bridge or console.{Colors.reset}")
        return False
    os.environ["BRIDGE_PORT"] = str(bridge_port)
    os.environ["BRIDGE_URL"] = f"ws://127.0.0.1:{bridge_port}"
    from MedlarTV.avatar.bridge.auth import create_session
    _avatar_session = create_session(os.getenv("TWITCH_CHANNEL", "devilmedlar"), os.environ["BRIDGE_URL"])
    if DEBUG:
        print(f"[DEBUG] Avatar Bridge selected port: {bridge_port}")
    started = start_process("Avatar Bridge Server", [str(VENV_PYTHON), "MedlarTV/avatar/bridge/server.py"],
                            use_venv=True, wait=2, required=False)
    if not started:
        _avatar_session = None
        os.environ["BRIDGE_PORT"] = ""
        os.environ["BRIDGE_URL"] = ""
        print(f"{Colors.yellow}[WARN] Avatar Bridge failed to start; "
              f"continuing without the optional bridge or console.{Colors.reset}")
    return started


def _run_services(mode: str = "all") -> None:
    if mode not in ("all", "ollama", "twitch"):
        raise StartupError(f"Unsupported launch mode: {mode}")
    if _stop_requested():
        return

    # Each Command Center mode has its own launcher and owned child processes.
    # Ollama-only startup needs neither Twitch configuration nor credentials.
    # Startup preserves existing logs and sessions, including failed-start
    # evidence. Retention is never an automatic side effect of launching.
    if mode != "ollama":
        if mode == "twitch":
            # The GUI and its Ollama launcher use the inherited endpoint.
            # Loading legacy .env must not retarget Core or its GUI status logs.
            endpoint_values = {key: os.environ.get(key) for key in ("OLLAMA_URL", "OLLAMA_HOST", "LOG_DIRECTORY")}
            try:
                _load_env_file()
            finally:
                for key, value in endpoint_values.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        else:
            _load_env_file()
        _load_saved_connections()
    banner()
    print(f"{Colors.green}Initializing Medlar Tactical AI systems...{Colors.reset}\n")

    # Only modes with Python services need the repository venv.
    if mode != "ollama" and not VENV_PYTHON.exists():
        print(f"{Colors.red}[ERROR] Python venv not found at {VENV_PYTHON}{Colors.reset}")
        raise StartupError("Python venv is missing")
    if _stop_requested():
        return

    # ───────────────────────────────────────────────
    # 1️⃣ Ollama (system)
    if mode == "twitch":
        # The separate Ollama launcher owns this listener. Twitch never stops it.
        try:
            api_ready = ollama_api_ready()
        except ValueError as exc:
            raise StartupError(f"Invalid Ollama endpoint configuration: {exc}") from exc
        if not api_ready:
            raise StartupError("Ollama is unavailable; open Command Center and wait for its brain to start")
    else:
        # Both owned startup modes validate alignment before any service probe/spawn.
        _validate_owned_ollama_endpoint()
        if not start_ollama():
            raise StartupError("Ollama Server failed to start")
    if _stop_requested():
        return

    if mode == "ollama":
        print(f"\n{Colors.green}Ollama ready for desktop chat{Colors.reset}")
        print(f"{Colors.yellow}Press Ctrl+C to shutdown gracefully...{Colors.reset}\n")
        _monitor_processes()
        return

    # ───────────────────────────────────────────────
    # 2️⃣ LibreTranslate (isolated environment)
    start_libretranslate(libretranslate_home())
    if _stop_requested():
        return

    bridge_started = start_avatar_bridge()
    if _stop_requested():
        return

    # ───────────────────────────────────────────────
    # 3️⃣ Medlar Core (direct)
    core_started = start_process(
        "Medlar Core",
        [str(VENV_PYTHON), "MedlarTV/core/main.py"],
        use_venv=True,
        wait=4,
        required=True,
        include_connections=True,
    )
    if not core_started:
        raise StartupError("Medlar Core failed to start")
    if _stop_requested():
        return

    pass

    # ───────────────────────────────────────────────
    # Avatar Console Client (inside venv, optional)
    console_enabled = os.getenv("ENABLE_AVATAR_CONSOLE", "0") == "1"
    print(f"[AvatarConsole] enabled={console_enabled}", flush=True)
    if bridge_started and console_enabled:
        print("[AvatarConsole] launch=attempt", flush=True)
        try:
            console_started = start_process("Avatar Console Client", [str(VENV_PYTHON), "MedlarTV/avatar_client/console_client.py"],
                                            use_venv=True, wait=2)
        except Exception:
            print("[AvatarConsole] launch=error", flush=True)
            raise
        print("[AvatarConsole] launch=started" if console_started else "[AvatarConsole] launch=failed", flush=True)
    else:
        print("[AvatarConsole] launch=skipped_disabled" if not console_enabled else "[AvatarConsole] launch=skipped_bridge", flush=True)

    # ───────────────────────────────────────────────
    print(f"\n{Colors.green}✅ MedlarTV Systems Operational{Colors.reset}")
    print(f"{Colors.yellow}Press Ctrl+C to shutdown gracefully...{Colors.reset}\n")

    _monitor_processes()


def main(mode: str = "all") -> int:
    status = 0
    try:
        _init_owner_watch()
        _protect_owned_processes()
        _run_services(mode=mode)
    except KeyboardInterrupt:
        print(f"{Colors.yellow}Ctrl+C received; stopping owned services...{Colors.reset}")
    except StartupError as exc:
        print(f"{Colors.red}[ERROR] {exc}{Colors.reset}")
        status = 1
    finally:
        try:
            if not stop_all():
                status = 2
        finally:
            _close_owner_watch()
    return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start Medlar services with owned process cleanup")
    parser.add_argument("--mode", choices=("all", "ollama", "twitch"), default="all")
    sys.exit(main(mode=parser.parse_args().mode))
