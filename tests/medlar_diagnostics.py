#!/usr/bin/env python3
"""Generate a consolidated diagnostic report for the local Medlar checkout.

The normal run performs local setup checks, non-mutating loopback service
checks, effective credential resolution, independent Twitch token validation,
safe active Helix GETs, and a read-only Twitch IRC authentication/join probe.
It does not start services, run inference, install packages, or mutate local
configuration or memory. ``--offline`` is an explicit incomplete mode. The
narrowly allowlisted title, category, and chat checks additionally require
``--exercise-twitch`` plus explicit target/value arguments; title and category
restoration is attempted and verified, but can fail or be interrupted.

The normal local report is intentionally PRIVATE/SENSITIVE and contains the
exact effective Twitch credentials plus complete HTTP request/response
evidence.  It is restricted to the repository's ignored ``logs/`` directory.
The CLI writes one full private report; no static or shareable-report mode is provided.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from contextvars import ContextVar
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace, asdict
from datetime import datetime, timezone
from http.client import HTTPException, IncompleteRead
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = REPO_ROOT / "logs" / "medlar_diagnostics.txt"
REEXEC_MARKER = "MEDLAR_DIAGNOSTICS_APP_PYTHON"

READY = "READY"
UNAVAILABLE = "UNAVAILABLE"
SKIPPED = "SKIPPED"
ERROR = "ERROR"
VALID_STATUSES = frozenset({READY, UNAVAILABLE, SKIPPED, ERROR})

CREDENTIAL_FIELDS: tuple[str, ...] = (
    "MEDLARTV_TWITCH_TOKEN",
    "MEDLARTV_TWITCH_CLIENT_ID",
    "APP_TWITCH_CLIENT_ID",
    "APP_SECRET_ID",
    "DEVILMEDLAR_TWITCH_CLIENT_ID",
    "DEVILMEDLAR_TWITCH_TOKEN",
)

CREDENTIAL_LABELS: Mapping[str, tuple[str, bool]] = {
    "MEDLARTV_TWITCH_TOKEN": ("Bot IRC token", True),
    "MEDLARTV_TWITCH_CLIENT_ID": ("Bot client ID", False),
    "APP_TWITCH_CLIENT_ID": ("Twitch app client ID", True),
    "APP_SECRET_ID": ("Twitch app client secret", True),
    "DEVILMEDLAR_TWITCH_CLIENT_ID": ("Broadcaster client ID", True),
    "DEVILMEDLAR_TWITCH_TOKEN": ("Broadcaster token", True),
}

BOT_REQUIRED_SCOPES: Mapping[str, str] = {
    "chat:read": "current documented IRC receive path used by twitch_listener.py",
    "chat:edit": "current documented IRC PRIVMSG send path used by twitch_listener.py",
}

BROADCASTER_REQUIRED_SCOPES: Mapping[str, str] = {
    "channel:manage:broadcast": "implemented title/category updates",
    "moderator:manage:shoutouts": "implemented optional Helix shoutout",
}

PUBLIC_ENV_FIELDS: tuple[str, ...] = (
    "TWITCH_NICK",
    "TWITCH_CHANNEL",
    "OLLAMA_URL",
    "OLLAMA_HOST",
    "MODEL_NAME",
    "OLLAMA_MODEL",
    "LIBRETRANSLATE_URL",
    "LIBRETRANSLATE_HOME",
    "LIBRETRANSLATE_PYTHON",
    "LIBRETRANSLATE_LANGUAGES",
    "LIBRETRANSLATE_UPDATE_MODELS",
)

RELEVANT_ENV_FIELDS = frozenset((*CREDENTIAL_FIELDS, *PUBLIC_ENV_FIELDS))

CONFIG_FILES: tuple[tuple[str, str, bool], ...] = (
    ("App settings", "MedlarTV/config/app_settings.yaml", True),
    ("Commands", "MedlarTV/config/commands.yaml", True),
    ("Content filter", "MedlarTV/config/content_filter.yaml", True),
    ("Co-pilots", "MedlarTV/config/copilots.yaml", True),
    ("Counters", "MedlarTV/config/counters.yaml", True),
    ("Events", "MedlarTV/config/events.yaml", True),
    ("Link whitelist", "MedlarTV/config/link_whitelist.yaml", True),
    ("Personality", "MedlarTV/config/personality.yaml", True),
    ("Timers", "MedlarTV/config/timers.yaml", True),
    ("Legacy devices", "MedlarTV/config/devices.yaml", False),
    ("Legacy moods", "MedlarTV/config/moods.yaml", False),
    ("Legacy policy", "MedlarTV/config/policy.yaml", False),
    ("Legacy style profiles", "MedlarTV/config/style_profiles.yaml", False),
)

PROJECT_DIRECTORIES: tuple[tuple[str, str], ...] = (
    ("Medlar package", "MedlarTV"),
    ("Core", "MedlarTV/core"),
    ("Avatar", "MedlarTV/avatar"),
    ("Avatar client", "MedlarTV/avatar_client"),
    ("Tools", "MedlarTV/tools"),
    ("Configuration", "MedlarTV/config"),
    ("Data", "MedlarTV/data"),
)

PACKAGE_MARKERS: tuple[str, ...] = (
    "MedlarTV/__init__.py",
    "MedlarTV/core/__init__.py",
    "MedlarTV/avatar/__init__.py",
    "MedlarTV/avatar/bridge/__init__.py",
    "MedlarTV/avatar_client/__init__.py",
    "MedlarTV/tools/__init__.py",
    "MedlarTV/config/__init__.py",
    "MedlarTV/data/__init__.py",
)

REQUIRED_SOURCE_FILES: tuple[str, ...] = (
    "launcher.py",
    "MedlarTV/app/medlar_app.py",
    "MedlarTV/app/config_editor_store.py",
    "MedlarTV/avatar/bridge/server.py",
    "MedlarTV/avatar/bridge/client.py",
    "MedlarTV/core/main.py",
    "MedlarTV/core/local_credentials.py",
    "MedlarTV/core/settings.py",
    "MedlarTV/core/command_handlers.py",
    "MedlarTV/core/moderation.py",
    "MedlarTV/core/twitch_events.py",
    "MedlarTV/core/ollama_endpoint.py",
    "MedlarTV/core/stream_management.py",
    "MedlarTV/core/llm_brain.py",
    "MedlarTV/core/translation.py",
    "MedlarTV/core/memory.py",
    "MedlarTV/core/viewer_profiles.py",
    "MedlarTV/core/conversation_context.py",
    "MedlarTV/tools/twitch_listener.py",
    "requirements.txt",
    "requirements-libretranslate.txt",
)

# These returned names are retained as raw strings in live reports. This table
# only classifies names that the checked-in historical catalog does not cover or
# whose historical description needs an explicit modern caveat.
MODERN_SCOPE_DESCRIPTIONS: Mapping[str, str] = {
    "channel:manage:clips": "create clips from the broadcaster's VODs and get supported clip downloads",
    "editor:manage:clips": "create clips from VODs and get supported clip downloads as an authorized editor",
    "moderator:manage:suspicious_users": "manage suspicious-user status in moderated channels",
    "moderator:read:banned_users": "receive supported banned-user EventSub information",
    "moderator:read:chat_messages": "receive deleted-message events and read supported pinned-message data",
    "moderator:read:moderators": "receive supported moderator-change EventSub information",
    "moderator:read:shoutouts": "receive supported shoutout EventSub information",
    "moderator:manage:shoutouts": "send/manage shoutouts through the supported Helix endpoint",
    "moderator:read:vips": "receive supported VIP-change EventSub information",
    "moderator:read:warnings": "receive supported warning EventSub information",
    "user:read:whispers": "receive new whispers through the supported EventSub event; not an inbox-history permission",
}

LEGACY_SCOPE_NAMES = frozenset({
    "channel_check_subscription", "channel_commercial", "channel_editor",
    "channel_feed_edit", "channel_feed_read", "channel_read", "channel_stream",
    "channel_subscriptions", "collections_edit", "communities_edit",
    "communities_moderate", "chat_login", "user_blocks_edit", "user_blocks_read",
    "user_follows_edit", "user_read", "user_subscriptions", "viewing_activity_read",
})

SPECIAL_SCOPE_NOTES: Mapping[str, str] = {
    "openid": "CURRENT-OIDC: OpenID Connect identity/claims scope; it is not a bot/chat permission",
    "whispers:read": "LEGACY-DECOMMISSIONED: legacy PubSub receive scope; current receive uses user:read:whispers",
    "whispers:edit": "OBSOLETE-REMOVED (obsolete): current whisper sending uses user:manage:whispers, not this old name",
    "user:edit:follows": "OBSOLETE-REMOVED (obsolete): public follow-writing endpoints were removed and have no write replacement",
}

IRC_SEND_REJECTION_NOTICE_IDS = frozenset({
    "msg_bad_characters", "msg_banned", "msg_channel_blocked", "msg_channel_suspended",
    "msg_duplicate",
    "msg_emoteonly", "msg_followersonly", "msg_followersonly_followed",
    "msg_followersonly_zero", "msg_r9k", "msg_ratelimit", "msg_rejected",
    "msg_rejected_mandatory", "msg_requires_verified_phone_number", "msg_slowmode",
    "msg_subsonly", "msg_suspended", "msg_timedout", "msg_verified_email",
    "msg_room_not_found", "tos_ban",
})


@dataclass(frozen=True)
class Check:
    section: str
    name: str
    status: str
    detail: str

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Unsupported diagnostic status: {self.status}")


@dataclass
class EnvInspection:
    path: Path
    exists: bool = False
    public_values: dict[str, str] = field(default_factory=dict)
    credential_presence: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class HttpJsonResult:
    status: int | None
    payload: Any = None
    error: str | None = None
    raw_body: str | None = field(default=None, repr=False)
    response_headers: tuple[tuple[str, str], ...] = field(default_factory=tuple, repr=False)
    body_truncated: bool = False
    body_bytes_observed: int = 0


@dataclass(frozen=True)
class HttpExchangeEvidence:
    sequence: int
    method: str
    url: str
    request_headers: tuple[tuple[str, str], ...]
    request_body: str | None
    response_status: int | None
    response_headers: tuple[tuple[str, str], ...]
    response_body: str | None = field(default=None, repr=False)
    response_body_source: str = "none"
    response_body_truncated: bool = False
    response_body_bytes_observed: int = 0
    error: str | None = None


@dataclass(frozen=True)
class StoppedBaseline:
    probe_completed: bool
    medlar_process_count: int = 0
    ollama_process_count: int = 0
    medlar_irc_connection_count: int = 0
    medlar_kinds: tuple[str, ...] = ()
    ollama_names: tuple[str, ...] = ()
    detail: str = ""
    irc_probe_completed: bool = True


@dataclass
class DiagnosticRun:
    checks: list[Check] = field(default_factory=list)
    secrets: tuple[str, ...] = field(default_factory=tuple, repr=False)
    sensitive_values: dict[str, str] = field(default_factory=dict, repr=False)
    http_exchanges: list[HttpExchangeEvidence] = field(default_factory=list, repr=False)
    configuration_values: dict[str, str] = field(default_factory=dict, repr=False)
    sensitive_values_resolved: bool = False
    offline: bool = False
    critical_failure: bool = False
    exercise_requested: bool = False
    complete: bool = False
    collection_failed: bool = False
    failures: list[dict[str, str]] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class TokenFacts:
    login: str
    client_id: str
    user_id: str
    scopes: frozenset[str]
    raw_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TwitchExercise:
    target_channel: str
    title: str
    category: str
    chat_message: str


@dataclass(frozen=True)
class IrcSendResult:
    connected: bool = False
    authenticated: bool = False
    joined: bool = False
    written: bool = False
    acknowledged: bool = False
    rejected: bool = False
    error: str | None = None


@dataclass(frozen=True)
class IrcProbeResult:
    connected: bool = False
    authenticated: bool = False
    joined: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ScopeFunctionalOutcome:
    verdict: str
    reason: str
    failure_status: str = UNAVAILABLE

    def __post_init__(self) -> None:
        if self.verdict not in {"PASS", "FAIL", "UNTESTED"}:
            raise ValueError(f"Unsupported functional verdict: {self.verdict}")
        if self.failure_status not in {UNAVAILABLE, ERROR}:
            raise ValueError(f"Unsupported failure status: {self.failure_status}")


HttpJson = Callable[..., HttpJsonResult]
IrcSender = Callable[[str, str, str, str], IrcSendResult]
IrcProbe = Callable[[str, str, str], IrcProbeResult]


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def launcher_python(root: Path = REPO_ROOT) -> Path | None:
    for candidate in (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        _capture_failure(sys.exc_info()[1])
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def reexec_in_launcher_python(
    argv: Sequence[str],
    *,
    root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> int | None:
    """Run once under Medlar's launcher interpreter before any diagnostic action."""
    interpreter = launcher_python(root)
    if interpreter is None or _same_path(interpreter, Path(sys.executable)):
        return None
    source_environment = os.environ if environ is None else environ
    if source_environment.get(REEXEC_MARKER) == "1":
        raise RuntimeError("Application-interpreter relaunch did not converge; collection was not started")
    child_environment = dict(source_environment)
    child_environment[REEXEC_MARKER] = "1"
    try:
        completed = runner(
            [str(interpreter), "-B", str(Path(__file__).resolve()), *argv],
            shell=False,
            env=child_environment,
            check=False,
        )
    except OSError:
        _capture_failure(sys.exc_info()[1])
        raise RuntimeError("Application-interpreter relaunch failed; collection was not started") from sys.exc_info()[1]
    return int(completed.returncode)


def _relative(path: Path, root: Path = REPO_ROOT) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        _capture_failure(sys.exc_info()[1])
        return "(external path withheld)"


