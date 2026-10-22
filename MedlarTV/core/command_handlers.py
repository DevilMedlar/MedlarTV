"""
MedlarTV Unified Command Handler (Refactored)
-------------------------------------------
Single source of truth for all bot commands.
This version is cleaned up, type-hinted, and aligned with the
updated MedlarTV.core.stream_management module.
"""
from __future__ import annotations

import calendar
import logging
import os
import time
from typing import Any, Callable, Dict, Optional
import random
import requests
from datetime import datetime, timezone
from pathlib import Path
import yaml
from MedlarTV.core.interaction_logger import log_command
from MedlarTV.core.time_lookup import TimeLookupUnavailable, get_times_for_location
from MedlarTV.core.time_lookup import world_clock_summary, list_timezones
from MedlarTV.core.web_search import search_web, search_wikipedia
from MedlarTV.core.translation_command import handle_t_command, handle_tlang_command, HELP as TRANSLATE_HELP

log = logging.getLogger("commands")
log.setLevel(logging.DEBUG)
log.debug("[DEBUG] command_handlers loaded and logging active")

# =============================================================================
# Type Aliases
# =============================================================================

CommandContext = Dict[str, Any]
CommandHandler = Callable[[str, str, CommandContext], str]

# =============================================================================
# COMMAND HANDLERS
# =============================================================================

def handle_ping(username: str, args: str, context: CommandContext) -> str:
    """!ping - Show bot latency based on message timestamp."""
    now = time.time()
    last_ts = context.get("message_timestamp", now)
    latency_ms = int((now - last_ts) * 1000)
    return f"🏓 Pong! Latency: {latency_ms}ms — Online and responsive!"

def handle_status(username: str, args: str, context: CommandContext) -> str:
    """!status - Check health of major subsystems."""
    systems: list[str] = []

    # IRC connection
    systems.append("IRC:OK" if context.get("socket_connected", False) else "IRC:FAILED")

    try:
        from MedlarTV.avatar.bridge.client import is_bridge_available
        systems.append("Bridge:OK" if is_bridge_available() else "Bridge:FAILED")
    except Exception:
        systems.append("Bridge:FAILED")

    # Emotional system
    try:
        from MedlarTV.core.emotional_system import get_emotional_system

        if get_emotional_system():
            systems.append("Emotions:OK")
        else:
            systems.append("Emotions:FAILED")
    except Exception:
        systems.append("Emotions:FAILED")

    # Legacy mood defaults do not establish readable storage or viewer/session
    # readiness. This status has no scoped store-health evidence; do not open
    # stored data merely to turn a fallback dictionary into an OK verdict.
    # Legacy persistence APIs and the normal startup load remain supported.
    systems.extend(("LegacyMoodMemory:UNVERIFIED", "ViewerMemory:UNVERIFIED",
                    "SessionMemory:UNVERIFIED"))

    # Incoming moderation is not connected to dispatch or a supported Twitch
    # action transport. Importability alone is not operational readiness.
    systems.append("Moderation:NOT_INTEGRATED")

    # Translation system
    try:
        from MedlarTV.core.translation import get_supported_languages

        systems.append("Translation:OK" if get_supported_languages() else "Translation:FAILED")
    except Exception:
        systems.append("Translation:FAILED")

    # Stream management (uses app token)
    try:
        from MedlarTV.core.stream_management import get_access_token

        token = get_access_token()
        systems.append("StreamAPI:OK" if token else "StreamAPI:FAILED")
    except Exception:
        systems.append("StreamAPI:FAILED")

    status_str = ", ".join(systems)
    bot_nick = context.get("bot_nick", "MedlarTV")
    return f"⚙️ System Status: {status_str} | Connected as {bot_nick}"


_EMOJI_MAP: Dict[str, str] = {
    "happiness": "😊",
    "excitement": "🔥",
    "gratitude": "💖",
    "chill": "😌",
    "supportive": "🤗",
    "sadness": "😢",
    "anger": "😠",
    "fear": "😰",
    "snarky": "😏",
    "energetic": "⚡",
    "tired": "😴",
    "stressed": "😫",
    "lonely": "😔",
    "connected": "🤝",
    "pride": "👑",
    "jealousy": "😒",
}

def handle_emotion(username: str, args: str, context: CommandContext) -> str:
    """!emotion - Show current dominant emotion."""
    try:
        from MedlarTV.core.emotional_system import get_emotional_system

        system = get_emotional_system()
        dominant = system.get_dominant_emotion()
        value = float(system.emotions.get(dominant, 0.0))

        emoji = _EMOJI_MAP.get(dominant, "💭")
        pct = int(value * 100)
        return f"{emoji} Current emotion: {dominant.capitalize()} ({pct}%)"

    except Exception as e:  # pragma: no cover - defensive
        log.error("[Emotion] Error: %s", type(e).__name__)
        return "❌ Failed to retrieve emotional state."


