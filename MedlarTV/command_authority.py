"""Pure IRC provenance and role checks; no configuration or I/O effects."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import cast
from dataclasses import dataclass
import re


def normalize_login(value: object, *, mention: bool = False) -> str | None:
    """Accept one ASCII login; mentions are permitted only for co-pilot names."""
    if not isinstance(value, str):
        return None
    if mention:
        value = value.strip()
        if value.startswith("@"):
            value = value[1:]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", value):
        return None
    return value.lower()


def normalize_channel(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("#"):
        return None
    login = normalize_login(value[1:])
    return "#" + login if login else None


def _stable_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]+", value)) and bool(value.strip("0"))


@dataclass(frozen=True)
class CommandAuthority:
    source_channel: str | None
    primary_channel: str | None
    login: str | None
    # Rejected malformed context retains its original values for comparison.
    user_id: object
    room_id: object
    role: str
    identity_valid: bool

    def permits_primary_write(self, username: object, role: object) -> bool:
        return bool(
            self.identity_valid and self.source_channel and self.primary_channel
            and self.source_channel == self.primary_channel
            and self.login == normalize_login(username) and self.role == role
        )


def command_authority(
    username: object, context: object, tags: object,
    primary_channel: object, copilots: object,
) -> CommandAuthority:
    """Build authority from server-parsed identity, never display names or chat text.

    Numeric sender/room tags are required for writes. Untagged read-only commands
    retain their existing behavior. Role precedence is preserved except that a
    broadcaster is never downgraded by a VIP tag.
    """
    context = cast(Mapping[object, object], context) if isinstance(context, Mapping) else {}
    tags = cast(Mapping[object, object], tags) if isinstance(tags, Mapping) else {}
    source = normalize_channel(context.get("channel"))
    primary = normalize_channel(primary_channel)
    login = normalize_login(username)
    user_id, room_id = context.get("user_id", ""), context.get("room_id", "")
    valid = bool(
        source and primary and login and normalize_login(context.get("login")) == login
        and _stable_id(user_id) and _stable_id(room_id)
        and tags.get("user-id") == user_id and tags.get("room-id") == room_id
        and tags.get("mod", "0") in ("0", "1")
        and tags.get("vip", "0") in ("0", "1")
    )
    copilots = cast(Iterable[object], copilots) if isinstance(copilots, (list, tuple, set)) else ()
    active = {name for value in copilots if (name := normalize_login(value, mention=True))}
    role = "user"
    if login and primary and login == primary[1:]:
        role = "pilot"
        # The broadcaster's stable sender ID must be the primary room owner ID.
        valid = valid and user_id == room_id
    elif tags.get("vip") == "1":
        role = "copilot" if login in active else "vip"
    elif tags.get("mod") == "1":
        role = "mod"
    elif login in active:
        role = "copilot"
    return CommandAuthority(source, primary, login, user_id, room_id, role, valid)


def copilot_channels(primary_channel: object, copilots: Iterable[object]) -> list[str]:
    """Membership changes apply on reconnect; authority is refreshed per command."""
    primary = normalize_channel(primary_channel)
    return sorted({"#" + name for value in copilots
                   if (name := normalize_login(value, mention=True)) and "#" + name != primary})