def redact_text(text: object, secrets: Sequence[str] = ()) -> str:
    """Apply defense-in-depth redaction without exposing prefixes or lengths."""
    cleaned = str(text)
    for secret in sorted(
        {value for value in secrets if isinstance(value, str) and value},
        key=len,
        reverse=True,
    ):
        cleaned = cleaned.replace(secret, "[REDACTED]")

    patterns = (
        (
            r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*['\"]?)"
            r"(?:bearer|oauth)\s+[^\s,;'\"}]+",
            r"\1[REDACTED]",
        ),
        (r"(?i)(\boauth:)\S+", r"\1[REDACTED]"),
        (
            r"(?i)(['\"]?(?:client[_-]?secret|access[_-]?token|refresh[_-]?token|"
            r"password|APP_SECRET_ID|DEVILMEDLAR_TWITCH_TOKEN|"
            r"MEDLARTV_TWITCH_TOKEN)['\"]?\s*[:=]\s*['\"]?)"
            r"[^\s,;'\"}]+",
            r"\1[REDACTED]",
        ),
        (r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@"),
    )
    for pattern, replacement in patterns:
        cleaned = re.sub(pattern, replacement, cleaned)
    return cleaned


def inspect_env_file(path: Path) -> EnvInspection:
    """Inspect exact relevant assignments while retaining no credential values."""
    result = EnvInspection(path=path, exists=path.is_file())
    if not result.exists:
        return result
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key in CREDENTIAL_FIELDS:
                    result.credential_presence[key] = bool(value)
                elif key in PUBLIC_ENV_FIELDS:
                    result.public_values[key] = value
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        result.error = "could not read the file as UTF-8"
    return result


def load_live_env_values(path: Path) -> dict[str, str]:
    """Load only diagnostic-relevant values for an explicit live-auth run."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in RELEVANT_ENV_FIELDS:
                values[key] = value.strip()
    return values


def effective_public_environment(
    environ: Mapping[str, str],
    inspection: EnvInspection | None,
    launcher_mode: str,
) -> dict[str, str]:
    """Resolve public settings using the chosen launcher's real precedence."""
    resolved = {key: str(environ[key]) for key in PUBLIC_ENV_FIELDS if key in environ}
    if inspection is None or inspection.error or launcher_mode == "ollama":
        return resolved

    preserved_endpoints = {
        key: resolved.get(key) for key in ("OLLAMA_URL", "OLLAMA_HOST")
    }
    resolved.update(inspection.public_values)
    if launcher_mode == "twitch":
        for key, value in preserved_endpoints.items():
            if value is None:
                resolved.pop(key, None)
            else:
                resolved[key] = value
    return resolved


def _load_source_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("module loader unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


def probe_vault_presence(root: Path = REPO_ROOT) -> tuple[dict[str, bool | None], str | None]:
    """Query only Windows Credential Manager record presence, never values."""
    if os.name != "nt":
        return {key: None for key in CREDENTIAL_FIELDS}, "Windows credential store is not available"
    try:
        module = _load_source_module(
            root / "MedlarTV" / "core" / "local_credentials.py",
            "_medlar_diagnostics_local_credentials",
        )
        result: dict[str, bool | None] = {}
        for key in CREDENTIAL_FIELDS:
            result[key] = bool(module.credential_present(key))
        return result, None
    except Exception:
        _capture_failure(sys.exc_info()[1])
        return {key: None for key in CREDENTIAL_FIELDS}, "presence query failed"


def check_credential_loader_contract(root: Path = REPO_ROOT) -> list[Check]:
    path = root / "MedlarTV" / "core" / "local_credentials.py"
    try:
        definitions = _source_definitions(path)
    except (OSError, UnicodeError, SyntaxError):
        _capture_failure(sys.exc_info()[1])
        return [Check(
            "Credentials", "Credential loader interface", ERROR,
            "local_credentials.py could not be parsed; no credential record or value was opened.",
        )]
    required = {"credential_present", "apply_saved_credentials"}
    missing = sorted(required - definitions)
    return [Check(
        "Credentials", "Credential loader interface",
        READY if not missing else UNAVAILABLE,
        "Presence and atomic saved-credential overlay entrypoints are defined; actual decode/apply viability is checked only by the live workflow."
        if not missing else
        f"Missing credential loader entrypoints: {', '.join(missing)}; no credential record or value was opened.",
    )]


def credential_checks(
    environ: Mapping[str, str],
    inspection: EnvInspection | None,
    vault_presence: Mapping[str, bool | None],
    vault_error: str | None,
    launcher_mode: str,
) -> list[Check]:
    checks: list[Check] = []
    env_file_applies = inspection is not None and not inspection.error and launcher_mode != "ollama"

    for key in CREDENTIAL_FIELDS:
        label, required = CREDENTIAL_LABELS[key]
        present = _nonempty(environ.get(key))
        source = "inherited process environment" if key in environ else "not configured"
        if env_file_applies and key in inspection.credential_presence:
            present = inspection.credential_presence[key]
            source = "repository .env" if present else "repository .env (empty override)"
        saved = vault_presence.get(key)
        if launcher_mode != "ollama" and saved is True:
            present = True
            source = "Windows Credential Manager override"

        if launcher_mode == "ollama":
            checks.append(Check(
                "Credentials", label, SKIPPED,
                "The launcher does not load Twitch credentials in ollama-only mode.",
            ))
            continue

        uncertainty = ""
        if saved is None and vault_error:
            uncertainty = "; saved-credential precedence could not be confirmed"
        if present:
            checks.append(Check(
                "Credentials", label, READY,
                f"Presence-only result: {key} is configured via {source}; value was not read or displayed{uncertainty}.",
            ))
        elif not required:
            checks.append(Check(
                "Credentials", label, SKIPPED,
                f"{key} is not configured. The current IRC login path does not consume this client ID; "
                f"it is used only for optional live identity comparison{uncertainty}.",
            ))
        elif saved is None and inspection is None and key not in environ:
            checks.append(Check(
                "Credentials", label, SKIPPED,
                f"Private sources were not inspected; effective presence of {key} is unknown.",
            ))
        else:
            checks.append(Check(
                "Credentials", label, UNAVAILABLE,
                f"No non-empty value was found for {key}; effective source: {source}{uncertainty}.",
            ))
    return checks


def check_public_twitch_settings(
    environment: Mapping[str, str], launcher_mode: str,
) -> list[Check]:
    if launcher_mode == "ollama":
        return [
            Check("Configuration", "Bot account (TWITCH_NICK)", SKIPPED,
                  "Ollama-only mode does not load Twitch configuration."),
            Check("Configuration", "Target channel (TWITCH_CHANNEL)", SKIPPED,
                  "Ollama-only mode does not load Twitch configuration."),
        ]
    bot = str(environment.get("TWITCH_NICK", "")).strip()
    channel = str(environment.get("TWITCH_CHANNEL", "")).strip().lstrip("#")
    return [
        Check(
            "Configuration", "Bot account (TWITCH_NICK)",
            READY if bot else UNAVAILABLE,
            (
                f"Effective configured bot login is {bot!r}."
                if bot else
                "TWITCH_NICK is not explicitly configured. Current application source has a fallback, but diagnostics do not use implicit identities for live authentication or actions."
            ),
        ),
        Check(
            "Configuration", "Target channel (TWITCH_CHANNEL)",
            READY if channel else UNAVAILABLE,
            (
                f"Effective configured target is #{channel}."
                if channel else
                "TWITCH_CHANNEL is not explicitly configured. Current application source has a fallback, but diagnostics do not target an implicit channel for live API, IRC, or mutation checks."
            ),
        ),
        Check(
            "Configuration", "Broadcaster-token fallback", READY,
            "Broadcaster actions use DEVILMEDLAR_TWITCH_TOKEN only; the bot token is never substituted.",
        ),
    ]


def request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
    allow_empty: bool = False,
    timeout: float = 2.0,
    opener: Callable[..., Any] | None = None,
) -> HttpJsonResult:
    """Make a JSON request and retain the complete response body for the private report."""
    if form is not None and json_body is not None:
        raise ValueError("form and JSON bodies are mutually exclusive")
    data = None
    request_headers = dict(headers or {})
    if form is not None:
        data = urllib_parse.urlencode(dict(form)).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(dict(json_body), ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib_request.Request(url, data=data, headers=request_headers, method=method.upper())
    open_url = opener or urllib_request.urlopen
    def response_headers(response: Any) -> tuple[tuple[str, str], ...]:
        headers_object = getattr(response, "headers", None)
        try:
            return tuple((str(key), str(value)) for key, value in headers_object.items())
        except (AttributeError, TypeError, ValueError):
            _capture_failure(sys.exc_info()[1])
            return ()

    def read_body(response: Any) -> tuple[bytes, str | None]:
        try:
            return response.read(), None
        except IncompleteRead as exc:
            _capture_failure(sys.exc_info()[1])
            partial = exc.partial if isinstance(exc.partial, bytes) else bytes(exc.partial or b"")
            return partial, (
                "HTTP response body ended prematurely (IncompleteRead); the available partial bytes are retained, "
                "but the response is explicitly incomplete"
            )
        except (HTTPException, OSError, TimeoutError, ValueError) as exc:
            _capture_failure(sys.exc_info()[1])
            return b"", (
                _exception_evidence(exc)
            )

    def finish(
        status: int,
        body: bytes,
        headers_out: tuple[tuple[str, str], ...],
        read_error: str | None = None,
    ) -> HttpJsonResult:
        retained = body
        try:
            raw_body = retained.decode("utf-8")
            utf8 = True
        except UnicodeError:
            _capture_failure(sys.exc_info()[1])
            raw_body = "[NON-UTF8 RESPONSE BODY; HEX]\n" + retained.hex()
            utf8 = False
        payload: Any = None
        if utf8 and retained:
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                _capture_failure(sys.exc_info()[1])
                payload = None
        common = {
            "payload": payload,
            "raw_body": raw_body,
            "response_headers": headers_out,
            "body_truncated": read_error is not None,
            "body_bytes_observed": len(body),
        }
        if read_error:
            return HttpJsonResult(status, error=read_error, **common)
        if status < 200 or status >= 300:
            return HttpJsonResult(status, error="HTTP request was rejected", **common)
        if not retained and allow_empty:
            return HttpJsonResult(status, payload=None, raw_body="", response_headers=headers_out,
                                  body_bytes_observed=0)
        if not utf8 or payload is None:
            return HttpJsonResult(status, error="response was not valid UTF-8 JSON", **common)
        return HttpJsonResult(status, **common)

    try:
        with open_url(req, timeout=timeout) as response:
            raw_status = getattr(response, "status", None)
            status = int(response.getcode() if raw_status is None else raw_status)
            headers_out = response_headers(response)
            body, read_error = read_body(response)
        return finish(status, body, headers_out, read_error)
    except HTTPError as exc:
        _capture_failure(sys.exc_info()[1])
        try:
            headers_out = response_headers(exc)
            body, read_error = read_body(exc)
            return finish(int(exc.code), body, headers_out, read_error)
        finally:
            exc.close()
    except (URLError, OSError, TimeoutError, ValueError, HTTPException) as exc:
        _capture_failure(sys.exc_info()[1])
        return HttpJsonResult(
            None,
            error=(
                _exception_evidence(exc)
            ),
        )


def _safe_url(value: str) -> str:
    """Display an endpoint without userinfo, query strings, or fragments."""
    try:
        parsed = urllib_parse.urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return "(invalid endpoint; value withheld)"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            _capture_failure(sys.exc_info()[1])
            return "(invalid endpoint; value withheld)"
        display = urllib_parse.urlunsplit((parsed.scheme, f"{host}{port}", "", "", ""))
        if parsed.path and parsed.path != "/":
            display += " (custom path withheld)"
        return display
    except (TypeError, ValueError):
        _capture_failure(sys.exc_info()[1])
        return "(invalid endpoint; value withheld)"


def _is_loopback_url(value: str) -> bool:
    try:
        host = urllib_parse.urlsplit(value).hostname
        if not host:
            return False
        if host.casefold().rstrip(".") == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        _capture_failure(sys.exc_info()[1])
        return False


def resolve_ollama_base_url(environment: Mapping[str, str], root: Path = REPO_ROOT) -> str:
    module = _load_source_module(
        root / "MedlarTV" / "core" / "ollama_endpoint.py",
        "_medlar_diagnostics_ollama_endpoint",
    )
    return str(module.ollama_base_url(environment))


def selected_ollama_model(environment: Mapping[str, str]) -> str:
    # Match llm_brain.py exactly: a defined empty MODEL_NAME remains empty.
    if "MODEL_NAME" in environment:
        return str(environment["MODEL_NAME"])
    if "OLLAMA_MODEL" in environment:
        return str(environment["OLLAMA_MODEL"])
    return "llama3"


def _model_names(payload: Any) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return set()
    names: set[str] = set()
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        for key in ("name", "model"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return names


def _model_available(selected: str, names: set[str]) -> bool:
    if not selected:
        return False
    if selected in names:
        return True
    if ":" not in selected:
        return any(name.startswith(selected + ":") for name in names)
    return False


def check_ollama_executable(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., Any] = subprocess.run,
) -> list[Check]:
    executable = which("ollama")
    if not executable:
        return [Check(
            "Ollama", "Command-line executable", UNAVAILABLE,
            "The ollama executable was not found on PATH. This is independent of an already-running API endpoint.",
        )]
    try:
        completed = runner(
            [executable, "--version"], capture_output=True, text=True,
            timeout=5, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _capture_failure(sys.exc_info()[1])
        return [Check(
            "Ollama", "Command-line executable", UNAVAILABLE,
            "The ollama executable was found, but the read-only version command did not complete.",
        )]
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    version_match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+){0,2})(?!\d)", combined)
    version = version_match.group(1) if version_match else "unreported"
    if completed.returncode == 0 or version != "unreported":
        return [Check(
            "Ollama", "Command-line executable", READY,
            f"The executable reported client version {version} (exit status {completed.returncode}). "
            f"Exact stdout={json.dumps(completed.stdout or '', ensure_ascii=False)}; "
            f"exact stderr={json.dumps(completed.stderr or '', ensure_ascii=False)}. A running server is checked separately.",
        )]
    return [Check(
        "Ollama", "Command-line executable", UNAVAILABLE,
        f"The read-only version command exited with status {completed.returncode}; parsed client version {version}; "
        f"exact stdout={json.dumps(completed.stdout or '', ensure_ascii=False)}; "
        f"exact stderr={json.dumps(completed.stderr or '', ensure_ascii=False)}.",
    )]


def check_ollama_bind_alignment(
    environment: Mapping[str, str], launcher_mode: str, *, root: Path = REPO_ROOT,
) -> list[Check]:
    if launcher_mode == "twitch":
        return [Check(
            "Ollama", "Launcher bind/client alignment", SKIPPED,
            "Twitch-only mode does not launch Ollama; only readiness of the configured client endpoint is relevant.",
        )]
    try:
        module = _load_source_module(
            root / "MedlarTV" / "core" / "ollama_endpoint.py",
            "_medlar_diagnostics_ollama_alignment",
        )
        bind_values = {"OLLAMA_HOST": str(environment.get("OLLAMA_HOST", "127.0.0.1:11434"))}
        bind_target = tuple(module.ollama_probe_target(bind_values))
        client_target = tuple(module.ollama_probe_target(environment))
    except (OSError, ValueError, ImportError, AttributeError, TypeError):
        _capture_failure(sys.exc_info()[1])
        return [Check(
            "Ollama", "Launcher bind/client alignment", ERROR,
            "Bind/client targets could not be resolved; raw configured values were withheld.",
        )]
    def host_key(host: str) -> str:
        if host.casefold().rstrip(".") == "localhost":
            return "loopback"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            _capture_failure(sys.exc_info()[1])
            return host.casefold()
        return "loopback" if address.is_loopback else address.compressed

    canonical_bind = (host_key(str(bind_target[0])), int(bind_target[1]))
    canonical_client = (host_key(str(client_target[0])), int(client_target[1]))
    if canonical_bind == canonical_client:
        return [Check(
            "Ollama", "Launcher bind/client alignment", READY,
            "The launcher-owned Ollama bind target matches the endpoint used by Medlar clients.",
        )]
    return [Check(
        "Ollama", "Launcher bind/client alignment", UNAVAILABLE,
        "OLLAMA_HOST's launcher bind target differs from the effective client target selected through OLLAMA_URL precedence.",
    )]


def check_ollama(
    environment: Mapping[str, str],
    *,
    root: Path = REPO_ROOT,
    http_json: HttpJson = request_json,
    probe_services: bool = True,
) -> list[Check]:
    selected = selected_ollama_model(environment)
    try:
        base_url = resolve_ollama_base_url(environment, root)
    except (OSError, ValueError, ImportError, AttributeError):
        _capture_failure(sys.exc_info()[1])
        return [
            Check("Ollama", "Endpoint configuration", ERROR,
                  "The configured endpoint is invalid; its raw value was withheld."),
            Check("Ollama", "Service readiness", SKIPPED,
                  "The API was not probed because endpoint resolution failed."),
            Check("Ollama", "Selected model", SKIPPED,
                  "Model existence was not checked because endpoint resolution failed."),
        ]

    endpoint_check = Check(
        "Ollama", "Endpoint configuration", READY,
        f"Effective client endpoint: {_safe_url(base_url)} "
        "(OLLAMA_URL, then OLLAMA_HOST, then local default).",
    )
    if not probe_services:
        return [
            endpoint_check,
            Check("Ollama", "Service readiness", SKIPPED,
                  "Service probe disabled; no HTTP request was sent."),
            Check("Ollama", "Selected model", SKIPPED,
                  f"Service probe disabled; configured model is {selected or '(empty)'!r}."),
        ]
    if not _is_loopback_url(base_url):
        return [
            endpoint_check,
            Check("Ollama", "Service readiness", SKIPPED,
                  "The selected endpoint is not loopback; external service probes are disabled."),
            Check("Ollama", "Selected model", SKIPPED,
                  "The selected endpoint is not loopback; external service probes are disabled."),
        ]

    response = http_json("GET", f"{base_url.rstrip('/')}/api/tags", timeout=2.0)
    if response.status != 200 or response.error:
        return [
            endpoint_check,
            Check("Ollama", "Service readiness", UNAVAILABLE,
                  f"Local /api/tags returned HTTP {response.status or 'unavailable'} without usable JSON; no inference was run."),
            Check("Ollama", "Selected model", SKIPPED,
                  f"Model existence is unknown because the service probe failed; configured model is {selected or '(empty)'!r}."),
        ]
    if not isinstance(response.payload, dict) or not isinstance(response.payload.get("models"), list):
        return [
            endpoint_check,
            Check("Ollama", "Service readiness", ERROR,
                  "Local /api/tags returned HTTP 200 but did not contain a models list."),
            Check("Ollama", "Selected model", SKIPPED,
                  "Model existence was not inferred from a malformed models response."),
        ]
    names = _model_names(response.payload)
    if _model_available(selected, names):
        status = READY
        detail = f"Configured model {selected!r} exists in the local model list. No inference was run."
    else:
        status = UNAVAILABLE
        detail = f"Configured model {selected or '(empty)'!r} was not found in the local model list."
    return [
        endpoint_check,
        Check("Ollama", "Service readiness", READY,
              f"Local /api/tags returned HTTP 200 with {len(names)} named model entry/entries; no inference was run."),
        Check("Ollama", "Selected model", status, detail),
    ]


def check_ollama_launch_precondition(
    environment: Mapping[str, str],
    launcher_mode: str,
    *,
    root: Path = REPO_ROOT,
    service_ready: bool = False,
) -> list[Check]:
    """Report launcher-mode ownership semantics separately from API health."""
    if launcher_mode == "twitch":
        return [Check(
            "Ollama", "Launcher mode precondition",
            READY if service_ready else UNAVAILABLE,
            "Twitch-only mode attaches to an existing Ollama API; the read-only /api/tags readiness check passed."
            if service_ready else
            "Twitch-only mode requires an already-running Ollama API; the read-only /api/tags readiness check did not pass.",
        )]
    try:
        module = _load_source_module(
            root / "MedlarTV" / "core" / "ollama_endpoint.py",
            "_medlar_diagnostics_ollama_launch_precondition",
        )
        host, port = module.ollama_probe_target({
            "OLLAMA_HOST": str(environment.get("OLLAMA_HOST", "127.0.0.1:11434")),
        })
        host = str(host)
        port = int(port)
    except (OSError, ValueError, ImportError, AttributeError, TypeError):
        _capture_failure(sys.exc_info()[1])
        return [Check(
            "Ollama", "Launcher mode precondition", ERROR,
            "The launcher-owned bind target could not be resolved; configured raw values were withheld.",
        )]
    if host.casefold().rstrip(".") == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        _capture_failure(sys.exc_info()[1])
        address = None
    if address is None or not address.is_loopback:
        return [Check(
            "Ollama", "Launcher mode precondition", SKIPPED,
            "Fresh-launch bindability was not tested because OLLAMA_HOST does not resolve to a literal loopback target.",
        )]
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    candidate = socket.socket(family, socket.SOCK_STREAM)
    try:
        candidate.bind((host, port))
    except OSError:
        _capture_failure(sys.exc_info()[1])
        return [Check(
            "Ollama", "Launcher mode precondition", UNAVAILABLE,
            f"{launcher_mode} mode asks launcher.py to own the configured Ollama bind, but that loopback port is occupied. "
            "A healthy existing API does not satisfy this fresh-launch precondition.",
        )]
    finally:
        candidate.close()
    return [Check(
        "Ollama", "Launcher mode precondition", READY,
        f"{launcher_mode} mode asks launcher.py to own Ollama, and a temporary bind check found its loopback port available. "
        "The check can race and does not start Ollama.",
    )]


def _requirement_rows(path: Path) -> list[tuple[str, str | None]]:
    rows: list[tuple[str, str | None]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        return rows
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:==([^\s;]+))?", line)
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


def check_dependencies(root: Path = REPO_ROOT) -> list[Check]:
    path = root / "requirements.txt"
    rows = _requirement_rows(path)
    if not path.is_file():
        return [Check("Dependencies", "Application manifest", ERROR,
                      "requirements.txt is missing.")]
    if not rows:
        return [Check("Dependencies", "Application manifest", ERROR,
                      "requirements.txt could not be parsed.")]

    interpreter = launcher_python(root)
    if interpreter is None:
        return [Check(
            "Dependencies", "Launcher interpreter", UNAVAILABLE,
            "The repository .venv interpreter used by launcher.py is missing; package versions were not inferred from another Python.",
        )]

    checks: list[Check] = [Check(
        "Dependencies", "Launcher interpreter", READY,
        f"Checking packages beside {_relative(interpreter, root)}, the interpreter used by launcher.py.",
    )]
    for distribution, expected in rows:
        try:
            installed = _metadata_version_near_python(interpreter, distribution)
        except Exception:
            _capture_failure(sys.exc_info()[1])
            checks.append(Check(
                "Dependencies", distribution, ERROR,
                "Distribution metadata beside the launcher interpreter could not be read.",
            ))
            continue
        if installed is None:
            checks.append(Check(
                "Dependencies", distribution, UNAVAILABLE,
                f"Not installed beside the launcher interpreter; required version {expected or 'unspecified'}.",
            ))
            continue
        if expected is not None and installed != expected:
            checks.append(Check(
                "Dependencies", distribution, UNAVAILABLE,
                f"Installed {installed}; requirements.txt pins {expected}.",
            ))
        else:
            checks.append(Check(
                "Dependencies", distribution, READY,
                f"Installed {installed}." + (" Matches the pinned version." if expected else ""),
            ))
    return checks


def check_dependency_imports(root: Path = REPO_ROOT) -> list[Check]:
    modules = (
        ("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
        ("websockets", "websockets"), ("yaml", "PyYAML"),
        ("dotenv", "python-dotenv"), ("requests", "requests"),
        ("colorama", "colorama"), ("argostranslate", "argostranslate"),
        ("langdetect", "langdetect"), ("ddgs", "ddgs"),
        ("wikipediaapi", "Wikipedia-API"), ("PySide6", "PySide6"),
    )
    interpreter = launcher_python(root)
    aligned = interpreter is not None and _same_path(interpreter, Path(sys.executable))
    checks: list[Check] = []
    for module_name, distribution in modules:
        if not aligned:
            checks.append(Check(
                "Application environment", f"Import — {module_name}", SKIPPED,
                f"Import was not attempted because the running interpreter is not the launcher environment for {distribution}.",
            ))
            continue
        try:
            importlib.import_module(module_name)
        except Exception:
            _capture_failure(sys.exc_info()[1])
            checks.append(Check(
                "Application environment", f"Import — {module_name}", UNAVAILABLE,
                f"{distribution} metadata may exist, but importing {module_name} failed; exception details were withheld.",
            ))
        else:
            checks.append(Check(
                "Application environment", f"Import — {module_name}", READY,
                f"Imported {module_name} successfully under the launcher interpreter.",
            ))
    return checks


def check_configs(root: Path = REPO_ROOT) -> list[Check]:
    try:
        import yaml
    except ImportError:
        _capture_failure(sys.exc_info()[1])
        yaml = None

    checks: list[Check] = [Check(
        "Configuration",
        "Application YAML parser",
        READY if yaml is not None else UNAVAILABLE,
        (
            "PyYAML is importable in the running application interpreter."
            if yaml is not None else
            "PyYAML is not importable in the running interpreter; YAML files are inventoried but not parsed. "
            "The CLI normally relaunches under Medlar's .venv before this check."
        ),
    )]
    for label, relative, active in CONFIG_FILES:
        path = root / relative
        if not path.is_file():
            checks.append(Check(
                "Configuration", label, UNAVAILABLE if active else SKIPPED,
                f"Missing {_relative(path, root)}."
                + (" It is referenced by current source." if active else " No current Python reference was found."),
            ))
            continue
        if yaml is None:
            checks.append(Check("Configuration", label, SKIPPED,
                                f"{relative} exists; syntax was not checked because PyYAML is unavailable in the running interpreter."))
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle)
        except (OSError, UnicodeError, yaml.YAMLError):
            _capture_failure(sys.exc_info()[1])
            checks.append(Check("Configuration", label, ERROR,
                                f"{relative} is not readable valid UTF-8 YAML."))
            continue
        if not isinstance(payload, dict):
            checks.append(Check("Configuration", label, ERROR,
                                f"{relative} must contain a top-level mapping."))
        else:
            checks.append(Check("Configuration", label, READY,
                                f"{relative} exists and parses as a mapping."
                                + ("" if active else " No current Python reference was found; this is dormant compatibility inventory.")))

    preferred_emotions = root / "MedlarTV" / "config" / "emotions.yaml"
    fallback_emotions = root / "MedlarTV" / "data" / "emotions.yaml"
    active_emotions = preferred_emotions if preferred_emotions.is_file() else fallback_emotions
    active_relative = _relative(active_emotions, root)
    if not active_emotions.is_file():
        checks.append(Check(
            "Configuration", "Emotions (active)", UNAVAILABLE,
            "Neither MedlarTV/config/emotions.yaml nor the MedlarTV/data/emotions.yaml fallback exists.",
        ))
    elif yaml is None:
        checks.append(Check(
            "Configuration", "Emotions (active)", SKIPPED,
            f"{active_relative} is the selected path; syntax was not checked because PyYAML is unavailable in the running interpreter.",
        ))
    else:
        try:
            with active_emotions.open("r", encoding="utf-8") as handle:
                emotion_payload = yaml.safe_load(handle)
        except (OSError, UnicodeError, yaml.YAMLError):
            _capture_failure(sys.exc_info()[1])
            checks.append(Check(
                "Configuration", "Emotions (active)", ERROR,
                f"{active_relative} is the selected path but is not readable valid UTF-8 YAML.",
            ))
        else:
            if isinstance(emotion_payload, dict):
                checks.append(Check(
                    "Configuration", "Emotions (active)", READY,
                    f"{active_relative} is the selected path and parses as a mapping.",
                ))
            else:
                checks.append(Check(
                    "Configuration", "Emotions (active)", ERROR,
                    f"{active_relative} is the selected path and must contain a top-level mapping.",
                ))
    return checks


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def check_config_schemas(root: Path = REPO_ROOT) -> list[Check]:
    """Validate the shapes consumed by current source, without importing writers."""
    names = (
        "Effective app settings", "Commands", "Content filter", "Co-pilots",
        "Counters", "Link whitelist", "Personality", "Timers", "Emotions",
    )
    try:
        import yaml
    except ImportError:
        _capture_failure(sys.exc_info()[1])
        return [Check(
            "Configuration semantics", name, SKIPPED,
            "PyYAML is unavailable; consumer schema was not checked.",
        ) for name in names]

    def load(relative: str) -> Any:
        from MedlarTV.config_contract import parse_mapping
        return parse_mapping((root / relative).read_bytes())

    checks: list[Check] = []
    settings_path = root / "MedlarTV" / "config" / "app_settings.yaml"
    from MedlarTV.config_contract import FEATURE_DEFAULTS, parse_features, parse_emotions
    settings_keys = set(FEATURE_DEFAULTS)
    try:
        parse_features(settings_path.read_bytes())
        settings_ok = True
    except (OSError, ValueError):
        _capture_failure(sys.exc_info()[1])
        settings_ok = False
    checks.append(Check(
        "Configuration semantics", "Effective app settings",
        READY if settings_ok else ERROR,
        (
            f"launcher.py injects MEDLARTV_ROOT at the repository root, selecting {_relative(settings_path, root)}; "
            f"all {len(settings_keys)} known flags are booleans when present and missing keys use source defaults."
            if settings_ok else
            "The launcher-selected app_settings.yaml is unreadable, not a mapping, or contains a non-boolean known flag."
        ),
    ))

    validators: tuple[tuple[str, str, Callable[[Any], bool], str], ...] = (
        (
            "Commands", "MedlarTV/config/commands.yaml",
            lambda p: isinstance(p, dict) and isinstance(p.get("commands"), dict)
            and all(isinstance(key, str) and isinstance(value, dict)
                    for key, value in p["commands"].items()),
            "commands must be a mapping of command names to mappings",
        ),
        (
            "Content filter", "MedlarTV/config/content_filter.yaml",
            lambda p: isinstance(p, dict) and _string_list(p.get("blocked_words"))
            and _string_list(p.get("blocked_topics")),
            "blocked_words and blocked_topics must both be string lists",
        ),
        (
            "Co-pilots", "MedlarTV/config/copilots.yaml",
            lambda p: isinstance(p, dict) and isinstance(p.get("copilots"), dict)
            and _string_list(p["copilots"].get("active")),
            "copilots.active must be a string list",
        ),
        (
            "Counters", "MedlarTV/config/counters.yaml",
            lambda p: isinstance(p, dict) and isinstance(p.get("counters"), dict)
            and all(isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
                    for key, value in p["counters"].items()),
            "counters must map text names to integer values",
        ),
        (
            "Link whitelist", "MedlarTV/config/link_whitelist.yaml",
            lambda p: isinstance(p, dict) and _string_list(p.get("whitelist")),
            "whitelist must be a string list",
        ),
        (
            "Personality", "MedlarTV/config/personality.yaml",
            lambda p: isinstance(p, dict) and isinstance(p.get("personality"), dict)
            and isinstance(p["personality"].get("name"), str)
            and isinstance(p["personality"].get("tone"), str)
            and _string_list(p["personality"].get("trigger_keywords")),
            "personality requires text name/tone and a string trigger_keywords list",
        ),
        (
            "Timers", "MedlarTV/config/timers.yaml",
            lambda p: isinstance(p, dict) and isinstance(p.get("timers"), list)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("message"), str)
                and item.get("type") in {"time", "chats"}
                and isinstance(item.get(
                    "interval_minutes" if item.get("type") == "time" else "interval_chats"
                ), int)
                and not isinstance(item.get(
                    "interval_minutes" if item.get("type") == "time" else "interval_chats"
                ), bool)
                and item.get(
                    "interval_minutes" if item.get("type") == "time" else "interval_chats"
                ) > 0
                for item in p["timers"]
            ),
            "timers must contain typed rules with text IDs/messages and positive integer intervals",
        ),
    )
    for name, relative, validator, contract in validators:
        try:
            payload = load(relative)
            valid = bool(validator(payload))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError, KeyError, TypeError):
            _capture_failure(sys.exc_info()[1])
            valid = False
        checks.append(Check(
            "Configuration semantics", name, READY if valid else ERROR,
            f"{relative} matches the current consumer contract: {contract}."
            if valid else f"{relative} does not match the current consumer contract: {contract}.",
        ))

    preferred = root / "MedlarTV" / "config" / "emotions.yaml"
    emotions_relative = (
        "MedlarTV/config/emotions.yaml" if preferred.is_file()
        else "MedlarTV/data/emotions.yaml"
    )
    try:
        _, inactive = parse_emotions((root / emotions_relative).read_bytes())
        emotions_ok = True
    except (OSError, ValueError):
        _capture_failure(sys.exc_info()[1])
        emotions_ok, inactive = False, ()
    checks.append(Check(
        "Configuration semantics", "Emotions", READY if emotions_ok else ERROR,
        f"{emotions_relative} passes the shared emotion parser. "
        + ("Flat nested values remain inactive under approved D10; effective defaults are preserved." if inactive else "Existing wrapped/scalar overrides remain effective.")
        if emotions_ok else
        f"{emotions_relative} is missing, unreadable, or fails the shared emotion contract.",
    ))
    return checks


def check_event_configuration(root: Path = REPO_ROOT) -> list[Check]:
    path = root / "MedlarTV" / "config" / "events.yaml"
    if not path.is_file():
        return [Check("Configuration semantics", "Events structure", UNAVAILABLE,
                      "events.yaml is missing.")]
    try:
        import yaml
    except ImportError:
        _capture_failure(sys.exc_info()[1])
        return [Check("Configuration semantics", "Events structure", SKIPPED,
                      "PyYAML is unavailable in the running interpreter.")]
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError):
        _capture_failure(sys.exc_info()[1])
        return [Check("Configuration semantics", "Events structure", ERROR,
                      "events.yaml could not be parsed as UTF-8 YAML.")]
    if not isinstance(payload, dict):
        return [Check("Configuration semantics", "Events structure", ERROR,
                      "events.yaml must contain a top-level mapping.")]
    events = payload.get("events")
    ignored = payload.get("ignored_users")
    checks = [Check(
        "Configuration semantics", "Events structure",
        READY if isinstance(events, list) and isinstance(ignored, list) else ERROR,
        (
            f"Found {len(events)} event rule(s) and {len(ignored)} ignored-user entry/entries."
            if isinstance(events, list) and isinstance(ignored, list) else
            "Both events and ignored_users must be lists."
        ),
    )]
    if not isinstance(events, list):
        return checks
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            checks.append(Check(
                "Configuration semantics", f"Event rule #{index}", ERROR,
                "Rule must be a mapping.",
            ))
            continue
        name = str(event.get("name") or f"rule-{index}")
        effects = event.get("effects")
        if not isinstance(effects, list):
            checks.append(Check(
                "Configuration semantics", f"Event effects — {name}", ERROR,
                "effects must be a list.",
            ))
            continue
        for effect_index, effect in enumerate(effects, start=1):
            if not isinstance(effect, dict) or not _nonempty(effect.get("type")):
                checks.append(Check(
                    "Configuration semantics", f"Event effect — {name} #{effect_index}", UNAVAILABLE,
                    "Effect type is blank or invalid; no event action was run.",
                ))
                continue
            effect_type = str(effect["type"]).strip()
            if effect_type != "play_sound":
                checks.append(Check(
                    "Configuration semantics", f"Event effect — {name} #{effect_index}", READY,
                    f"Effect type {effect_type!r} is structurally present; it was not functionally triggered.",
                ))
                continue
            raw_path = effect.get("path")
            filename = PureWindowsPath(str(raw_path or "")).name or "(blank filename)"
            configured = Path(str(raw_path)) if _nonempty(raw_path) else None
            configured_exists = bool(configured and configured.is_file())
            repository_candidate = root / "Sounds" / filename
            checks.append(Check(
                "Configuration semantics", f"Sound asset - {name} / {filename}",
                READY if configured_exists else UNAVAILABLE,
                (
                    "The configured sound path names an existing file. Media contents were not opened."
                    if configured_exists else
                    "The configured sound path is missing. "
                    + ("A same-named file exists in this checkout's Sounds directory, but current configuration does not point to it. "
                       if repository_candidate.is_file() else "No same-named repository Sounds asset was found. ")
                    + "No media contents were opened."
                ),
            ))
    checks.append(Check(
        "Configuration semantics", "Event sound playback", SKIPPED,
        "Sound rules and target files were inspected statically, but no audio was played; file presence does not prove device or playback behavior.",
    ))
    return checks


def check_source_layout(root: Path = REPO_ROOT) -> list[Check]:
    missing = [relative for relative in REQUIRED_SOURCE_FILES if not (root / relative).is_file()]
    if missing:
        return [Check("Local project", "Current source layout", UNAVAILABLE,
                      "Missing: " + ", ".join(missing))]
    return [Check("Local project", "Current source layout", READY,
                  "Current launcher, credential, Twitch, Ollama, translation, and memory sources are present.")]


def check_source_syntax(root: Path = REPO_ROOT) -> list[Check]:
    checks: list[Check] = []
    for relative in REQUIRED_SOURCE_FILES:
        if not relative.endswith(".py"):
            continue
        path = root / relative
        if not path.is_file():
            checks.append(Check(
                "Component source readiness", f"Syntax - {relative}", UNAVAILABLE,
                "Source file is missing; it was not imported or executed.",
            ))
            continue
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError):
            _capture_failure(sys.exc_info()[1])
            checks.append(Check(
                "Component source readiness", f"Syntax - {relative}", ERROR,
                "Source is not readable UTF-8 Python syntax; exception details were withheld.",
            ))
        else:
            checks.append(Check(
                "Component source readiness", f"Syntax - {relative}", READY,
                "Parsed with ast.parse without importing or executing the module.",
            ))
    return checks


def _source_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def check_component_contracts(root: Path = REPO_ROOT) -> list[Check]:
    contracts = (
        ("Launcher entrypoint", "launcher.py", {"_run_services", "start_process", "main"}),
        ("Core entrypoint", "MedlarTV/core/main.py", {"initialize_systems", "main"}),
        ("Bridge entrypoint", "MedlarTV/avatar/bridge/server.py", {"handler", "run_server"}),
        ("Command Center GUI", "MedlarTV/app/medlar_app.py", {"ControlCenter", "main"}),
    )
    checks: list[Check] = []
    for label, relative, required in contracts:
        try:
            definitions = _source_definitions(root / relative)
        except (OSError, UnicodeError, SyntaxError):
            _capture_failure(sys.exc_info()[1])
            checks.append(Check(
                "Static component contracts", label, ERROR,
                f"{relative} could not be parsed; no component process was started.",
            ))
            continue
        missing = sorted(required - definitions)
        checks.append(Check(
            "Static component contracts", label, READY if not missing else UNAVAILABLE,
            f"{relative} contains the expected entrypoint definitions; this is static evidence only and no process was started."
            if not missing else
            f"{relative} is missing expected definitions: {', '.join(missing)}; no process was started.",
        ))
    try:
        settings_source = (root / "MedlarTV" / "core" / "settings.py").read_text(encoding="utf-8")
        update_block = settings_source.split("def update_settings", 1)[1].split("def ", 1)[0]
        dormant_deadlock = (
            "threading.Lock()" in settings_source
            and "with _lock" in update_block
            and "get_settings()" in update_block
        )
    except (OSError, UnicodeError, IndexError):
        _capture_failure(sys.exc_info()[1])
        dormant_deadlock = False
    checks.append(Check(
        "Static component contracts", "Dormant settings update route",
        UNAVAILABLE if dormant_deadlock else SKIPPED,
        "settings.update_settings() acquires a non-reentrant Lock and then calls get_settings(), which acquires the same lock; the dormant POST route would block if served. No write or HTTP call was attempted."
        if dormant_deadlock else
        "The known nested non-reentrant settings-lock pattern was not confirmed; the dormant route was not invoked.",
    ))
    try:
        console_source = (root / "MedlarTV" / "avatar_client" / "console_client.py").read_text(encoding="utf-8")
        dynamic_console = 'os.getenv("BRIDGE_URL"' in console_source
        hardcoded_console = not dynamic_console and (
            "ws://localhost:8765" in console_source or "ws://127.0.0.1:8765" in console_source
        )
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        hardcoded_console = False
    checks.append(Check(
        "Static component contracts", "Avatar console dynamic-port compatibility",
        UNAVAILABLE if hardcoded_console else READY,
        "console_client.py hardcodes bridge port 8765 while launcher.py may select 8765-8775; the optional console will miss a bridge launched on a later port. No client was started."
        if hardcoded_console else
        "The known unconditional port-8765 console pattern was not found; environment-based URL selection is static evidence only and no client was started.",
    ))
    try:
        tree = ast.parse((root / "MedlarTV/core/main.py").read_text(encoding="utf-8"))
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        publisher_wired = {"start_publisher", "set_mood_observer"} <= calls
    except (OSError, UnicodeError, SyntaxError):
        _capture_failure(sys.exc_info()[1])
        publisher_wired = False
    checks.extend([
        Check(
            "Component runtime readiness", "Core runtime boot", SKIPPED,
            "Core main() was not executed: it loads live credentials, initializes state, opens Twitch IRC, and enters a long-running loop.",
        ),
        Check(
            "Component runtime readiness", "Command Center runtime boot", SKIPPED,
            "ControlCenter was not instantiated: its constructor schedules service startup and GUI lifecycle work.",
        ),
        Check(
            "Component runtime readiness", "Core HTTP API", SKIPPED,
            "FastAPI route objects exist in core/main.py, but launcher.py executes core/main.py directly and does not serve them with uvicorn; CORE_URL is not treated as a live dependency.",
        ),
        Check(
            "Component runtime readiness", "Core-to-Bridge event publishing", READY if publisher_wired else SKIPPED,
            "Current source contains publisher/observer wiring; this is static evidence only, authentication/delivery were not exercised." if publisher_wired else "Publisher wiring was not confirmed; runtime delivery remains unverified.",
        ),
    ])
    return checks


def check_launcher_controls(
    environment: Mapping[str, str], root: Path = REPO_ROOT,
) -> list[Check]:
    try:
        source = (root / "launcher.py").read_text(encoding="utf-8")
        wired = all(marker in source for marker in (
            "MEDLARTV_OWNER_PID", "MEDLARTV_STOP_FILE", "_stop_requested",
        ))
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        wired = False
    checks = [Check(
        "Launcher controls", "Owner/stop control wiring", READY if wired else ERROR,
        "launcher.py contains owner-process and stop-file checks; diagnostics never signal, kill, create, or delete these controls."
        if wired else "Launcher control wiring could not be confirmed from source.",
    )]
    owner = str(environment.get("MEDLARTV_OWNER_PID", "")).strip()
    if not owner:
        checks.append(Check(
            "Launcher controls", "MEDLARTV_OWNER_PID", SKIPPED,
            "Not set for this diagnostic process; owner liveness was not tested.",
        ))
    else:
        valid = owner.isdigit() and int(owner) > 0
        checks.append(Check(
            "Launcher controls", "MEDLARTV_OWNER_PID", READY if valid else UNAVAILABLE,
            "Configured value has valid positive-PID syntax; the process was not opened or signaled."
            if valid else "Configured value is not valid positive-PID syntax; its value was withheld.",
        ))
    stop_value = str(environment.get("MEDLARTV_STOP_FILE", "")).strip()
    if not stop_value:
        checks.append(Check(
            "Launcher controls", "MEDLARTV_STOP_FILE", SKIPPED,
            "Not set for this diagnostic process; no stop marker was read, created, or deleted.",
        ))
    else:
        try:
            stop_exists = Path(stop_value).is_file()
        except OSError:
            _capture_failure(sys.exc_info()[1])
            stop_exists = True
        checks.append(Check(
            "Launcher controls", "MEDLARTV_STOP_FILE",
            UNAVAILABLE if stop_exists else READY,
            "The configured stop marker exists, so launcher.py would request shutdown; the path and contents were withheld and unchanged."
            if stop_exists else
            "The configured stop marker is absent; the path value was withheld and no marker was created.",
        ))
    return checks