def handle_emotions(username: str, args: str, context: CommandContext) -> str:
    """!emotions - Show top 3 emotions with percentages."""
    try:
        from MedlarTV.core.emotional_system import get_emotional_system

        system = get_emotional_system()
        top_3 = system.get_top_emotions(3)

        parts: list[str] = []
        for name, val in top_3.items():
            pct = int(float(val) * 100)
            parts.append(f"{name.capitalize()} {pct}%")

        if not parts:
            return "📊 No emotions available yet."

        return f"📊 Top emotions: {' | '.join(parts)}"

    except Exception as e:  # pragma: no cover - defensive
        log.error("[Emotions] Error: %s", type(e).__name__)
        return "❌ Failed to retrieve emotional state."

def handle_feelstate(username: str, args: str, context: CommandContext) -> str:
    """!feelstate - Show detailed emotional description."""
    try:
        from MedlarTV.core.emotional_system import get_emotional_system

        system = get_emotional_system()
        desc = system.get_mood_description()
        return f"🧠 Emotional State: {desc}"

    except Exception as e:  # pragma: no cover
        log.error("[Feelstate] Error: %s", type(e).__name__)
        return "❌ Failed to retrieve emotional state."

def handle_resetemotions(username: str, args: str, context: CommandContext) -> str:
    """!resetemotions - Reset all emotions to baseline (Pilot/Mod only)."""
    try:
        from MedlarTV.core.emotional_system import get_emotional_system

        system = get_emotional_system()
        system.reset_to_baseline()
        return "✅ Emotions reset to baseline. All weights returned to default values."

    except Exception as e:  # pragma: no cover
        log.error("[ResetEmotions] Error: %s", type(e).__name__)
        return "❌ Failed to reset emotions."

def handle_streaminfo(username: str, args: str, context: CommandContext) -> str:
    """!streaminfo - Show current stream information."""
    try:
        from MedlarTV.core.stream_management import get_stream_info, format_stream_info

        info = get_stream_info()
        return format_stream_info(info)

    except Exception as e:  # pragma: no cover
        log.error("[StreamInfo] Error: %s", type(e).__name__)
        return "❌ Failed to retrieve stream information."

def handle_title(username: str, args: str, context: CommandContext) -> str:
    """!title [new title] - Get or set stream title.

    - If args provided: attempt to set title (permission enforced in executor).
    - If no args: read current title from channel info.
    """
    try:
        from MedlarTV.core.stream_management import get_channel_info, update_stream_title

        if args.strip():
            # Setting title (broadcaster token is used inside stream_management)
            success = update_stream_title(args.strip())
            if success:
                return f"✅ Stream title updated to: {args.strip()}"
            return "❌ Failed to update stream title."

        # Getting title
        channel = get_channel_info()
        if channel:
            return f"📺 Current title: {channel['title']}"
        return "❌ Failed to retrieve channel info."

    except Exception as e:  # pragma: no cover
        log.error("[Title] Error: %s", type(e).__name__)
        return "❌ Failed to process title command."

def handle_game(username: str, args: str, context: CommandContext) -> str:
    """!game [new category] - Get or set stream category/game."""
    try:
        from MedlarTV.core.stream_management import get_channel_info, update_stream_category

        if args.strip():
            success = update_stream_category(args.strip())
            if success:
                return f"✅ Category updated to: {args.strip()}"
            return "❌ Failed to update category. Make sure the game name is correct."

        channel = get_channel_info()
        if channel:
            return f"🎮 Current category: {channel['game_name']}"
        return "❌ Failed to retrieve channel info."

    except Exception as e:  # pragma: no cover
        log.error("[Game] Error: %s", type(e).__name__)
        return "❌ Failed to process game command."

def handle_commands(username: str, args: str, context: CommandContext) -> str:
    # Both chat paths resolve the same YAML catalog and explicit compatibility overrides.
    from MedlarTV.commands_link import OVERRIDE_KEYS, resolve_commands_link
    url = resolve_commands_link({key: os.getenv(key) for key in OVERRIDE_KEYS})
    return f"📋 Command catalog: {url}"


def handle_tip(username: str, args: str, context: CommandContext) -> str:
    return "💖 Tip page: https://soundalerts.com/@devilmedlar"

def handle_roulette(username: str, args: str, context: CommandContext) -> str:
    roll = random.randint(1, 6)
    if roll == 1:
        return "💥 BANG!"
    return "🔫 Click… safe."

