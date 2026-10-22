"""Current times from the local clock and IANA timezone data; no network lookup."""

from typing import Sequence, cast
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

log = logging.getLogger("commands.time_lookup")


class TimeLookupUnavailable(RuntimeError):
    """Timezone data or the local clock could not provide a requested time."""


def _log_lookup_result(operation: str, reason: str, exception: Exception | None,
                       started: float) -> None:
    """Log local outcomes without locations, messages, payloads, or URLs."""
    elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
    log.log(
        logging.INFO if reason == "ok" else logging.WARNING,
        "TimeLookup source=local operation=%s reason=%s exception=%s elapsed_ms=%d",
        operation, reason,
        type(exception).__name__ if exception is not None else "None", elapsed_ms,
    )


def _normalize_query(query: str | None) -> str:
    return " ".join(query.lower().split()) if isinstance(query, str) else ""


def _fetch_time(timezone: str) -> datetime:
    """Calculate the current time in a known IANA zone using the laptop clock."""
    started = time.perf_counter()
    try:
        zone = ZoneInfo(timezone)
    except Exception as exc:
        _log_lookup_result("time_by_zone", "timezone_data_unavailable", exc, started)
        raise TimeLookupUnavailable("Time lookup unavailable") from exc
    try:
        result = datetime.now(zone)
    except Exception as exc:
        _log_lookup_result("time_by_zone", "clock_error", exc, started)
        raise TimeLookupUnavailable("Time lookup unavailable") from exc
    _log_lookup_result("time_by_zone", "ok", None, started)
    return result


def _fetch_timezones() -> list[str]:
    """List installed IANA zones, distinguishing absent data from no matches."""
    started = time.perf_counter()
    try:
        zones = cast(Sequence[object], sorted(available_timezones()))
        if not zones or any(not isinstance(zone, str) or not zone for zone in zones):
            raise ZoneInfoNotFoundError("Timezone data unavailable")
    except Exception as exc:
        _log_lookup_result("timezone_list", "timezone_data_unavailable", exc, started)
        raise TimeLookupUnavailable("Timezone list unavailable") from exc
    _log_lookup_result("timezone_list", "ok", None, started)
    return cast(list[str], zones)

# Shared aliases for common city/country names → canonical timezones
TIMEZONE_ALIASES: dict[str, str] = {
    # Countries / cities
    "philippines": "Asia/Manila",
    "manila": "Asia/Manila",
    "japan": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "uk": "Europe/London",
    "united kingdom": "Europe/London",
    "england": "Europe/London",
    "london": "Europe/London",
    "usa": "America/New_York",
    "us": "America/New_York",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "california": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "china": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "canada": "America/Toronto",
    "toronto": "America/Toronto",
    "russia": "Europe/Moscow",
    "moscow": "Europe/Moscow",
    "delhi": "Asia/Kolkata",
    "india": "Asia/Kolkata",
}

def _format_time(label: str, dt: datetime, *, include_date: bool = True) -> str:
    """Render one place once; summaries retain their compact time-only format."""
    result = f"{label}: {dt.strftime('%I:%M %p')}"
    if include_date:
        result += f" on {dt.strftime('%B %d, %Y')}"
    return result


def get_current_time(location: str) -> str | None:
    """Get a dated local time for a known alias using its resolved city label."""
    timezone = TIMEZONE_ALIASES.get(_normalize_query(location))
    if not timezone:
        return None
    dt = _fetch_time(timezone)
    label = timezone.split("/")[-1].replace("_", " ")
    return _format_time(label, dt)


def should_lookup_time(message: object) -> tuple[bool, str | None]:
    """
    Check if message is asking for current time.

    Returns:
        (should_lookup, location)
    """
    if not isinstance(message, str):
        return False, None
    message_lower = message.lower()

    # Time indicators
    time_indicators = [
        "what time is it",
        "current time",
        "time now",
        "what's the time",
        "whats the time"
    ]

    indicator_match = any(indicator in message_lower for indicator in time_indicators)

    if not indicator_match:
        return False, None

    m = re.search(r"\bin\s+([a-zA-Z0-9/_+\-\s]+)", message_lower)
    if m:
        loc = " ".join(m.group(1).split()).strip("?.! ")
        loc = re.sub(r"\s+today$", "", loc)
        if loc.startswith("the "):
            loc = loc[4:]
        return True, loc
    return True, None


# Integration with LLM brain
def resolve_timezones_for_query(query: str) -> list[str]:
    q = _normalize_query(query)
    if not q:
        return []
    synonyms = {
        "rusia": "russia",
        "united states": "us",
        "united kingdom": "uk",
        "england": "europe/london",
        "nyc": "new york",
        "la": "los angeles",
        "eastern time": "america/new_york",
        "est": "america/new_york",
        "edt": "america/new_york",
    }
    q = synonyms.get(q, q)
    # Common aliases resolve without enumerating the local timezone catalog.
    if q in TIMEZONE_ALIASES:
        return [TIMEZONE_ALIASES[q]]
    zones = list_timezones(None)
    if not zones:
        return []
    pattern_space = q.replace(" ", "_")
    matches: list[str] = []
    for z in zones:
        zl = z.lower()
        last = z.split("/")[-1].lower().replace("_", " ")
        if q in zl or pattern_space in zl or q in last:
            matches.append(z)
    # Exact timezone path
    if "/" in q:
        for z in zones:
            if z.lower() == q:
                return [z]
    # Deduplicate
    uniq: list[str] = []
    seen: set[str] = set()
    for z in matches:
        if z not in seen:
            uniq.append(z)
            seen.add(z)
    return uniq