def check_project_inventory(root: Path = REPO_ROOT) -> list[Check]:
    """Retain the original setup inventory without making namespace markers blockers."""
    checks: list[Check] = []
    for label, relative in PROJECT_DIRECTORIES:
        path = root / relative
        checks.append(Check(
            "Setup path inventory", f"Directory — {label}",
            READY if path.is_dir() else UNAVAILABLE,
            f"{relative} {'exists' if path.is_dir() else 'is missing'}.",
        ))
    for relative in PACKAGE_MARKERS:
        path = root / relative
        checks.append(Check(
            "Setup path inventory", f"Package marker — {relative}",
            READY if path.is_file() else SKIPPED,
            (
                f"{relative} exists."
                if path.is_file() else
                f"{relative} is absent; modern namespace packages can work without a marker, so absence alone is not a readiness failure."
            ),
        ))
    return checks


def check_legacy_setup_claims(root: Path = REPO_ROOT) -> list[Check]:
    main_source = root / "MedlarTV" / "core" / "main.py"
    launcher_source = root / "launcher.py"
    try:
        main_text = main_source.read_text(encoding="utf-8")
        launcher_text = launcher_source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        return [Check(
            "Setup topology", "Legacy port claims", ERROR,
            "Current entry-point sources could not be inspected.",
        )]
    core_listener = "uvicorn.run" in main_text or "uvicorn " in launcher_text
    free_bridge_port = None
    for port in range(8765, 8776):
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candidate.bind(("0.0.0.0", port))
        except OSError:
            _capture_failure(sys.exc_info()[1])
            pass
        else:
            free_bridge_port = port
            candidate.close()
            break
        finally:
            try:
                candidate.close()
            except OSError:
                _capture_failure(sys.exc_info()[1])
                pass
    return [
        Check(
            "Setup topology", "CORE_URL / FastAPI listener",
            UNAVAILABLE if core_listener else SKIPPED,
            (
                "A current uvicorn launch pattern was found; listener readiness still requires a protocol check."
                if core_listener else
                "CORE_URL is not consumed and launcher.py starts core/main.py directly without uvicorn; the old verifier's claimed FastAPI-port check was not a real current check."
            ),
        ),
        Check(
            "Setup topology", "Avatar bridge port",
            READY if "_find_free_bridge_port(bridge_host)" in launcher_text else SKIPPED,
            "launcher.py selects a candidate port in 8765-8775 and generates BRIDGE_URL at runtime; an exhausted range skips the optional bridge and console. Selection is not a port reservation.",
        ),
        Check(
            "Setup topology", "Avatar bridge range capacity",
            READY if free_bridge_port is not None else UNAVAILABLE,
            (
                f"A temporary bind test found port {free_bridge_port} available in 8765–8775; launcher selection can still race with another process."
                if free_bridge_port is not None else
                "No port in launcher.py's 8765–8775 bridge range was bindable during this check."
            ),
        ),
        Check(
            "Setup topology", "Commands URL port",
            SKIPPED,
            "COMMANDS_PORT is used only to compose a displayed command-catalog URL; current source does not start a server on that port.",
        ),
    ]


async def _bridge_handshake(session: Any) -> bool:
    """Use an existing subscriber capability; authenticate only, never register/publish."""
    import websockets
    auth = _load_source_module(REPO_ROOT / "MedlarTV/avatar/bridge/auth.py", "_medlar_diagnostics_avatar_auth")
    async with websockets.connect(session.url, open_timeout=0.35, close_timeout=0.2,
                                  ping_interval=None, max_size=64 * 1024,
                                  logger=auth.transport_logger()) as websocket:
        await auth.authenticate_client(websocket, session)
    return True


def _default_bridge_probe(session: Any) -> bool:
    return bool(asyncio.run(_bridge_handshake(session)))


def check_bridge_readiness(
    *, probe_services: bool = True, probe: Callable[[Any], bool] = _default_bridge_probe,
    session_context: Mapping[str, Any] | None = None, root: Path = REPO_ROOT,
) -> list[Check]:
    if not probe_services:
        return [Check("Component connections", "Avatar bridge handshake", SKIPPED,
                      "Service probes are disabled; no WebSocket connection was opened.")]
    if session_context is None:
        return [Check("Component connections", "Avatar bridge handshake", UNAVAILABLE,
                      "Current bridge requires an existing per-launch subscriber capability delivered by private pipe. This standalone diagnostic has no valid session context; no capability was generated, read from a secret source or guessed, and no unauthenticated greeting probe was sent.")]
    try:
        auth = _load_source_module(root / "MedlarTV/avatar/bridge/auth.py", "_medlar_diagnostics_avatar_auth")
        session = auth.parse_bootstrap(dict(session_context), "subscriber")
        ready = probe(session)
    except Exception as exc:
        _capture_failure(sys.exc_info()[1])
        _capture_failure(exc, "Avatar bridge authentication")
        return [Check("Component connections", "Avatar bridge handshake", UNAVAILABLE,
                      f"Existing session authentication did not pass: {type(exc).__name__}: {exc}; traceback is in this private report.")]
    return [Check("Component connections", "Avatar bridge handshake", READY if ready is True else UNAVAILABLE,
                  "Existing subscriber session authenticated with matching role/channel. No register, publish or application frame was sent." if ready is True else
                  "Existing subscriber session did not authenticate; no application frame was sent.")]


def _nearest_existing_directory(path: Path) -> Path | None:
    candidate = path
    while not candidate.is_dir():
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return candidate


def transient_write_probe(directory: Path) -> tuple[bool, bool]:
    """Temporarily create the exact path, probe it, and remove only probe-created artifacts."""
    source: Path | None = None
    target: Path | None = None
    created_directories: list[Path] = []
    success = False
    cleanup_ok = True
    try:
        if not directory.is_dir():
            missing: list[Path] = []
            candidate = directory
            while not candidate.exists():
                missing.append(candidate)
                if candidate.parent == candidate:
                    break
                candidate = candidate.parent
            directory.mkdir(parents=True, exist_ok=True)
            created_directories = list(reversed(missing))
        descriptor, raw_source = tempfile.mkstemp(
            prefix=".medlar-diagnostics-write-", suffix=".tmp", dir=str(directory),
        )
        source = Path(raw_source)
        target = source.with_suffix(".replace.tmp")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"medlar diagnostics write probe\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(source, target)
        source = None
        target.unlink()
        target = None
        success = True
    except OSError:
        _capture_failure(sys.exc_info()[1])
        success = False
    finally:
        for candidate in (source, target):
            if candidate is not None:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    _capture_failure(sys.exc_info()[1])
                    cleanup_ok = False
        for created in reversed(created_directories):
            try:
                created.rmdir()
            except OSError:
                _capture_failure(sys.exc_info()[1])
                cleanup_ok = False
    return success, cleanup_ok


def check_runtime_writability(
    environment: Mapping[str, str],
    root: Path = REPO_ROOT,
    *,
    enabled: bool = True,
    probe: Callable[[Path], tuple[bool, bool]] = transient_write_probe,
) -> list[Check]:
    profile, _profile_display = _display_profile_path(environment)
    targets = (
        ("Logs/report parent", root / "logs"),
        ("Mood-memory parent", root),
        ("Mutable configuration parent", root / "MedlarTV" / "config"),
        ("Session parent", root / "db" / "sessions"),
        ("Viewer-profile parent", profile.parent),
        ("GUI stop-marker temporary parent", Path(tempfile.gettempdir())),
    )
    checks: list[Check] = []
    for name, intended in targets:
        if not enabled:
            checks.append(Check(
                "Runtime path writability", name, SKIPPED,
                "Transient write probes are disabled; no file was created.",
            ))
            continue
        try:
            success, cleanup_ok = probe(intended)
        except Exception:
            _capture_failure(sys.exc_info()[1])
            success, cleanup_ok = False, True
        if not cleanup_ok:
            checks.append(Check(
                "Runtime path writability", name, ERROR,
                "Disposable probe cleanup could not be confirmed; inspect the target parent for a .medlar-diagnostics-write-* temporary file.",
            ))
        elif success:
            checks.append(Check(
                "Runtime path writability", name, READY,
                "The exact runtime directory was used (temporarily created and removed when absent); a unique disposable file was created, flushed, atomically replaced, and deleted; no existing file was opened.",
            ))
        else:
            checks.append(Check(
                "Runtime path writability", name, UNAVAILABLE,
                "The disposable create/flush/replace/delete sequence did not complete; exception details and paths outside the repository were withheld.",
            ))
    return checks


def check_twitch_source_compatibility(root: Path = REPO_ROOT) -> list[Check]:
    """Flag known current-source compatibility boundaries without live calls."""
    try:
        commands = (root / "MedlarTV/core/command_handlers.py").read_text(encoding="utf-8")
        listener = (root / "MedlarTV/tools/twitch_listener.py").read_text(encoding="utf-8")
        moderation = (root / "MedlarTV/core/moderation.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        return [Check("Twitch source compatibility", "Static source review", ERROR,
                      "One or more relevant source files could not be read.")]

    checks: list[Check] = []
    if "/helix/users/follows" in commands:
        checks.append(Check(
            "Twitch source compatibility", "Followage command", UNAVAILABLE,
            "The implementation still calls the removed /helix/users/follows path. "
            "This is a code/API compatibility issue, not proof of bad app credentials; no new scope is requested.",
        ))
    else:
        checks.append(Check("Twitch source compatibility", "Followage command", READY,
                            "The legacy /helix/users/follows path was not found."))

    if '"https://api.twitch.tv/helix/chat/emotes"' in listener and '"emote_set_id"' in listener:
        checks.append(Check(
            "Twitch source compatibility", "Emote-set lookup", UNAVAILABLE,
            "The current source sends emote_set_id to /helix/chat/emotes instead of the emote-set endpoint. "
            "It also pairs the broadcaster user token with APP_TWITCH_CLIENT_ID, which can fail when they belong to different OAuth clients. "
            "This is a code/API compatibility issue, not evidence that a scope is invalid or missing.",
        ))
    else:
        checks.append(Check("Twitch source compatibility", "Emote-set lookup", READY,
                            "The known legacy emote-set call pattern was not found."))

    irc_commands = any(marker in moderation for marker in (":/timeout", ":/ban", ":/delete", ":/shoutout"))
    if irc_commands:
        checks.append(Check(
            "Twitch source compatibility", "IRC moderation fallbacks", UNAVAILABLE,
            "The implementation sends moderation/shoutout slash commands through IRC. "
            "Scope validation cannot prove these work with recent Twitch IRC; no replacement permission is requested.",
        ))
    else:
        checks.append(Check("Twitch source compatibility", "IRC moderation fallbacks", READY,
                            "The known IRC slash-command fallback pattern was not found."))
    privmsg_gate = listener.find("if username is None or message is None or source_channel is None")
    raid_dispatch = listener.find("raid_info = detect_raid(raw)")
    if 0 <= privmsg_gate < raid_dispatch:
        checks.append(Check(
            "Twitch source compatibility", "USERNOTICE event dispatch", UNAVAILABLE,
            "The listener returns when PRIVMSG parsing yields no user/message/channel before raid, subscription, and redemption detectors run. "
            "Those USERNOTICE behaviors are not functionally reachable through this dispatcher; no live event was injected.",
        ))
    else:
        checks.append(Check(
            "Twitch source compatibility", "USERNOTICE event dispatch", READY,
            "The known pre-detector PRIVMSG-only return ordering was not found; live event behavior remains separately UNTESTED.",
        ))
    return checks


def _libretranslate_home(environment: Mapping[str, str], root: Path) -> Path:
    configured = environment.get("LIBRETRANSLATE_HOME")
    if _nonempty(configured):
        candidate = Path(str(configured)).expanduser()
        return candidate if candidate.is_absolute() else root / candidate
    local = root / ".libretranslate"
    legacy = Path("C:/LibreTranslate")
    return local if local.is_dir() or not legacy.is_dir() else legacy


def _libretranslate_python(
    environment: Mapping[str, str], home: Path, root: Path = REPO_ROOT,
) -> tuple[Path | None, str]:
    configured = environment.get("LIBRETRANSLATE_PYTHON")
    if _nonempty(configured):
        candidate = Path(str(configured)).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return (candidate if candidate.is_file() else None, "configured override")
    for candidate in (
        home / ".venv" / "Scripts" / "python.exe",
        home / "venv" / "Scripts" / "python.exe",
        home / ".venv" / "bin" / "python",
        home / "venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate, "isolated service environment"
    found = shutil.which("python")
    return (Path(found), "PATH fallback") if found else (None, "not found")


def _canonical_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _metadata_version_near_python(python: Path, distribution: str) -> str | None:
    """Read nearby dist-info metadata without executing the configured interpreter."""
    try:
        if python.resolve() == Path(sys.executable).resolve():
            return importlib.metadata.version(distribution)
    except (OSError, importlib.metadata.PackageNotFoundError):
        _capture_failure(sys.exc_info()[1])
        pass

    sites: list[Path] = []
    if python.parent.name.casefold() in {"scripts", "bin"}:
        root = python.parent.parent
        sites.append(root / "Lib" / "site-packages")
        try:
            sites.extend((root / "lib").glob("python*/site-packages"))
        except OSError:
            _capture_failure(sys.exc_info()[1])
            pass
    wanted = _canonical_distribution(distribution)
    for site in sites:
        try:
            metadata_files = list(site.glob("*.dist-info/METADATA"))
        except OSError:
            _capture_failure(sys.exc_info()[1])
            continue
        for metadata_file in metadata_files:
            try:
                name = None
                version = None
                with metadata_file.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if line.startswith("Name: "):
                            name = line[6:].strip()
                        elif line.startswith("Version: "):
                            version = line[9:].strip()
                        if name and version:
                            break
                if name and _canonical_distribution(name) == wanted:
                    return version
            except OSError:
                _capture_failure(sys.exc_info()[1])
                continue
    return None


def _libretranslate_base_url(value: str) -> str:
    raw = value.rstrip("/")
    if raw.endswith("/translate"):
        raw = raw[:-len("/translate")]
    parsed = urllib_parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid service endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in endpoint")
    if parsed.query or parsed.fragment:
        raise ValueError("query or fragment in endpoint")
    _ = parsed.port
    return raw


def check_libretranslate(
    environment: Mapping[str, str],
    *,
    root: Path = REPO_ROOT,
    http_json: HttpJson = request_json,
    probe_services: bool = True,
    runner: Callable[..., Any] = subprocess.run,
) -> list[Check]:
    checks: list[Check] = []
    home = _libretranslate_home(environment, root)
    python, source = _libretranslate_python(environment, home, root)
    manifest_rows = _requirement_rows(root / "requirements-libretranslate.txt")
    if not home.is_dir():
        checks.append(Check("LibreTranslate", "Separate environment", UNAVAILABLE,
                            f"Service home {_relative(home, root)} does not exist."))
    elif python is None:
        checks.append(Check("LibreTranslate", "Separate environment", UNAVAILABLE,
                            f"Service home exists, but its {source} interpreter is unavailable."))
    else:
        problems: list[str] = []
        installed_rows: list[str] = []
        for distribution, expected in manifest_rows:
            try:
                installed = _metadata_version_near_python(python, distribution)
            except Exception:
                _capture_failure(sys.exc_info()[1])
                problems.append(f"{distribution} metadata unreadable")
                continue
            if installed is None:
                problems.append(f"{distribution} metadata missing")
            elif expected is not None and installed != expected:
                problems.append(f"{distribution} {installed} != {expected}")
            else:
                installed_rows.append(f"{distribution} {installed}")
        if not manifest_rows:
            problems.append("requirements-libretranslate.txt could not be parsed")
        if problems:
            checks.append(Check(
                "LibreTranslate", "Separate environment", UNAVAILABLE,
                f"Interpreter found via {source}; " + "; ".join(problems) + ". Nothing was imported or installed.",
            ))
        elif source == "PATH fallback":
            checks.append(Check(
                "LibreTranslate", "Separate environment", UNAVAILABLE,
                "The launcher PATH fallback has matching packages, but it is not a dedicated LibreTranslate environment. Nothing was imported or installed.",
            ))
        else:
            checks.append(Check(
                "LibreTranslate", "Separate environment", READY,
                f"Interpreter found via {source}; " + ", ".join(installed_rows)
                + " match the separate manifest. Nothing was imported or installed.",
            ))

    if python is None:
        checks.append(Check(
            "LibreTranslate", "Selected interpreter runnable", SKIPPED,
            "No selected LibreTranslate interpreter was available to execute a bounded metadata-only command.",
        ))
    else:
        try:
            completed = runner(
                [str(python), "-I", "-c", "import sys; raise SystemExit(0)"],
                capture_output=True, text=True, timeout=5, check=False, shell=False,
            )
            runnable = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _capture_failure(sys.exc_info()[1])
            runnable = False
        checks.append(Check(
            "LibreTranslate", "Selected interpreter runnable",
            READY if runnable else UNAVAILABLE,
            "The exact selected interpreter completed a bounded isolated metadata-only command; output was discarded and no service was started."
            if runnable else
            "The exact selected interpreter did not complete a bounded isolated metadata-only command; output/exception text was withheld and no service was started.",
        ))

    endpoint = str(environment.get("LIBRETRANSLATE_URL", "http://127.0.0.1:5000/translate"))
    try:
        endpoint_parts = urllib_parse.urlsplit(endpoint)
        launcher_owned = (
            endpoint_parts.scheme == "http"
            and (endpoint_parts.hostname or "").casefold().rstrip(".") in {
                "localhost", "127.0.0.1",
            }
            and endpoint_parts.path.rstrip("/") == "/translate"
            and not endpoint_parts.query and not endpoint_parts.fragment
        )
    except (TypeError, ValueError):
        _capture_failure(sys.exc_info()[1])
        launcher_owned = False
    checks.append(Check(
        "LibreTranslate", "Launcher endpoint ownership",
        READY if launcher_owned else SKIPPED,
        "Endpoint is the exact local HTTP /translate form that launcher.py may own and start."
        if launcher_owned else
        "Endpoint is not the exact launcher-owned local HTTP /translate form; diagnostics will not infer that launcher.py starts it.",
    ))
    update_models = str(environment.get("LIBRETRANSLATE_UPDATE_MODELS", "")).strip().casefold()
    checks.append(Check(
        "LibreTranslate", "Model update mode", SKIPPED,
        "Configured model-update flag is enabled, but diagnostics never download or update language models."
        if update_models in {"1", "true", "yes", "on"} else
        "Model updates are not requested by the effective flag; diagnostics never download or update language models.",
    ))
    try:
        base_url = _libretranslate_base_url(endpoint)
    except (TypeError, ValueError):
        _capture_failure(sys.exc_info()[1])
        checks.append(Check("LibreTranslate", "Service readiness", ERROR,
                            "The configured endpoint is invalid; its raw value was withheld."))
        checks.append(Check("LibreTranslate", "Translation/detection processing", SKIPPED,
                            "Endpoint configuration is invalid; POST /translate and /detect were not called."))
        return checks
    if not probe_services:
        checks.append(Check("LibreTranslate", "Service readiness", SKIPPED,
                            f"Probe disabled for {_safe_url(base_url)}; the service was not started."))
        checks.append(Check("LibreTranslate", "Translation/detection processing", SKIPPED,
                            "Service probes are disabled; POST /translate and /detect were not called."))
        return checks
    if not _is_loopback_url(base_url):
        checks.append(Check("LibreTranslate", "Service readiness", SKIPPED,
                            "The configured endpoint is external; external service probes are disabled."))
        checks.append(Check("LibreTranslate", "Translation/detection processing", SKIPPED,
                            "The endpoint is external; POST /translate and /detect were not called."))
        return checks

    response = http_json("GET", f"{base_url.rstrip('/')}/languages", timeout=2.0)
    languages = response.payload if isinstance(response.payload, list) else []
    codes = {
        str(item.get("code")).strip()
        for item in languages
        if isinstance(item, dict) and _nonempty(item.get("code"))
    }
    if response.status == 200 and not response.error and codes:
        checks.append(Check("LibreTranslate", "Service readiness", READY,
                            f"Local /languages returned {len(codes)} language code(s); no translation was requested."))
    else:
        checks.append(Check("LibreTranslate", "Service readiness", UNAVAILABLE,
                            "Local /languages did not return a non-empty language list; the service was not started."))
    checks.append(Check(
        "LibreTranslate", "Translation/detection processing", SKIPPED,
        "POST /translate and /detect were not called; service readiness does not claim translation behavior.",
    ))
    return checks


def _display_profile_path(environ: Mapping[str, str]) -> tuple[Path, str]:
    local = environ.get("LOCALAPPDATA")
    if _nonempty(local):
        return (
            Path(str(local)) / "MedlarTV" / "viewer_profiles.sqlite3",
            r"%LOCALAPPDATA%\MedlarTV\viewer_profiles.sqlite3",
        )
    if os.name == "nt":
        return (
            Path.home() / "AppData" / "Local" / "MedlarTV" / "viewer_profiles.sqlite3",
            r"%USERPROFILE%\AppData\Local\MedlarTV\viewer_profiles.sqlite3",
        )
    xdg = environ.get("XDG_DATA_HOME")
    base = Path(str(xdg)) if _nonempty(xdg) else Path.home() / ".local" / "share"
    return base / "MedlarTV" / "viewer_profiles.sqlite3", \
        "$XDG_DATA_HOME/MedlarTV/viewer_profiles.sqlite3"


def check_memory_paths(environ: Mapping[str, str], root: Path = REPO_ROOT) -> list[Check]:
    mood = root / "memory.json"
    profile, profile_display = _display_profile_path(environ)
    legacy = root / "MedlarTV" / "data" / "memory.yaml"
    return [
        Check(
            "Memory", "Mood memory",
            READY if mood.is_file() else SKIPPED,
            "Normal launcher path is repository-root memory.json; "
            + ("the file exists" if mood.is_file() else "absence is accepted and runtime defaults are used")
            + ". Contents were not read.",
        ),
        Check(
            "Memory", "Durable viewer profiles",
            READY if profile.is_file() else SKIPPED,
            f"Active path is {profile_display}; "
            + ("the database exists" if profile.is_file() else "the database has not been initialized")
            + ". No database contents, schema, or records were opened.",
        ),
        Check("Memory", "Conversation context", READY,
              "Conversation context is bounded in-process state; no backing file is expected."),
        Check(
            "Memory", "Legacy memory YAML", SKIPPED,
            f"MedlarTV/data/memory.yaml {'exists' if legacy.is_file() else 'is absent'} but has no current runtime reference; it was not created or opened.",
        ),
    ]


def consolidation_coverage_checks(observed: Sequence[Check]) -> list[Check]:
    """Map each legacy script purpose to this run's real outcomes."""
    section = "Original-script parity"

    def matching(section_name: str | None, names: Sequence[str]) -> list[Check]:
        return [
            item for item in observed
            if (section_name is None or item.section == section_name) and item.name in names
        ]

    def aggregate(rows: Sequence[Check]) -> str:
        if not rows or all(item.status == SKIPPED for item in rows):
            return SKIPPED
        if any(item.status == ERROR for item in rows):
            return ERROR
        if any(item.status == UNAVAILABLE for item in rows):
            return UNAVAILABLE
        if any(item.status == SKIPPED for item in rows):
            return SKIPPED
        return READY

    specs = (
        (
            "test_api_direct.py - app exchange and broadcaster lookup",
            matching("Live Twitch", ("App credential exchange", "Read-only channel API")),
            "App-token creation and exact broadcaster lookup are represented by the linked live rows; no token value/body is copied.",
        ),
        (
            "test_api_direct.py - title update and restoration",
            matching("Full-live Twitch exercise", (
                "Title update request", "Title update verification",
                "Title restoration verification",
            )),
            "The old unconditional title mutation is replaced by an explicit temporary value, read-back, and verified restoration.",
        ),
        (
            "Expanded parity - category update and restoration",
            matching("Full-live Twitch exercise", (
                "Category resolution", "Category update request",
                "Category update verification", "Category restoration verification",
            )),
            "Category mutation is separately gated, exact-match resolved, read back, and restored.",
        ),
        (
            "Expanded parity - one diagnostic chat message",
            matching("Full-live Twitch exercise", (
                "IRC connection and authentication", "Diagnostic chat command write",
                "IRC post-send acknowledgement", "Independent chat delivery observation",
            )),
            "Socket write and protocol acknowledgement are distinct from independent viewer delivery, which remains UNTESTED.",
        ),
        (
            "test_tokens.py - bot token identity/scopes",
            matching("Live Twitch", (
                "Bot token validation", "Bot identity", "Bot raw scopes",
                "Bot scope classification", "Bot feature scopes",
            )),
            "Bot validation, identity, every raw scope, classification, and source-derived expectations remain independent.",
        ),
        (
            "test_tokens.py - broadcaster token identity/scopes",
            matching("Live Twitch", (
                "Broadcaster token validation", "Broadcaster identity",
                "Broadcaster raw scopes", "Broadcaster scope classification",
                "Broadcaster feature scopes",
            )),
            "Broadcaster validation is independent of bot/app results and every returned scope has a separate functional outcome.",
        ),
        (
            "verify_setup.py - interpreter/dependencies",
            [item for item in observed if item.section in {"Application environment", "Dependencies"}],
            "Launcher interpreter, pinned distributions, and the expanded import matrix are reported individually.",
        ),
        (
            "verify_setup.py - environment/configuration",
            [item for item in observed if item.section in {
                "Configuration", "Configuration semantics", "Credentials",
            }],
            "Effective precedence, exact assignment presence, parsed YAML, and consumer schemas are checked without writing defaults.",
        ),
        (
            "verify_setup.py - memory",
            [item for item in observed if item.section == "Memory"],
            "Active mood/viewer/context paths and the dormant legacy YAML are reported without opening records or creating/migrating files.",
        ),
        (
            "verify_setup.py - Ollama endpoint/model/launch mode",
            [item for item in observed if item.section == "Ollama"],
            "Executable, endpoint, /api/tags, selected model, bind alignment, and launcher-mode ownership are separate outcomes; no inference runs.",
        ),
        (
            "Current setup - LibreTranslate",
            [item for item in observed if item.section == "LibreTranslate"],
            "Separate interpreter/manifest and loopback /languages readiness remain independent; no translation is submitted.",
        ),
    )
    checks = [Check(section, name, aggregate(rows), detail) for name, rows, detail in specs]
    checks.append(Check(
        section, "Original evidence fields and safe side-effect boundary", READY,
        "The private/full report preserves exact token values, token prefixes/lengths, client IDs, outgoing headers/forms, response statuses/headers/bodies, and complete scope/ability rows. "
        "Import-time file truncation, automatic memory creation, uncontrolled mutations, and blanket ability claims remain intentionally excluded.",
    ))
    return checks


def _scope_catalog(root: Path) -> dict[str, str]:
    path = root / "scopes.txt"
    catalog: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        return catalog
    current_scope: str | None = None
    collecting_description = False
    for line in lines[1:]:
        stripped = line.strip()
        heading = re.fullmatch(r"\d+\.\s+([A-Za-z0-9:_-]+)", stripped)
        if heading:
            current_scope = heading.group(1)
            collecting_description = False
            continue
        if current_scope is not None and stripped.startswith("What it does:"):
            catalog[current_scope] = stripped.partition(":")[2].strip()
            collecting_description = True
            continue
        if collecting_description:
            if not stripped or re.match(r"(?:Type|Example|Limits|Sources):", stripped):
                collecting_description = False
            else:
                catalog[current_scope] = f"{catalog[current_scope]} {stripped}".strip()
                continue

        # Backward compatibility with the original two-column catalog.
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) == 2 and ":" in parts[0]:
            catalog[parts[0]] = parts[1]
    return catalog


def classify_scope(scope: str, catalog: Mapping[str, str]) -> str:
    if scope in SPECIAL_SCOPE_NOTES:
        return SPECIAL_SCOPE_NOTES[scope]
    if scope in LEGACY_SCOPE_NAMES:
        return "LEGACY-DECOMMISSIONED: legacy raw scope name; not treated as an alias for a modern required permission"
    if scope in MODERN_SCOPE_DESCRIPTIONS:
        return "CURRENT: currently supported; " + MODERN_SCOPE_DESCRIPTIONS[scope]
    if scope in catalog:
        return "CURRENT: documented access; " + catalog[scope]
    return "UNKNOWN/UNMAPPED: not described by the embedded or local catalog; this does not make it invalid"


def scope_category(scope: str, catalog: Mapping[str, str]) -> str:
    if scope in LEGACY_SCOPE_NAMES or scope in {"whispers:read", "whispers:edit", "user:edit:follows"}:
        return "Legacy / Removed"
    if scope == "openid":
        return "Identity (OIDC)"
    if scope not in catalog and scope not in MODERN_SCOPE_DESCRIPTIONS:
        return "Unmapped"
    if scope.startswith("analytics:"):
        return "Analytics"
    if scope == "bits:read" or scope in {
        "channel:edit:commercial", "channel:manage:ads", "channel:read:ads",
    }:
        return "Ads / Monetization"
    if scope in {
        "chat:read", "chat:edit", "channel:bot", "user:bot", "user:read:chat",
        "user:write:chat", "user:manage:chat_color",
    }:
        return "Bot & Chat"
    if scope in {
        "clips:edit", "channel:manage:clips", "editor:manage:clips",
        "channel:manage:videos",
    }:
        return "Clips & Videos"
    if "extensions" in scope:
        return "Extensions"
    if "charity" in scope:
        return "Charity"
    if "guest_star" in scope:
        return "Guest Star"
    if scope.startswith("moderator:") or scope in {
        "moderation:read", "channel:moderate", "channel:manage:moderators",
        "channel:read:vips", "channel:manage:vips",
    }:
        return "Moderation"
    if "redemptions" in scope:
        return "Channel Points"
    if "polls" in scope or "predictions" in scope:
        return "Polls & Predictions"
    if "raids" in scope or "schedule" in scope:
        return "Raids & Schedule"
    if "subscriptions" in scope:
        return "Subscriptions"
    if "whisper" in scope:
        return "Whispers"
    if scope.startswith("user:"):
        return "User / Account"
    if scope in {"channel:read:goals", "channel:read:hype_train"}:
        return "Goals / Hype Train"
    return "Broadcast / Channel"