def handle_8ball(username: str, args: str, context: CommandContext) -> str:
    responses = [
        "It is certain.", "Without a doubt.", "You may rely on it.", "Yes definitely.",
        "As I see it, yes.", "Most likely.", "Outlook good.", "Yes.",
        "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
        "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
        "Don't count on it.", "My reply is no.", "My sources say no.",
        "Outlook not so good.", "Very doubtful."
    ]
    return random.choice(responses)

def handle_coinflip(username: str, args: str, context: CommandContext) -> str:
    return "Heads" if random.randint(0, 1) == 0 else "Tails"

def handle_diceroll(username: str, args: str, context: CommandContext) -> str:
    try:
        sides = int(args.strip()) if args.strip() else 6
        if sides < 2:
            sides = 6
    except Exception:
        sides = 6
    value = random.randint(1, sides)
    return f"🎲 Rolled a d{sides}: {value}"

def _get_json(url: str, headers: Dict[str, str] | None = None) -> Dict[str, Any]:
    try:
        r = requests.get(url, headers=headers or {}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def _get_text(url: str, headers: Dict[str, str] | None = None) -> str:
    try:
        r = requests.get(url, headers=headers or {}, timeout=10)
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    return ""

def handle_catfact(username: str, args: str, context: CommandContext) -> str:
    data = _get_json("https://catfact.ninja/fact")
    fact = str(data.get("fact") or "Cats are awesome.")
    return fact

def handle_dogfact(username: str, args: str, context: CommandContext) -> str:
    data = _get_json("https://dog-api.kinduff.com/api/facts")
    facts = data.get("facts") or []
    fact = str(facts[0] if facts else "Dogs are great.")
    return fact

def handle_funfact(username: str, args: str, context: CommandContext) -> str:
    data = _get_json("https://uselessfacts.jsph.pl/random.json?language=en")
    fact = str(data.get("text") or "Fun fact unavailable.")
    return fact


def handle_joke(username: str, args: str, context: CommandContext) -> str:
    data = _get_json("https://official-joke-api.appspot.com/random_joke")
    setup = str(data.get("setup") or "")
    punch = str(data.get("punchline") or "")
    if setup and punch:
        return f"{setup} — {punch}"
    data2 = _get_json("https://icanhazdadjoke.com/", headers={"Accept": "application/json"})
    joke = str(data2.get("joke") or "No joke found.")
    return joke

def handle_chucknorris(username: str, args: str, context: CommandContext) -> str:
    data = _get_json("https://api.chucknorris.io/jokes/random")
    return str(data.get("value") or "Chuck Norris approved.")

_WATCH_START: Dict[str, float] = {}

def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h} hours {m} mins"
    if m > 0:
        return f"{m} mins {sec} secs"
    return f"{sec} seconds"

def handle_watchtime(username: str, args: str, context: CommandContext) -> str:
    now = time.time()
    key = username.lower()
    start = _WATCH_START.get(key)
    if start is None:
        _WATCH_START[key] = now
        elapsed = 0.0
    else:
        elapsed = now - start
    channel = os.getenv("TWITCH_CHANNEL", "#devilmedlar").lstrip("#")
    return f"{username} has spent {_fmt_duration(elapsed)} watching {channel}"

def handle_uptime(username: str, args: str, context: CommandContext) -> str:
    try:
        from MedlarTV.core.stream_management import get_stream_info
        info = get_stream_info()
        if not info:
            return "Stream is offline"
        started_at = info.get("started_at")
        if not started_at:
            return "Uptime unavailable"
        try:
            dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            return "Uptime unavailable"
        now = datetime.now(timezone.utc)
        elapsed = (now - dt).total_seconds()
        return f"{os.getenv('TWITCH_CHANNEL', '#devilmedlar').lstrip('#')} has been streaming for {_fmt_duration(elapsed)}"
    except Exception:
        return "Uptime unavailable"

def _fmt_age(seconds: float) -> str:
    s = int(max(0, seconds))
    years = s // (365 * 24 * 3600)
    s %= 365 * 24 * 3600
    months = s // (30 * 24 * 3600)
    s %= 30 * 24 * 3600
    days = s // (24 * 3600)
    s %= 24 * 3600
    hours = s // 3600
    parts = []
    if years: parts.append(f"{years} year{'s' if years!=1 else ''}")
    if months: parts.append(f"{months} month{'s' if months!=1 else ''}")
    if days: parts.append(f"{days} day{'s' if days!=1 else ''}")
    if hours: parts.append(f"{hours} hour{'s' if hours!=1 else ''}")
    return " ".join(parts) or "0 hours"

