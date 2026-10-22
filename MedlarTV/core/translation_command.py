"""Parse and execute Twitch translation commands."""

import re
from typing import Optional, Tuple

from MedlarTV.core.translation import (
    SERVICE_UNAVAILABLE,
    get_supported_languages,
    normalize_lang,
    supported_list_human,
    translate_text,
)

HELP = "!t <target> <text> | Optional source override: !t <target> <source>: <text> | Example: !t en tl: mahal kita"


def _parse_args(raw: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (target, optional source override, text) from chat arguments."""
    if not raw:
        return None, None, None

    parts = raw.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None, None, None

    target, text = parts[0].lower(), parts[1].strip()
    source = None
    match = re.match(r"^([a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?):\s*(.+)$", text)
    if match:
        source = match.group(1).lower()
        text = match.group(2)
    return target, source, text


def handle_t_command(raw_after_cmd: str, username: str) -> str:
    """Translate the text after !t, !trans, or !translate."""
    target, source, text = _parse_args(raw_after_cmd)
    if not target or not text:
        return HELP

    supported = get_supported_languages()
    if not supported:
        return f"@{username} {SERVICE_UNAVAILABLE}"

    target_code = normalize_lang(target, supported)
    if not target_code:
        return f"@{username} Unsupported language '{target}'. {HELP}"

    source_code = normalize_lang(source, supported) if source else None
    if source and not source_code:
        return f"@{username} Unsupported source language '{source}'. {HELP}"

    ok, result = translate_text(text, target_code, source_code, supported=supported)
    if not ok:
        return f"@{username} {result}"

    return f"@{username} → [{target_code}] {result} (from [{source_code or 'auto'}] to [{target_code}])"


def handle_tlang_command() -> str:
    return supported_list_human()