def scope_test_policy(scope: str) -> str:
    if scope == "channel:manage:broadcast":
        return "Functional title/category checks run only in the explicit exercise mode, with snapshot and verified restoration."
    if scope == "chat:edit":
        return "One harmless IRC message may be sent only in the explicit exercise mode; it cannot be restored."
    if scope == "chat:read":
        return "Used only for IRC authentication/protocol acknowledgement; viewer messages are not collected into the report."
    if any(part in scope for part in (":manage:", ":edit:", ":write:")) or scope.endswith(":edit"):
        return "No functional mutation was attempted: this permission can change consequential state and is outside the title/category/chat allowlist."
    return "No per-scope behavior is inferred from the grant alone; endpoint identity, role, account state, and implementation still matter."


def _scope_ability(scope: str, catalog: Mapping[str, str]) -> str:
    if scope in MODERN_SCOPE_DESCRIPTIONS:
        return MODERN_SCOPE_DESCRIPTIONS[scope]
    if scope in catalog:
        return catalog[scope]
    if scope == "openid":
        return "provide OpenID Connect identity claims"
    if scope in SPECIAL_SCOPE_NOTES:
        return SPECIAL_SCOPE_NOTES[scope]
    if scope in LEGACY_SCOPE_NAMES:
        return "historical Twitch authorization name; no current ability is inferred"
    return "unknown to the checked-in catalogs; the grant is still reported exactly"


def _functional_status(outcome: ScopeFunctionalOutcome) -> str:
    if outcome.verdict == "PASS":
        return READY
    if outcome.verdict == "FAIL":
        return outcome.failure_status
    return SKIPPED


def detailed_scope_checks(
    role: str,
    scopes: Sequence[str],
    catalog: Mapping[str, str],
    outcomes: Mapping[str, ScopeFunctionalOutcome] | None = None,
) -> list[Check]:
    functional = outcomes or {}
    checks: list[Check] = []
    for scope in sorted(set(scopes), key=lambda value: (scope_category(value, catalog), value)):
        classification = classify_scope(scope, catalog)
        outcome = functional.get(scope, ScopeFunctionalOutcome(
            "UNTESTED", scope_test_policy(scope),
        ))
        checks.append(Check(
            f"Live Twitch - {role} granted scopes",
            f"{scope_category(scope, catalog)} - {scope}",
            _functional_status(outcome),
            f"Grant=GRANTED; Classification={classification}; "
            f"Ability={_scope_ability(scope, catalog)}; Functional={outcome.verdict}; "
            f"Reason={outcome.reason}",
        ))
    if not checks:
        checks.append(Check(
            f"Live Twitch - {role} granted scopes", "No granted scopes", SKIPPED,
            "Grant=EMPTY; Functional=UNTESTED; Reason=Twitch returned an empty scope list. "
            "This does not invalidate the token, but no scoped behavior can be tested.",
        ))
    return checks


def feature_scope_checks(
    role: str,
    scopes: Sequence[str],
    required_scopes: Mapping[str, str],
    outcomes: Mapping[str, ScopeFunctionalOutcome] | None = None,
) -> list[Check]:
    granted = set(scopes)
    functional = outcomes or {}
    checks: list[Check] = []
    for scope, feature_reason in required_scopes.items():
        if scope not in granted:
            checks.append(Check(
                "Live Twitch - Feature-derived scope expectations",
                f"{role} - {scope}",
                UNAVAILABLE,
                f"Grant=MISSING; Functional=UNTESTED; Reason=Current Medlar source expects this grant for {feature_reason}; "
                "no behavior was attempted. Missing authorization is distinct from a failed functional request.",
            ))
            continue
        outcome = functional.get(scope, ScopeFunctionalOutcome(
            "UNTESTED", scope_test_policy(scope),
        ))
        checks.append(Check(
            "Live Twitch - Feature-derived scope expectations",
            f"{role} - {scope}",
            _functional_status(outcome),
            f"Grant=GRANTED; Functional={outcome.verdict}; Reason={outcome.reason} "
            f"Source expectation={feature_reason}.",
        ))
    return checks


def _normalized_oauth_token(value: str) -> str:
    return value[len("oauth:"):] if value.startswith("oauth:") else value


def _valid_twitch_login(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]{1,25}", value))


def _valid_irc_token_wire_value(value: str) -> bool:
    """Allow OAuth-token characters that cannot break an IRC PASS line."""
    return bool(value) and len(value) <= 2048 and bool(re.fullmatch(r"[A-Za-z0-9._~-]+", value))


def validate_twitch_exercise(exercise: TwitchExercise) -> None:
    channel = exercise.target_channel.strip().lstrip("#")
    if not _valid_twitch_login(channel):
        raise ValueError("--target-channel must be a Twitch login with 1–25 letters, digits, or underscores")
    for flag, value, limit in (
        ("--test-title", exercise.title, 140),
        ("--test-category", exercise.category, 100),
        ("--test-chat-message", exercise.chat_message, 450),
    ):
        if not value or value != value.strip():
            raise ValueError(f"{flag} must be non-empty and have no leading or trailing whitespace")
        if len(value) > limit:
            raise ValueError(f"{flag} exceeds its diagnostic safety limit of {limit} characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f"{flag} must not contain control characters")
    message = exercise.chat_message
    if not message.startswith("[Medlar diagnostics]"):
        raise ValueError("--test-chat-message must start with '[Medlar diagnostics]'")
    if "@" in message:
        raise ValueError("--test-chat-message must not contain @ mentions")
    if message.lstrip().startswith(("/", ".")):
        raise ValueError("--test-chat-message must not be a chat command")
    wire = f"PRIVMSG #{channel} :{message}\r\n".encode("utf-8")
    if len(wire) > 512:
        raise ValueError("--test-chat-message exceeds Twitch IRC's 512-byte line limit")


def _irc_message_tags(line: str) -> dict[str, str]:
    if not line.startswith("@") or " " not in line:
        return {}
    raw_tags = line[1:].split(" ", 1)[0]
    tags: dict[str, str] = {}
    for item in raw_tags.split(";"):
        key, separator, value = item.partition("=")
        if key:
            tags[key] = value if separator else ""
    return tags


def send_irc_diagnostic(
    bot_login: str,
    raw_token: str,
    channel: str,
    message: str,
    *,
    timeout: float = 10.0,
) -> IrcSendResult:
    """Send exactly one IRC PRIVMSG and retain no received chat content."""
    token = _normalized_oauth_token(raw_token.strip())
    channel = channel.strip().lstrip("#")
    channel_folded = channel.casefold()
    exercise = TwitchExercise(channel, "x", "x", message)
    validate_twitch_exercise(exercise)
    if not _valid_twitch_login(bot_login):
        return IrcSendResult(error="bot login is missing or not a safe Twitch login")
    if not _valid_irc_token_wire_value(token):
        return IrcSendResult(error="token is missing or contains characters unsafe for an IRC PASS line")

    connected = authenticated = joined = written = acknowledged = rejected = False
    raw_socket = None
    tls_socket = None
    try:
        raw_socket = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=timeout)
        context = ssl.create_default_context()
        tls_socket = context.wrap_socket(raw_socket, server_hostname="irc.chat.twitch.tv")
        raw_socket = None
        tls_socket.settimeout(min(timeout, 2.0))
        connected = True

        def send_line(line: str) -> None:
            assert tls_socket is not None
            tls_socket.sendall((line + "\r\n").encode("utf-8"))

        send_line(f"PASS oauth:{token}")
        send_line(f"NICK {bot_login}")
        send_line("CAP REQ :twitch.tv/membership twitch.tv/commands twitch.tv/tags")
        send_line(f"JOIN #{channel}")

        buffer = b""
        deadline = time.monotonic() + timeout
        initial_userstate_seen = False
        while time.monotonic() < deadline and not (authenticated and joined and initial_userstate_seen):
            try:
                chunk = tls_socket.recv(8192)
            except socket.timeout:
                _capture_failure(sys.exc_info()[1])
                continue
            if not chunk:
                break
            buffer = (buffer + chunk)[-131072:]
            while b"\r\n" in buffer:
                raw_line, buffer = buffer.split(b"\r\n", 1)
                line = raw_line.decode("utf-8", errors="replace")
                folded = line.casefold()
                if line.startswith("PING "):
                    send_line("PONG " + line[5:])
                elif " NOTICE * :Login authentication failed" in line or " NOTICE * :Improperly formatted auth" in line:
                    rejected = True
                    return IrcSendResult(connected, False, False, False, False, True,
                                         "Twitch rejected IRC authentication")
                elif re.search(r"\s001\s", line):
                    authenticated = True
                elif (
                    f" join #{channel_folded}" in folded
                    or f" join :#{channel_folded}" in folded
                    or (re.search(r"\s366\s", line) and f"#{channel_folded}" in folded)
                    or f" roomstate #{channel_folded}" in folded
                ):
                    joined = True
                if f" userstate #{channel_folded}" in folded:
                    joined = True
                    initial_userstate_seen = True

        if not authenticated or not joined or not initial_userstate_seen:
            return IrcSendResult(connected, authenticated, joined, False, False, rejected,
                                 "IRC authentication, channel join, or initial USERSTATE was not confirmed")

        # Do not let an incomplete pre-send protocol fragment be mistaken for
        # Twitch's documented USERSTATE response to this PRIVMSG.
        buffer = b""
        send_line(f"PRIVMSG #{channel} :{message}")
        written = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = tls_socket.recv(8192)
            except socket.timeout:
                _capture_failure(sys.exc_info()[1])
                continue
            if not chunk:
                break
            buffer = (buffer + chunk)[-131072:]
            while b"\r\n" in buffer:
                raw_line, buffer = buffer.split(b"\r\n", 1)
                line = raw_line.decode("utf-8", errors="replace")
                folded = line.casefold()
                if line.startswith("PING "):
                    send_line("PONG " + line[5:])
                elif " notice #" in folded:
                    notice_id = _irc_message_tags(line).get("msg-id", "")
                    if notice_id in IRC_SEND_REJECTION_NOTICE_IDS:
                        rejected = True
                        return IrcSendResult(connected, authenticated, joined, written, False, True,
                                             f"Twitch returned post-send IRC rejection NOTICE msg-id={notice_id}")
                elif f" userstate #{channel_folded}" in folded:
                    # Twitch documents USERSTATE itself as the sender-side
                    # response to the bot's own PRIVMSG. USERSTATE has no
                    # message-id tag; requiring one would reject valid sends.
                    acknowledged = True
            if acknowledged:
                return IrcSendResult(connected, authenticated, joined, written, True, False)
        return IrcSendResult(connected, authenticated, joined, written, False, rejected,
                             "No documented post-send USERSTATE response arrived before timeout")
    except (OSError, ssl.SSLError, ValueError):
        _capture_failure(sys.exc_info()[1])
        return IrcSendResult(connected, authenticated, joined, written, acknowledged, rejected,
                             "IRC transport could not complete")
    finally:
        for candidate in (tls_socket, raw_socket):
            if candidate is not None:
                try:
                    candidate.close()
                except OSError:
                    _capture_failure(sys.exc_info()[1])
                    pass


def probe_irc_readiness(
    bot_login: str,
    raw_token: str,
    channel: str,
    *,
    timeout: float = 10.0,
) -> IrcProbeResult:
    """Authenticate and join Twitch IRC without writing a chat message."""
    token = _normalized_oauth_token(raw_token.strip())
    channel = channel.strip().lstrip("#")
    if not _valid_twitch_login(bot_login) or not _valid_twitch_login(channel):
        return IrcProbeResult(error="bot login or channel is missing or not a safe Twitch login")
    if not _valid_irc_token_wire_value(token):
        return IrcProbeResult(error="token is missing or contains characters unsafe for an IRC PASS line")
    connected = authenticated = joined = False
    raw_socket = None
    tls_socket = None
    try:
        raw_socket = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=timeout)
        context = ssl.create_default_context()
        tls_socket = context.wrap_socket(raw_socket, server_hostname="irc.chat.twitch.tv")
        raw_socket = None
        tls_socket.settimeout(min(timeout, 2.0))
        connected = True

        def send_line(line: str) -> None:
            assert tls_socket is not None
            tls_socket.sendall((line + "\r\n").encode("utf-8"))

        send_line(f"PASS oauth:{token}")
        send_line(f"NICK {bot_login}")
        send_line("CAP REQ :twitch.tv/membership twitch.tv/commands twitch.tv/tags")
        send_line(f"JOIN #{channel}")
        buffer = b""
        channel_folded = channel.casefold()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not (authenticated and joined):
            try:
                chunk = tls_socket.recv(8192)
            except socket.timeout:
                _capture_failure(sys.exc_info()[1])
                continue
            if not chunk:
                break
            buffer = (buffer + chunk)[-131072:]
            while b"\r\n" in buffer:
                raw_line, buffer = buffer.split(b"\r\n", 1)
                line = raw_line.decode("utf-8", errors="replace")
                folded = line.casefold()
                if line.startswith("PING "):
                    send_line("PONG " + line[5:])
                elif " NOTICE * :Login authentication failed" in line or " NOTICE * :Improperly formatted auth" in line:
                    return IrcProbeResult(connected, False, False, "Twitch rejected IRC authentication")
                elif re.search(r"\s001\s", line):
                    authenticated = True
                elif (
                    f" join #{channel_folded}" in folded
                    or f" join :#{channel_folded}" in folded
                    or (re.search(r"\s366\s", line) and f"#{channel_folded}" in folded)
                    or f" roomstate #{channel_folded}" in folded
                    or f" userstate #{channel_folded}" in folded
                ):
                    joined = True
        if authenticated and joined:
            return IrcProbeResult(True, True, True)
        return IrcProbeResult(
            connected, authenticated, joined,
            "IRC authentication or target-channel join was not confirmed",
        )
    except (OSError, ssl.SSLError, ValueError):
        _capture_failure(sys.exc_info()[1])
        return IrcProbeResult(connected, authenticated, joined, "IRC transport could not complete")
    finally:
        for candidate in (tls_socket, raw_socket):
            if candidate is not None:
                try:
                    candidate.close()
                except OSError:
                    _capture_failure(sys.exc_info()[1])
                    pass


def _exercise_check_names() -> tuple[str, ...]:
    return (
        "Exercise preflight", "Original channel-state snapshot", "Category resolution",
        "Broadcaster offline before mutation",
        "Title update request", "Title update verification", "Category update request",
        "Category update verification", "Restoration request", "Title restoration verification",
        "Category restoration verification", "Broadcaster offline before chat",
        "IRC connection and authentication",
        "Diagnostic chat command write", "IRC post-send acknowledgement",
        "Independent chat delivery observation", "Full exercise result",
    )


def _skipped_exercise_checks(reason: str) -> list[Check]:
    return [
        Check("Full-live Twitch exercise", name, SKIPPED, reason)
        for name in _exercise_check_names()
    ]


def _skipped_token_checks(role: str, reason: str) -> list[Check]:
    return [
        Check("Live Twitch", f"{role} identity", SKIPPED, reason),
        Check("Live Twitch", f"{role} raw scopes", SKIPPED, reason),
        Check("Live Twitch", f"{role} scope classification", SKIPPED, reason),
        Check("Live Twitch", f"{role} feature scopes", SKIPPED, reason),
        Check(f"Live Twitch — {role} granted scopes", "Detailed granted scopes", SKIPPED, reason),
    ]


