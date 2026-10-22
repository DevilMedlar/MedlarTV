"""Local, user-scoped Windows credentials for Command Center connections.

Only credentials explicitly saved by the user are read. Legacy environment
values are never imported into the vault. Call ``apply_saved_credentials`` in
the launcher after loading legacy configuration and before starting children.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from types import MappingProxyType
from typing import MutableMapping


CREDENTIAL_FIELDS = MappingProxyType({
    "MEDLARTV_TWITCH_TOKEN": "MedlarTV bot Twitch OAuth token",
    "MEDLARTV_TWITCH_CLIENT_ID": "MedlarTV bot Twitch client ID",
    "APP_TWITCH_CLIENT_ID": "Twitch app client ID",
    "APP_SECRET_ID": "Twitch app client secret",
    "DEVILMEDLAR_TWITCH_CLIENT_ID": "DevilMedlar broadcaster Twitch client ID",
    "DEVILMEDLAR_TWITCH_TOKEN": "DevilMedlar broadcaster Twitch OAuth token",
})

_TARGET_PREFIX = "MedlarTV/Connections/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _target(key: str) -> str:
    if not isinstance(key, str) or key not in CREDENTIAL_FIELDS:
        raise ValueError("Unsupported credential field")
    return _TARGET_PREFIX + key


def _get_api():
    if os.name != "nt":
        raise OSError("Local credential storage requires Windows")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _read_record(api, key: str, *, include_value: bool) -> tuple[bool, str | None]:
    record_ptr = ctypes.POINTER(_CredentialW)()
    if not api.CredReadW(_target(key), _CRED_TYPE_GENERIC, 0, ctypes.byref(record_ptr)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return False, None
        raise OSError(error, "Unable to read local credential")

    try:
        if not include_value:
            return True, None
        record = record_ptr.contents
        size = record.CredentialBlobSize
        if size % 2 or (size and not record.CredentialBlob):
            raise ValueError("Stored credential has an invalid format")
        value = ctypes.string_at(record.CredentialBlob, size).decode("utf-16-le")
        return True, value
    finally:
        api.CredFree(ctypes.cast(record_ptr, ctypes.c_void_p))


def save_credential(key: str, value: str) -> None:
    """Save one newly entered value in the current user's local Windows vault."""
    target = _target(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Credential value cannot be empty")

    blob = value.encode("utf-16-le")
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    record = _CredentialW()
    record.Type = _CRED_TYPE_GENERIC
    record.TargetName = target
    record.CredentialBlobSize = len(blob)
    record.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
    record.Persist = _CRED_PERSIST_LOCAL_MACHINE
    record.UserName = "MedlarTV"
    if not _get_api().CredWriteW(ctypes.byref(record), 0):
        raise OSError(ctypes.get_last_error(), "Unable to save local credential")


def credential_present(key: str) -> bool:
    """Report whether a local override exists without decoding its value."""
    _target(key)
    found, _ = _read_record(_get_api(), key, include_value=False)
    return found


def apply_saved_credentials(environ: MutableMapping[str, str] | None = None) -> set[str]:
    """Overlay only saved credential keys; leave all other values untouched.

    Reads complete before modifying the target environment, so an API error
    cannot leave a partial credential overlay.
    """
    target = os.environ if environ is None else environ
    api = _get_api()
    saved: dict[str, str] = {}
    for key in CREDENTIAL_FIELDS:
        found, value = _read_record(api, key, include_value=True)
        if found:
            saved[key] = value if value is not None else ""
    target.update(saved)
    return set(saved)