def _calendar_anniversary(created_at: datetime, months: int) -> datetime:
    """Advance from the original date, clamping month-end anniversaries."""
    year, month_index = divmod(created_at.year * 12 + created_at.month - 1 + months, 12)
    month = month_index + 1
    day = min(created_at.day, calendar.monthrange(year, month)[1])
    return created_at.replace(year=year, month=month, day=day)

def _fmt_calendar_age(created_at: datetime, now: datetime) -> str:
    """Count completed UTC calendar months, then remaining days and hours."""
    if created_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("Account age requires timezone-aware timestamps")
    created_at = created_at.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    if created_at > now:
        raise ValueError("Account creation time is in the future")

    completed_months = (now.year - created_at.year) * 12 + now.month - created_at.month
    anniversary = _calendar_anniversary(created_at, completed_months)
    if anniversary > now:
        completed_months -= 1
        anniversary = _calendar_anniversary(created_at, completed_months)

    years, months = divmod(completed_months, 12)
    remainder = now - anniversary
    days = remainder.days
    hours = remainder.seconds // 3600
    parts = []
    if years: parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months: parts.append(f"{months} month{'s' if months != 1 else ''}")
    if days: parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours: parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    return " ".join(parts) or "0 hours"

def _twitch_headers_app() -> Dict[str, str]:
    try:
        from MedlarTV.core.stream_management import get_access_token
        tok = get_access_token() or ""
    except Exception:
        tok = ""
    client_id = os.getenv("APP_TWITCH_CLIENT_ID", "")
    return {"Client-ID": client_id, "Authorization": f"Bearer {tok}"}

def handle_accountage(username: str, args: str, context: CommandContext) -> str:
    target = (args.strip() or username).strip().lstrip("@").lower()
    headers = _twitch_headers_app()
    try:
        r = requests.get(
            "https://api.twitch.tv/helix/users",
            headers=headers,
            params={"login": target},
            timeout=10,
        )
        if r.status_code != 200:
            return f"Unable to fetch account age for {target}"
        data = (r.json().get("data") or [])
        if not data:
            return f"User not found: {target}"
        created_at = data[0].get("created_at")
        if not created_at:
            return f"Account age unavailable for {target}"
        try:
            dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = _fmt_calendar_age(dt, datetime.now(timezone.utc))
        except (TypeError, ValueError):
            return f"Account age unavailable for {target}"
        return f"{target} was created {age} ago."
    except Exception:
        return f"Unable to fetch account age for {target}"