def _validate_user_token(
    *,
    role: str,
    raw_token: str,
    expected_login: str,
    configured_client_id: str,
    required_scopes: Mapping[str, str],
    client_id_required: bool,
    catalog: Mapping[str, str],
    http_json: HttpJson,
) -> tuple[list[Check], TokenFacts | None]:
    section = "Live Twitch"
    token = _normalized_oauth_token(raw_token.strip())
    if not token:
        reason = "Not checked because this token is not configured."
        return ([Check(section, f"{role} token validation", SKIPPED,
                       f"Validation=UNTESTED; Reason=The {role.casefold()} token is not configured, so no validation request was sent."),
                 *_skipped_token_checks(role, reason)], None)

    response = _safe_http_call(
        http_json, "GET", "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}"}, timeout=10.0,
    )
    if response.status != 200 or response.error or response.body_truncated or not isinstance(response.payload, dict):
        status_text = f"HTTP {response.status}" if response.status is not None else "network unavailable"
        reason = "Not checked because independent token validation failed."
        definitive_rejection = response.status is not None and 400 <= response.status < 500
        result_status = UNAVAILABLE if definitive_rejection else SKIPPED
        verdict = "FAIL" if definitive_rejection else "UNTESTED"
        return ([Check(section, f"{role} token validation", result_status,
                       f"Validation={verdict}; Reason=Independent validation returned {status_text}; "
                       "the private/full credential block and HTTP transcript retain the exact value, headers, status, and complete body."),
                 *_skipped_token_checks(role, reason)], None)

    payload = response.payload
    scopes_value = payload.get("scopes")
    invalid = [key for key in ("login", "client_id", "user_id")
               if not isinstance(payload.get(key), str) or not payload[key].strip()]
    if (not isinstance(scopes_value, list)
            or any(not isinstance(value, str) or not value for value in scopes_value)):
        invalid.append("scopes")
    if type(payload.get("expires_in")) is not int or payload["expires_in"] < 0:
        invalid.append("expires_in")
    if response.body_truncated or invalid:
        reason = ("Validation=FAIL; malformed/incomplete validation response; invalid fields="
                  + ", ".join(invalid) + ". The complete available raw response remains in the HTTP transcript.")
        return [Check(section, f"{role} token validation", UNAVAILABLE, reason), *_skipped_token_checks(role, reason)], None
    login = str(payload.get("login") or "").strip()
    validated_client = str(payload.get("client_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    expires_in = payload.get("expires_in")
    raw_scopes = payload.get("scopes")
    returned_scopes = tuple(
        str(item) for item in raw_scopes if isinstance(item, str)
    ) if isinstance(raw_scopes, list) else ()
    scopes = frozenset(returned_scopes)
    facts = TokenFacts(login, validated_client, user_id, scopes, returned_scopes)
    checks = [Check(section, f"{role} token validation", READY,
                    f"Validation=PASS; Twitch accepted the token (HTTP 200); login={login or '(missing)'!r}; "
                    f"user_id={user_id or '(missing)'!r}; client_id={validated_client or '(missing)'!r}; "
                    f"expires_in={expires_in!r}. The private/full credential block and HTTP transcript retain the exact value, headers, status, and complete body.")]

    identity_problems: list[str] = []
    if expected_login and login.casefold() != expected_login.casefold():
        identity_problems.append("validated login does not match the configured account")
    if configured_client_id:
        if validated_client != configured_client_id:
            identity_problems.append("validated client ID does not match the configured client ID")
    elif client_id_required:
        identity_problems.append("configured client ID is missing")
    if identity_problems:
        checks.append(Check(section, f"{role} identity", UNAVAILABLE,
                            "Identity=FAIL; " + "; ".join(identity_problems)
                            + f". Validated login={login or '(missing)'!r}; user_id={user_id or '(missing)'!r}; "
                            f"configured client_id={configured_client_id or '(missing)'!r}; validated client_id={validated_client or '(missing)'!r}."))
    elif not expected_login and not configured_client_id:
        checks.append(Check(section, f"{role} identity", SKIPPED,
                            "Identity=UNTESTED; no configured account or client ID was available for comparison."))
    else:
        if configured_client_id:
            comparison = "configured login and client-ID comparisons passed"
            note = (
                " Bot client-ID matching is informational because current IRC login does not send it."
                if role == "Bot" else ""
            )
        else:
            comparison = "configured login comparison passed; optional bot client ID was not configured, so that comparison was skipped"
            note = ""
        checks.append(Check(section, f"{role} identity", READY,
                            f"Identity=PASS; validated login={login or '(missing)'!r}; user_id={user_id or '(missing)'!r}; "
                            f"configured client_id={configured_client_id or '(missing)'!r}; validated client_id={validated_client or '(missing)'!r}; {comparison}."
                            + note))

    raw_names = list(returned_scopes)
    checks.append(Check(
        section, f"{role} raw scopes", READY,
        f"Twitch returned {len(raw_names)} raw scope entr{'y' if len(raw_names) == 1 else 'ies'} "
        f"in API order ({len(scopes)} unique): "
        + (", ".join(raw_names) if raw_names else "none") + ".",
    ))
    classifications = [
        f"{scope} = {classify_scope(scope, catalog)}"
        for scope in sorted(scopes)
    ]
    checks.append(Check(
        section, f"{role} scope classification", READY,
        "A detailed categorized row follows for every raw scope. "
        + ("Classification summary: " + "; ".join(classifications)
           if classifications else "No scopes were returned. This is descriptive, not an invalid-token judgment."),
    ))
    missing = sorted(set(required_scopes) - scopes)
    expected_present = sorted(set(required_scopes) & scopes)
    parts = [
        "Feature-derived expectations only; scope presence does not prove account roles or behavior.",
        "Present expected: " + (", ".join(expected_present) if expected_present else "none") + ".",
        "Missing expected: " + (", ".join(missing) if missing else "none") + ".",
        "Expectations: " + "; ".join(
            f"{scope} ({reason})" for scope, reason in required_scopes.items()
        ) + ".",
    ]
    checks.append(Check(section, f"{role} feature scopes", READY if not missing else UNAVAILABLE,
                        "Grant inventory only. " + " ".join(parts)))
    return checks, facts


def _reject_incomplete(result: Any) -> Any:
    if isinstance(result, HttpJsonResult) and result.body_truncated and not result.error:
        return replace(result, error="Incomplete HTTP body: available payload is retained as evidence but cannot support a passing verdict.")
    return result


def _usable_app_exchange(result: HttpJsonResult) -> bool:
    body = result.payload
    return (result.status == 200 and not result.error and not result.body_truncated
            and isinstance(body, dict) and isinstance(body.get("access_token"), str)
            and bool(body["access_token"].strip())
            and type(body.get("expires_in")) is int and body["expires_in"] > 0
            and isinstance(body.get("token_type"), str) and body["token_type"].casefold() == "bearer")


def _safe_http_call(http_json: HttpJson, method: str, url: str, **kwargs: Any) -> HttpJsonResult:
    try:
        result = _reject_incomplete(http_json(method, url, **kwargs))
    except Exception as exc:
        _capture_failure(sys.exc_info()[1])
        return HttpJsonResult(
            None,
            error=(
                _exception_evidence(exc)
            ),
        )
    return result if isinstance(result, HttpJsonResult) else HttpJsonResult(None, error="invalid request result")


def _recording_http_json(
    delegate: HttpJson,
    exchanges: list[HttpExchangeEvidence],
) -> HttpJson:
    """Wrap a transport so the private report receives a complete request/response transcript."""
    def recorded(method: str, url: str, **kwargs: Any) -> HttpJsonResult:
        headers = dict(kwargs.get("headers") or {})
        request_body: str | None = None
        if kwargs.get("form") is not None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            request_body = urllib_parse.urlencode(dict(kwargs["form"]))
        elif kwargs.get("json_body") is not None:
            headers.setdefault("Content-Type", "application/json")
            request_body = json.dumps(dict(kwargs["json_body"]), ensure_ascii=False)
        try:
            result = _reject_incomplete(delegate(method, url, **kwargs))
        except Exception as exc:
            _capture_failure(sys.exc_info()[1])
            exchanges.append(HttpExchangeEvidence(
                sequence=len(exchanges) + 1,
                method=method.upper(),
                url=url,
                request_headers=tuple((str(key), str(value)) for key, value in headers.items()),
                request_body=request_body,
                response_status=None,
                response_headers=(),
                response_body=None,
                response_body_source="unavailable: transport raised before returning a response",
                error=_exception_evidence(exc),
            ))
            raise

        if isinstance(result, HttpJsonResult):
            if result.raw_body is not None:
                response_body = result.raw_body
                body_source = "raw transport body"
            elif result.payload is not None:
                response_body = json.dumps(result.payload, ensure_ascii=False, separators=(",", ":"))
                body_source = "normalized JSON reconstructed because the injected transport supplied no raw body"
            elif result.status is not None and 200 <= result.status < 300:
                response_body = ""
                body_source = "empty response body"
            else:
                response_body = None
                body_source = "unavailable: transport supplied no body"
            exchanges.append(HttpExchangeEvidence(
                sequence=len(exchanges) + 1,
                method=method.upper(),
                url=url,
                request_headers=tuple((str(key), str(value)) for key, value in headers.items()),
                request_body=request_body,
                response_status=result.status,
                response_headers=result.response_headers,
                response_body=response_body,
                response_body_source=body_source,
                response_body_truncated=result.body_truncated,
                response_body_bytes_observed=result.body_bytes_observed,
                error=result.error,
            ))
        else:
            exchanges.append(HttpExchangeEvidence(
                sequence=len(exchanges) + 1,
                method=method.upper(),
                url=url,
                request_headers=tuple((str(key), str(value)) for key, value in headers.items()),
                request_body=request_body,
                response_status=None,
                response_headers=(),
                response_body=None,
                response_body_source="unavailable: transport returned an invalid result object",
                error="invalid request result",
            ))
        return result

    return recorded


def _channel_state_from_response(response: HttpJsonResult) -> dict[str, str] | None:
    rows = response.payload.get("data") if not response.error and not response.body_truncated and isinstance(response.payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if any(not isinstance(row.get(key), str) for key in ("title", "game_id", "game_name")):
        return None
    return {
        "title": str(row.get("title") or ""),
        "game_id": str(row.get("game_id") or ""),
        "game_name": str(row.get("game_name") or ""),
    }


READ_ONLY_TWITCH_API_NAMES: tuple[str, ...] = (
    "GET /helix/channels",
    "GET /helix/streams",
    "GET /helix/search/categories",
)


def _skipped_read_only_twitch_api_checks(reason: str) -> list[Check]:
    return [
        Check("Live Twitch - Read-only API coverage", name, SKIPPED, reason)
        for name in READ_ONLY_TWITCH_API_NAMES
    ]


def _read_only_twitch_api_checks(
    *,
    app_client: str,
    app_token: str,
    channel_id: str,
    http_json: HttpJson,
) -> list[Check]:
    """Probe safe GET endpoints used by current Medlar source; the private transcript retains bodies."""
    if not app_client or not app_token or not channel_id:
        return _skipped_read_only_twitch_api_checks(
            "App authentication and an exact configured-channel ID are required; no request was sent."
        )
    headers = {"Client-ID": app_client, "Authorization": f"Bearer {app_token}"}
    requests = (
        (
            "GET /helix/channels",
            "https://api.twitch.tv/helix/channels?" + urllib_parse.urlencode({
                "broadcaster_id": channel_id,
            }),
            "nonempty_mapping",
        ),
        (
            "GET /helix/streams",
            "https://api.twitch.tv/helix/streams?" + urllib_parse.urlencode({
                "user_id": channel_id,
            }),
            "list",
        ),
        (
            "GET /helix/search/categories",
            "https://api.twitch.tv/helix/search/categories?" + urllib_parse.urlencode({
                "query": "Just Chatting", "first": "1",
            }),
            "list",
        ),
    )
    checks: list[Check] = []
    for name, url, expected in requests:
        response = _safe_http_call(
            http_json, "GET", url, headers=headers, timeout=10.0,
        )
        rows = response.payload.get("data") if not response.error and not response.body_truncated and isinstance(response.payload, dict) else None
        shape_ok = isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
        if expected == "nonempty_mapping":
            shape_ok = bool(shape_ok and len(rows) == 1 and rows[0].get("broadcaster_id") == channel_id
                            and _channel_state_from_response(response) is not None)
        elif name == "GET /helix/streams":
            shape_ok = bool(shape_ok and all(_nonempty(row.get("id")) and row.get("user_id") == channel_id for row in rows))
        else:
            shape_ok = bool(shape_ok and all(_nonempty(row.get("id")) and _nonempty(row.get("name")) for row in rows))
        status_display = str(response.status) if response.status is not None else "unavailable"
        if response.status == 200 and not response.error and shape_ok:
            count = len(rows) if isinstance(rows, list) else 0
            note = (
                f"HTTP 200; response schema contains data[{count}]. "
                "An empty stream/category/game list is a valid endpoint result where applicable; the complete response is in the private HTTP transcript."
            )
            checks.append(Check("Live Twitch - Read-only API coverage", name, READY, note))
        else:
            checks.append(Check(
                "Live Twitch - Read-only API coverage", name, UNAVAILABLE,
                f"HTTP {status_display}; expected a JSON data list"
                + (" with a channel mapping" if expected == "nonempty_mapping" else "")
                + "; the complete response is in the private HTTP transcript when a response existed.",
            ))
    return checks


def _outcome_from_exercise(
    exercise_checks: Sequence[Check],
    names: Sequence[str],
    *,
    pass_reason: str,
    untested_reason: str,
    attempt_names: Sequence[str] | None = None,
    acceptable_skips: Sequence[str] = (),
) -> ScopeFunctionalOutcome:
    selected = [item for item in exercise_checks if item.name in names]
    attempted_names = set(names if attempt_names is None else attempt_names)
    behavior_attempted = any(
        item.name in attempted_names and item.status != SKIPPED
        for item in exercise_checks
    )
    if not behavior_attempted:
        return ScopeFunctionalOutcome("UNTESTED", untested_reason)
    acceptable_skip_names = set(acceptable_skips)
    attempted = [item for item in selected if item.status != SKIPPED]
    if selected and all(
        item.status == READY or (item.status == SKIPPED and item.name in acceptable_skip_names)
        for item in selected
    ):
        return ScopeFunctionalOutcome("PASS", pass_reason)
    definitive_markers = (
        "definitively rejected", "read-back definitively returned",
        "rejection notice", "restoration was not verified",
    )
    definitive_failures = [
        item for item in attempted
        if item.status == ERROR
        or (
            item.status == UNAVAILABLE
            and any(marker in item.detail.casefold() for marker in definitive_markers)
        )
    ]
    if definitive_failures:
        failed = ", ".join(
            item.name for item in definitive_failures
        )
        failure_status = ERROR if any(item.status == ERROR for item in definitive_failures) else UNAVAILABLE
        return ScopeFunctionalOutcome(
            "FAIL", f"Explicit allowlisted exercise did not pass: {failed}.", failure_status,
        )
    indeterminate = [item.name for item in attempted if item.status == UNAVAILABLE]
    if indeterminate:
        return ScopeFunctionalOutcome(
            "UNTESTED",
            "The exercise was attempted but its result was indeterminate: "
            + ", ".join(indeterminate)
            + ". No functional failure is inferred from ambiguous transport/protocol evidence.",
        )
    return ScopeFunctionalOutcome("UNTESTED", untested_reason)


_FAILURE_RUN: ContextVar[DiagnosticRun | None] = ContextVar("medlar_diagnostic_run", default=None)
_FAILURE_STAGE: ContextVar[str] = ContextVar("medlar_diagnostic_stage", default="collection")


def _exception_evidence(exc: BaseException) -> str:
    # Private local report only. Include traceback/message, never collect locals.
    return "".join(traceback.TracebackException.from_exception(exc, capture_locals=False).format())


def _capture_failure(exc: BaseException, stage: str | None = None) -> None:
    run = _FAILURE_RUN.get()
    if run is None:
        return
    entry = {"stage": stage or (_FAILURE_STAGE.get() + "/" + (exc.__traceback__.tb_frame.f_code.co_name if exc.__traceback__ else "unknown")), "exception": type(exc).__name__,
             "traceback": _exception_evidence(exc)}
    if entry not in run.failures:
        run.failures.append(entry)


def _run_check_stage(stage: str, callback: Callable[[], list[Check]]) -> list[Check]:
    marker = _FAILURE_STAGE.set(stage)
    try:
        return callback()
    except Exception as exc:
        _capture_failure(sys.exc_info()[1])
        _capture_failure(exc)
        run = _FAILURE_RUN.get()
        if run is not None:
            run.collection_failed = True
        return [Check("Diagnostic stage failures", stage, ERROR,
                      f"{type(exc).__name__}: {exc}; full traceback is recorded in this report.")]
    finally:
        _FAILURE_STAGE.reset(marker)


def _mandatory_pass(checks: Sequence[Check], required: Sequence[tuple[str, str]]) -> bool:
    return all(len(rows := [row for row in checks if (row.section, row.name) == key]) == 1
               and rows[0].status == READY for key in required)


def _required_live_rows() -> list[tuple[str, str]]:
    return [("Live Twitch", name) for name in (
        "Bot token validation", "Bot identity", "Bot feature scopes", "Read-only IRC authentication and join",
        "Bot authentication", "Broadcaster token validation", "Broadcaster identity", "Broadcaster feature scopes",
        "Broadcaster authentication", "App credential exchange", "Read-only channel API",
        "Account-age source data", "Broadcaster/API identity correlation", "App/API authentication")
    ] + [("Live Twitch - Read-only API coverage", name) for name in READ_ONLY_TWITCH_API_NAMES]


def _finish_coverage(run: DiagnosticRun) -> None:
    # Preserve existing rows and append explicit evidence for anything not reached.
    present = {(row.section, row.name) for row in run.checks}
    for row in _blocked_live_checks("Stage was not reached or returned no evidence; see failures in this report.",
                                    exercise_requested=run.exercise_requested):
        if (row.section, row.name) not in present:
            run.checks.append(row)
            present.add((row.section, row.name))
    required = _required_live_rows()
    if run.exercise_requested:
        required.append(("Full-live Twitch exercise", "Full exercise result"))
    run.complete = not run.offline and not run.collection_failed and _mandatory_pass(run.checks, required)
    detail = ("All mandatory authentication, identity, scope and read-only API stages passed."
              if run.complete else "Collection is incomplete: missing, skipped, failed, conflicting or interrupted mandatory stages remain; individual evidence is retained.")
    if run.offline:
        detail = "OFFLINE/INCOMPLETE MODE: Twitch validation was not requested; local diagnostic evidence is retained."
    for index, row in enumerate(run.checks):
        if (row.section, row.name) == ("Diagnostic coverage", "Run completeness"):
            run.checks[index] = Check(row.section, row.name, READY if run.complete else SKIPPED, detail)
            break
    else:
        run.checks.append(Check("Diagnostic coverage", "Run completeness", READY if run.complete else SKIPPED, detail))


def _failure_evidence_lines(run: DiagnosticRun, *, markdown: bool) -> list[str]:
    lines = ["", "## Diagnostic failures and tracebacks" if markdown else "DIAGNOSTIC FAILURES AND TRACEBACKS",
             "" if markdown else "=" * 72]
    if not run.failures:
        lines.append("No handled exception or interrupted collection was recorded.")
    for index, entry in enumerate(run.failures, 1):
        lines.extend([f"Failure {index}: stage={entry['stage']}; exception={entry['exception']}",
                      "<<<PRIVATE TRACEBACK", entry["traceback"], "PRIVATE TRACEBACK"])
    return lines


@dataclass(frozen=True)
class ExerciseGate:
    local_stopped: bool
    remote_offline: bool
    marker: Any = field(repr=False, compare=False)


_EXERCISE_GATE_MARKER = object()


def _exercise_gate_from_checks(checks: Sequence[Check]) -> ExerciseGate | None:
    required = [("Required execution baseline", name) for name in (
        "Medlar closed", "Medlar Twitch disconnected", "Ollama processes stopped", "Ollama endpoint stopped",
        "Local stopped-state gate", "Broadcaster offline before diagnostic", "Remote baseline gate")]
    return ExerciseGate(True, True, _EXERCISE_GATE_MARKER) if _mandatory_pass(checks, required) else None


def _valid_exercise_gate(gate: ExerciseGate | None) -> bool:
    return (isinstance(gate, ExerciseGate) and gate.marker is _EXERCISE_GATE_MARKER
            and gate.local_stopped is True and gate.remote_offline is True)


def run_twitch_exercise(
    environment: Mapping[str, str],
    *,
    exercise: TwitchExercise,
    bot_facts: TokenFacts | None,
    broadcaster_facts: TokenFacts | None,
    app_client: str,
    app_token: str,
    resolved_channel_id: str,
    http_json: HttpJson,
    irc_sender: IrcSender = send_irc_diagnostic,
    gate_context: ExerciseGate | None = None,
) -> tuple[list[Check], bool]:
    """Exercise only title, category, and one chat message; restore channel state."""
    try:
        validate_twitch_exercise(exercise)
    except ValueError:
        _capture_failure(sys.exc_info()[1])
        checks = [Check(
            "Full-live Twitch exercise", "Exercise preflight", UNAVAILABLE,
            "Exercise values failed safety validation; values were not echoed and no Twitch action was attempted.",
        )]
        checks.extend(_skipped_exercise_checks(
            "Exercise values failed safety validation; no Twitch action was attempted."
        )[1:-1])
        checks.append(Check(
            "Full-live Twitch exercise", "Full exercise result", UNAVAILABLE,
            "Exercise values failed safety validation; no Twitch action was attempted.",
        ))
        return checks, True
    if not _valid_exercise_gate(gate_context):
        rows = _skipped_exercise_checks("Validated local/remote stopped-state context is unavailable; no action attempted.")
        rows[-1] = Check("Full-live Twitch exercise", "Full exercise result", UNAVAILABLE, "Stopped-state gate context is missing or invalid.")
        return rows, True
    checks: list[Check] = []
    target = exercise.target_channel.strip().lstrip("#")
    configured_channel = str(environment.get("TWITCH_CHANNEL", "")).strip().lstrip("#")
    bot_login = str(environment.get("TWITCH_NICK", "")).strip()
    broadcaster_client = str(environment.get("DEVILMEDLAR_TWITCH_CLIENT_ID", "")).strip()
    broadcaster_token = _normalized_oauth_token(
        str(environment.get("DEVILMEDLAR_TWITCH_TOKEN", "")).strip()
    )
    bot_token = str(environment.get("MEDLARTV_TWITCH_TOKEN", "")).strip()
    checks.append(Check(
        "Full-live Twitch exercise", "Action plan", READY,
        f"Target=#{target}; temporary title={exercise.title!r}; temporary category={exercise.category!r}; "
        f"one chat message={exercise.chat_message!r}. Allowlist: title, category, and this single message only; "
        "no moderation, permissions, ads, security, bans/timeouts, channel-points, or other mutations. "
        "The sequence is fail-closed: later actions stop after an unverified result. Temporary changes can be externally visible; "
        "restoration is attempted and verified but can fail or be interrupted. Field-aware restoration preserves a third-party "
        "edit already visible in the immediate pre-restoration read-back. Twitch offers no compare-and-swap here, so an edit in "
        "the narrow GET-to-PATCH race cannot be guaranteed safe; the final read-back exposes any mismatch. The chat message cannot be undone.",
    ))

    blockers: list[str] = []
    if not configured_channel:
        blockers.append("TWITCH_CHANNEL is not explicitly configured")
    if not bot_login:
        blockers.append("TWITCH_NICK is not explicitly configured")
    if target.casefold() != configured_channel.casefold():
        blockers.append("explicit target does not match TWITCH_CHANNEL")
    if bot_facts is None:
        blockers.append("bot token validation is unavailable")
    else:
        if bot_facts.login.casefold() != bot_login.casefold():
            blockers.append("bot token login does not match TWITCH_NICK")
        missing_bot = {"chat:read", "chat:edit"} - bot_facts.scopes
        if missing_bot:
            blockers.append("bot token lacks current IRC read/send scopes")
    if broadcaster_facts is None:
        blockers.append("broadcaster token validation is unavailable")
    else:
        if broadcaster_facts.login.casefold() != target.casefold():
            blockers.append("broadcaster token login does not match the explicit target")
        if broadcaster_facts.client_id != broadcaster_client:
            blockers.append("broadcaster token client ID does not match its configured client ID")
        if broadcaster_facts.user_id != resolved_channel_id:
            blockers.append("broadcaster token user ID does not match the app channel lookup")
        if "channel:manage:broadcast" not in broadcaster_facts.scopes:
            blockers.append("broadcaster token lacks channel:manage:broadcast")
    if not app_client or not app_token or not resolved_channel_id:
        blockers.append("app exchange and exact channel lookup are required")
    if not broadcaster_client or not broadcaster_token or not bot_token:
        blockers.append("required credential fields are incomplete")
    if blockers:
        checks.append(Check(
            "Full-live Twitch exercise", "Exercise preflight", UNAVAILABLE,
            "; ".join(blockers) + ". No mutation or chat send was attempted.",
        ))
        checks.extend(_skipped_exercise_checks(
            "Preflight did not pass; no mutation or chat send was attempted."
        )[1:-1])
        checks.append(Check(
            "Full-live Twitch exercise", "Full exercise result", UNAVAILABLE,
            "Preflight did not pass; no mutation or chat send was attempted.",
        ))
        return checks, True

    app_headers = {"Client-ID": app_client, "Authorization": f"Bearer {app_token}"}
    user_headers = {"Client-ID": broadcaster_client, "Authorization": f"Bearer {broadcaster_token}"}
    channel_url = "https://api.twitch.tv/helix/channels?" + urllib_parse.urlencode({
        "broadcaster_id": resolved_channel_id,
    })
    original_response = _safe_http_call(
        http_json, "GET", channel_url, headers=app_headers, timeout=10.0,
    )
    original = _channel_state_from_response(original_response)
    if original_response.status != 200 or original_response.error or original is None:
        checks.append(Check(
            "Full-live Twitch exercise", "Original channel-state snapshot", UNAVAILABLE,
            f"GET /helix/channels did not return one usable row (HTTP {original_response.status or 'unavailable'}). No mutation or chat send was attempted.",
        ))
        checks.append(Check(
            "Full-live Twitch exercise", "Exercise preflight", UNAVAILABLE,
            "A restorable original title/category snapshot is required before any action.",
        ))
        checks.extend(_skipped_exercise_checks(
            "The original channel state was unavailable; no mutation or chat send was attempted."
        )[2:-1])
        checks.append(Check(
            "Full-live Twitch exercise", "Full exercise result", UNAVAILABLE,
            "The original channel state was unavailable; no mutation or chat send was attempted.",
        ))
        return checks, True
    checks.append(Check(
        "Full-live Twitch exercise", "Original channel-state snapshot", READY,
        f"HTTP 200; original title={original['title']!r}; original category={original['game_name'] or '(unset)'!r}; "
        f"broadcaster_id={resolved_channel_id!r}. Exact request headers and response body are in the private HTTP transcript.",
    ))

    search_url = "https://api.twitch.tv/helix/search/categories?" + urllib_parse.urlencode({
        "query": exercise.category, "first": "100",
    })
    category_response = _safe_http_call(
        http_json, "GET", search_url, headers=app_headers, timeout=10.0,
    )
    category_rows = category_response.payload.get("data") if isinstance(category_response.payload, dict) else None
    category_row = next((
        row for row in category_rows
        if isinstance(row, dict)
        and str(row.get("name") or "").casefold() == exercise.category.casefold()
        and _nonempty(row.get("id"))
    ), None) if isinstance(category_rows, list) else None
    if category_response.status != 200 or category_response.error or category_row is None:
        checks.append(Check(
            "Full-live Twitch exercise", "Category resolution", UNAVAILABLE,
            f"No exact case-insensitive category match was resolved (HTTP {category_response.status or 'unavailable'}); a substring result was not used.",
        ))
        checks.append(Check(
            "Full-live Twitch exercise", "Exercise preflight", UNAVAILABLE,
            "Exact category resolution is required before any action.",
        ))
        checks.extend(_skipped_exercise_checks(
            "Category preflight failed; no mutation or chat send was attempted."
        )[3:-1])
        checks.append(Check(
            "Full-live Twitch exercise", "Full exercise result", UNAVAILABLE,
            "Category preflight failed; no mutation or chat send was attempted.",
        ))
        return checks, True
    category_id = str(category_row["id"])
    resolved_category_name = str(category_row.get("name") or exercise.category)
    checks.append(Check(
        "Full-live Twitch exercise", "Category resolution", READY,
        f"HTTP 200; exact category match resolved as {resolved_category_name!r}.",
    ))

    state_blockers: list[str] = []
    if not original["title"]:
        state_blockers.append("original title is empty and cannot be safely restored through this endpoint")
    if original["title"] == exercise.title:
        state_blockers.append("temporary title equals the original title")
    if original["game_id"] == category_id:
        state_blockers.append("temporary category equals the original category")
    if state_blockers:
        checks.append(Check(
            "Full-live Twitch exercise", "Exercise preflight", UNAVAILABLE,
            "; ".join(state_blockers) + ". Choose different temporary values; no action was attempted.",
        ))
        checks.extend(_skipped_exercise_checks(
            "Requested temporary state would not prove a reversible change; no action was attempted."
        )[3:-1])
        checks.append(Check(
            "Full-live Twitch exercise", "Full exercise result", UNAVAILABLE,
            "Requested temporary state would not prove a reversible change; no action was attempted.",
        ))
        return checks, True

    stream_url = "https://api.twitch.tv/helix/streams?" + urllib_parse.urlencode({
        "user_id": resolved_channel_id,
    })
    offline_response = _safe_http_call(
        http_json, "GET", stream_url, headers=app_headers, timeout=10.0,
    )
    offline_rows = (
        offline_response.payload.get("data")
        if isinstance(offline_response.payload, dict) else None
    )
    offline_confirmed = (
        offline_response.status == 200
        and not offline_response.error
        and isinstance(offline_rows, list)
        and len(offline_rows) == 0
    )
    checks.append(Check(
        "Full-live Twitch exercise", "Broadcaster offline before mutation",
        READY if offline_confirmed else UNAVAILABLE,
        "GET /helix/streams returned HTTP 200 with data=[] immediately before mutation; the broadcaster was offline at this sampled instant."
        if offline_confirmed else
        (
            f"Broadcaster is live: GET /helix/streams returned {len(offline_rows)} active stream row(s); no mutation or chat send was attempted."
            if offline_response.status == 200 and isinstance(offline_rows, list) and offline_rows else
            f"Broadcaster offline state was not positively established (HTTP {offline_response.status or 'unavailable'}); no mutation or chat send was attempted."
        ),
    ))
    if not offline_confirmed:
        checks.append(Check(
            "Full-live Twitch exercise", "Exercise preflight", UNAVAILABLE,
            "The broadcaster-offline requirement did not pass; no mutation or chat send was attempted.",
        ))
        remaining = {
            "Title update request", "Title update verification", "Category update request",
            "Category update verification", "Restoration request", "Title restoration verification",
            "Category restoration verification", "Broadcaster offline before chat",
            "IRC connection and authentication", "Diagnostic chat command write",
            "IRC post-send acknowledgement", "Independent chat delivery observation",
        }
        checks.extend(
            Check("Full-live Twitch exercise", name, SKIPPED,
                  "Broadcaster offline state was not confirmed; no mutation or chat send was attempted.")
            for name in _exercise_check_names()
            if name in remaining
        )
        checks.append(Check(
            "Full-live Twitch exercise", "Full exercise result", UNAVAILABLE,
            "Broadcaster offline state was not confirmed; no mutation or chat send was attempted.",
        ))
        return checks, True
    checks.append(Check(
        "Full-live Twitch exercise", "Exercise preflight", READY,
        "All identity, scope, snapshot, exact-category, reversible-value, local stopped-state, and broadcaster-offline requirements passed before any action.",
    ))

    patch_url = "https://api.twitch.tv/helix/channels?" + urllib_parse.urlencode({
        "broadcaster_id": resolved_channel_id,
    })
    title_restore_eligible = False
    category_restore_eligible = False
    restoration_failed = False
    action_failed = False
    unsafe_to_continue = False
    title_verified = False
    category_verified = False
    try:
        title_restore_eligible = True
        before_title_response = _safe_http_call(http_json, "GET", channel_url, headers=app_headers, timeout=10.0)
        before_title = _channel_state_from_response(before_title_response) if before_title_response.status == 200 and not before_title_response.error and not before_title_response.body_truncated else None
        if before_title is None or before_title["title"] != original["title"]:
            title_restore_eligible = False
            raise RuntimeError("Title changed or became unknown before temporary write")
        title_response = _safe_http_call(
            http_json, "PATCH", patch_url, headers=user_headers,
            json_body={"title": exercise.title}, allow_empty=True, timeout=10.0,
        )
        title_accepted = title_response.status == 204 and not title_response.error
        checks.append(Check(
            "Full-live Twitch exercise", "Title update request",
            READY if title_accepted else UNAVAILABLE,
            f"PATCH /helix/channels returned HTTP {title_response.status or 'unavailable'}; "
            + (
                "accepted by the API, pending read-back verification."
                if title_accepted else
                "definitively rejected by the API; complete response evidence is in the private HTTP transcript."
                if title_response.status is not None and 400 <= title_response.status < 500 else
                "outcome is indeterminate; complete response evidence is in the private HTTP transcript when a response existed."
            ),
        ))
        if title_accepted:
            verify_title_response = _safe_http_call(
                http_json, "GET", channel_url, headers=app_headers, timeout=10.0,
            )
            title_state = (
                _channel_state_from_response(verify_title_response)
                if verify_title_response.status == 200 and not verify_title_response.error
                else None
            )
            title_verified = bool(title_state and title_state["title"] == exercise.title)
            checks.append(Check(
                "Full-live Twitch exercise", "Title update verification",
                READY if title_verified else UNAVAILABLE,
                "Read-back confirmed the temporary title." if title_verified else
                "Read-back definitively returned a different title; later actions were stopped and restoration was attempted."
                if title_state is not None else
                "Title read-back was unavailable or unusable, so the mutation effect is indeterminate; later actions were stopped and restoration was attempted.",
            ))
            if not title_verified:
                unsafe_to_continue = True
                action_failed = True
        else:
            action_failed = True
            unsafe_to_continue = True
            # A 4xx response is a definitive client-side rejection. Transport
            # failures, 5xx responses, redirects, and other unexpected statuses
            # can be returned after an intermediary or server applied the write,
            # so they must keep restoration armed.
            if (
                title_response.status is not None
                and 400 <= title_response.status < 500
            ):
                title_restore_eligible = False
            checks.append(Check(
                "Full-live Twitch exercise", "Title update verification", SKIPPED,
                "No accepted title request was available to verify.",
            ))
            if title_response.status is None:
                unsafe_to_continue = True

        if unsafe_to_continue:
            checks.extend([
                Check("Full-live Twitch exercise", "Category update request", SKIPPED,
                      "Stopped after a rejected, ambiguous, or unverified title outcome; restoration takes priority."),
                Check("Full-live Twitch exercise", "Category update verification", SKIPPED,
                      "Category update was not attempted."),
            ])
        else:
            before_category_response = _safe_http_call(http_json, "GET", channel_url, headers=app_headers, timeout=10.0)
            before_category = _channel_state_from_response(before_category_response) if before_category_response.status == 200 and not before_category_response.error and not before_category_response.body_truncated else None
            if before_category is None or before_category["game_id"] != original["game_id"]:
                category_restore_eligible = False
                raise RuntimeError("Category changed or became unknown before temporary write")
            category_restore_eligible = True
            category_update = _safe_http_call(
                http_json, "PATCH", patch_url, headers=user_headers,
                json_body={"game_id": category_id}, allow_empty=True, timeout=10.0,
            )
            category_accepted = category_update.status == 204 and not category_update.error
            checks.append(Check(
                "Full-live Twitch exercise", "Category update request",
                READY if category_accepted else UNAVAILABLE,
                f"PATCH /helix/channels returned HTTP {category_update.status or 'unavailable'}; "
                + (
                    "accepted by the API, pending read-back verification."
                    if category_accepted else
                    "definitively rejected by the API; complete response evidence is in the private HTTP transcript."
                    if category_update.status is not None and 400 <= category_update.status < 500 else
                    "outcome is indeterminate; complete response evidence is in the private HTTP transcript when a response existed."
                ),
            ))
            if category_accepted:
                verify_category_response = _safe_http_call(
                    http_json, "GET", channel_url, headers=app_headers, timeout=10.0,
                )
                category_state = (
                    _channel_state_from_response(verify_category_response)
                    if verify_category_response.status == 200 and not verify_category_response.error
                    else None
                )
                category_verified = bool(category_state and category_state["game_id"] == category_id)
                checks.append(Check(
                    "Full-live Twitch exercise", "Category update verification",
                    READY if category_verified else UNAVAILABLE,
                    "Read-back confirmed the temporary category." if category_verified else
                    "Read-back definitively returned a different category; restoration was attempted."
                    if category_state is not None else
                    "Category read-back was unavailable or unusable, so the mutation effect is indeterminate; restoration was attempted.",
                ))
                if not category_verified:
                    action_failed = True
                    unsafe_to_continue = True
            else:
                action_failed = True
                unsafe_to_continue = True
                if (
                    category_update.status is not None
                    and 400 <= category_update.status < 500
                ):
                    category_restore_eligible = False
                checks.append(Check(
                    "Full-live Twitch exercise", "Category update verification", SKIPPED,
                    "No accepted category request was available to verify.",
                ))
    except Exception:
        _capture_failure(sys.exc_info()[1])
        action_failed = True
        unsafe_to_continue = True
        checks.append(Check(
            "Full-live Twitch exercise", "Mutation sequence", ERROR,
            "An unexpected internal failure interrupted the allowlisted sequence; restoration was still attempted.",
        ))
    finally:
        current_response = _safe_http_call(
            http_json, "GET", channel_url, headers=app_headers, timeout=10.0,
        )
        current_state = (
            _channel_state_from_response(current_response)
            if current_response.status == 200 and not current_response.error
            else None
        )
        restored_state = current_state
        restore_accepted = False
        already_original = bool(
            current_state
            and current_state["title"] == original["title"]
            and current_state["game_id"] == original["game_id"]
        )
        if current_state is None:
            checks.append(Check(
                "Full-live Twitch exercise", "Restoration request", ERROR,
                "Pre-restoration read-back was unavailable; no blind restoration PATCH was sent because it could overwrite a concurrent channel edit.",
            ))
        elif already_original:
            restore_accepted = True
            checks.append(Check(
                "Full-live Twitch exercise", "Restoration request", SKIPPED,
                "A read-back already showed the original title/category; no restoration PATCH was needed.",
            ))
        else:
            restore_body: dict[str, str] = {}
            concurrent_fields: list[str] = []
            if current_state["title"] != original["title"]:
                if title_restore_eligible and current_state["title"] == exercise.title:
                    restore_body["title"] = original["title"]
                else:
                    concurrent_fields.append("title")
            if current_state["game_id"] != original["game_id"]:
                if category_restore_eligible and current_state["game_id"] == category_id:
                    restore_body["game_id"] = original["game_id"]
                else:
                    concurrent_fields.append("category")

            if not restore_body:
                checks.append(Check(
                    "Full-live Twitch exercise", "Restoration request", ERROR,
                    "Read-back found an unexpected concurrent or unattributable channel change; it was preserved and no stale restoration PATCH was sent.",
                ))
            else:
                restore_response = _safe_http_call(
                    http_json, "PATCH", patch_url, headers=user_headers,
                    json_body=restore_body, allow_empty=True, timeout=10.0,
                )
                patch_accepted = restore_response.status == 204 and not restore_response.error
                restore_accepted = patch_accepted and not concurrent_fields
                if concurrent_fields:
                    request_detail = (
                        f"Eligible field restoration returned HTTP {restore_response.status or 'unavailable'}; "
                        "an unexpected concurrent channel edit was preserved instead of overwritten."
                    )
                else:
                    request_detail = (
                        f"Field-aware restoration returned HTTP {restore_response.status or 'unavailable'}; "
                        + ("accepted, pending read-back verification." if patch_accepted
                           else "channel state may still be changed; inspect it immediately.")
                    )
                checks.append(Check(
                    "Full-live Twitch exercise", "Restoration request",
                    READY if restore_accepted else ERROR,
                    request_detail,
                ))
                restored_state = None
                if patch_accepted:
                    restored_response = _safe_http_call(
                        http_json, "GET", channel_url, headers=app_headers, timeout=10.0,
                    )
                    restored_state = (
                        _channel_state_from_response(restored_response)
                        if restored_response.status == 200 and not restored_response.error
                        else None
                    )

        title_restored = bool(restored_state and restored_state["title"] == original["title"])
        category_restored = bool(restored_state and restored_state["game_id"] == original["game_id"])
        checks.extend([
            Check(
                "Full-live Twitch exercise", "Title restoration verification",
                READY if title_restored else ERROR,
                "Read-back confirmed the original title." if title_restored else
                "Read-back did not confirm the original title; channel state may remain changed.",
            ),
            Check(
                "Full-live Twitch exercise", "Category restoration verification",
                READY if category_restored else ERROR,
                "Read-back confirmed the original category." if category_restored else
                "Read-back did not confirm the original category; channel state may remain changed.",
            ),
        ])
        restoration_failed = not (restore_accepted and title_restored and category_restored)

    if restoration_failed or unsafe_to_continue:
        action_failed = True
        stop_reason = (
            "Chat was not attempted after restoration failed."
            if restoration_failed else
            "Chat was not attempted after an ambiguous or unverified channel-update outcome."
        )
        checks.extend([
            Check("Full-live Twitch exercise", "Broadcaster offline before chat", SKIPPED,
                  stop_reason),
            Check("Full-live Twitch exercise", "IRC connection and authentication", SKIPPED,
                  stop_reason),
            Check("Full-live Twitch exercise", "Diagnostic chat command write", SKIPPED,
                  stop_reason),
            Check("Full-live Twitch exercise", "IRC post-send acknowledgement", SKIPPED,
                  stop_reason),
            Check("Full-live Twitch exercise", "Independent chat delivery observation", SKIPPED,
                  stop_reason),
        ])
    else:
        chat_offline_response = _safe_http_call(
            http_json, "GET", stream_url, headers=app_headers, timeout=10.0,
        )
        chat_offline_rows = (
            chat_offline_response.payload.get("data")
            if isinstance(chat_offline_response.payload, dict) else None
        )
        chat_offline = (
            chat_offline_response.status == 200
            and not chat_offline_response.error
            and isinstance(chat_offline_rows, list)
            and len(chat_offline_rows) == 0
        )
        checks.append(Check(
            "Full-live Twitch exercise", "Broadcaster offline before chat",
            READY if chat_offline else UNAVAILABLE,
            "GET /helix/streams again returned HTTP 200 with data=[] after restoration and immediately before chat."
            if chat_offline else
            "The broadcaster was live or offline state was indeterminate after restoration; the diagnostic chat message was not sent.",
        ))
        if not chat_offline:
            action_failed = True
            stop_reason = "Broadcaster offline state was not confirmed after restoration; no chat message was sent."
            checks.extend([
                Check("Full-live Twitch exercise", "IRC connection and authentication", SKIPPED, stop_reason),
                Check("Full-live Twitch exercise", "Diagnostic chat command write", SKIPPED, stop_reason),
                Check("Full-live Twitch exercise", "IRC post-send acknowledgement", SKIPPED, stop_reason),
                Check("Full-live Twitch exercise", "Independent chat delivery observation", SKIPPED, stop_reason),
            ])
        else:
            try:
                chat_result = irc_sender(bot_login, bot_token, target, exercise.chat_message)
            except Exception:
                _capture_failure(sys.exc_info()[1])
                chat_result = IrcSendResult(error="IRC sender failed")
            chat_ready = chat_result.connected and chat_result.authenticated and chat_result.joined
            checks.extend([
                Check(
                    "Full-live Twitch exercise", "IRC connection and authentication",
                    READY if chat_ready else UNAVAILABLE,
                    "TLS connection, token authentication, and target-channel join were confirmed."
                    if chat_ready else
                    "IRC connection, authentication, or target-channel join was not confirmed. "
                    f"Sanitized protocol/transport result: {chat_result.error or 'no more specific failure was reported'}.",
                ),
                Check(
                    "Full-live Twitch exercise", "Diagnostic chat command write",
                    READY if chat_result.written else UNAVAILABLE,
                    "Exactly one allowlisted PRIVMSG command was written to the authenticated socket."
                    if chat_result.written else
                    "A complete diagnostic PRIVMSG socket write was not confirmed; transmission and delivery may be ambiguous. "
                    f"Sanitized protocol/transport result: {chat_result.error or 'no more specific failure was reported'}.",
                ),
                Check(
                    "Full-live Twitch exercise", "IRC post-send acknowledgement",
                    READY if chat_result.acknowledged and not chat_result.rejected else UNAVAILABLE,
                    "Twitch's documented post-send USERSTATE response arrived and no rejection NOTICE was observed; USERSTATE carries no message ID."
                    if chat_result.acknowledged and not chat_result.rejected else
                    "No documented post-send USERSTATE response was confirmed, or Twitch returned a rejection NOTICE. "
                    f"Sanitized protocol/transport result: {chat_result.error or 'no more specific failure was reported'}.",
                ),
                Check(
                    "Full-live Twitch exercise", "Independent chat delivery observation", SKIPPED,
                    "Twitch does not echo a sender's own PRIVMSG. No second viewer/bot was used, so accepted send is not claimed as independently observed delivery.",
                ),
            ])
            if not (chat_ready and chat_result.written and chat_result.acknowledged and not chat_result.rejected):
                action_failed = True

    checks.append(Check(
        "Full-live Twitch exercise", "Full exercise result",
        ERROR if restoration_failed else (UNAVAILABLE if action_failed else READY),
        (
            "Restoration was not verified; channel state may remain changed."
            if restoration_failed else
            "All requested title, category, chat-protocol, and restoration checks passed; independent viewer delivery was intentionally not claimed."
            if not action_failed else
            "At least one requested functional check did not pass; restoration was verified."
        ),
    ))
    return checks, action_failed or restoration_failed


def run_live_twitch_checks(
    environment: Mapping[str, str],
    *,
    root: Path = REPO_ROOT,
    http_json: HttpJson = request_json,
    exercise: TwitchExercise | None = None,
    irc_sender: IrcSender = send_irc_diagnostic,
    irc_probe: IrcProbe | None = None,
    secret_capture: dict[str, str] | None = None,
    http_exchanges: list[HttpExchangeEvidence] | None = None,
    gate_context: ExerciseGate | None = None,
    partial_checks: list[Check] | None = None,
) -> list[Check]:
    """Run complete read-only validation, plus the gated exercise when requested."""
    if http_exchanges is not None:
        http_json = _recording_http_json(http_json, http_exchanges)
    if exercise is not None and not _valid_exercise_gate(gate_context):
        return _blocked_live_checks("Validated stopped-state context is missing; no live request/action attempted.", exercise_requested=True)
    catalog = _scope_catalog(root)
    bot_login = str(environment.get("TWITCH_NICK", "")).strip()
    channel = str(environment.get("TWITCH_CHANNEL", "")).strip().lstrip("#")
    checks: list[Check] = [] if partial_checks is None else partial_checks
    if exercise is not None:
        try:
            validate_twitch_exercise(exercise)
        except ValueError:
            _capture_failure(sys.exc_info()[1])
            reason = "Exercise values failed safety validation; no credentials were validated and no Twitch action was attempted."
            checks.extend([
                Check("Live Twitch", "Bot token validation", SKIPPED, reason),
                *_skipped_token_checks("Bot", reason),
                Check("Live Twitch", "Broadcaster token validation", SKIPPED, reason),
                *_skipped_token_checks("Broadcaster", reason),
                Check("Live Twitch", "App credential exchange", SKIPPED, reason),
                Check("Live Twitch", "Read-only channel API", SKIPPED, reason),
                Check("Live Twitch", "Account-age source data", SKIPPED, reason),
                Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED, reason),
                Check("Live Twitch", "Read-only IRC authentication and join", SKIPPED, reason),
                *_skipped_read_only_twitch_api_checks(reason),
                Check("Full-live Twitch exercise", "Exercise preflight", UNAVAILABLE, reason),
                *_skipped_exercise_checks(reason)[1:-1],
                Check("Full-live Twitch exercise", "Full exercise result", UNAVAILABLE, reason),
            ])
            return checks
    bot_checks, bot_facts = _validate_user_token(
        role="Bot",
        raw_token=str(environment.get("MEDLARTV_TWITCH_TOKEN", "")),
        expected_login=bot_login,
        configured_client_id=str(environment.get("MEDLARTV_TWITCH_CLIENT_ID", "")).strip(),
        required_scopes=BOT_REQUIRED_SCOPES,
        client_id_required=False,
        catalog=catalog,
        http_json=http_json,
    )
    checks.extend(bot_checks)
    broadcaster_checks, broadcaster_facts = _validate_user_token(
        role="Broadcaster",
        raw_token=str(environment.get("DEVILMEDLAR_TWITCH_TOKEN", "")),
        expected_login=channel,
        configured_client_id=str(environment.get("DEVILMEDLAR_TWITCH_CLIENT_ID", "")).strip(),
        required_scopes=BROADCASTER_REQUIRED_SCOPES,
        client_id_required=True,
        catalog=catalog,
        http_json=http_json,
    )
    checks.extend(broadcaster_checks)

    app_client = str(environment.get("APP_TWITCH_CLIENT_ID", "")).strip()
    app_secret = str(environment.get("APP_SECRET_ID", "")).strip()
    app_token = ""
    resolved_id = ""
    matching_row: Mapping[str, Any] | None = None
    if not app_client or not app_secret:
        checks.extend([
            Check("Live Twitch", "App credential exchange", UNAVAILABLE,
                  "App client ID and secret are both required; no token request was sent."),
            Check("Live Twitch", "Read-only channel API", SKIPPED,
                  "Channel lookup was not attempted because the app exchange did not run."),
            Check("Live Twitch", "Account-age source data", SKIPPED,
                  "GET /helix/users was not available, so the created_at source used by the account-age command was not checked."),
            Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED,
                  "No app lookup was available for a user-ID comparison."),
            *_skipped_read_only_twitch_api_checks(
                "App credential exchange did not run; no dependent API request was sent."
            ),
        ])
    else:
        exchange = _safe_http_call(
            http_json, "POST", "https://id.twitch.tv/oauth2/token",
            form={"client_id": app_client, "client_secret": app_secret,
                  "grant_type": "client_credentials"}, timeout=10.0,
        )
        token_value = exchange.payload.get("access_token") if isinstance(exchange.payload, dict) else None
        if not _usable_app_exchange(exchange):
            checks.extend([
                Check("Live Twitch", "App credential exchange", UNAVAILABLE,
                      f"App-token POST returned HTTP {exchange.status or 'unavailable'} without a structurally valid, unexpired bearer token; complete response evidence is in the private HTTP transcript when a response existed."),
                Check("Live Twitch", "Read-only channel API", SKIPPED,
                      "Channel lookup was not attempted because the app exchange failed."),
                Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED,
                      "No app lookup was available for a user-ID comparison."),
                Check("Live Twitch", "Account-age source data", SKIPPED, "App exchange failed; source data was unavailable."),
                *_skipped_read_only_twitch_api_checks("App exchange failed; dependent requests were not attempted."),
            ])
        else:
            app_token = token_value
            if secret_capture is not None:
                secret_capture["TWITCH_EPHEMERAL_APP_ACCESS_TOKEN"] = app_token
            checks.append(Check(
                "Live Twitch", "App credential exchange", READY,
                "App-token POST returned HTTP 200 with an ephemeral app token; app tokens carry no user scopes. "
                "The exact token is retained only in the private/full credential block and its use is shown in the HTTP transcript.",
            ))
            if not channel:
                checks.extend([
                    Check("Live Twitch", "Read-only channel API", SKIPPED,
                          "TWITCH_CHANNEL is empty, so no user lookup was sent."),
                    Check("Live Twitch", "Account-age source data", SKIPPED,
                          "TWITCH_CHANNEL is empty, so GET /helix/users was not sent."),
                    Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED,
                          "No app lookup was available for a user-ID comparison."),
                    *_skipped_read_only_twitch_api_checks(
                        "TWITCH_CHANNEL is empty, so dependent API requests were not sent."
                    ),
                ])
            else:
                query = urllib_parse.urlencode({"login": channel})
                lookup = _safe_http_call(
                    http_json, "GET", f"https://api.twitch.tv/helix/users?{query}",
                    headers={"Client-ID": app_client, "Authorization": f"Bearer {app_token}"},
                    timeout=10.0,
                )
                rows = lookup.payload.get("data") if isinstance(lookup.payload, dict) else None
                matching_row = None
                if isinstance(rows, list) and len(rows) == 1:
                    matching_row = next((
                        row for row in rows
                        if isinstance(row, dict)
                        and _nonempty(row.get("id")) and _nonempty(row.get("login"))
                        and row["login"].casefold() == channel.casefold()
                    ), None)
                if lookup.status == 200 and not lookup.error and matching_row is not None:
                    resolved_id = str(matching_row.get("id") or "")
                    resolved_login = str(matching_row.get("login") or "")
                    checks.append(Check(
                        "Live Twitch", "Read-only channel API", READY,
                        f"GET /helix/users returned HTTP 200 with an exact configured-login row; public login={resolved_login!r}, user_id={resolved_id or '(missing)'!r}.",
                    ))
                    created_at = matching_row.get("created_at")
                    created_at_valid = False
                    if isinstance(created_at, str) and created_at.strip():
                        try:
                            datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
                        except ValueError:
                            _capture_failure(sys.exc_info()[1])
                            pass
                        else:
                            created_at_valid = True
                    checks.append(Check(
                        "Live Twitch", "Account-age source data",
                        READY if created_at_valid else UNAVAILABLE,
                        f"GET /helix/users supplied parseable created_at={str(created_at)!r}, used by the account-age command."
                        if created_at_valid else
                        "GET /helix/users resolved the account but did not supply a parseable created_at timestamp; account-age behavior is not proven.",
                    ))
                    if broadcaster_facts is None:
                        checks.append(Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED,
                                            "Broadcaster token validation was unavailable for a user-ID comparison."))
                    elif resolved_id and broadcaster_facts.user_id == resolved_id:
                        checks.append(Check(
                            "Live Twitch", "Broadcaster/API identity correlation", READY,
                            f"Validated broadcaster login={broadcaster_facts.login!r}, user_id={broadcaster_facts.user_id!r} matches the app lookup.",
                        ))
                    else:
                        checks.append(Check(
                            "Live Twitch", "Broadcaster/API identity correlation", UNAVAILABLE,
                            "The broadcaster token user ID does not match the configured channel lookup, or one ID was absent.",
                        ))
                    checks.extend(_read_only_twitch_api_checks(
                        app_client=app_client, app_token=app_token,
                        channel_id=resolved_id, http_json=http_json,
                    ))
                else:
                    checks.extend([
                        Check("Live Twitch", "Read-only channel API", UNAVAILABLE,
                              f"GET /helix/users returned HTTP {lookup.status or 'unavailable'} without an exact channel row; complete response evidence is in the private HTTP transcript when a response existed."),
                        Check("Live Twitch", "Account-age source data", SKIPPED,
                              "No exact GET /helix/users row was available; created_at was not checked."),
                        Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED,
                              "No successful app lookup was available for a user-ID comparison."),
                        *_skipped_read_only_twitch_api_checks(
                            "Exact configured-channel lookup failed; no dependent API request was sent."
                        ),
                    ])

    bot_token = str(environment.get("MEDLARTV_TWITCH_TOKEN", "")).strip()
    irc_ready = False
    if irc_probe is None:
        checks.append(Check(
            "Live Twitch", "Read-only IRC authentication and join", SKIPPED,
            "No IRC probe implementation was supplied to this direct library call; no socket was opened.",
        ))
    elif bot_facts is None or not bot_login or not bot_token or not channel:
        checks.append(Check(
            "Live Twitch", "Read-only IRC authentication and join", SKIPPED,
            "Validated bot identity, token, and configured channel are required; no IRC connection was attempted.",
        ))
    else:
        try:
            irc_result = irc_probe(bot_login, bot_token, channel)
        except Exception:
            _capture_failure(sys.exc_info()[1])
            irc_result = IrcProbeResult(error="IRC probe failed")
        irc_ready = irc_result.connected and irc_result.authenticated and irc_result.joined
        checks.append(Check(
            "Live Twitch", "Read-only IRC authentication and join",
            READY if irc_ready else UNAVAILABLE,
            "TLS connection, IRC token authentication, and target-channel join were confirmed; no PRIVMSG was sent and no received chat content was retained."
            if irc_ready else
            "IRC connection, token authentication, or target-channel join was not confirmed; no PRIVMSG was sent. "
            f"Sanitized protocol/transport result: {irc_result.error or 'no more specific failure was reported'}.",
        ))

    exercise_checks: list[Check]
    if exercise is None:
        exercise_checks = _skipped_exercise_checks(
            "Normal live diagnostics are read-only. Use --exercise-twitch with an explicit target, temporary title, temporary category, and diagnostic message."
        )
    else:
        exercise_checks, _exercise_failed = run_twitch_exercise(
            environment, exercise=exercise, gate_context=gate_context, bot_facts=bot_facts,
            broadcaster_facts=broadcaster_facts, app_client=app_client,
            app_token=app_token, resolved_channel_id=resolved_id,
            http_json=http_json, irc_sender=irc_sender,
        )
    checks.extend(exercise_checks)

    bot_outcomes = {
        "chat:read": ScopeFunctionalOutcome(
            "UNTESTED",
            "IRC authentication/join does not prove receipt and handling of an independent viewer PRIVMSG; viewer messages were not collected.",
        ),
        "chat:edit": _outcome_from_exercise(
            exercise_checks,
            ("IRC connection and authentication", "Diagnostic chat command write", "IRC post-send acknowledgement"),
            pass_reason="The explicit exercise authenticated, joined, wrote exactly one diagnostic PRIVMSG, and received Twitch's documented sender-side USERSTATE response; independent viewer delivery remains unobserved.",
            untested_reason="No diagnostic message write was attempted, so send functionality was not tested.",
            attempt_names=("IRC connection and authentication",),
        ),
    }
    broadcaster_outcomes = {
        "channel:manage:broadcast": _outcome_from_exercise(
            exercise_checks,
            (
                "Title update request", "Title update verification",
                "Category update request", "Category update verification",
                "Restoration request", "Title restoration verification",
                "Category restoration verification",
            ),
            pass_reason="The explicit exercise changed and read back title and category, then restored and read back both original fields.",
            untested_reason="No title/category mutation was attempted after the safety preflight, so behavior was not tested.",
            attempt_names=("Title update request", "Category update request"),
            acceptable_skips=("Restoration request",),
        ),
        "moderator:manage:shoutouts": ScopeFunctionalOutcome(
            "UNTESTED",
            "Shoutouts are externally visible and outside the diagnostic mutation allowlist; no POST was sent.",
        ),
    }
    if bot_facts is not None:
        checks.extend(detailed_scope_checks("Bot", sorted(bot_facts.scopes), catalog, bot_outcomes))
        checks.extend(feature_scope_checks(
            "Bot", sorted(bot_facts.scopes), BOT_REQUIRED_SCOPES, bot_outcomes,
        ))
    if broadcaster_facts is not None:
        checks.extend(detailed_scope_checks(
            "Broadcaster", sorted(broadcaster_facts.scopes), catalog, broadcaster_outcomes,
        ))
        checks.extend(feature_scope_checks(
            "Broadcaster", sorted(broadcaster_facts.scopes),
            BROADCASTER_REQUIRED_SCOPES, broadcaster_outcomes,
        ))
    checks.extend([
        Check(
            "Live Twitch - Functional boundaries", "Shoutout POST", SKIPPED,
            "Functional=UNTESTED; POST /helix/chat/shoutouts is externally visible and outside the title/category/single-message allowlist.",
        ),
        Check(
            "Live Twitch - Functional boundaries", "Moderation and channel administration", SKIPPED,
            "Functional=UNTESTED; no bans, timeouts, deletes, AutoMod, channel settings, permissions, ads, security, clear-chat, rewards, polls, raids, schedules, or VIP changes were attempted.",
        ),
        Check(
            "Live Twitch - Functional boundaries", "Dormant GET /helix/games/top", SKIPPED,
            "Functional=UNTESTED; the helper exists but is not on the active Medlar startup/chat path, so the live diagnostic does not call it merely to inflate coverage.",
        ),
        Check(
            "Live Twitch - Functional boundaries", "EventSub and IRC event behavior", SKIPPED,
            "Functional=UNTESTED; no production EventSub subscription transport is present, and no synthetic Twitch events or viewer messages were injected into the live channel.",
        ),
    ])
    checks.extend([
        Check(
            "Live Twitch", "Bot authentication",
            READY if bot_facts is not None and irc_ready and _mandatory_pass(checks, [("Live Twitch", "Bot identity")]) else UNAVAILABLE,
            "Token validation, configured identity comparison, TLS IRC authentication, and channel join all passed; no chat message was sent by the read-only probe."
            if bot_facts is not None and irc_ready and _mandatory_pass(checks, [("Live Twitch", "Bot identity")]) else
            "Bot token validation, identity comparison or read-only IRC authentication/join did not pass; see the independent rows for the exact boundary.",
        ),
        Check(
            "Live Twitch", "Broadcaster authentication",
            READY if broadcaster_facts is not None and _mandatory_pass(checks, [("Live Twitch", "Broadcaster identity")]) else UNAVAILABLE,
            "Broadcaster token validation returned usable identity and scope metadata."
            if broadcaster_facts is not None and _mandatory_pass(checks, [("Live Twitch", "Broadcaster identity")]) else
            "Broadcaster token validation or configured identity comparison did not pass.",
        ),
        Check(
            "Live Twitch", "App/API authentication",
            READY if app_token and resolved_id and _mandatory_pass(checks, [("Live Twitch", "Broadcaster/API identity correlation")]) else UNAVAILABLE,
            "App-token exchange and exact configured-channel lookup passed."
            if app_token and resolved_id and _mandatory_pass(checks, [("Live Twitch", "Broadcaster/API identity correlation")]) else
            "App-token exchange, exact configured-channel lookup or identity correlation did not pass.",
        ),
    ])
    return checks


