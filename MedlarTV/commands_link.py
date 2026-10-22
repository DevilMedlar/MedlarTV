"""Shared catalog location; configuration inspection never guesses network routes."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Mapping, cast

DEFAULT_COMMANDS_URL = "https://DevilMedlar.github.io/medlar-commands-site/"
COMMANDS_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "commands.yaml"
OVERRIDE_KEYS = ("COMMANDS_URL", "MEDLAR_COMMANDS_URL", "COMMANDS_HOST", "COMMANDS_PORT")


def resolve_commands_link(environ: Mapping[str, str | None] | None = None) -> str:
    """Explicit URL, compatibility URL, explicit host/port, YAML, public fallback.

    Preserve intentional private/LAN HTTP addresses. Empty settings are unset;
    host/port defaults apply only when at least one was explicitly selected.
    """
    values = environ if environ is not None else {key: os.getenv(key) for key in OVERRIDE_KEYS}
    def setting(key: str) -> str:
        return (values.get(key) or "").strip()

    for key in ("COMMANDS_URL", "MEDLAR_COMMANDS_URL"):
        if setting(key):
            return setting(key)
    if setting("COMMANDS_HOST") or setting("COMMANDS_PORT"):
        host = setting("COMMANDS_HOST") or "127.0.0.1"
        port = setting("COMMANDS_PORT") or "8080"
        return f"http://{host}:{port}/"

    # The same non-secret YAML owns catalog prose and the default catalog link.
    import yaml
    try:
        data: object = yaml.safe_load(COMMANDS_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise TypeError("Invalid commands catalog mapping")
        mapping = cast(dict[object, object], data)
        command = cast(dict[object, object], mapping["commands"])["commands"]
        if not isinstance(command, dict):
            raise AttributeError("Invalid commands response mapping")
        response = cast(dict[object, object], command).get("response", "")
        if isinstance(response, str):
            match = re.search(r"https?://[^\s]+", response)
            if match:
                return match.group(0)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
        pass
    return DEFAULT_COMMANDS_URL