def _fmt_calendar_followage(start: datetime, now: datetime) -> str:
    """UTC calendar anniversaries clamp month ends; retain minutes and seconds."""
    if start.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("Followage requires timezone-aware timestamps")
    start, now = start.astimezone(timezone.utc), now.astimezone(timezone.utc)
    if start > now:
        raise ValueError("Follow timestamp is in the future")
    months = (now.year - start.year) * 12 + now.month - start.month
    anniversary = _calendar_anniversary(start, months)
    if anniversary > now:
        months -= 1
        anniversary = _calendar_anniversary(start, months)
    years, months = divmod(months, 12)
    remainder = now - anniversary
    hours, seconds = divmod(remainder.seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    for count, unit in ((years, "year"), (months, "month"), (remainder.days, "day"),
                        (hours, "hour"), (minutes, "minute"), (seconds, "second")):
        if count:
            parts.append(f"{count} {unit}{'s' if count != 1 else ''}")
    return " ".join(parts) or "0 seconds"

def _followage_http_error(response) -> str | None:
    status = response.status_code
    if status == 200:
        return None
    if status in (401, 403):
        return "Followage unavailable: authorization or scope requirements were not met."
    if status == 429:
        return "Followage unavailable: rate limited; try later."
    if 500 <= status <= 599:
        return "Followage unavailable: Twitch service error; try later."
    return "Followage unavailable: HTTP request failed."


def handle_followage(username: str, args: str, context: CommandContext) -> str:
    # Followage is a calendar interval for one login, not approximate seconds.
    target = (args.strip() or username).removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,25}", target):
        return "Followage unavailable: provide one valid Twitch username."
    try:
        from MedlarTV.core.stream_management import _broadcaster_headers, CHANNEL_NAME
        headers = _broadcaster_headers()
        if not headers.get("Authorization") or not headers.get("Client-ID"):
            return "Followage unavailable: broadcaster user authorization is unavailable."
        ru = requests.get("https://api.twitch.tv/helix/users", headers=headers,
                          params={"login": target}, timeout=10)
        failure = _followage_http_error(ru)
        if failure:
            return failure  # Error bodies never become missing users/followers.
        users = ru.json()
        if not isinstance(users, dict) or not isinstance(users.get("data"), list):
            raise ValueError("Invalid user response")
        rows = users["data"]
        if not rows:
            return f"User not found: {target}"
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("Invalid user row")
        from_id = rows[0].get("id")
        if not isinstance(from_id, str) or not from_id.isdecimal():
            raise ValueError("Invalid user ID")
        from MedlarTV.core.stream_management import get_broadcaster_id
        to_id = get_broadcaster_id()
        if not isinstance(to_id, str) or not to_id.isdecimal():
            return "Followage unavailable: broadcaster identity is unavailable."
        # The existing broadcaster user provider must prove scope/identity before
        # an empty follower list can mean not following, rather than withheld data.
        validation = requests.get("https://id.twitch.tv/oauth2/validate",
                                  headers={"Authorization": headers["Authorization"]}, timeout=10)
        failure = _followage_http_error(validation)
        if failure:
            return failure
        authority = validation.json()
        if not isinstance(authority, dict):
            raise ValueError("Invalid authorization response")
        scopes = authority.get("scopes")
        if (authority.get("user_id") != to_id
                or authority.get("client_id") != headers["Client-ID"]
                or not isinstance(scopes, list)
                or "moderator:read:followers" not in scopes):
            return "Followage unavailable: broadcaster user identity or follower scope requirements were not met."
        rf = requests.get("https://api.twitch.tv/helix/channels/followers", headers=headers,
                          params={"user_id": from_id, "broadcaster_id": to_id}, timeout=10)
        failure = _followage_http_error(rf)
        if failure:
            return failure
        body = rf.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("Invalid follower response")
        data = body["data"]
        if not data:
            return f"{target} is not following {CHANNEL_NAME.lstrip('#')}"
        if len(data) != 1 or not isinstance(data[0], dict) or data[0].get("user_id") != from_id:
            raise ValueError("Invalid follower row")
        stamp = data[0].get("followed_at")
        if not isinstance(stamp, str):
            raise ValueError("Invalid follow timestamp")
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None or dt > now:
            raise ValueError("Invalid follow timestamp")
        age = _fmt_calendar_followage(dt, now)
        return f"{target} has been following {CHANNEL_NAME.lstrip('#')} for {age}"
    except requests.exceptions.SSLError:
        return "Followage unavailable: TLS transport/verification error."
    except requests.exceptions.RequestException:
        return "Followage unavailable: transport error."
    except (ValueError, TypeError, KeyError, IndexError):
        return "Followage unavailable: malformed response."
    except Exception:
        return "Followage unavailable"


_COUNTERS_FILE = Path(__file__).resolve().parents[1] / "config" / "counters.yaml"
from threading import RLock
_COUNTERS_LOCK = RLock()


def _load_counters() -> Dict[str, Any]:
    # Missing is the existing zero-counter default; unreadable/malformed is not.
    try:
        with _COUNTERS_FILE.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return {"counters": {}}
    if data is None:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("counters", {}), dict):
        raise ValueError("Invalid counter mapping")
    # Preserve the existing int-compatible value behavior, without treating bad
    # state as zero and overwriting it on the next increment/reset.
    int(data.get("counters", {}).get("death", 0))
    return data


