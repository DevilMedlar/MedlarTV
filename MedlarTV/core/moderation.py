from __future__ import annotations
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, cast

import os
import re
from urllib.parse import SplitResult, urlsplit
import yaml
from pathlib import Path
from typing import Dict, Any, Optional

if TYPE_CHECKING:
    class _CommandSocket(Protocol):
        def send(self, data: bytes) -> object: ...

_timeouts: Dict[str, int] = {}

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
LINK_WHITELIST_PATH = CONFIG_DIR / "link_whitelist.yaml"
CONTENT_FILTER_PATH = CONFIG_DIR / "content_filter.yaml"

def load_link_whitelist() -> set[str]:
    p = LINK_WHITELIST_PATH
    if not p.exists():
        return set()
    try:
        with p.open("r", encoding="utf-8") as f:
            data: object = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise TypeError("Invalid whitelist mapping")
            mapping = cast(dict[object, object], data)
            items = cast(Iterable[object], mapping.get("whitelist", []))
            return set(str(x).lower() for x in items)
    except Exception:
        return set()

def _extract_domains(text: str) -> list[str]:
    # This parser repairs the dormant checker; it does not enable moderation.
    urls = re.findall(r"(?:https?://|www\.)[^\s]+", text, flags=re.IGNORECASE)
    domains: list[str] = []
    for value in urls:
        value = value.rstrip(".,;!?)}")
        if value.endswith("]") and "[" not in value:
            value = value.rstrip("]")  # Preserve IPv6 brackets.
        try:
            parsed = cast(SplitResult, urlsplit(value if value.lower().startswith("http") else "http://" + value))
            parsed.port  # Malformed ports are invalid evidence, not a trusted host.
            host = parsed.hostname
            if parsed.scheme.lower() not in {"http", "https"} or not host:
                raise ValueError("Invalid link")
            domains.append(host.lower())
        except ValueError:
            domains.append("")  # Fail closed if the dormant checker is invoked.
    return domains


def _has_excess_caps(text: str) -> bool:
    alpha = re.sub(r"[^A-Za-z]", "", text)
    if len(alpha) < 6:
        return False
    upper = sum(1 for c in alpha if c.isupper())
    return upper / max(1, len(alpha)) > 0.7

def _has_spam(text: str) -> bool:
    if re.search(r"(.)\1{4,}", text):
        return True
    if len(text) > 300 and any(word for word in text.split() if len(word) > 40):
        return True
    return False

def get_user_timeout_count(username: str) -> int:
    return _timeouts.get(username.lower(), 0)

def _inc_timeout(username: str) -> int:
    key = username.lower()
    _timeouts[key] = _timeouts.get(key, 0) + 1
    return _timeouts[key]

def check_message(username: str, message: str, tags: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg_path = CONTENT_FILTER_PATH
    blocked_words: Iterable[object] = []
    blocked_topics: Iterable[object] = []
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
                c: object = yaml.safe_load(f) or {}
                if not isinstance(c, dict):
                    raise TypeError("Invalid moderation mapping")
                mapping = cast(dict[object, object], c)
                blocked_words = cast(Iterable[object], mapping.get("blocked_words", []))
                blocked_topics = cast(Iterable[object], mapping.get("blocked_topics", []))
        except Exception:
            pass

    from MedlarTV.core.content_filter import normalize_text
    norm = normalize_text(message)

    for w in blocked_words:
        wnorm = normalize_text(cast(str, w))
        if wnorm and re.search(r"\b" + re.escape(wnorm) + r"\b", norm):
            return {"is_allowed": False, "action": "timeout", "duration": 600, "reason": "blocked word"}

    for t in blocked_topics:
        tnorm = normalize_text(cast(str, t))
        if tnorm and tnorm in norm:
            return {"is_allowed": False, "action": "timeout", "duration": 600, "reason": "blocked topic"}

    link_filter = os.getenv("MOD_LINK_FILTER", "true").lower() == "true"
    if link_filter:
        wl = load_link_whitelist()
        domains = _extract_domains(message)
        for d in domains:
            base = d  # Already the parsed hostname, never URL user-info.
            if base.startswith("www."):
                base = base[4:]
            if base not in wl:
                msg_id = tags.get("id") if tags else None
                if msg_id:
                    return {"is_allowed": False, "action": "delete", "msg_id": msg_id, "reason": "link not allowed"}
                count = _inc_timeout(username)
                dur = 60 * min(10, count)
                return {"is_allowed": False, "action": "timeout", "duration": dur, "reason": "link not allowed"}

    caps_filter = os.getenv("MOD_CAPS_FILTER", "true").lower() == "true"
    if caps_filter and _has_excess_caps(message):
        count = _inc_timeout(username)
        dur = 30 * min(10, count)
        return {"is_allowed": False, "action": "warn", "duration": dur, "reason": "excess caps"}

    spam_filter = os.getenv("MOD_SPAM_FILTER", "true").lower() == "true"
    if spam_filter and _has_spam(message):
        count = _inc_timeout(username)
        dur = 120 * min(10, count)
        return {"is_allowed": False, "action": "timeout", "duration": dur, "reason": "spam"}

    return {"is_allowed": True}

def execute_timeout(sock: _CommandSocket | None, channel: str, username: str, duration: int, reason: str = "") -> None:
    if not sock:
        return
    cmd = f"PRIVMSG {channel} :/timeout {username} {int(duration)} {reason}".strip()
    sock.send((cmd + "\r\n").encode("utf-8"))

def execute_ban(sock: _CommandSocket | None, channel: str, username: str, reason: str = "") -> None:
    if not sock:
        return
    cmd = f"PRIVMSG {channel} :/ban {username} {reason}".strip()
    sock.send((cmd + "\r\n").encode("utf-8"))

def execute_delete(sock: _CommandSocket | None, channel: str, msg_id: str) -> None:
    if not sock:
        return
    cmd = f"PRIVMSG {channel} :/delete {msg_id}".strip()
    sock.send((cmd + "\r\n").encode("utf-8"))

def is_mod_command(message: str) -> bool:
    m = message.strip().lower()
    return m.startswith("!timeout") or m.startswith("!ban") or m.startswith("!warn") or m.startswith("!unwarn")

def execute_shoutout(sock: _CommandSocket | None, channel: str, target_username: str) -> None:
    if not sock:
        return
    cmd = f"PRIVMSG {channel} :/shoutout {target_username}".strip()
    sock.send((cmd + "\r\n").encode("utf-8"))

def handle_mod_command(message: str) -> Optional[Dict[str, Any]]:
    m = message.strip()
    parts = m.split()
    if not parts:
        return None
    cmd = parts[0].lower()
    if cmd == "!timeout" and len(parts) >= 3:
        user = parts[1]
        try:
            dur = int(parts[2])
        except Exception:
            dur = 60
        reason = " ".join(parts[3:]) if len(parts) > 3 else ""
        return {"action": "timeout", "user": user, "duration": dur, "reason": reason}
    if cmd == "!ban" and len(parts) >= 2:
        user = parts[1]
        reason = " ".join(parts[2:]) if len(parts) > 2 else ""
        return {"action": "ban", "user": user, "reason": reason}
    if cmd == "!warn" and len(parts) >= 2:
        user = parts[1]
        reason = " ".join(parts[2:]) if len(parts) > 2 else ""
        return {"action": "warn", "user": user, "reason": reason}
    if cmd == "!unwarn" and len(parts) >= 2:
        user = parts[1]
        return {"action": "unwarn", "user": user}
    return None