def classify_process_snapshot(rows: Sequence[Mapping[str, Any]], root: Path, irc_rows: Sequence[Mapping[str, Any]] | None = None) -> StoppedBaseline:
    import ntpath
    base = ntpath.normcase(ntpath.normpath(str(root))).rstrip("\\")
    def owned_path(value: Any) -> bool:
        if not isinstance(value, str): return False
        value = ntpath.normcase(ntpath.normpath(value.strip('"')))
        return value == base or value.startswith(base + "\\")
    if not isinstance(rows, list): return StoppedBaseline(False, detail="Process snapshot schema invalid")
    kinds = []
    ollama = []
    owned = set()
    pending = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("pid")) is not int or type(row.get("parent")) is not int:
            return StoppedBaseline(False, detail="Process ownership schema invalid")
        name, command, executable = row.get("name"), row.get("command"), row.get("executable")
        if not all(isinstance(value,str) for value in (name,command,executable)):
            return StoppedBaseline(False, detail="Process command/ownership unavailable")
        if re.fullmatch(r"ollama(?: app|_llama_server)?\.exe", name, re.I): ollama.append("Ollama process")
        # A case-normalized executable path or exact absolute script path proves repository ownership.
        normalized = command.replace("/", "\\").casefold()
        absolute = re.search(re.escape(base) + r"\\(?:launcher\.py|medlartv\\[^\"]+\.py)(?:\"|\s|$)", normalized)
        owner = owned_path(executable) or bool(absolute)
        relative_form = re.search(r"(?:^|[\s\"\\])(?:launcher\.py|medlartv\\(?:app\\medlar_app|core\\main|avatar\\bridge\\server|avatar_client\\console_client|tools\\python_service_entry)\.py)(?:\"|\s|$)", normalized)
        module_form = re.search(r"-m\s+medlartv\.(?:app\.medlar_app|core\.main|avatar\.bridge\.server|avatar_client\.console_client)(?:\"|\s|$)", normalized)
        adapter_form = "python_service_entry.py" in normalized or "python_service_entry" in normalized
        if relative_form or module_form or adapter_form or re.fullmatch(r"Medlar.*\.exe", name, re.I):
            pending.append((row,owner))
    # Parent chains support owned service adapters, without treating unrelated module names as ours.
    for _ in range(len(pending)+1):
        progress = False
        for row,owner in pending:
            if row["pid"] not in owned and (owner or row["parent"] in owned):
                owned.add(row["pid"]); kinds.append("Owned Medlar service"); progress=True
        if not progress: break
    unresolved = any(row["pid"] not in owned for row,_owner in pending)
    if unresolved:
        return StoppedBaseline(False, medlar_process_count=len(owned), ollama_process_count=len(ollama),
                               medlar_kinds=tuple(kinds), ollama_names=tuple(ollama), detail="Possible Medlar process has unverified ownership")
    if irc_rows is not None and (not isinstance(irc_rows, list) or any(
            not isinstance(row, dict) or type(row.get("pid")) is not int
            or row.get("port") not in (6697, 6667) for row in irc_rows)):
        return StoppedBaseline(False, detail="Owned IRC snapshot schema invalid", irc_probe_completed=False)
    irc_completed = irc_rows is not None or not owned
    irc_count = sum(row["pid"] in owned for row in irc_rows or [])
    return StoppedBaseline(True, medlar_process_count=len(owned), ollama_process_count=len(ollama),
                           medlar_irc_connection_count=irc_count, medlar_kinds=tuple(kinds), ollama_names=tuple(ollama),
                           detail="Process ownership classified; IRC ownership sampled" if irc_completed else
                           "Process ownership classified; IRC connections were not sampled", irc_probe_completed=irc_completed)


def probe_stopped_baseline(
    root: Path = REPO_ROOT,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> StoppedBaseline:
    """Return a privacy-filtered Windows process/IRC snapshot for the mutation gate."""
    if os.name != "nt":
        return StoppedBaseline(False, detail="required stopped-process probing is implemented only for Windows")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return StoppedBaseline(False, detail="PowerShell was not found")
    script = r"""
$ErrorActionPreference = 'Stop'
$selfPid = [int]$env:MEDLAR_DIAGNOSTIC_PID
$rows = @(@(Get-CimInstance Win32_Process) | Where-Object {
    $_.ProcessId -ne $selfPid -and $_.Name -match '(?i)^(python(?:w)?|ollama(?: app|_llama_server)?|Medlar.*)\.exe$'
} | ForEach-Object {
    [ordered]@{ pid=[int]$_.ProcessId; parent=[int]$_.ParentProcessId;
      name=$_.Name; command=$_.CommandLine; executable=$_.ExecutablePath }
})
$irc = @(Get-NetTCPConnection -State Established | Where-Object { $_.RemotePort -in @(6697,6667) } | ForEach-Object {
    [ordered]@{ pid=[int]$_.OwningProcess; port=[int]$_.RemotePort }
})
[ordered]@{processes=@($rows); irc_connections=@($irc)} | ConvertTo-Json -Compress -Depth 4
"""
    child_environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"}
    }
    child_environment["MEDLAR_DIAGNOSTIC_ROOT"] = str(root.resolve())
    child_environment["MEDLAR_DIAGNOSTIC_PID"] = str(os.getpid())
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "check": False,
        "shell": False,
        "env": child_environment,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = runner([powershell, "-NoProfile", "-NonInteractive", "-Command", script], **kwargs)
        if completed.returncode != 0:
            raise ValueError("process probe failed")
        payload = json.loads(completed.stdout)
        return classify_process_snapshot(payload["processes"], root, payload["irc_connections"])
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, json.JSONDecodeError):
        _capture_failure(sys.exc_info()[1])
        return StoppedBaseline(False, detail="process or TCP ownership snapshot could not be completed")


def probe_ollama_endpoint_stopped(
    environment: Mapping[str, str],
    root: Path = REPO_ROOT,
    *,
    connector: Callable[..., Any] = socket.create_connection,
) -> tuple[bool | None, str]:
    try:
        base_url = resolve_ollama_base_url(environment, root)
        parsed = urllib_parse.urlsplit(base_url)
        if not parsed.hostname or not _is_loopback_url(base_url):
            return None, "configured Ollama client endpoint is invalid or non-loopback"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if os.name == "nt" and connector is socket.create_connection:
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if powershell:
                listener_environment = {
                    key: value for key, value in os.environ.items()
                    if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"}
                }
                listener_environment["MEDLAR_DIAGNOSTIC_PORT"] = str(port)
                listener_script = (
                    "$ErrorActionPreference='Stop'; "
                    "$p=[int]$env:MEDLAR_DIAGNOSTIC_PORT; "
                    "$listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $p }); "
                    "$listeners.Count"
                )
                kwargs: dict[str, Any] = {
                    "capture_output": True, "text": True, "timeout": 10,
                    "check": False, "shell": False, "env": listener_environment,
                }
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                try:
                    completed = subprocess.run(
                        [powershell, "-NoProfile", "-NonInteractive", "-Command", listener_script],
                        **kwargs,
                    )
                    if completed.returncode == 0:
                        listener_count = int(completed.stdout.strip())
                        if listener_count == 0:
                            return True, f"Windows TCP listener table confirms no listener on configured loopback port {port}"
                        return False, f"Windows TCP listener table found {listener_count} listener(s) on configured loopback port {port}"
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    _capture_failure(sys.exc_info()[1])
                    pass
        connection = connector((parsed.hostname, port), timeout=0.25)
    except TimeoutError:
        _capture_failure(sys.exc_info()[1])
        return None, "configured Ollama endpoint probe timed out"
    except ConnectionRefusedError:
        _capture_failure(sys.exc_info()[1])
        return True, "configured Ollama endpoint refused the TCP connection"
    except OSError:
        _capture_failure(sys.exc_info()[1])
        return None, "configured Ollama endpoint state is unknown after a transport error"
    except (ValueError, ImportError, AttributeError, TypeError):
        _capture_failure(sys.exc_info()[1])
        return None, "configured Ollama endpoint could not be resolved"
    try:
        connection.close()
    finally:
        pass
    return False, "configured Ollama endpoint accepted a TCP connection"