def _save_counters(data: Dict[str, Any]) -> None:
    import tempfile
    payload = yaml.safe_dump(data).encode("utf-8")
    _COUNTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{_COUNTERS_FILE.name}.", suffix=".tmp", dir=_COUNTERS_FILE.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory replacement avoids truncating the old counter on a
        # failed write. Only this transaction's newly created temp is cleaned.
        os.replace(temporary, _COUNTERS_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def handle_death(username: str, args: str, context: CommandContext) -> str:
    try:
        # Serialize read/modify/save in this process; external editors do not
        # participate, so this is not a cross-process concurrency guarantee.
        with _COUNTERS_LOCK:
            data = _load_counters()
            c = data.setdefault("counters", {})
            c["death"] = int(c.get("death", 0)) + 1
            _save_counters(data)
            return f"Has died {c['death']} times devilmeSAD"
    except Exception as error:
        log.error("[Counter] Increment unavailable: %s", type(error).__name__)
        return "Death counter update unavailable; persistence could not be confirmed."


def handle_deathreset(username: str, args: str, context: CommandContext) -> str:
    try:
        with _COUNTERS_LOCK:
            data = _load_counters()
            c = data.setdefault("counters", {})
            c["death"] = 0
            _save_counters(data)
            return "Death Count has been set to 0 devilmeSUS"
    except Exception as error:
        log.error("[Counter] Reset unavailable: %s", type(error).__name__)
        return "Death counter reset unavailable; persistence could not be confirmed."


def handle_deathhash(username: str, args: str, context: CommandContext) -> str:
    try:
        with _COUNTERS_LOCK:
            data = _load_counters()
            val = int(data.get("counters", {}).get("death", 0))
            return f"DevilMedlar has died {val} so far! devilmeRAGE"
    except Exception as error:
        log.error("[Counter] Read unavailable: %s", type(error).__name__)
        return "Death counter unavailable."


def handle_dadjoke(username: str, args: str, context: CommandContext) -> str:
    try:
        r = requests.get("https://icanhazdadjoke.com/", headers={"Accept": "text/plain"}, timeout=10)
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    return "No dad joke found."

def handle_luck(username: str, args: str, context: CommandContext) -> str:
    pct = random.randint(0, 100)
    return f"{username} is {pct}% lucky."

def handle_discord(username: str, args: str, context: CommandContext) -> str:
    return "https://discord.gg/6VwgAwu7y"

def handle_lurk(username: str, args: str, context: CommandContext) -> str:
    return f"devilmeLURK {username} is hiding from the demons! devilmeLURK"

# =============================================================================
# COMMAND REGISTRY / PERMISSIONS
# =============================================================================

PERMISSION_EVERYONE = "everybody"
PERMISSION_PILOT = "pilot"
PERMISSION_MOD = "mod"
PERMISSION_COPILOT = "copilot"
PERMISSION_VIP = "vip"


 

# =============================================================================
# COMMAND EXECUTION ENGINE
# =============================================================================

def check_permission(username: str, role: str, required_permissions: list[str]) -> bool:
    """Return True if a user with `role` can execute a command.

    - Everyone can run commands that include PERMISSION_EVERYONE.
    - Pilot always has access to everything.
    - Otherwise, role must be in required_permissions.
    """
    if PERMISSION_EVERYONE in required_permissions:
        return True
    if role == PERMISSION_PILOT:
        return True
    return role in required_permissions

def execute_command(
    command: str,
    username: str,
    role: str,
    args: str,
    context: CommandContext,
) -> Optional[str]:
    """Execute a command and return a response string.

    Returns None if the command does not exist.
    """
    # An unregistered command has no handler.
    if command not in COMMAND_REGISTRY:
        return None
    log.debug(
        "[DEBUG] Command recognized: !%s | User=%s | Role=%s | arg_chars=%d",
        command, username, role, len(args),
    )

    cmd_info = COMMAND_REGISTRY[command]

    # A room's role tags cannot authorize effects on the configured broadcaster.
    # Keep this gate in dispatch as well as listener-inline co-pilot management.
    effect = cmd_info.get("primary_effect")
    if effect == "always" or (effect == "with_args" and args.strip()):
        from MedlarTV.command_authority import CommandAuthority
        authority = context.get("command_authority") if isinstance(context, dict) else None
        if not isinstance(authority, CommandAuthority) or not authority.permits_primary_write(username, role):
            return f"@{username} This command requires verified primary-channel identity."

    # Permission gate
    if not check_permission(username, role, cmd_info.get("permissions", [])):
        role_names = {
            "pilot": "Broadcaster",
            "mod": "Moderator",
            "copilot": "Co-Pilot",
            "vip": "VIP",
            "user": "Viewer",
        }
        allowed_names = [role_names.get(p, p) for p in cmd_info.get("permissions", [])]
        required = ", ".join(allowed_names)
        return f"@{username} ❌ Permission denied. This command requires: {required}"

    # Extra protection for mutating stream commands: title/game SET requires pilot/copilot
    if command in {"title", "game"} and args.strip():
        if role not in {PERMISSION_PILOT, PERMISSION_COPILOT}:
            return f"@{username} ❌ Only Broadcaster and Co-Pilots can change the {command}."

    try:
        handler: CommandHandler = cmd_info["handler"]
        log.debug(f"[DEBUG] Executing handler for !{command}: {handler.__name__}")
        log.debug("[DEBUG] Command context available=%s", bool(context))

        response = handler(username, args, context)

        log.debug("[DEBUG] Handler returned text=%s", bool(response))
        log.info(f"[Command] {username} executed !{command}")
        try:
            log_command(username, f"!{command}", success=True)
        except Exception:
            pass

        return response

    except Exception as e:  # pragma: no cover
        log.error("[Command] Error executing !%s: %s", command, type(e).__name__)
        log.debug("[DEBUG] Exception occurred inside command handler")

        try:
            log_command(username, f"!{command}", success=False, error=str(e))
        except Exception:
            pass
        return f"❌ Command failed: {command}"


def get_command_help(command: str | None = None) -> str:
    """Return help text for a specific command or a categorized list."""
    if command:
        if command == "forget":
            return "!forget <fact> removes one of your saved facts; !forget all clears all saved facts. Bare !forget shows usage."
        if command == "forgetme":
            return "!forgetme clears your saved facts and this chat's memory."
        if command in COMMAND_REGISTRY:
            info = COMMAND_REGISTRY[command]
            text = f"!{command} - {info['description']}"
            usage = info.get("usage")
            if usage:
                text += f"\nUsage: {usage}"
            return text
        return f"Command !{command} not found."

    categories: Dict[str, list[str]] = {
        "System": ["ping", "status"],
        "Emotions": ["emotion", "emotions", "feelstate", "resetemotions"],
        "Stream": ["streaminfo", "title", "game"],
    }

    lines: list[str] = ["Common commands:"]
    for cat, cmds in categories.items():
        names = [f"!{name}" for name in cmds if name in COMMAND_REGISTRY]
        lines.append(f"{cat}: {', '.join(names)}")

    lines.append("Memory: !forget <fact>, !forgetme")
    lines.append("Use !help <command> for details; !commands for the public catalog.")

    return " | ".join(lines)


def handle_help(username: str, args: str, context: CommandContext) -> str:
    parts = args.strip().split(maxsplit=1)
    return get_command_help(parts[0].lstrip("!").lower() if parts else None)


"""Quick template for adding new commands:

1. Define a handler:

   def handle_mycommand(username: str, args: str, context: CommandContext) -> str:
       
       return "Response message"

2. Register it in COMMAND_REGISTRY:

   COMMAND_REGISTRY["mycommand"] = {
       "handler": handle_mycommand,
       "permissions": [PERMISSION_EVERYONE],  # or [PERMISSION_PILOT, PERMISSION_MOD]
       "description": "Does something cool",
       "usage": "!mycommand [optional args]",  # optional
   }

That's it. The unified handler will start routing !mycommand automatically.
"""
def handle_time(username: str, args: str, context: CommandContext) -> str:
    q = (args or "").strip()
    try:
        if not q:
            from MedlarTV.core.time_lookup import get_default_local_time
            res = get_default_local_time()
            return res or "Unable to get local time"
        res = get_times_for_location(q)
    except TimeLookupUnavailable:
        return "Time lookup unavailable" if q else "Unable to get local time"
    if not res:
        return f"Location not found: {q}"
    return res

def handle_worldclock(username: str, args: str, context: CommandContext) -> str:
    summary = world_clock_summary()
    return summary

def handle_timezones(username: str, args: str, context: CommandContext) -> str:
    f = (args or "").strip() or None
    try:
        zones = list_timezones(f)
    except TimeLookupUnavailable:
        return "Timezone list unavailable"
    if not zones:
        return "No timezones found"
    count = len(zones)
    sample = ", ".join(zones[:12])
    if f:
        return f"{count} zones match '{f}': {sample}"
    return f"{count} zones available: {sample}"

def handle_search(username: str, args: str, context: CommandContext) -> str:
    q = (args or "").strip()
    if not q:
        return "Usage: !search <query>"
    try:
        results = search_web(q, max_results=2)
    except Exception:
        results = []
    if not results:
        return f"No results for: {q}"
    parts = []
    for r in results[:2]:
        t = r.get("title", "N/A")
        u = r.get("href", "")
        if u:
            parts.append(f"{t} — {u}")
        else:
            parts.append(t)
    return " | ".join(parts)

def handle_wiki(username: str, args: str, context: CommandContext) -> str:
    q = (args or "").strip()
    if not q:
        return "Usage: !wiki <topic>"
    try:
        res = search_wikipedia(q, sentences=2)
    except Exception:
        res = None
    if not res:
        return f"No Wikipedia page found for: {q}"
    title = res.get("title", q)
    summary = res.get("summary", "")
    url = res.get("url", "")
    msg = f"{title}: {summary}"
    if len(msg) > 280:
        msg = msg[:277] + "..."
    if url:
        return f"{msg} | {url}"
    return msg

def handle_t(username: str, args: str, context: CommandContext) -> str:
    return handle_t_command(args, username)

def handle_tlang(username: str, args: str, context: CommandContext) -> str:
    return handle_tlang_command()

def handle_thelp(username: str, args: str, context: CommandContext) -> str:
    return TRANSLATE_HELP

COMMAND_REGISTRY: Dict[str, Dict[str, Any]] = {
    "ping": {
        "handler": handle_ping,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Check bot latency and responsiveness",
    },
    "status": {
        "handler": handle_status,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Check status of all bot systems",
    },
    "help": {
        "handler": handle_help,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show command guidance",
        "usage": "!help [command]",
    },
    "emotion": {
        "handler": handle_emotion,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show MedlarTV's current dominant emotion",
    },
    "emotions": {
        "handler": handle_emotions,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show top 3 emotions with percentages",
    },
    "feelstate": {
        "handler": handle_feelstate,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show detailed emotional state description",
    },
    "time": {
        "handler": handle_time,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Get current time for a location/country",
        "usage": "!time <location>",
    },
    "search": {
        "handler": handle_search,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Web search via DuckDuckGo",
        "usage": "!search <query>",
    },
    "wiki": {
        "handler": handle_wiki,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Wikipedia lookup",
        "usage": "!wiki <topic>",
    },
    "worldclock": {
        "handler": handle_worldclock,
        "permissions": [PERMISSION_EVERYONE],
        "description": "World clock summary across major cities",
    },
    "timezones": {
        "handler": handle_timezones,
        "permissions": [PERMISSION_EVERYONE],
        "description": "List available timezones (optional filter)",
        "usage": "!timezones [filter]",
    },
    "t": {
        "handler": handle_t,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Translate text to another language",
        "usage": TRANSLATE_HELP,
    },
    "trans": {
        "handler": handle_t,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Translate text to another language",
        "usage": TRANSLATE_HELP,
    },
    "translate": {
        "handler": handle_t,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Translate text to another language",
        "usage": TRANSLATE_HELP,
    },
    "tlang": {
        "handler": handle_tlang,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show supported translation languages",
    },
    "thelp": {
        "handler": handle_thelp,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show translation command usage",
    },
    "resetemotions": {
        "handler": handle_resetemotions,
        "primary_effect": "always",
        "permissions": [PERMISSION_PILOT, PERMISSION_MOD],
        "description": "Reset all emotions to baseline (Pilot/Mod only)",
    },
    "streaminfo": {
        "handler": handle_streaminfo,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Get current stream information (title, game, viewers, uptime)",
    },
    "title": {
        "handler": handle_title,
        "primary_effect": "with_args",
        "permissions": [PERMISSION_EVERYONE],
        "description": "Get or set stream title",
        "usage": "!title [new title] - Leave blank to view, provide title to set",
    },
    "game": {
        "handler": handle_game,
        "primary_effect": "with_args",
        "permissions": [PERMISSION_EVERYONE],
        "description": "Get or set stream category/game",
        "usage": "!game [new game] - Leave blank to view, provide game name to set",
    },
    "commands": {
        "handler": handle_commands,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show all available commands",
    },
    "tip": {
        "handler": handle_tip,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Link to tip page",
        "usage": "!tip",
    },
    "roulette": {
        "handler": handle_roulette,
        "primary_effect": "always",
        "permissions": [PERMISSION_EVERYONE],
        "description": "Play Russian Roulette",
        "usage": "!roulette",
    },
    "8ball": {
        "handler": handle_8ball,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Magic 8 Ball response",
        "usage": "!8ball <question>",
    },
    "coinflip": {
        "handler": handle_coinflip,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Flip a coin",
        "usage": "!coinflip",
    },
    "diceroll": {
        "handler": handle_diceroll,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Roll a die",
        "usage": "!diceroll [sides]",
    },
    "catfact": {
        "handler": handle_catfact,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Random cat fact",
    },
    "dogfact": {
        "handler": handle_dogfact,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Random dog fact",
    },
    "funfact": {
        "handler": handle_funfact,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Random fun fact",
    },
    "joke": {
        "handler": handle_joke,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Random joke",
    },
    "chucknorris": {
        "handler": handle_chucknorris,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Random Chuck Norris joke",
    },
    "watchtime": {
        "handler": handle_watchtime,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show viewer watch time (session)",
    },
    "uptime": {
        "handler": handle_uptime,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show current stream uptime",
    },
    "accountage": {
        "handler": handle_accountage,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show a user's account age",
        "usage": "!accountage [username]",
    },
    "followage": {
        "handler": handle_followage,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show how long a user has followed the channel",
        "usage": "!followage [username]",
    },
    "death": {
        "handler": handle_death,
        "primary_effect": "always",
        "permissions": [PERMISSION_EVERYONE],
        "description": "Increment death counter",
    },
    "deathreset": {
        "handler": handle_deathreset,
        "primary_effect": "always",
        "permissions": [PERMISSION_PILOT],
        "description": "Reset death counter",
    },
    "death#": {
        "handler": handle_deathhash,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show deaths so far",
    },
    "dadjoke": {
        "handler": handle_dadjoke,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Random dad joke",
    },
    "luck": {
        "handler": handle_luck,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Show luck percentage",
    },
    "discord": {
        "handler": handle_discord,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Discord invite link",
    },
    "lurk": {
        "handler": handle_lurk,
        "permissions": [PERMISSION_EVERYONE],
        "description": "Announce lurk message",
    },
}