def get_times_for_location(location: str) -> str | None:
    loc = _normalize_query(location)
    if not loc:
        return None
    # Single-zone alias fast path
    if loc in TIMEZONE_ALIASES:
        tz = TIMEZONE_ALIASES[loc]
        dt = cast(datetime | None, _fetch_time(tz))
        if dt is not None:
            label = tz.split("/")[-1].replace("_", " ")
            return _format_time(label, dt)
    multi = {
        "usa": [
            ("America/New_York", "Eastern"),
            ("America/Chicago", "Central"),
            ("America/Denver", "Mountain"),
            ("America/Los_Angeles", "Pacific"),
        ],
        "america": [
            ("America/New_York", "Eastern"),
            ("America/Chicago", "Central"),
            ("America/Denver", "Mountain"),
            ("America/Los_Angeles", "Pacific"),
        ],
        "us": [
            ("America/New_York", "Eastern"),
            ("America/Chicago", "Central"),
            ("America/Denver", "Mountain"),
            ("America/Los_Angeles", "Pacific"),
        ],
        "canada": [
            ("America/Vancouver", "Pacific"),
            ("America/Edmonton", "Mountain"),
            ("America/Winnipeg", "Central"),
            ("America/Toronto", "Eastern"),
            ("America/Halifax", "Atlantic"),
            ("America/St_Johns", "Newfoundland"),
        ],
        "russia": [
            ("Europe/Moscow", "Moscow"),
            ("Asia/Yekaterinburg", "Yekaterinburg"),
            ("Asia/Novosibirsk", "Novosibirsk"),
            ("Asia/Krasnoyarsk", "Krasnoyarsk"),
            ("Asia/Irkutsk", "Irkutsk"),
            ("Asia/Yakutsk", "Yakutsk"),
            ("Asia/Vladivostok", "Vladivostok"),
            ("Asia/Magadan", "Magadan"),
            ("Asia/Kamchatka", "Kamchatka"),
        ],
        "china": [
            ("Asia/Shanghai", "China"),
        ],
    }
    if loc in multi:
        parts: list[str] = []
        for tz, label in multi[loc]:
            try:
                dt = _fetch_time(tz)
            except TimeLookupUnavailable:
                continue
            parts.append(_format_time(label, dt, include_date=False))
        if parts:
            return " | ".join(parts)
        raise TimeLookupUnavailable("Time lookup unavailable")
    zones = resolve_timezones_for_query(location)
    if not zones:
        return None
    if len(zones) == 1:
        dt = cast(datetime | None, _fetch_time(zones[0]))
        if dt is not None:
            label = zones[0].split("/")[-1].replace("_", " ")
            return _format_time(label, dt)
        return None
    parts: list[str] = []
    for tz in zones[:8]:
        try:
            dt = _fetch_time(tz)
        except TimeLookupUnavailable:
            continue
        label = tz.split("/")[-1].replace("_", " ")
        parts.append(_format_time(label, dt, include_date=False))
    if not parts:
        raise TimeLookupUnavailable("Time lookup unavailable")
    return " | ".join(parts)

def get_default_local_time() -> str | None:
    dt = cast(datetime | None, _fetch_time("America/New_York"))
    if dt is not None:
        return _format_time("Eastern Time (US & Canada)", dt)
    return None

def list_timezones(filter_text: object = None) -> list[str]:
    if filter_text is not None and not isinstance(filter_text, str):
        return []
    zones = _fetch_timezones()
    if filter_text:
        ft = filter_text.lower()
        zones = [z for z in zones if ft in z.lower()]
    return zones

def world_clock_summary() -> str:
    zones = [
        ("Europe/London", "London"),
        ("Europe/Berlin", "Berlin"),
        ("Europe/Moscow", "Moscow"),
        ("Africa/Johannesburg", "Johannesburg"),
        ("Asia/Dubai", "Dubai"),
        ("Asia/Kolkata", "Delhi"),
        ("Asia/Shanghai", "Shanghai"),
        ("Asia/Tokyo", "Tokyo"),
        ("Australia/Sydney", "Sydney"),
        ("Pacific/Auckland", "Auckland"),
        ("America/Sao_Paulo", "São Paulo"),
        ("America/New_York", "New York"),
        ("America/Chicago", "Chicago"),
        ("America/Denver", "Denver"),
        ("America/Los_Angeles", "Los Angeles"),
        ("America/Anchorage", "Anchorage"),
        ("Pacific/Honolulu", "Honolulu"),
    ]
    parts: list[str] = []
    for tz, label in zones:
        try:
            dt = _fetch_time(tz)
        except TimeLookupUnavailable:
            continue
        parts.append(_format_time(label, dt, include_date=False))
    return " | ".join(parts) if parts else "World clock unavailable"

def enhance_time_query(user_message: str) -> str:
    """
    If message asks for time, get real time data.
    Returns context string to add to prompt, or empty string.
    """
    should_lookup, location = should_lookup_time(user_message)

    if should_lookup and location:
        time_info = get_times_for_location(location)

        if time_info:
            return f"\n\n[REAL-TIME DATA]\n{time_info}\n[END REAL-TIME DATA]\n\n"
    return ""


if __name__ == "__main__":
    print("Testing time lookup...")
    print(get_current_time("Philippines"))
    print(get_current_time("Tokyo"))
    print(get_current_time("London"))

    print("\nTesting detection...")
    tests = [
        "What time is it in the Philippines?",
        "Current time in Tokyo?",
        "Who made you?"
    ]

    for test in tests:
        print(f"[DEBUG time_lookup] Testing message: {test!r}")
        should, loc = should_lookup_time(test)
        print(f"{test} -> {should}, {loc}")