def required_stopped_baseline_checks(
    baseline: StoppedBaseline,
    ollama_endpoint_off: bool | None,
    ollama_endpoint_detail: str,
) -> tuple[list[Check], bool]:
    section = "Required execution baseline"
    if not baseline.probe_completed:
        rows = [
            Check(section, "Medlar closed", ERROR, baseline.detail + "; exercise is blocked."),
            Check(section, "Medlar Twitch disconnected", ERROR, baseline.detail + "; exercise is blocked."),
            Check(section, "Ollama processes stopped", ERROR, baseline.detail + "; exercise is blocked."),
        ]
    else:
        medlar_detail = (
            "No known Medlar GUI, launcher, Core, Bridge, or avatar runtime process was found."
            if baseline.medlar_process_count == 0 else
            f"Found {baseline.medlar_process_count} known Medlar runtime process(es): "
            + ", ".join(baseline.medlar_kinds) + "."
        )
        irc_detail = (
            "No established Twitch IRC connection owned by a detected Medlar runtime was found."
            if baseline.medlar_irc_connection_count == 0 else
            f"Found {baseline.medlar_irc_connection_count} established Twitch IRC connection(s) owned by Medlar."
        )
        ollama_detail = (
            "No Ollama process was found."
            if baseline.ollama_process_count == 0 else
            f"Found {baseline.ollama_process_count} Ollama process(es): "
            + ", ".join(baseline.ollama_names) + "."
        )
        rows = [
            Check(section, "Medlar closed", READY if baseline.medlar_process_count == 0 else UNAVAILABLE, medlar_detail),
            Check(section, "Medlar Twitch disconnected", (READY if baseline.medlar_irc_connection_count == 0 else UNAVAILABLE) if baseline.irc_probe_completed else SKIPPED, irc_detail if baseline.irc_probe_completed else "IRC ownership was not sampled; disconnection is unverified."),
            Check(section, "Ollama processes stopped", READY if baseline.ollama_process_count == 0 else UNAVAILABLE, ollama_detail),
        ]
    endpoint_status = READY if ollama_endpoint_off is True else (UNAVAILABLE if ollama_endpoint_off is False else ERROR)
    rows.append(Check(section, "Ollama endpoint stopped", endpoint_status, ollama_endpoint_detail + "."))
    passed = all(item.status == READY for item in rows)
    rows.append(Check(
        section, "Local stopped-state gate", READY if passed else UNAVAILABLE,
        "All local stopped-state conditions were positively confirmed before credential resolution."
        if passed else
        "At least one local stopped-state condition was present or indeterminate; no credential was resolved and no Twitch connection/action was attempted.",
    ))
    return rows, passed


def probe_broadcaster_offline_before_diagnostic(
    environment: Mapping[str, str],
    *,
    http_json: HttpJson,
    secret_capture: dict[str, str],
) -> tuple[list[Check], bool]:
    """Use only app authentication to establish the remote baseline before user-token/IRC tests."""
    section = "Required execution baseline"
    app_client = str(environment.get("APP_TWITCH_CLIENT_ID", "")).strip()
    app_secret = str(environment.get("APP_SECRET_ID", "")).strip()
    channel = str(environment.get("TWITCH_CHANNEL", "")).strip().lstrip("#")
    if not app_client or not app_secret or not channel:
        reason = "App client ID, app secret, and configured channel are required for the remote offline preflight."
        return [
            Check(section, "Broadcaster offline before diagnostic", UNAVAILABLE, reason),
            Check(section, "Remote baseline gate", UNAVAILABLE,
                  reason + " No user token was validated and no IRC connection or Twitch action was attempted."),
        ], False
    exchange = _safe_http_call(
        http_json, "POST", "https://id.twitch.tv/oauth2/token",
        form={"client_id": app_client, "client_secret": app_secret,
              "grant_type": "client_credentials"}, timeout=10.0,
    )
    token = exchange.payload.get("access_token") if isinstance(exchange.payload, dict) else None
    if not _usable_app_exchange(exchange):
        reason = f"Remote baseline app-token exchange failed (HTTP {exchange.status or 'unavailable'})."
        return [
            Check(section, "Broadcaster offline before diagnostic", ERROR, reason),
            Check(section, "Remote baseline gate", UNAVAILABLE,
                  reason + " No user token was validated and no IRC connection or Twitch action was attempted."),
        ], False
    secret_capture["TWITCH_BASELINE_APP_ACCESS_TOKEN"] = token
    headers = {"Client-ID": app_client, "Authorization": f"Bearer {token}"}
    lookup = _safe_http_call(
        http_json, "GET",
        "https://api.twitch.tv/helix/users?" + urllib_parse.urlencode({"login": channel}),
        headers=headers, timeout=10.0,
    )
    rows = lookup.payload.get("data") if isinstance(lookup.payload, dict) else None
    match = next((
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("login") or "").casefold() == channel.casefold()
        and _nonempty(row.get("id"))
    ), None) if isinstance(rows, list) else None
    if lookup.status != 200 or lookup.error or match is None:
        reason = f"Remote baseline channel lookup failed (HTTP {lookup.status or 'unavailable'})."
        return [
            Check(section, "Broadcaster offline before diagnostic", ERROR, reason),
            Check(section, "Remote baseline gate", UNAVAILABLE,
                  reason + " No user token was validated and no IRC connection or Twitch action was attempted."),
        ], False
    stream = _safe_http_call(
        http_json, "GET",
        "https://api.twitch.tv/helix/streams?" + urllib_parse.urlencode({
            "user_id": str(match["id"]),
        }),
        headers=headers, timeout=10.0,
    )
    stream_rows = stream.payload.get("data") if isinstance(stream.payload, dict) else None
    if stream.status == 200 and not stream.error and isinstance(stream_rows, list):
        offline = len(stream_rows) == 0
        detail = (
            "GET /helix/streams returned HTTP 200 with data=[] before any user-token validation or IRC connection; broadcaster offline was positively sampled."
            if offline else
            f"GET /helix/streams returned {len(stream_rows)} active stream row(s); the broadcaster is live."
        )
        return [
            Check(section, "Broadcaster offline before diagnostic", READY if offline else UNAVAILABLE, detail),
            Check(
                section, "Remote baseline gate", READY if offline else UNAVAILABLE,
                "Remote baseline passed; independent user-token and IRC diagnostics may proceed."
                if offline else
                "Remote baseline failed; no user token was validated and no IRC connection, mutation, or chat send was attempted.",
            ),
        ], offline
    reason = f"Broadcaster live state was indeterminate (HTTP {stream.status or 'unavailable'} or malformed response)."
    return [
        Check(section, "Broadcaster offline before diagnostic", ERROR, reason),
        Check(section, "Remote baseline gate", UNAVAILABLE,
              reason + " No user token was validated and no IRC connection, mutation, or chat send was attempted."),
    ], False


def _effective_live_environment(
    environ: Mapping[str, str],
    *,
    root: Path,
    use_env_file: bool,
    use_vault: bool,
    credential_overlay: Callable[[dict[str, str]], object] | None = None,
) -> tuple[dict[str, str] | None, Check]:
    # Explicit Twitch mode follows launcher --mode=twitch: .env applies except
    # inherited Ollama endpoints, then saved connection values override it.
    resolved = {key: str(environ[key]) for key in RELEVANT_ENV_FIELDS if key in environ}
    inherited_endpoints = {key: resolved.get(key) for key in ("OLLAMA_URL", "OLLAMA_HOST")}
    if use_env_file:
        try:
            resolved.update(load_live_env_values(root / ".env"))
        except (OSError, UnicodeError):
            _capture_failure(sys.exc_info()[1])
            return None, Check("Live Twitch", "Saved credential decode/apply viability", ERROR,
                               "The repository .env could not be read as UTF-8; no live requests were sent.")
        for key, value in inherited_endpoints.items():
            if value is None:
                resolved.pop(key, None)
            else:
                resolved[key] = value
    if use_vault:
        try:
            overlay = credential_overlay
            if overlay is None:
                module = _load_source_module(
                    root / "MedlarTV" / "core" / "local_credentials.py",
                    "_medlar_diagnostics_live_credentials",
                )
                overlay = module.apply_saved_credentials
            overlay(resolved)
        except Exception:
            _capture_failure(sys.exc_info()[1])
            return None, Check("Live Twitch", "Saved credential decode/apply viability", ERROR,
                               "Saved credential overlay failed atomically; no live requests were sent.")
    return resolved, Check(
        "Live Twitch", "Saved credential decode/apply viability", READY,
        "Credential loading completed with saved credentials over repository .env over inherited environment; exact effective values are listed only in the private/full report. "
        "This proves the loader completed for this run, not that Twitch accepted the credentials.",
    )


def _blocked_live_checks(reason: str, *, exercise_requested: bool) -> list[Check]:
    checks = [
        Check("Live Twitch", "Bot authentication", SKIPPED, reason),
        Check("Live Twitch", "Broadcaster authentication", SKIPPED, reason),
        Check("Live Twitch", "App/API authentication", SKIPPED, reason),
        Check("Live Twitch", "Bot token validation", SKIPPED, reason),
        *_skipped_token_checks("Bot", reason),
        Check("Live Twitch", "Broadcaster token validation", SKIPPED, reason),
        *_skipped_token_checks("Broadcaster", reason),
        Check("Live Twitch", "App credential exchange", SKIPPED, reason),
        Check("Live Twitch", "Read-only channel API", SKIPPED, reason),
        Check("Live Twitch", "Account-age source data", SKIPPED, reason),
        Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED, reason),
        Check("Live Twitch", "Read-only IRC authentication and join", SKIPPED, reason),
        *_skipped_read_only_twitch_api_checks(reason),
    ]
    exercise_rows = _skipped_exercise_checks(reason)
    if exercise_requested:
        exercise_rows[-1] = Check(
            "Full-live Twitch exercise", "Full exercise result", UNAVAILABLE, reason,
        )
    checks.extend(exercise_rows)
    return checks


def _collect_diagnostics_into_run(
    *,
    root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
    launcher_mode: str = "all",
    use_env_file: bool = True,
    use_vault: bool = True,
    probe_services: bool = True,
    probe_writes: bool = True,
    live_twitch: bool = False,
    twitch_exercise: TwitchExercise | None = None,
    include_secret_values: bool = False,
    http_json: HttpJson = request_json,
    irc_sender: IrcSender = send_irc_diagnostic,
    irc_probe: IrcProbe = probe_irc_readiness,
    bridge_probe: Callable[[int], bool] = _default_bridge_probe,
    vault_probe: Callable[[Path], tuple[dict[str, bool | None], str | None]] = probe_vault_presence,
    credential_overlay: Callable[[dict[str, str]], object] | None = None,
    baseline_probe: Callable[[Path], StoppedBaseline] = probe_stopped_baseline,
    ollama_endpoint_probe: Callable[[Mapping[str, str], Path], tuple[bool | None, str]] = probe_ollama_endpoint_stopped,
    bridge_session: Mapping[str, Any] | None = None,
    _run: DiagnosticRun,
) -> DiagnosticRun:
    values = os.environ if environ is None else environ
    checks = _run.checks
    http_exchanges = _run.http_exchanges
    recorded_http = _recording_http_json(http_json, http_exchanges)
    checks.append(Check(
        "Diagnostic coverage", "Run completeness", SKIPPED,
        "Final execution completeness is evaluated after all fail-closed gates and requested checks.",
    ))
    checks.extend(_run_check_stage("check_source_layout", lambda: check_source_layout(root)))
    checks.extend(_run_check_stage("check_source_syntax", lambda: check_source_syntax(root)))
    checks.extend(_run_check_stage("check_component_contracts", lambda: check_component_contracts(root)))
    checks.extend(_run_check_stage("check_project_inventory", lambda: check_project_inventory(root)))
    checks.append(Check(
        "Local project", "Python runtime",
        READY if sys.version_info >= (3, 10) else UNAVAILABLE,
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; current source requires Python 3.10 or newer.",
    ))
    checks.append(Check("Local project", "Repository-relative paths", READY,
                        "All project paths are resolved from tests/medlar_diagnostics.py, not the caller's working directory."))
    checks.append(Check("Local project", "Default report", READY,
                        "The owned default output is logs/medlar_diagnostics.txt."))
    app_python = launcher_python(root)
    if app_python is None:
        checks.append(Check(
            "Application environment", "Execution interpreter", UNAVAILABLE,
            "The repository .venv interpreter used by launcher.py is missing.",
        ))
    elif _same_path(app_python, Path(sys.executable)):
        checks.append(Check(
            "Application environment", "Execution interpreter", READY,
            f"Diagnostics are running under {_relative(app_python, root)}, the same interpreter used by launcher.py.",
        ))
    else:
        checks.append(Check(
            "Application environment", "Execution interpreter", UNAVAILABLE,
            f"Diagnostics are running under a different interpreter from {_relative(app_python, root)}; the CLI normally relaunches before collection.",
        ))

    inspection: EnvInspection | None
    if launcher_mode == "ollama":
        inspection = None
        checks.append(Check("Configuration", "Repository .env", SKIPPED,
                            "Ollama-only launcher mode does not load this file; it was not opened."))
    elif use_env_file:
        inspection = inspect_env_file(root / ".env")
        if inspection.error:
            checks.append(Check("Configuration", "Repository .env", ERROR,
                                "The file exists but could not be inspected safely as UTF-8."))
        elif inspection.exists:
            checks.append(Check("Configuration", "Repository .env", READY,
                                "Exact relevant assignments were inspected for precedence. The presence-only phase does not copy values; a complete live run separately lists every effective value in the private/full credential block."))
        else:
            checks.append(Check("Configuration", "Repository .env", SKIPPED,
                                "No repository .env exists; inherited environment and saved credentials may still apply."))
    else:
        inspection = None
        checks.append(Check("Configuration", "Repository .env", SKIPPED,
                            "Inspection was disabled; the file was not opened."))

    if launcher_mode == "ollama":
        vault_presence = {key: None for key in CREDENTIAL_FIELDS}
        vault_error = "not loaded in ollama-only mode"
        checks.append(Check("Credentials", "Windows credential-store presence", SKIPPED,
                            "Ollama-only launcher mode does not load connections; no records were opened."))
    elif use_vault:
        vault_presence, vault_error = vault_probe(root)
        checks.append(Check(
            "Credentials", "Windows credential-store presence",
            READY if not vault_error else ERROR,
            "Presence-only queries completed; no values were decoded." if not vault_error
            else "Presence-only query failed; no values were decoded.",
        ))
    else:
        vault_presence = {key: None for key in CREDENTIAL_FIELDS}
        vault_error = "probe disabled"
        checks.append(Check("Credentials", "Windows credential-store presence", SKIPPED,
                            "Presence inspection was disabled; no records were opened."))
    checks.extend(credential_checks(values, inspection, vault_presence, vault_error, launcher_mode))
    checks.extend(_run_check_stage("check_credential_loader_contract", lambda: check_credential_loader_contract(root)))

    public_environment = effective_public_environment(values, inspection, launcher_mode)
    _run.configuration_values = {key: str(public_environment.get(key, "")) for key in PUBLIC_ENV_FIELDS}
    checks.extend(_run_check_stage("check_public_twitch_settings", lambda: check_public_twitch_settings(public_environment, launcher_mode)))
    local_baseline_ready = True
    if live_twitch and twitch_exercise is not None:
        try:
            stopped_snapshot = baseline_probe(root)
        except Exception:
            _capture_failure(sys.exc_info()[1])
            stopped_snapshot = StoppedBaseline(False, detail="stopped-process probe raised an internal error")
        try:
            endpoint_off, endpoint_detail = ollama_endpoint_probe(public_environment, root)
        except Exception:
            _capture_failure(sys.exc_info()[1])
            endpoint_off, endpoint_detail = None, "configured Ollama endpoint probe raised an internal error"
        baseline_rows, local_baseline_ready = required_stopped_baseline_checks(
            stopped_snapshot, endpoint_off, endpoint_detail,
        )
        checks.extend(baseline_rows)
    else:
        checks.extend([
            Check("Required execution baseline", "Medlar closed", SKIPPED,
                  "Stopped-state gate is required and sampled when --exercise-twitch is requested."),
            Check("Required execution baseline", "Medlar Twitch disconnected", SKIPPED,
                  "Stopped-state gate is required and sampled when --exercise-twitch is requested."),
            Check("Required execution baseline", "Ollama processes stopped", SKIPPED,
                  "Stopped-state gate is required and sampled when --exercise-twitch is requested."),
            Check("Required execution baseline", "Ollama endpoint stopped", SKIPPED,
                  "Stopped-state gate is required and sampled when --exercise-twitch is requested."),
            Check("Required execution baseline", "Local stopped-state gate", SKIPPED,
                  "No mutation exercise was requested, so the stopped-state gate was not sampled."),
        ])
    checks.extend(_run_check_stage("check_configs", lambda: check_configs(root)))
    checks.extend(_run_check_stage("check_config_schemas", lambda: check_config_schemas(root)))
    checks.extend(_run_check_stage("check_event_configuration", lambda: check_event_configuration(root)))
    checks.extend(_run_check_stage("check_dependencies", lambda: check_dependencies(root)))
    checks.extend(_run_check_stage("check_dependency_imports", lambda: check_dependency_imports(root)))
    if probe_services:
        checks.extend(_run_check_stage("check_legacy_setup_claims", lambda: check_legacy_setup_claims(root)))
    else:
        checks.append(Check("Setup topology", "Legacy port claims", SKIPPED, "Service probes disabled; no temporary bind was attempted."))
    checks.extend(_run_check_stage("check_launcher_controls", lambda: check_launcher_controls(values, root)))
    checks.extend(_run_check_stage("check_runtime_writability", lambda: check_runtime_writability(
        values, root, enabled=probe_writes,
    )))
    checks.extend(_run_check_stage("check_bridge_readiness", lambda: check_bridge_readiness(
        probe_services=probe_services, probe=bridge_probe, session_context=bridge_session, root=root,
    )))
    checks.extend(_run_check_stage("check_twitch_source_compatibility", lambda: check_twitch_source_compatibility(root)))
    checks.extend(_run_check_stage("check_ollama_executable", lambda: check_ollama_executable()))
    checks.extend(_run_check_stage("check_ollama_bind_alignment", lambda: check_ollama_bind_alignment(public_environment, launcher_mode, root=root)))
    ollama_checks = check_ollama(
        public_environment, root=root, http_json=recorded_http,
        probe_services=probe_services,
    )
    checks.extend(ollama_checks)
    service_ready = any(
        item.section == "Ollama" and item.name == "Service readiness"
        and item.status == READY for item in ollama_checks
    )
    if probe_services:
        checks.extend(_run_check_stage("check_ollama_launch_precondition", lambda: check_ollama_launch_precondition(
            public_environment, launcher_mode, root=root, service_ready=service_ready,
        )))
    else:
        checks.append(Check("Ollama", "Launcher precondition", SKIPPED, "Service probes disabled; no temporary bind was attempted."))
    checks.append(Check(
        "Ollama", "Command Center staged lifecycle", SKIPPED,
        "The GUI normally starts ollama mode, waits, then starts twitch mode. Diagnostics do not start or stop services, so that two-stage transition is not claimed as proven.",
    ))
    checks.extend(_run_check_stage("check_libretranslate", lambda: check_libretranslate(public_environment, root=root, http_json=recorded_http,
                                       probe_services=probe_services)))
    checks.extend(_run_check_stage("check_memory_paths", lambda: check_memory_paths(values, root)))
    checks.extend([
        Check(
            "Manual UI evidence", "Dashboard navigation screenshots", SKIPPED,
            "No browser or GUI automation ran. Only dashboard navigation screenshots are eligible for a separate explicit manual capture.",
        ),
        Check(
            "Manual UI evidence", "Personal chat display-preference screenshots", SKIPPED,
            "No browser or GUI automation ran. Only the user's personal chat display preferences are eligible for a separate explicit manual capture.",
        ),
        Check(
            "Manual UI evidence", "AutoMod/channel/browser preference changes", SKIPPED,
            "Outside scope: diagnostics do not change AutoMod, channel settings, Clear Chat state, browser preferences, or any other dashboard setting.",
        ),
    ])
    secrets: tuple[str, ...] = ()
    sensitive_values: dict[str, str] = {}
    sensitive_values_resolved = False
    if not live_twitch:
        checks.extend([
            Check("Required execution baseline", "Broadcaster offline before diagnostic", SKIPPED,
                  "Explicit offline mode makes no Twitch API request, so broadcaster live state was not sampled."),
            Check("Required execution baseline", "Remote baseline gate", SKIPPED,
                  "Explicit offline mode does not run the remote baseline gate."),
            Check("Live Twitch", "Bot authentication", SKIPPED,
                  "Explicit offline mode resolved no credential value and opened no Twitch connection."),
            Check("Live Twitch", "Broadcaster authentication", SKIPPED,
                  "Explicit offline mode resolved no credential value and opened no Twitch connection."),
            Check("Live Twitch", "App/API authentication", SKIPPED,
                  "Explicit offline mode did not mint an app token or run a read-only user lookup."),
            Check("Live Twitch", "Bot token validation", SKIPPED,
                  "Explicit offline mode performs no token validation."),
            *_skipped_token_checks("Bot", "Explicit offline mode resolved no live credential value."),
            Check("Live Twitch", "Broadcaster token validation", SKIPPED,
                  "Explicit offline mode performs no token validation."),
            *_skipped_token_checks("Broadcaster", "Explicit offline mode resolved no live credential value."),
            Check("Live Twitch", "App credential exchange", SKIPPED,
                  "No app token was minted in explicit offline mode."),
            Check("Live Twitch", "Read-only channel API", SKIPPED,
                  "No authenticated Twitch API lookup was sent in explicit offline mode."),
            Check("Live Twitch", "Account-age source data", SKIPPED,
                  "No GET /helix/users request was sent in explicit offline mode."),
            Check("Live Twitch", "Broadcaster/API identity correlation", SKIPPED,
                  "No live identities were resolved in explicit offline mode."),
            Check("Live Twitch", "Read-only IRC authentication and join", SKIPPED,
                  "No Twitch IRC connection was opened in explicit offline mode."),
            *_skipped_read_only_twitch_api_checks(
                "Explicit offline mode sent no Twitch API request."
            ),
            *_skipped_exercise_checks(
                "Explicit offline mode makes no Twitch connection or mutation."
            ),
        ])
    else:
        if twitch_exercise is not None and not local_baseline_ready:
            checks.append(Check(
                "Live Twitch", "Saved credential decode/apply viability", SKIPPED,
                "The local stopped-state gate did not pass; no credential source was decoded or overlaid.",
            ))
            checks.extend(_blocked_live_checks(
                "The local stopped-state gate did not pass; no credential was resolved and no Twitch request, socket, mutation, or chat send was attempted.",
                exercise_requested=True,
            ))
            live_environment = None
        else:
            live_environment, resolution = _effective_live_environment(
                values, root=root, use_env_file=use_env_file, use_vault=use_vault,
                credential_overlay=credential_overlay,
            )
            checks.append(resolution)
        if live_environment is None and local_baseline_ready:
            checks.extend(_blocked_live_checks(
                "Credential resolution failed; no live request, socket, mutation, or chat send was attempted.",
                exercise_requested=twitch_exercise is not None,
            ))
        elif live_environment is not None:
            sensitive_values_resolved = True
            _run.sensitive_values_resolved = True
            if include_secret_values:
                _run.sensitive_values.update({key: str(live_environment.get(key, "")) for key in CREDENTIAL_FIELDS})
            resolved_secrets = [
                str(live_environment.get(key, ""))
                for key in CREDENTIAL_FIELDS if str(live_environment.get(key, ""))
            ]
            _run.secrets = tuple(resolved_secrets)
            captured: dict[str, str] = {}
            remote_baseline_ready = True
            if twitch_exercise is not None:
                remote_rows, remote_baseline_ready = probe_broadcaster_offline_before_diagnostic(
                    live_environment, http_json=recorded_http, secret_capture=captured,
                )
                checks.extend(remote_rows)
            else:
                checks.extend([
                    Check("Required execution baseline", "Broadcaster offline before diagnostic", SKIPPED,
                          "No mutation exercise was requested, so the remote stopped-state gate was not sampled."),
                    Check("Required execution baseline", "Remote baseline gate", SKIPPED,
                          "No mutation exercise was requested."),
                ])
            if remote_baseline_ready:
                live_checks: list[Check] = []
                try:
                    run_live_twitch_checks(
                        live_environment, root=root, http_json=recorded_http,
                        exercise=twitch_exercise, irc_sender=irc_sender,
                        irc_probe=irc_probe, secret_capture=captured,
                        gate_context=_exercise_gate_from_checks(checks) if twitch_exercise is not None else None,
                        partial_checks=live_checks,
                    )
                finally:
                    checks.extend(live_checks)
                    _run.secrets = tuple((*resolved_secrets, *captured.values()))
                    if include_secret_values:
                        _run.sensitive_values.update(captured)
            else:
                checks.extend(_blocked_live_checks(
                    "The broadcaster-offline remote baseline did not pass; no user token was validated and no IRC connection, mutation, or chat send was attempted.",
                    exercise_requested=True,
                ))
            secrets = tuple((*resolved_secrets, *captured.values()))
            if include_secret_values:
                sensitive_values = {
                    key: str(live_environment.get(key, ""))
                    for key in CREDENTIAL_FIELDS
                }
                sensitive_values.update(captured)
    exercise_result = next((
        item for item in checks
        if item.section == "Full-live Twitch exercise"
        and item.name == "Full exercise result"
    ), None)
    exercise_failed = bool(twitch_exercise) and (
        exercise_result is None or exercise_result.status != READY
    )
    if not live_twitch:
        checks[0] = Check(
            "Diagnostic coverage", "Run completeness", SKIPPED,
            "OFFLINE/INCOMPLETE MODE: no credential values were resolved and no Twitch HTTPS or IRC connection was attempted; live outcomes remain explicitly UNTESTED.",
        )
    elif twitch_exercise is None:
        checks[0] = Check(
            "Diagnostic coverage", "Run completeness", READY,
            "READ-ONLY DIAGNOSTIC SUITE ATTEMPTED: individual READY/UNAVAILABLE/SKIPPED/ERROR rows record what actually passed; no mutation was requested.",
        )
    elif exercise_result is not None and exercise_result.status == READY:
        checks[0] = Check(
            "Diagnostic coverage", "Run completeness", READY,
            "REQUESTED TWITCH EXERCISE COMPLETED: the Full exercise result passed, including final restoration read-back and the documented post-send IRC response.",
        )
    else:
        result_status = ERROR if exercise_result is not None and exercise_result.status == ERROR else UNAVAILABLE
        checks[0] = Check(
            "Diagnostic coverage", "Run completeness", result_status,
            "REQUESTED TWITCH EXERCISE BLOCKED OR INCOMPLETE: inspect the baseline, exercise, and restoration rows; no completion claim is made.",
        )
    checks.extend(consolidation_coverage_checks(checks))
    _run.secrets = secrets
    _run.sensitive_values = sensitive_values
    _run.sensitive_values_resolved = sensitive_values_resolved
    _run.critical_failure = exercise_failed
    return _run


def collect_diagnostics(*, root: Path = REPO_ROOT, **kwargs: Any) -> DiagnosticRun:
    run = DiagnosticRun(offline=not kwargs.get("live_twitch", False), exercise_requested=kwargs.get("twitch_exercise") is not None)
    marker = _FAILURE_RUN.set(run)
    try:
        _collect_diagnostics_into_run(root=root, _run=run, **kwargs)
    except Exception as exc:
        _capture_failure(sys.exc_info()[1])
        run.collection_failed = True
        run.critical_failure = run.exercise_requested
        _capture_failure(exc, "Interrupted diagnostic collection")
        run.checks.append(Check("Diagnostic stage failures", "Interrupted collection", ERROR, f"{type(exc).__name__}: {exc}; completed evidence is retained."))
        run.checks.extend(consolidation_coverage_checks(run.checks))
    finally:
        try:
            _finish_coverage(run)
        except Exception as exc:
            run.collection_failed = True
            run.complete = False
            _capture_failure(exc, "Final completeness evaluation")
            run.checks.append(Check("Diagnostic coverage", "Completeness evaluation failure", ERROR, str(exc)))
        finally:
            _FAILURE_RUN.reset(marker)
    return run


def _credential_evidence_lines(
    run: DiagnosticRun, *, markdown: bool, share_safe: bool,
) -> list[str]:
    heading = "## PRIVATE credential values used" if markdown else "PRIVATE CREDENTIAL VALUES USED"
    if share_safe:
        heading = "## Credential values (redacted)" if markdown else "CREDENTIAL VALUES (REDACTED)"
    lines = ["", heading, "" if markdown else "=" * 72]
    lines.append(
        "Every credential field is represented separately. Exact values, prefixes, and lengths are present only when credential resolution actually ran in the private/full report."
    )
    primary = (*CREDENTIAL_FIELDS, "TWITCH_BASELINE_APP_ACCESS_TOKEN",
               "TWITCH_EPHEMERAL_APP_ACCESS_TOKEN")
    ordered = (*primary, *sorted(set(run.sensitive_values) - set(primary)))
    token_fields = {
        "MEDLARTV_TWITCH_TOKEN", "DEVILMEDLAR_TWITCH_TOKEN",
        "TWITCH_BASELINE_APP_ACCESS_TOKEN",
        "TWITCH_EPHEMERAL_APP_ACCESS_TOKEN",
    }
    values_were_resolved = run.sensitive_values_resolved or bool(run.sensitive_values)
    for key in ordered:
        value = run.sensitive_values.get(key, "")
        if share_safe:
            lines.append(
                f"- {key} is withheld in share-safe mode "
                "(exact value, prefix, normalized wire value, and lengths)."
            )
            continue
        if not values_were_resolved:
            lines.append(
                f"- {key}: NOT RESOLVED (a fail-closed gate stopped the run before credential values were read; no missing/empty claim is made)."
            )
            continue
        exact = json.dumps(value if value else None, ensure_ascii=False)
        lines.append(f"- {key}: exact effective value={exact}")
        prefix = value[:20] + ("..." if len(value) > 20 else "") if value else None
        lines.append(
            f"  configured prefix (first 20 characters, diagnostic derivative)="
            f"{json.dumps(prefix, ensure_ascii=False)}; configured length={len(value)}"
        )
        if key in token_fields:
            normalized = _normalized_oauth_token(value) if value else ""
            normalized_prefix = (
                normalized[:20] + ("..." if len(normalized) > 20 else "")
                if normalized else None
            )
            lines.append(
                f"  exact normalized wire value={json.dumps(normalized if normalized else None, ensure_ascii=False)}; "
                f"wire prefix={json.dumps(normalized_prefix, ensure_ascii=False)}; wire length={len(normalized)}"
            )
    if share_safe:
        lines.append(
            "- Effective broadcaster token selection (legacy TWITCH_USER_TOKEN listing): "
            "DEVILMEDLAR_TWITCH_TOKEN is the only source; exact value, prefix, and length are withheld."
        )
    elif values_were_resolved:
        broadcaster = run.sensitive_values.get("DEVILMEDLAR_TWITCH_TOKEN", "")
        broadcaster_prefix = (
            broadcaster[:20] + ("..." if len(broadcaster) > 20 else "")
            if broadcaster else None
        )
        lines.append(
            "- Effective broadcaster token selection (legacy TWITCH_USER_TOKEN listing): "
            "source=DEVILMEDLAR_TWITCH_TOKEN only (no bot-token fallback); "
            f"exact value={json.dumps(broadcaster if broadcaster else None, ensure_ascii=False)}; "
            f"prefix={json.dumps(broadcaster_prefix, ensure_ascii=False)}; length={len(broadcaster)}"
        )
    else:
        lines.append(
            "- Effective broadcaster token selection (legacy TWITCH_USER_TOKEN listing): NOT RESOLVED because a fail-closed gate stopped the run before credential resolution."
        )
    return lines


def _configuration_evidence_lines(
    run: DiagnosticRun, *, markdown: bool, share_safe: bool,
) -> list[str]:
    heading = "## Effective diagnostic configuration" if markdown else "EFFECTIVE DIAGNOSTIC CONFIGURATION"
    lines = ["", heading, "" if markdown else "=" * 72]
    lines.append(
        "Only the diagnostic-relevant allowlisted configuration fields are listed; unrelated environment data and raw .env text are not collected."
    )
    for key in PUBLIC_ENV_FIELDS:
        value = run.configuration_values.get(key, "")
        shown = (
            "[REDACTED IN SHARE-SAFE MODE]" if share_safe
            else json.dumps(value if value else None, ensure_ascii=False)
        )
        lines.append(f"- {key}: {shown}")
    return lines


def _http_evidence_lines(
    run: DiagnosticRun, *, markdown: bool, share_safe: bool,
) -> list[str]:
    heading = "## HTTP request/response transcript" if markdown else "HTTP REQUEST/RESPONSE TRANSCRIPT"
    lines = ["", heading, "" if markdown else "=" * 72]
    if not run.http_exchanges:
        lines.append("- No HTTP request was attempted or no transport evidence was available.")
        return lines
    for exchange in run.http_exchanges:
        lines.extend([
            "",
            f"### Exchange {exchange.sequence}" if markdown else f"EXCHANGE {exchange.sequence}",
            f"- Request: {exchange.method} {exchange.url}",
            "- Application-supplied request headers:",
        ])
        if exchange.request_headers:
            for key, value in exchange.request_headers:
                shown = (
                    "[REDACTED IN SHARE-SAFE MODE]"
                    if share_safe and key.casefold() in {"authorization", "client-id", "client_secret"}
                    else value
                )
                lines.append(f"  - {key}: {shown}")
        else:
            lines.append("  - (none)")
        if exchange.request_body is None:
            lines.append("- Request body: (none)")
        elif share_safe:
            lines.append("- Request body: [REDACTED IN SHARE-SAFE MODE; exact body retained only in private/full report]")
        else:
            lines.extend(["- Request body (exact encoded text):", "<<<REQUEST BODY", exchange.request_body, "REQUEST BODY"])
        lines.append(
            f"- Response status: {exchange.response_status if exchange.response_status is not None else '(no response)'}"
        )
        lines.append("- Response headers:")
        if exchange.response_headers:
            for key, value in exchange.response_headers:
                shown = "[REDACTED IN SHARE-SAFE MODE]" if share_safe else value
                lines.append(f"  - {key}: {shown}")
        else:
            lines.append("  - (none supplied)")
        lines.append(f"- Response body source: {exchange.response_body_source}")
        lines.append(f"- Response bytes observed: {exchange.response_body_bytes_observed}")
        lines.append(
            "- Response body completeness: "
            + (
                "INCOMPLETE - the transport explicitly marked this body incomplete or the HTTP body read ended prematurely. "
                "All available partial bytes are retained; "
                "the built-in diagnostic does not intentionally shorten complete responses."
                if exchange.response_body_truncated else "COMPLETE (no diagnostic truncation)"
            )
        )
        if share_safe:
            lines.append("- Response body: [REDACTED IN SHARE-SAFE MODE; exact body retained only in private/full report]")
        elif exchange.response_body is None:
            lines.append("- Response body: [UNAVAILABLE — transport supplied no body]")
        elif exchange.response_body == "":
            lines.append("- Response body: [EMPTY BODY]")
        else:
            lines.extend(["- Response body (exact retained text):", "<<<RESPONSE BODY", exchange.response_body, "RESPONSE BODY"])
        lines.append(f"- Transport/parser result: {exchange.error or 'no error'}")
    return lines


def _render_check_detail(check: Check, run: DiagnosticRun, *, share_safe: bool) -> str:
    if not share_safe:
        return check.detail
    identity_names = {
        "Bot account (TWITCH_NICK)", "Target channel (TWITCH_CHANNEL)",
        "Bot token validation", "Bot identity", "Broadcaster token validation",
        "Broadcaster identity", "Read-only channel API", "Account-age source data",
        "Broadcaster/API identity correlation",
    }
    if check.section == "Full-live Twitch exercise" or check.name in identity_names:
        return "[REDACTED IN SHARE-SAFE MODE; status and check name retained, exact evidence remains only in the private/full report]"
    return redact_text(check.detail, run.secrets)


def _report_boundaries(markdown: bool) -> list[str]:
    return [
        "", "## Boundaries" if markdown else "BOUNDARIES",
        "" if markdown else "-" * 72,
        "- A valid token or listed scope does not prove every implemented feature works.",
        "- An unfamiliar scope is not invalid merely because a local/historical catalog lacks a description.",
        "- Legacy scope names are not treated as automatic aliases for modern permissions.",
        "- Normal read-only live mode performs OAuth validation, an app-token exchange, the active safe GET /helix/users, /helix/channels, /helix/streams, and /helix/search/categories probes, plus IRC authentication/join without PRIVMSG.",
        "- --offline is deliberately incomplete: it does not resolve credential values or contact Twitch.",
        "- Exercise mode is a separate explicit gate. It may PATCH only title/category, sends exactly one labeled IRC message, and verifies restoration of title/category.",
        "- Restoration is field-aware and preserves edits visible in its immediate pre-restoration GET. Twitch provides no compare-and-swap for this PATCH, so a third-party edit in the narrow GET-to-PATCH race cannot be guaranteed safe; the final read-back records any mismatch.",
        "- No mode changes moderator roles, permissions, ads, security settings, bans/timeouts, channel points, rewards, polls, raids, schedule, VIPs, or any other Twitch state.",
        "- Every returned scope is listed with an independent Grant and Functional=PASS/FAIL/UNTESTED outcome; a grant alone never upgrades functional status.",
        "- Ollama probing lists models only; it never invokes model inference.",
        "- LibreTranslate probing reads /languages only; it never submits text for translation.", "",
        "- The built-in HTTP transport retains complete response bodies without diagnostic truncation. Full transport exception text and tracebacks are retained in this same private local report; unrelated viewer IRC traffic and traceback locals are not collected.",
        "- Raw IRC lines are not copied into the report because joined-channel traffic can contain unrelated viewer content. The report records protocol phases, sanitized failure reasons, rejection NOTICE msg-id values, and the documented post-send USERSTATE result.", "",
    ]


def render_report(
    run: DiagnosticRun,
    *,
    root: Path = REPO_ROOT,
    launcher_mode: str = "all",
    generated_at: datetime | None = None,
    markdown: bool = False,
    share_safe: bool = False,
) -> str:
    now = generated_at or datetime.now(timezone.utc)
    counts = Counter(
        check.status for check in run.checks
        if check.section != "Original-script parity"
    )
    lines: list[str] = []
    exercise_result = next((
        item for item in run.checks
        if item.section == "Full-live Twitch exercise"
        and item.name == "Full exercise result"
    ), None)
    mode_label = (
        "OFFLINE / INCOMPLETE" if run.offline else
        "AUTOMATED TWITCH EXERCISE / COMPLETED" if run.exercise_requested and exercise_result is not None and exercise_result.status == READY else
        "AUTOMATED TWITCH EXERCISE / BLOCKED OR INCOMPLETE" if run.exercise_requested else
        "READ-ONLY DIAGNOSTIC SUITE ATTEMPTED"
    )
    privacy_label = "SHARE-SAFE / REDACTED / INCOMPLETE" if share_safe else "PRIVATE / FULL / SENSITIVE"
    if markdown:
        lines.extend([
            "# Medlar diagnostics", "",
            f"- Generated: {now.astimezone(timezone.utc).isoformat(timespec='seconds')}",
            f"- Repository: `{'[REDACTED IN SHARE-SAFE MODE]' if share_safe else root.resolve()}`",
            f"- Launcher precedence mode: `{launcher_mode}`",
            f"- Diagnostic mode: **{mode_label}**",
            f"- Report fidelity: **{privacy_label}**",
            "- Default report: `logs/medlar_diagnostics.txt`",
            "- WARNING: the normal report contains exact usable credentials and raw network evidence. Keep it local; do not upload, paste, share, attach, or commit it." if not share_safe else
            "- WARNING: this share-safe rendering intentionally redacts credentials, request bodies, response bodies, and sensitive headers; it is not the full diagnostic dump.",
            "- Safety: normal mode performs read-only live validation; `--offline` is intentionally incomplete. The separate exercise mode permits only explicit temporary title/category changes plus one diagnostic chat message.",
            "", "## Summary", "",
            f"READY={counts[READY]}  UNAVAILABLE={counts[UNAVAILABLE]}  SKIPPED={counts[SKIPPED]}  ERROR={counts[ERROR]}",
            "Original-script parity rows are excluded from these runtime outcome totals.",
        ])
    else:
        lines.extend([
            "MEDLAR DIAGNOSTICS", "=" * 72,
            f"Generated: {now.astimezone(timezone.utc).isoformat(timespec='seconds')}",
            f"Repository: {'[REDACTED IN SHARE-SAFE MODE]' if share_safe else root.resolve()}",
            f"Launcher precedence mode: {launcher_mode}",
            f"Diagnostic mode: {mode_label}",
            f"Report fidelity: {privacy_label}",
            "Default report: logs/medlar_diagnostics.txt",
            "WARNING: THE NORMAL REPORT CONTAINS EXACT USABLE CREDENTIALS AND RAW"
            if not share_safe else
            "WARNING: SHARE-SAFE OUTPUT IS INTENTIONALLY REDACTED AND IS NOT THE",
            "         NETWORK EVIDENCE. KEEP IT LOCAL; DO NOT UPLOAD, PASTE, SHARE,"
            if not share_safe else
            "         FULL DIAGNOSTIC DUMP. CREDENTIALS, SENSITIVE HEADERS, REQUEST",
            "         ATTACH, OR COMMIT IT."
            if not share_safe else
            "         BODIES, AND RESPONSE BODIES ARE WITHHELD.",
            "Safety: normal mode performs read-only live validation; --offline is",
            "        intentionally incomplete. The separate exercise mode permits",
            "        only explicit temporary title/category changes plus one",
            "        diagnostic chat message.",
            "", "SUMMARY", "-" * 72,
            f"READY={counts[READY]}  UNAVAILABLE={counts[UNAVAILABLE]}  "
            f"SKIPPED={counts[SKIPPED]}  ERROR={counts[ERROR]}",
            "Original-script parity rows are excluded from runtime outcome totals.",
        ])

    sections: list[str] = []
    for check in run.checks:
        if check.section not in sections:
            sections.append(check.section)
    for section in sections:
        lines.extend(["", f"## {section}" if markdown else section.upper(),
                      "" if markdown else "-" * 72])
        for check in (item for item in run.checks if item.section == section):
            display_status = check.status
            detail = _render_check_detail(check, run, share_safe=share_safe)
            lines.append(f"- [{display_status}] {check.name}: {detail}")

    lines.extend(_configuration_evidence_lines(run, markdown=markdown, share_safe=share_safe))
    lines.extend(_credential_evidence_lines(run, markdown=markdown, share_safe=share_safe))
    lines.extend(_http_evidence_lines(run, markdown=markdown, share_safe=share_safe))
    lines.extend(_failure_evidence_lines(run, markdown=markdown))

    lines.extend(_report_boundaries(markdown))
    report = "\n".join(lines)
    return redact_text(report, run.secrets) if share_safe else report


def resolve_output_path(value: str | Path | None, root: Path = REPO_ROOT) -> Path:
    path = root / "logs" / "medlar_diagnostics.txt" if value is None else Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    # Normalize lexically without following a final symlink/reparse point.
    path = Path(os.path.abspath(os.path.normpath(path)))
    for index, component in enumerate(path.parts):
        # The first component of a resolved Windows path is the drive anchor
        # (for example, ``C:\\``); its colon is not an ADS separator.
        if index == 0 and component == path.anchor:
            continue
        if ":" in component:
            raise ValueError("Report path must not contain an alternate data stream")
        is_reserved = getattr(os.path, "isreserved", None)
        reserved = (
            bool(is_reserved(component)) if callable(is_reserved)
            else PureWindowsPath(component).is_reserved()
        )
        if reserved:
            raise ValueError("Report path must not use a reserved Windows name")
    if path.suffix.casefold() not in {".txt", ".md"}:
        raise ValueError("Report filename must end in .txt or .md")
    if path.exists() and path.is_dir():
        raise ValueError("Report path names a directory")
    return path


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        _capture_failure(sys.exc_info()[1])
        return False
    except OSError:
        _capture_failure(sys.exc_info()[1])
        raise ValueError("Report path safety could not be verified") from None


def _normalized_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def ensure_output_allowed(
    path: Path, *, overwrite: bool = False, default_report: Path = DEFAULT_REPORT,
) -> None:
    current = path
    while True:
        if _is_link_or_reparse_point(current):
            raise ValueError("Report path must not traverse a symlink, junction, or reparse point")
        if current.parent == current:
            break
        current = current.parent
    owned_default = _normalized_path_key(path) == _normalized_path_key(default_report)
    if path.exists() and not owned_default and not overwrite:
        raise FileExistsError(
            "Refusing to replace an existing non-default report; pass --overwrite explicitly"
        )


def ensure_sensitive_output_allowed(path: Path, root: Path = REPO_ROOT) -> None:
    logs_root = Path(os.path.abspath(os.path.normpath(root / "logs")))
    normalized = Path(os.path.abspath(os.path.normpath(path)))
    try:
        contained = os.path.commonpath((str(logs_root), str(normalized))) == str(logs_root)
    except ValueError:
        _capture_failure(sys.exc_info()[1])
        contained = False
    if not contained or normalized == logs_root:
        raise ValueError("Sensitive reports must use an explicit .txt/.md file under the repository logs directory")
    try:
        ignore_lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        _capture_failure(sys.exc_info()[1])
        ignore_lines = []
    if "/logs/" not in {line.strip() for line in ignore_lines}:
        raise ValueError("Sensitive output requires the repository root /logs/ ignore rule")
    git_marker = root / ".git"
    if git_marker.exists():
        try:
            relative = normalized.relative_to(root)
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(relative)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            _capture_failure(sys.exc_info()[1])
            raise ValueError("Sensitive output Git-ignore status could not be verified") from None
        if ignored.returncode != 0:
            raise ValueError(
                "Sensitive output must be effectively Git-ignored and untracked; git check-ignore did not confirm it"
            )


def write_report(
    path: Path, text: str, *, overwrite: bool = False,
    default_report: Path = DEFAULT_REPORT,
) -> None:
    path = Path(os.path.abspath(os.path.normpath(path)))
    ensure_output_allowed(path, overwrite=overwrite, default_report=default_report)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        _capture_failure(sys.exc_info()[1])
        try:
            temporary.unlink()
        except OSError:
            _capture_failure(sys.exc_info()[1])
            pass
        raise


def open_in_editor(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    editor = os.environ.get("EDITOR")
    if editor:
        subprocess.Popen([*shlex.split(editor), str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Medlar's PRIVATE/FULL local diagnostic report. The default output contains "
            "exact usable credentials and complete raw HTTP network evidence and must remain in ignored logs/."
        )
    )
    parser.add_argument("--output", metavar="FILE",
                        help="Report path ending in .txt or .md; relative paths use the project root.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Allow replacing an existing non-default .txt/.md report.")
    parser.add_argument("--open", action="store_true", dest="open_editor",
                        help="Open the report in the system editor after a successful write.")
    parser.add_argument("--launcher-mode", choices=("all", "twitch", "ollama"), default="all",
                        help="Configuration precedence to model (default: all).")
    parser.add_argument("--live-twitch", action="store_true",
                        help="Compatibility flag: normal all/twitch runs already perform complete read-only Twitch checks.")
    parser.add_argument("--offline", action="store_true",
                        help="Run an explicitly incomplete diagnostic without resolving credential values or contacting Twitch.")
    parser.add_argument("--exercise-twitch", action="store_true",
                        help="Run only allowlisted title/category/chat checks; changes are externally visible, restoration can fail, and chat cannot be undone.")
    parser.add_argument("--target-channel", metavar="LOGIN",
                        help="Explicit Twitch channel login required by --exercise-twitch.")
    parser.add_argument("--test-title", metavar="TEXT",
                        help="Temporary title required by --exercise-twitch (restoration is attempted and verified).")
    parser.add_argument("--test-category", metavar="NAME",
                        help="Exact temporary category required by --exercise-twitch (restoration is attempted and verified).")
    parser.add_argument("--test-chat-message", metavar="TEXT",
                        help="One message starting '[Medlar diagnostics]' required by --exercise-twitch.")
    parser.add_argument("--no-env-file", action="store_true",
                        help="Do not open the repository .env file.")
    parser.add_argument("--no-credential-store", action="store_true",
                        help="Do not query saved Windows credential presence or values.")
    parser.add_argument("--no-service-probes", action="store_true",
                        help="Disable local HTTP/WebSocket/bind probes; exercise baseline safety probes remain required.")
    parser.add_argument("--no-write-probes", action="store_true",
                        help="Do not create/delete unique disposable files when checking runtime path writability.")
    parser.add_argument("--include-secret-values", action="store_true",
                        help="Compatibility flag: exact credential values are already included in the normal private/full report.")
    return parser



def _terminal_failure(stage: str, exc: BaseException, output: Path | None, *, saved: bool = False) -> None:
    # Terminal is not a private-report destination: do not print exception values.
    kind = next((cls.__name__ for cls in type(exc).__mro__
                 if cls.__module__ == "builtins" and issubclass(cls, BaseException)), "Exception")
    frame, line = exc.__traceback__, None
    while frame is not None:
        if frame.tb_frame.f_code.co_filename == __file__:
            line = frame.tb_lineno
        frame = frame.tb_next
    codes = "".join(f"; {name}={value}" for name in ("errno", "winerror")
                    if type(value := getattr(exc, name, None)) is int)
    location = f"medlar_diagnostics.py:{line}" if line is not None else "external collaborator"
    print(f"ERROR: stage={stage}; exception={kind}; location={location}{codes}.", file=sys.stderr)
    if stage == "editor" and saved:
        print("The report was saved, but the editor did not open. Its failure is appended to the same report when saving succeeds.", file=sys.stderr)
    else:
        print("The report was not refreshed. An existing report at that destination remains stale; no fallback report was created.", file=sys.stderr)
    if output is not None:
        print(f"Report destination: {output}", file=sys.stderr)


def _append_private_value(lines: list[str], label: str, value: Any) -> None:
    if isinstance(value, dict):
        lines.append(label + f" (mapping; entries={len(value)}):")
        for key, item in value.items():
            _append_private_value(lines, label + "." + str(key), item)
    elif isinstance(value, (list, tuple)):
        lines.append(label + f" (sequence; entries={len(value)}):")
        for index, item in enumerate(value):
            _append_private_value(lines, f"{label}[{index}]", item)
    else:
        lines.extend([label + ":", value if isinstance(value, str) else repr(value)])


def _emergency_full_report(run: DiagnosticRun, *, root: Path, launcher_mode: str) -> str:
    # Same destination, full evidence. Emit raw strings verbatim, including bodies,
    # instead of repr(), which can wrap or escape them during rendering failures.
    lines = ["MEDLAR DIAGNOSTICS - FULL PRIVATE EVIDENCE (RENDERING FAILURE)",
             "WARNING: SENSITIVE CREDENTIALS AND COMPLETE AVAILABLE NETWORK EVIDENCE. KEEP LOCAL.",
             f"Generated: {datetime.now(timezone.utc).isoformat()}", f"Repository: {root}",
             f"Launcher precedence mode: {launcher_mode}", "SUMMARY",
             f"Collection complete={run.complete}; collection failed={run.collection_failed}"]
    for section in dict.fromkeys(check.section for check in run.checks):
        lines.extend(["", section.upper()])
        lines.extend(f"- [{check.status}] {check.name}: {check.detail}" for check in run.checks if check.section == section)
    headings = {"configuration_values": "CONFIGURATION VALUES USED", "sensitive_values": "PRIVATE CREDENTIAL VALUES USED",
                "http_exchanges": "HTTP REQUEST/RESPONSE TRANSCRIPT", "failures": "DIAGNOSTIC FAILURES AND TRACEBACKS"}
    for name, value in asdict(run).items():
        lines.extend(["", headings.get(name, name.upper())])
        _append_private_value(lines, name, value)
    lines.extend(_report_boundaries(False))
    return "\n".join(lines) + "\n"


def _render_full_with_failures(run: DiagnosticRun, root: Path, launcher_mode: str, markdown: bool) -> str:
    try:
        return render_report(run, root=root, launcher_mode=launcher_mode, markdown=markdown, share_safe=False)
    except Exception as exc:
        run.collection_failed = True
        run.failures.append({"stage": "report rendering", "exception": type(exc).__name__, "traceback": _exception_evidence(exc)})
        _finish_coverage(run)
        return _emergency_full_report(run, root=root, launcher_mode=launcher_mode)


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    relaunch_failure = None
    try:
        relaunched = reexec_in_launcher_python(raw_argv)
    except Exception as exc:
        relaunch_failure = exc
        relaunched = None
    if relaunched is not None:
        return relaunched
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    if args.offline and args.live_twitch:
        parser.error("--offline and --live-twitch are mutually exclusive")
    if args.live_twitch and args.launcher_mode == "ollama":
        parser.error("--live-twitch cannot be combined with --launcher-mode ollama")
    exercise_values = (
        args.target_channel, args.test_title, args.test_category, args.test_chat_message,
    )
    if args.exercise_twitch and (args.offline or args.launcher_mode == "ollama"):
        parser.error("--exercise-twitch requires normal live all/twitch mode and cannot be used with --offline")
    if any(value is not None for value in exercise_values) and not args.exercise_twitch:
        parser.error("--target-channel and --test-* values require --exercise-twitch")
    if args.exercise_twitch and not all(value is not None for value in exercise_values):
        parser.error(
            "--exercise-twitch requires --target-channel, --test-title, --test-category, and --test-chat-message"
        )
    exercise = None
    if args.exercise_twitch:
        exercise = TwitchExercise(
            target_channel=args.target_channel,
            title=args.test_title,
            category=args.test_category,
            chat_message=args.test_chat_message,
        )
        try:
            validate_twitch_exercise(exercise)
        except ValueError as exc:
            _capture_failure(sys.exc_info()[1])
            parser.error(str(exc))
    effective_live = not args.offline and args.launcher_mode != "ollama"
    run: DiagnosticRun | None = None
    output: Path | None = None
    try:
        output = resolve_output_path(args.output, REPO_ROOT)
        ensure_sensitive_output_allowed(output, REPO_ROOT)
        ensure_output_allowed(output, overwrite=args.overwrite, default_report=DEFAULT_REPORT)
    except Exception as exc:
        _terminal_failure("output validation", exc, output)
        return 2
    print("WARNING: the full private report contains exact usable credentials and complete available HTTP evidence; retain locally.", file=sys.stderr)
    try:
        if relaunch_failure is not None:
            raise relaunch_failure
        run = collect_diagnostics(
            root=REPO_ROOT, launcher_mode=args.launcher_mode,
            use_env_file=not args.no_env_file, use_vault=not args.no_credential_store,
            probe_services=not args.no_service_probes, probe_writes=not args.no_write_probes,
            live_twitch=effective_live, twitch_exercise=exercise, include_secret_values=True,
        )
    except Exception as exc:
        # Collector normally returns its owned partial run; retain a collaborator
        # supplied partial run too if an unexpected outer integration fails.
        partial = getattr(exc, "diagnostic_run", None)
        run = partial if isinstance(partial, DiagnosticRun) else DiagnosticRun()
        run.offline = not effective_live
        run.exercise_requested = args.exercise_twitch
        run.collection_failed = True
        run.critical_failure = args.exercise_twitch
        run.failures.append({"stage": "interpreter relaunch" if relaunch_failure is not None else "outer diagnostic collection", "exception": type(exc).__name__, "traceback": _exception_evidence(exc)})
        _finish_coverage(run)
    text = _render_full_with_failures(run, REPO_ROOT, args.launcher_mode, output.suffix.casefold() == ".md")
    try:
        write_report(output, text, overwrite=args.overwrite, default_report=DEFAULT_REPORT)
    except Exception as exc:
        _terminal_failure("report save", exc, output)
        return 4 if run.critical_failure or args.exercise_twitch else 2
    print(f"Report written: {output}")
    counts = Counter(check.status for check in run.checks if check.section != "Original-script parity")
    print(f"Summary: READY={counts[READY]} UNAVAILABLE={counts[UNAVAILABLE]} SKIPPED={counts[SKIPPED]} ERROR={counts[ERROR]}")
    terminal_outcomes = {
        ("Required execution baseline", "Medlar closed"),
        ("Required execution baseline", "Medlar Twitch disconnected"),
        ("Required execution baseline", "Ollama processes stopped"),
        ("Required execution baseline", "Ollama endpoint stopped"),
        ("Required execution baseline", "Broadcaster offline before diagnostic"),
        ("Required execution baseline", "Remote baseline gate"),
        ("Component connections", "Avatar bridge handshake"),
        ("Ollama", "Service readiness"),
        ("Ollama", "Selected model"),
        ("LibreTranslate", "Service readiness"),
        ("Live Twitch", "Bot token validation"),
        ("Live Twitch", "Broadcaster token validation"),
        ("Live Twitch", "App credential exchange"),
        ("Full-live Twitch exercise", "Broadcaster offline before mutation"),
        ("Full-live Twitch exercise", "Title update verification"),
        ("Full-live Twitch exercise", "Category update verification"),
        ("Full-live Twitch exercise", "Title restoration verification"),
        ("Full-live Twitch exercise", "Category restoration verification"),
        ("Full-live Twitch exercise", "Broadcaster offline before chat"),
        ("Full-live Twitch exercise", "Diagnostic chat command write"),
        ("Full-live Twitch exercise", "IRC post-send acknowledgement"),
        ("Full-live Twitch exercise", "Full exercise result"),
    }
    for check in run.checks:
        if (check.section, check.name) in terminal_outcomes:
            print(f"Outcome: {check.section} / {check.name} = {check.status}")
    editor_failed = False
    if args.open_editor:
        try:
            open_in_editor(output)
        except Exception as exc:
            editor_failed = True
            run.failures.append({"stage": "report editor", "exception": type(exc).__name__, "traceback": _exception_evidence(exc)})
            try:
                write_report(output, _render_full_with_failures(run, REPO_ROOT, args.launcher_mode, output.suffix.casefold() == ".md"), overwrite=args.overwrite, default_report=DEFAULT_REPORT)
            except Exception as save_exc:
                _terminal_failure("editor evidence save", save_exc, output)
            _terminal_failure("editor", exc, output, saved=True)
    if run.critical_failure:
        print("ERROR: requested exercise was blocked or incomplete; review the full report and restoration evidence.", file=sys.stderr)
        return 4
    if editor_failed:
        return 3
    if run.collection_failed:
        print("ERROR: collection or rendering was interrupted; completed evidence and full failure details were saved in the report.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
