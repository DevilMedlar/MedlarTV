# Refactored twitch_listener.py
# Clean, stable, updated for new command handlers & OAuth-safe stream management

import os
import socket
import ssl
import time
import threading
import logging
from typing import Dict, Any, Optional, List
from pathlib import Path
import yaml
import winsound

from MedlarTV.core.fuzzy_trigger import should_respond
from MedlarTV.core.command_handlers import execute_command
from MedlarTV.core.moderation import check_message, execute_timeout, execute_ban, execute_delete
from MedlarTV.core.twitch_events import (
    detect_raid, detect_subscription, detect_channel_point_redemption, detect_bits,
)
from MedlarTV.core.llm_brain import generate_response
from MedlarTV.core.interaction_logger import log_interaction
from MedlarTV.core.time_lookup import should_lookup_time, get_times_for_location, get_default_local_time
from MedlarTV.core.settings import is_enabled, get_memory_control

log = logging.getLogger("twitch_listener")

# ----------------------------------------------------------------------------
# IRC Connection Setup
# ----------------------------------------------------------------------------

TWITCH_SERVER = "irc.chat.twitch.tv"
TWITCH_PORT = 6697

# IMPORTANT:
# IRC login MUST use the BOT token, not broadcaster token.
OAUTH_TOKEN = os.getenv("MEDLARTV_TWITCH_TOKEN", "")

# Bot nickname
BOT_NICK = os.getenv("TWITCH_NICK", "MedlarTV")

# Twitch channel
CHANNEL = os.getenv("TWITCH_CHANNEL", "#devilmedlar")

# Clean oauth format
if OAUTH_TOKEN.startswith("oauth:"):
    IRC_TOKEN = OAUTH_TOKEN
else:
    IRC_TOKEN = f"oauth:{OAUTH_TOKEN}"

# ----------------------------------------------------------------------------
# Co-Pilot management
# ----------------------------------------------------------------------------

_COPILOTS_FILE = Path(__file__).resolve().parents[1] / "config" / "copilots.yaml"

def _ensure_copilots_file() -> None:
    if not _COPILOTS_FILE.exists():
        data = {"copilots": {"active": []}}
        _COPILOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _COPILOTS_FILE.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)

def _load_copilots() -> Dict[str, Any]:
    # Authorization reads must never create configuration for a denied command.
    try:
        with _COPILOTS_FILE.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data
    except Exception:
        return {"copilots": {"active": []}}

def _save_copilots(data: Dict[str, Any]) -> None:
    _COPILOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _COPILOTS_FILE.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)

def _get_active_copilots() -> List[str]:
    from MedlarTV.command_authority import normalize_login
    data = _load_copilots()
    active = data.get("copilots", {}).get("active", [])
    if not isinstance(active, list):
        return []
    return [name for value in active
            if (name := normalize_login(value, mention=True))]

def _role_prefix(username: str) -> str:
    try:
        channel_name = CHANNEL.lstrip("#").lower()
        if username.lower() == channel_name:
            return "Pilot "
        if username.lower() in [u.lower() for u in _get_active_copilots()]:
            return f"Co-Pilot @{username} "
        return f"@{username} "
    except Exception:
        return f"@{username} "

def _username_for_model(username: str) -> str:
    try:
        channel_name = CHANNEL.lstrip("#").lower()
        if username.lower() == channel_name:
            return "Pilot"
        return username
    except Exception:
        return username

def _sanitize_reply_text(text: str, username: str) -> str:
    try:
        t = str(text)
        # normalize spacing and zero-widths
        t = t.replace("\u00a0", " ").replace("\u200b", "").replace("\u2009", " ")
        t = t.lstrip()
        import re
        # Remove leading speaker label
        t = re.sub(r"^(medlartv|medlar)\s*:\s*", "", t, flags=re.I)
        # Remove leading duplicates of the addressed username
        uname = re.escape(username)
        patterns = [
            # Direct leading name or @name with punctuation/space
            rf"^\s*@?\s*{uname}\b[^A-Za-z0-9]*\s*",
            # Greeting followed by name (allow emojis/punct between)
            rf"^\s*(hey|hi|hello|yo|hiya|hey there|sup|what's up|whats up)\b[^A-Za-z0-9@]*@?\s*{uname}\b[^A-Za-z0-9]*\s*",
        ]
        for p in patterns:
            t = re.sub(p, "", t, flags=re.I)
        return t.lstrip()
    except Exception:
        return text

def _is_pilot(username: str) -> bool:
    try:
        return username.lower() == CHANNEL.lstrip("#").lower()
    except Exception:
        return False

def _prefix_for_llm_reply(username: str) -> str:
    try:
        if _is_pilot(username):
            return ""
        return _role_prefix(username)
    except Exception:
        return _role_prefix(username)

_TIMERS_FILE = Path(__file__).resolve().parents[1] / "config" / "timers.yaml"

def _load_timers_yaml() -> List[Dict[str, Any]]:
    try:
        with _TIMERS_FILE.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return list(data.get("timers", []))
    except Exception:
        return []

def _get_commands_link() -> str:
    # Both chat paths resolve the same YAML catalog and explicit compatibility overrides.
    from MedlarTV.commands_link import OVERRIDE_KEYS, resolve_commands_link
    url = resolve_commands_link({key: os.getenv(key) for key in OVERRIDE_KEYS})
    return url


def _prepare_msg(text: str) -> str:
    try:
        return " ".join(text.splitlines())
    except Exception:
        return text

def _add_copilot(username: str) -> bool:
    from MedlarTV.command_authority import normalize_login
    username = normalize_login(username, mention=True)
    if not username:
        return False
    data = _load_copilots()
    active = data.setdefault("copilots", {}).setdefault("active", [])
    if not isinstance(active, list):
        return False
    uname = username
    if uname not in [normalize_login(u, mention=True) for u in active]:
        active.append(uname)
        # Persist active list only; write event to logs
        _save_copilots({"copilots": {"active": active}})
        log.info("[Copilot] add user=%s ts=%f", uname, time.time())
        return True
    return False

def _remove_copilot(username: str) -> bool:
    from MedlarTV.command_authority import normalize_login
    username = normalize_login(username, mention=True)
    if not username:
        return False
    data = _load_copilots()
    active = data.setdefault("copilots", {}).setdefault("active", [])
    if not isinstance(active, list):
        return False
    remaining = [u for u in active if normalize_login(u, mention=True) != username]
    if len(remaining) != len(active):
        # Revoke every equivalent entry, including legacy case/mention duplicates.
        # Persist active list only; write event to logs
        _save_copilots({"copilots": {"active": remaining}})
        log.info("[Copilot] remove user=%s ts=%f", username, time.time())
        return True
    return False

# ----------------------------------------------------------------------------
# Firebot-like Events & Effects
# ----------------------------------------------------------------------------

_seen_users: set[str] = set()

def _load_event_rules() -> list[dict[str, Any]]:
    cfg = Path(__file__).resolve().parents[1] / "config" / "events.yaml"
    try:
        with cfg.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return list(data.get("events", []))
    except Exception:
        return []

def _is_ignored(username: str) -> bool:
    try:
        cfg = Path(__file__).resolve().parents[1] / "config" / "events.yaml"
        with cfg.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        ignore = set(str(u).lower() for u in (data.get("ignored_users") or []))
        return username.lower() in ignore
    except Exception:
        return False

def _play_local_sound(path: str, duration_ms: int = 0) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    # simple cooldown per file to avoid echo/retrigger
    try:
        with _audio_lock:
            last = _last_audio_play.get(str(p), 0.0)
            now = time.time()
            delta = now - last
            if delta < 1.0:
                try:
                    log.info(f"[SoundEffect] cooldown skip path={str(p)} delta={delta:.2f}s")
                except Exception:
                    pass
                return False
            _last_audio_play[str(p)] = now
            try:
                log.info(f"[SoundEffect] cooldown pass path={str(p)}")
            except Exception:
                pass
    except Exception:
        pass

    if p.suffix.lower() == ".wav":
        try:
            try:
                log.info(f"[SoundEffect] playing wav path={str(p)}")
            except Exception:
                pass
            winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return True
        except Exception:
            try:
                log.error(f"[SoundEffect] wav playback failed path={str(p)}")
            except Exception:
                pass
            return False
    if p.suffix.lower() in {".mp3", ".m4a", ".aac"}:
        try:
            from MedlarTV.audio_playback import launch_media_player
            sleep_ms = int(duration_ms) if duration_ms and duration_ms > 0 else 10000
            # A fixed player consumes the absolute filename as data, never code.
            launched = launch_media_player(str(p.resolve()), sleep_ms)
            if not launched:
                log.error("[SoundEffect] MediaPlayer launch failed")
            return launched
        except Exception:
            log.error("[SoundEffect] MediaPlayer launch failed")
            return False
    # Unknown file types are not playback requests; never execute a file association.
    log.warning("[SoundEffect] unsupported audio type")
    return False


def _apply_event_rules(event_name: str, username: str, send_message: callable, sock) -> None:
    if _is_ignored(username):
        try:
            log.info(f"[Events] ignored user={username} for trigger={event_name}")
        except Exception:
            pass
        return
    rules = _load_event_rules()

    # Exclusive handling: if any rule explicitly targets this username, only run those
    try:
        exact_rules = []
        for rr in rules:
            if str(rr.get("trigger", "")) != event_name:
                continue
            fl = rr.get("filters", {}) or {}
            eq = str(fl.get("username_equals", ""))
            if eq and eq.lower() == username.lower():
                exact_rules.append(rr)
        if exact_rules:
            rules_to_run = exact_rules
        else:
            rules_to_run = [r for r in rules if str(r.get("trigger", "")) == event_name]
    except Exception:
        rules_to_run = [r for r in rules if str(r.get("trigger", "")) == event_name]

    for r in rules_to_run:
        if str(r.get("trigger", "")) != event_name:
            continue
        flt = r.get("filters", {}) or {}
        uname_eq = str(flt.get("username_equals", ""))
        if uname_eq and uname_eq.lower() != username.lower():
            continue
        try:
            exclude_list = flt.get("username_not_in", []) or []
            exclude_norm = set(str(u).lower().lstrip("@") for u in exclude_list)
            if username.lower().lstrip("@") in exclude_norm:
                continue
        except Exception:
            pass
        effects = r.get("effects", []) or []
        try:
            log.info(f"[Events] trigger={event_name} user={username} matched rule={r.get('name','')} effects={len(effects)}")
        except Exception:
            pass
        for eff in effects:
            try:
                delay_ms = int(eff.get("delay_ms", 0))
            except Exception:
                delay_ms = 0
            if delay_ms > 0:
                try:
                    time.sleep(delay_ms / 1000.0)
                except Exception:
                    pass
            t = str(eff.get("type", ""))
            if t == "chat":
                msg = str(eff.get("message", ""))
                if msg:
                    try:
                        formatted = msg
                        formatted = formatted.replace("@{username}", f"@{username}")
                        formatted = formatted.replace("{username}", username)
                    except Exception:
                        formatted = msg
                    send_message(formatted)
            elif t == "twitch_shoutout":
                target = str(eff.get("target", username))
                try:
                    from MedlarTV.core.stream_management import send_shoutout
                    ok = send_shoutout(target)
                except Exception:
                    ok = False
                if not ok:
                    from MedlarTV.core.moderation import execute_shoutout
                    try:
                        execute_shoutout(sock, CHANNEL, target)
                    except Exception:
                        pass
                try:
                    log.info(f"[Events] shoutout effect target={target} api_ok={ok}")
                except Exception:
                    pass
            elif t == "play_sound":
                path = str(eff.get("path", ""))
                if path:
                    try:
                        duration_ms = int(eff.get("duration_ms", 0))
                    except Exception:
                        duration_ms = 0
                    _play_local_sound(path, duration_ms)

# ----------------------------------------------------------------------------
# IRC CLIENT CLASS
# ----------------------------------------------------------------------------

class TwitchIRC:
    """One restartable Twitch IRC session with generation-safe background work."""

    WORK_QUEUE_SIZE = 64
    OUTBOUND_QUEUE_SIZE = 128
    CHAT_MIN_INTERVAL = 8.0
    CHAT_MAX_QUEUE_AGE = 30.0
    WORKER_COUNT = 1
    CONNECT_TIMEOUT = 10.0
    HANDSHAKE_TIMEOUT = 15.0
    SOCKET_POLL_TIMEOUT = 0.5
    RECONNECT_INITIAL_DELAY = 1.0
    RECONNECT_MAX_DELAY = 30.0
    RECONNECT_RESET_AFTER = 30.0
    MAX_CHAT_CHARACTERS = 500
    # Conservative untagged RFC1459 frame budget, including command and CRLF.
    MAX_IRC_FRAME_BYTES = 512
    # Incoming IRCv3 tags can exceed the outgoing chat budget. Bound each line.
    MAX_INCOMING_LINE_BYTES = 65536
    _OPERATIONAL_COMMANDS = frozenset({"!pokecatch ultraball", "!pokeshop ultraball 1"})
    _CHAT_REJECTION_REASONS = {
        "msg_banned": "banned",
        "msg_bad_characters": "unprocessable_characters",
        "msg_channel_blocked": "account_restricted",
        "msg_channel_suspended": "channel_unavailable",
        "msg_duplicate": "duplicate",
        "msg_emoteonly": "emote_only",
        "msg_followersonly": "followers_only",
        "msg_followersonly_followed": "follower_duration",
        "msg_followersonly_zero": "followers_only",
        "msg_ratelimit": "rate_limit",
        "msg_rejected": "moderation_pending",
        "msg_rejected_mandatory": "moderation_rejected",
        "msg_requires_verified_phone_number": "phone_verification_required",
        "msg_room_not_found": "channel_unavailable",
        "msg_slowmode": "slow_mode",
        "msg_subsonly": "subscribers_only",
        "msg_suspended": "permission_denied",
        "msg_timedout": "timed_out",
        "msg_verified_email": "email_verification_required",
    }

    class AuthenticationError(ConnectionError):
        pass

    class JoinError(ConnectionError):
        pass

    class Cancelled(ConnectionError):
        pass

    class _GenerationSocket:
        """Socket-shaped adapter that keeps worker-side IRC writes serialized."""

        def __init__(self, owner, generation: int):
            self._owner = owner
            self._generation = generation

        def send(self, payload: bytes) -> int:
            text = payload.decode("utf-8").removesuffix("\r\n")
            if not self._owner._queue_outbound(text, self._generation):
                raise OSError("IRC transport is no longer active")
            return len(payload)

        def sendall(self, payload: bytes) -> None:
            self.send(payload)

    def __init__(self):
        self.sock: Optional[socket.socket] = None
        self.connected: bool = False
        self.stop_flag: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._chat_schedule_lock = threading.Lock()
        self._chat_clock = self._monotonic
        self._chat_wait = self._stop_event.wait
        self._last_chat_write_at = None
        self._worker_start_lock = threading.Lock()
        self._memory_lock = threading.RLock()
        self._memory_owner = object()
        self._memory_control_state = None
        self._memory_revision = 0
        self._generation: int = 0
        self._incoming_framer = None
        self._pending_lines: List[str] = []
        self._work_local = threading.local()
        self._work_queue = None
        self._outbound_queue = None
        self._worker_threads: List[threading.Thread] = []
        self._sender_thread: Optional[threading.Thread] = None
        self._workers_started: bool = False
        self._stopping: bool = False
        self.available_emotes: List[str] = []
        self.user_emotes: List[str] = []
        self._timers: List[Dict[str, Any]] = []
        self._chat_count: int = 0
        self._next_chat_due: Dict[str, int] = {}
        self._next_time_due: Dict[str, float] = {}
        self._timer_stop: threading.Event = threading.Event()
        self._timer_thread: Optional[threading.Thread] = None

    def _conversation_store(self):
        store = getattr(self, "_scoped_conversation", None)
        if store is None:
            from MedlarTV.core.conversation_context import ConversationContextStore
            store = ConversationContextStore()
            self._scoped_conversation = store
        return store

    def _profile_store(self):
        store = getattr(self, "_viewer_profiles", None)
        if store is None:
            from MedlarTV.core.health_conditions import HealthConditionStore
            store = HealthConditionStore()
            self._viewer_profiles = store
        return store

    def _load_memory_control(self):
        return get_memory_control()

    def _refresh_memory_control(self):
        """Observe validated state and invalidate old work on every control revision."""
        with self._memory_lock:
            try:
                snapshot = self._load_memory_control()
                if type(snapshot.enabled) is not bool or not isinstance(snapshot.revision, str):
                    raise ValueError("Invalid memory control")
                state = (snapshot.enabled, snapshot.revision)
            except Exception:
                state = (False, "unavailable")
            if state != self._memory_control_state:
                if self._memory_control_state is not None or not state[0]:
                    self._reset_memory_session()
                self._memory_control_state = state
            return state[0], self._memory_revision

    def _reset_memory_session(self):
        with self._memory_lock:
            self._memory_revision += 1
            store = getattr(self, "_scoped_conversation", None)
            if store is not None:
                store.clear_all()

    def _invalidate_connection_memory(self):
        # Connection loss invalidates replies, not valid user source evidence.
        with self._memory_lock:
            self._memory_revision += 1

    def _make_reply_receipt(self, conversation, observation, channel, viewer,
                            profile_channel, profile_viewer):
        from MedlarTV.core.memory_receipts import ReplyReceipt
        lease = observation.lease if observation is not None else conversation.lease_for(channel, viewer)
        return ReplyReceipt(self._memory_owner, self._origin_generation(), self._memory_revision,
                            lease, observation, profile_channel, profile_viewer)

    def _memory_receipt_current(self, receipt):
        with self._memory_lock:
            enabled, revision = self._refresh_memory_control()
            if not enabled or receipt.owner is not self._memory_owner \
                    or receipt.control_revision != revision \
                    or not self._generation_is_connected(receipt.generation):
                return False
            try:
                if self._profile_store().is_excluded(receipt.profile_channel, receipt.profile_viewer):
                    self._conversation_store().forget_viewer(receipt.lease.channel, receipt.lease.viewer)
                    return False
                return self._conversation_store().receipt_is_current(receipt.lease, receipt.observation)
            except Exception as exc:
                log.warning("[MEMORY] Receipt validation unavailable: %s", type(exc).__name__)
                return False

    def _record_written(self, written):
        """A write is not delivery; only this event can complete application history."""
        with self._memory_lock:
            if written.generation != written.receipt.generation or not self._memory_receipt_current(written.receipt):
                return
            if written.receipt.observation is not None:
                self._conversation_store().complete_written(written.receipt.observation, written.body)

    def _send_memory_reply(self, text, receipt):
        # Preserve send_message's public boolean/positional API. A worker-local
        # scope attaches memory identity only to this one reply, never timers.
        previous = getattr(self._work_local, "memory_receipt", None)
        self._work_local.memory_receipt = receipt
        try:
            if receipt is not None and not self._memory_receipt_current(receipt):
                return False
            return self.send_message(text)
        finally:
            self._work_local.memory_receipt = previous

    @staticmethod
    def _use_twitch_handle(reply: str, username: str, real_name: str | None) -> str:
        """Do not let a model turn a saved real name into a form of address."""
        if not reply or not real_name:
            return reply
        import re
        # Single given names can also be ordinary words (for example, May).
        # Remove them only where the model is addressing the viewer.
        names = "|".join(re.escape(name) for name in sorted(
            {real_name, *real_name.split()}, key=len, reverse=True,
        ))
        months = {
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        }
        calendar_names = "|".join(re.escape(name) for name in real_name.split()
                                  if name.casefold() in months)
        calendar_opening = re.match(
            rf"^\s*(?P<month>{calendar_names})\s*[,;:!?.]\s*"
            r"(?:\d{1,4}(?:st|nd|rd|th)?\b|['\u2019]\d{2}\b|the\s+month\b)",
            reply, re.I,
        ) if calendar_names else None
        def remove_greeting_name(match):
            # A greeting can be followed by the modal "May I/we/...".
            # Its grammar matters more than a saved name with the same spelling.
            if not match.group("tag") and match.group("name").casefold() == "may" \
                    and re.match(
                        r"\s+(?:i|we|you|he|she|they|it|the|a|an|"
                        r"this|that|our|your|my)\b", reply[match.end():], re.I,
                    ):
                return match.group(0)
            return match.group("prefix").rstrip(" ,!:")

        reply = re.sub(
            rf"^(?P<prefix>\s*(?:hi|hello|hey|yo|hiya|sup|thanks|thank you|"
            rf"welcome(?: back)?|(?:good|great|nice|awesome|excellent) "
            rf"(?:job|work|play|call|catch|luck)|well done|"
            rf"well played|see you|(?:great|good|glad|nice) to see you|"
            rf"how are you|what's up|whats up)\b[\s,!:]+)"
            rf"(?P<tag>@?)(?P<name>{names})"
            rf"(?=\s|[,!?.:]|$)",
            remove_greeting_name,
            reply, flags=re.I,
        )
        reply = re.sub(
            rf"^\s*@?(?:{names})\s*[,!?.:]\s*",
            lambda match: match.group(0) if calendar_opening else "",
            reply, flags=re.I,
        )
        reply = re.sub(
            rf",\s*@?(?:{names})(?=\s*[,!?.:]|\s*$)",
            "", reply, flags=re.I,
        )
        # A name in the middle of a clause can still address the viewer.
        # Preserve month and subject uses such as "Meet us in May, then play".
        not_address = {
            "in", "on", "at", "during", "until", "since", "by", "for",
            "from", "of", "about", "named", "called", "is", "was",
            "with", "to", "me", "like", "saw", "met",
        }

        def replace_mid_reply_name(match):
            if calendar_opening and match.start() == calendar_opening.start("month"):
                return match.group(0)
            prefix = reply[:match.start()].rstrip()
            previous = re.search(r"[\w']+$", prefix)
            if previous and previous.group().casefold() in not_address:
                return match.group(0)
            return f"@{username}"

        reply = re.sub(
            rf"(?<![\w@])@?(?:{names})(?=\s*[,;]\s+\S)",
            replace_mid_reply_name, reply, flags=re.I,
        )
        # A final name after praise or a sentence is usually a vocative.
        # Preserve date and subject uses such as "Meet us in May."
        trailing = re.search(
            rf"(?<!\w)@?(?:{names})(?P<punct>[.!?]*)\s*$",
            reply, flags=re.I,
        )
        if trailing:
            prefix = reply[:trailing.start()].rstrip()
            previous = re.search(r"[\w']+$", prefix)
            if prefix and previous and previous.group().casefold() not in not_address:
                reply = prefix + trailing.group("punct")
        # A multiword full name is distinctive enough to scrub elsewhere in
        # a generated response without rewriting ordinary single words.
        if len(real_name.split()) > 1:
            reply = re.sub(
                r"(?<![\w@])" + re.escape(real_name) + r"(?!\w)",
                f"@{username}", reply, flags=re.I,
            )
        # Generated prose has many possible forms of address. The earlier
        # patterns handled clear vocatives; leave ordinary month words intact.
        # Direct self-lookups are handled separately before this path.
        for name in sorted(set(real_name.split()), key=len, reverse=True):
            def replace_remaining_name(match):
                if name.casefold() in months and not match.group(0).startswith("@"):
                    return match.group(0)
                return f"@{username}"

            reply = re.sub(
                r"(?<![\w@])@?" + re.escape(name) + r"(?!\w)",
                replace_remaining_name, reply, flags=re.I,
            )
        return reply

    @staticmethod
    def _memory_identity(username: str, ctx: Dict[str, Any]):
        source_channel = str(ctx.get("channel") or CHANNEL).lower()
        # The channel name stays stable for this process even when an IRC
        # message lacks room-id. Durable profiles use room-id separately.
        channel_key = source_channel
        stable_viewer = str(ctx.get("user_id") or "").strip()
        viewer_key = stable_viewer or "login:" + username.lower()
        return source_channel, channel_key, stable_viewer, viewer_key

    @staticmethod
    def _monotonic() -> float:
        clock = getattr(time, "monotonic", None) or time.time
        return float(clock())

    def _origin_generation(self) -> int:
        generation = getattr(self._work_local, "generation", None)
        if generation is not None:
            return int(generation)
        with self._state_lock:
            return self._generation

    def _generation_is_current(self, generation: int) -> bool:
        with self._state_lock:
            return generation == self._generation

    def _generation_is_connected(self, generation: int) -> bool:
        with self._state_lock:
            return (
                generation == self._generation
                and self.connected
                and not self._stopping
                and not self._stop_event.is_set()
            )

    @staticmethod
    def _irc_command(line: str) -> str:
        rest = line
        if rest.startswith("@") and " " in rest:
            rest = rest.split(" ", 1)[1]
        parts = rest.split()
        if not parts:
            return ""
        if parts[0].startswith(":") and len(parts) > 1:
            return parts[1].upper()
        return parts[0].upper()

    @staticmethod
    def _is_authentication_welcome(line: str) -> bool:
        rest = line
        if rest.startswith("@") and " " in rest:
            rest = rest.split(" ", 1)[1]
        parts = rest.split()
        try:
            command_index = 1 if parts[0].startswith(":") else 0
            return (
                parts[command_index] == "001"
                and parts[command_index + 1].casefold() == BOT_NICK.casefold()
            )
        except (IndexError, AttributeError):
            return False

    @staticmethod
    def _is_self_join(line: str) -> bool:
        rest = line
        if rest.startswith("@") and " " in rest:
            rest = rest.split(" ", 1)[1]
        if " JOIN " not in rest:
            return False
        prefix, target = rest.split(" JOIN ", 1)
        nick = prefix.split("!", 1)[0].lstrip(":")
        joined = target.split(" ", 1)[0].lstrip(":").strip().lower()
        return nick.casefold() == BOT_NICK.casefold() and joined == CHANNEL.lower()

    @staticmethod
    def _authentication_rejected(line: str) -> bool:
        lowered = line.casefold()
        return (
            "login authentication failed" in lowered
            or "improperly formatted auth" in lowered
            or "error logging in" in lowered
            or TwitchIRC._irc_command(line) == "464"
        )

    @staticmethod
    def _join_rejected(line: str) -> bool:
        lowered = line.casefold()
        target_notice = f" notice {CHANNEL.lower()} " in lowered
        rejected_ids = (
            "msg-id=msg_banned",
            "msg-id=msg_channel_suspended",
            "msg-id=msg_room_not_found",
            "msg-id=msg_requires_verified_phone_number",
        )
        return target_notice and any(item in lowered for item in rejected_ids)

    def _begin_transport(self) -> int:
        with self._send_lock:
            with self._state_lock:
                if self._stop_event.is_set() or self.stop_flag or self._stopping:
                    raise self.Cancelled("IRC connection cancelled")
                self._generation += 1
                generation = self._generation
                previous = self.sock
                self.sock = None
                self.connected = False
                self._incoming_framer = None
                self._pending_lines = []
        self._invalidate_connection_memory()
        if previous is not None:
            try:
                previous.close()
            except Exception:
                pass
        return generation

    def _disconnect_generation(self, generation: int, reason: str) -> bool:
        with self._send_lock:
            with self._state_lock:
                if generation != self._generation:
                    return False
                was_connected = self.connected
                closing = self.sock
                self.connected = False
                self.sock = None
                self._incoming_framer = None
                self._pending_lines = []
                # Advancing the generation invalidates queued and in-flight work.
                self._generation += 1
                if was_connected:
                    log.warning("[IRC] State: DISCONNECTED reason=%s", reason)
            if closing is not None:
                try:
                    closing.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    closing.close()
                except Exception:
                    pass
        self._invalidate_connection_memory()
        self._stop_timers()
        return True

    def _wait_for_chat_slot(self, text, generation, queued_at, allow_stopping):
        # No transport/state/memory lock is held across an interruptible wait.
        while True:
            with self._state_lock:
                current = generation == self._generation and self.connected
                stopping = self._stopping or self._stop_event.is_set()
            if not current or (stopping and not allow_stopping):
                self._log_outbound("debug", text, generation, "dropped", "stale_or_inactive")
                return False
            now = self._chat_clock()
            age = now - queued_at
            if age > self.CHAT_MAX_QUEUE_AGE:
                self._log_outbound("debug", text, generation, "dropped", "queue_expired")
                return False
            delay = 0.0 if self._last_chat_write_at is None else self._last_chat_write_at + self.CHAT_MIN_INTERVAL - now
            if delay <= 0:
                return True
            if stopping:
                self._log_outbound("debug", text, generation, "dropped", "shutdown_paced")
                return False
            remaining = self.CHAT_MAX_QUEUE_AGE - age
            if remaining <= 0:
                self._log_outbound("debug", text, generation, "dropped", "queue_expired")
                return False
            self._chat_wait(min(delay, remaining, 0.1))

    def _send_immediate(self, text, generation, *, allow_unverified=False,
                        transport=None, trusted_command=False, memory_receipt=None,
                        queued_at=None):
        kwargs = dict(allow_unverified=allow_unverified, transport=transport,
                      trusted_command=trusted_command, memory_receipt=memory_receipt)
        if text.partition(" ")[0].upper() != "PRIVMSG":
            return self._write_immediate(text, generation, **kwargs)
        if queued_at is None:
            queued_at = self._chat_clock()
        # One sender normally owns this lock; synchronous fallback writes must
        # obey the same account-wide gap. PONG never takes this scheduling lock.
        with self._chat_schedule_lock:
            if not self._wait_for_chat_slot(text, generation, queued_at, allow_unverified):
                return False
            written = self._write_immediate(text, generation, queued_at=queued_at, **kwargs)
            if written:
                self._last_chat_write_at = self._chat_clock()
            return written

    def _write_immediate(
        self,
        text: str,
        generation: int,
        *,
        allow_unverified: bool = False,
        transport=None,
        trusted_command: bool = False,
        memory_receipt=None,
        queued_at=None,
    ) -> bool:
        from contextlib import nullcontext
        with self._memory_lock if memory_receipt is not None else nullcontext():
            if memory_receipt is not None and (
                    memory_receipt.generation != generation or not self._memory_receipt_current(memory_receipt)):
                self._log_outbound("debug", text, generation, "dropped", "memory_invalidated")
                return False
            text = self._gate_outbound(text, generation, trusted_command=trusted_command)
            if text is None:
                return False
            with self._send_lock:
                with self._state_lock:
                    if generation != self._generation:
                        raise self.Cancelled("stale IRC generation")
                    target = transport if transport is not None else self.sock
                    if target is None:
                        raise OSError("IRC socket is unavailable")
                    if not allow_unverified and (
                        not self.connected or self._stopping or self._stop_event.is_set()
                    ):
                        raise OSError("IRC transport is not connected")
                # Policy reload and waiting for the transport lock can take time.
                # Recheck the persisted control immediately before the write;
                # the memory lock also serializes forget/exclusion in this client.
                if memory_receipt is not None and not self._memory_receipt_current(memory_receipt):
                    self._log_outbound("debug", text, generation, "dropped", "memory_invalidated")
                    return False
                if queued_at is not None and self._chat_clock() - queued_at > self.CHAT_MAX_QUEUE_AGE:
                    self._log_outbound("debug", text, generation, "dropped", "queue_expired")
                    return False
                if queued_at is not None:
                    # A failed sendall may have written a partial frame. Preserve
                    # the account gap across reconnect rather than replaying it.
                    self._last_chat_write_at = self._chat_clock()
                target.sendall((text + "\r\n").encode("utf-8"))
            # A complete socket write is observable; Twitch/public delivery is not.
            self._log_outbound("info", text, generation, "written", "socket_write")
            if memory_receipt is not None:
                try:
                    from MedlarTV.core.memory_receipts import WrittenReply
                    self._record_written(WrittenReply(memory_receipt, generation, text.partition(" :")[2]))
                except Exception as exc:
                    log.warning("[MEMORY] Written receipt unavailable: %s", type(exc).__name__)
            return True

    # ---------------------------- CONNECT ----------------------------
    def connect(self):
        log.info("[IRC] Connecting to Twitch IRC…")
        log.info("[IRC] State: CONNECTING")
        generation = self._begin_transport()
        base_sock = None
        tls_sock = None
        base_published = False
        tls_published = False
        try:
            base_sock = socket.socket()
            context = ssl.create_default_context()
            # A verified trust-store wrapper expects an already-connected
            # transport. Bound and publish the raw connect first so stop() can
            # still close it while TCP establishment is in progress.
            base_sock.settimeout(self.CONNECT_TIMEOUT)
            with self._state_lock:
                if (
                    generation != self._generation
                    or self._stop_event.is_set()
                    or self._stopping
                ):
                    raise self.Cancelled("IRC connection cancelled")
                self.sock = base_sock
                base_published = True
            base_sock.connect((TWITCH_SERVER, TWITCH_PORT))
            with self._state_lock:
                if (
                    generation != self._generation
                    or self._stop_event.is_set()
                    or self._stopping
                    or self.sock is not base_sock
                ):
                    raise self.Cancelled("IRC connection cancelled")

            # On Windows pip-system-certs/truststore verifies immediately while
            # wrapping a connected socket. This synchronous step inherits the
            # connect timeout, but cannot be interrupted reliably until it
            # returns; it can therefore consume the shared shutdown budget.
            tls_sock = context.wrap_socket(base_sock, server_hostname=TWITCH_SERVER)
            with self._state_lock:
                if (
                    generation != self._generation
                    or self._stop_event.is_set()
                    or self._stopping
                    or self.sock is not base_sock
                ):
                    raise self.Cancelled("IRC connection cancelled")
                self.sock = tls_sock
                tls_published = True
            # Polling responsiveness is part of the cancellation contract.
            # A timeout configuration failure must fail the connection closed.
            tls_sock.settimeout(self.SOCKET_POLL_TIMEOUT)
            with self._state_lock:
                if (
                    generation != self._generation
                    or self._stop_event.is_set()
                    or self._stopping
                ):
                    raise self.Cancelled("IRC connection cancelled")

            # Membership must be requested before JOIN so Twitch echoes the
            # bot's own JOIN, which is the acknowledgement we verify below.
            for command in (
                f"PASS {IRC_TOKEN}",
                f"NICK {BOT_NICK}",
                "CAP REQ :twitch.tv/commands twitch.tv/tags twitch.tv/membership",
                f"JOIN {CHANNEL}",
            ):
                self._send_immediate(
                    command, generation, allow_unverified=True, transport=tls_sock,
                )

            authenticated = False
            joined = False
            from MedlarTV.core.irc_framing import IRCLineBuffer
            framer = IRCLineBuffer(self.MAX_INCOMING_LINE_BYTES)
            pending: List[str] = []
            pending_dropped = 0
            deadline = self._monotonic() + self.HANDSHAKE_TIMEOUT
            while not (authenticated and joined):
                if (
                    self._stop_event.is_set()
                    or self._stopping
                    or generation != self._generation
                ):
                    raise self.Cancelled("IRC connection cancelled")
                if self._monotonic() >= deadline:
                    if not authenticated:
                        raise self.AuthenticationError(
                            "IRC authentication acknowledgement timed out"
                        )
                    raise self.JoinError("IRC primary-channel JOIN acknowledgement timed out")
                try:
                    data = tls_sock.recv(2048)
                except TimeoutError:
                    continue
                if not data:
                    raise ConnectionError("IRC socket closed during authentication")
                for line in self._incoming_lines(framer, data):
                    if line.startswith("PING"):
                        pong = "PONG" + line[4:]
                        self._send_immediate(
                            pong, generation, allow_unverified=True, transport=tls_sock,
                        )
                        continue
                    if self._authentication_rejected(line):
                        raise self.AuthenticationError(
                            "Twitch rejected IRC authentication"
                        )
                    if self._join_rejected(line):
                        raise self.JoinError("Twitch rejected the primary-channel JOIN")
                    if self._is_authentication_welcome(line):
                        authenticated = True
                        continue
                    if self._is_self_join(line):
                        joined = True
                        continue
                    if any(token in line for token in (
                        " PRIVMSG ", " USERSTATE ", " GLOBALUSERSTATE ", " JOIN ",
                    )):
                        if len(pending) < self.WORK_QUEUE_SIZE:
                            pending.append(line)
                        else:
                            pending_dropped += 1

            if pending_dropped:
                log.warning("[IRC] Handshake pending frames dropped count=%d", pending_dropped)
            with self._state_lock:
                if (
                    generation != self._generation
                    or self._stop_event.is_set()
                    or self._stopping
                ):
                    raise self.Cancelled("IRC connection cancelled")
                self._incoming_framer = framer
                self._pending_lines = pending
                self.connected = True
                log.info(f"[IRC] Connected as {BOT_NICK} to {CHANNEL}")
                log.info("[IRC] State: CONNECTED channel=%s", CHANNEL)
        except BaseException as exc:
            if base_sock is not None and not base_published:
                try:
                    base_sock.close()
                except Exception:
                    pass
            self._disconnect_generation(generation, type(exc).__name__)
            if tls_sock is not None and not tls_published:
                try:
                    tls_sock.close()
                except Exception:
                    pass
            if (
                (self._stop_event.is_set() or self._stopping)
                and not isinstance(exc, self.Cancelled)
            ):
                raise self.Cancelled("IRC connection cancelled") from exc
            raise

        try:
            names = _get_active_copilots()
        except Exception:
            names = []
        try:
            from MedlarTV.command_authority import copilot_channels
            # Existing joined rooms end with this connection; no implicit PART
            # or reconnect is triggered by a privilege change. Re-read on connect.
            for chan in copilot_channels(CHANNEL, names):
                self._send_immediate(f"JOIN {chan}", generation)
                log.info("[IRC] Joining copilot channel %s", chan)
        except Exception:
            self._disconnect_generation(generation, "copilot_join_failure")
            raise

        try:
            self._init_emotes()
        except Exception:
            pass

        try:
            self._timers = _load_timers_yaml()
            self._init_timers(generation)
        except Exception:
            pass

        try:
            if os.getenv("ENABLE_ONLINE_OFFLINE_MSGS", "true").lower() == "true":
                self._send_raw(f"PRIVMSG {CHANNEL} :MedlarTV online")
        except Exception:
            pass

        return generation

    # ------------------------------------------------------------------
    def _send_raw(self, text: str, *, memory_receipt=None):
        generation = self._origin_generation()
        if memory_receipt is not None:
            return self._queue_outbound(text, generation, memory_receipt=memory_receipt)
        return self._queue_outbound(text, generation)

    def _send_operational(self, body: str) -> bool:
        # Internal callers select a closed command, never arbitrary viewer text.
        return self._queue_outbound(
            f"PRIVMSG {CHANNEL} :{body}", self._origin_generation(),
            trusted_command=True,
        )

    @staticmethod
    def _normalize_chat_text(text: str) -> str:
        text = str(text).replace("\x00", "")
        text = " ".join(text.splitlines())
        return text.encode("utf-8", errors="replace").decode("utf-8")

    @classmethod
    def _fit_chat_body(cls, text: str, channel: str, suffix: str = "") -> str:
        """Fit the final body, reserving its already-chosen decoration once."""
        text = cls._normalize_chat_text(text)
        suffix = cls._normalize_chat_text(suffix)
        header = f"PRIVMSG {channel} :"
        byte_budget = cls.MAX_IRC_FRAME_BYTES - len(header.encode("utf-8")) - 2
        chars_left = cls.MAX_CHAT_CHARACTERS - len(suffix)
        bytes_left = byte_budget - len(suffix.encode("utf-8"))
        if chars_left < 0 or bytes_left < 0:
            raise ValueError("IRC decoration exceeds message budget")
        if len(text) > chars_left or len(text.encode("utf-8")) > bytes_left:
            from MedlarTV.core.text_clusters import truncate_clusters
            marker = "..." if min(chars_left, bytes_left) >= 3 else ""
            text = truncate_clusters(text, chars_left, bytes_left, marker=marker)
            if marker:
                text = text[:-len(marker)].rstrip() + marker
            else:
                text = text.rstrip()
        return text + suffix

    @classmethod
    def _prepare_outbound(cls, text: str) -> str:
        """Bound public-chat frames without changing authentication/PING commands."""
        command, space, rest = text.partition(" ")
        if command.upper() != "PRIVMSG":
            if command.upper() not in {"PASS", "NICK", "CAP", "JOIN", "PONG", "PING", "QUIT"} \
                    or any(char in text for char in "\r\n\x00"):
                raise ValueError("Invalid IRC control frame")
            return text
        target, separator, body = rest.partition(" :")
        if not separator or not target or any(char.isspace() for char in target) \
                or "\x00" in target:
            raise ValueError("Malformed IRC chat frame")
        return f"PRIVMSG {target} :{cls._fit_chat_body(body, target)}"

    def _load_output_policy(self):
        from MedlarTV.core.settings import settings_path
        from MedlarTV.core.twitch_output import load_policy
        return load_policy(settings_path())

    def _gate_outbound(self, text, generation, *, trusted_command=False):
        """Pure last-write validation. No model work and no reconnect on a drop."""
        try:
            text = self._prepare_outbound(text)
            if not text.startswith("PRIVMSG "):
                return text
            target, _, body = text[len("PRIVMSG "):].partition(" :")
            if trusted_command:
                if target != CHANNEL or body not in self._OPERATIONAL_COMMANDS:
                    raise ValueError("Invalid operational command")
                return text
            policy = self._load_output_policy()
            if policy.permits(body):
                return text
            self._log_outbound("warning", text, generation, "dropped", "content_blocked")
        except Exception as exc:
            self._log_outbound(
                "warning", text, generation, "dropped", "validation_unavailable",
                type(exc).__name__,
            )
        return None

    @staticmethod
    def _log_outbound(level, text, generation, outcome, reason, exception="None"):
        """Persist counts and fixed outcomes, never payloads or credentials."""
        if not text.startswith("PRIVMSG ") or " :" not in text:
            return
        body = text.partition(" :")[2]
        getattr(log, level)(
            "[IRC] Outbound outcome=%s reason=%s generation=%d chars=%d "
            "body_bytes=%d frame_bytes=%d exception=%s",
            outcome, reason, generation, len(body),
            len(body.encode("utf-8", errors="replace")),
            len(text.encode("utf-8", errors="replace")) + 2, exception,
        )

    def send_message(self, msg: str) -> bool:
        generation = self._origin_generation()
        receipt = getattr(self._work_local, "memory_receipt", None)
        if receipt is not None and not self._memory_receipt_current(receipt):
            return False
        if not self._generation_is_connected(generation):
            self._log_outbound(
                "warning", f"PRIVMSG {CHANNEL} :{msg}", generation,
                "dropped", "stale_or_inactive",
            )
            return False
        try:
            policy = self._load_output_policy()
            final = None
            if policy.permits(msg):
                candidate = self._decorate_message(policy.format_body(msg))
                if policy.permits(candidate):
                    final = candidate
            if final is None:
                redirect = None
                # Only response workers may make the one extra bounded model call.
                # Reader, sender, startup/shutdown and direct GUI sends never do.
                if policy.llm_enabled and getattr(self._work_local, "redirect_allowed", False) \
                        and getattr(self._work_local, "generation", None) == generation:
                    try:
                        from MedlarTV.core.llm_brain import generate_twitch_redirect
                        redirect = generate_twitch_redirect()
                    except Exception:
                        pass
                from MedlarTV.core.twitch_output import NEUTRAL_FALLBACK
                for replacement in (redirect, NEUTRAL_FALLBACK):
                    if not isinstance(replacement, str) or not policy.permits(replacement):
                        continue
                    candidate = self._decorate_message(policy.format_body(replacement))
                    if policy.permits(candidate):
                        final = candidate
                        break
            if final is None:
                self._log_outbound(
                    "warning", f"PRIVMSG {CHANNEL} :{msg}", generation,
                    "dropped", "no_validated_reply",
                )
                return False
        except Exception as exc:
            self._log_outbound(
                "warning", f"PRIVMSG {CHANNEL} :{msg}", generation,
                "dropped", "validation_unavailable", type(exc).__name__,
            )
            return False
        if not self._generation_is_connected(generation):
            self._log_outbound(
                "warning", f"PRIVMSG {CHANNEL} :{final}", generation,
                "dropped", "stale_or_inactive",
            )
            return False
        # True means queue acceptance (or a synchronous write), not delivery.
        if receipt is not None:
            return self._send_raw(f"PRIVMSG {CHANNEL} :{final}", memory_receipt=receipt)
        return self._send_raw(f"PRIVMSG {CHANNEL} :{final}")

    def _ensure_background_workers(self) -> None:
        import queue

        with self._worker_start_lock:
            if self._workers_started:
                return
            self._work_queue = queue.Queue(maxsize=self.WORK_QUEUE_SIZE)
            self._outbound_queue = queue.Queue(maxsize=self.OUTBOUND_QUEUE_SIZE)
            self._worker_threads = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"TwitchIRC-work-{index + 1}",
                    daemon=True,
                )
                for index in range(self.WORKER_COUNT)
            ]
            self._sender_thread = threading.Thread(
                target=self._sender_loop,
                name="TwitchIRC-send",
                daemon=True,
            )
            self._workers_started = True
            self._sender_thread.start()
            for worker in self._worker_threads:
                worker.start()

    def _queue_outbound(
        self,
        text: str,
        generation: int,
        *,
        completion=None,
        allow_stopping: bool = False,
        trusted_command: bool = False,
        memory_receipt=None,
    ) -> bool:
        import queue

        with self._state_lock:
            valid = (
                generation == self._generation
                and self.connected
                and (allow_stopping or (
                    not self._stopping and not self._stop_event.is_set()
                ))
            )
        if not valid:
            self._log_outbound(
                "warning", text, generation, "dropped", "stale_or_inactive",
            )
            if completion is not None:
                completion.set()
            return False

        if memory_receipt is not None and (
                memory_receipt.generation != generation or not self._memory_receipt_current(memory_receipt)):
            if completion is not None:
                completion.set()
            self._log_outbound("debug", text, generation, "dropped", "memory_invalidated")
            return False

        queued_at = getattr(self._work_local, "outbound_created_at", None)
        if queued_at is None:
            queued_at = self._chat_clock()

        original = text
        text = self._gate_outbound(text, generation, trusted_command=trusted_command)
        if text is None:
            if completion is not None:
                completion.set()
            return False
        self._log_outbound(
            "debug", text, generation, "prepared",
            "normalized_or_limited" if text != original else "within_limits",
        )

        sender = self._sender_thread
        if self._outbound_queue is None or sender is None or not sender.is_alive():
            if self._workers_started and text.startswith("PRIVMSG "):
                self._log_outbound("warning", text, generation, "dropped", "sender_unavailable")
                if completion is not None:
                    completion.set()
                return False
            try:
                kwargs = {"trusted_command": True} if trusted_command else {}
                if memory_receipt is not None:
                    kwargs["memory_receipt"] = memory_receipt
                written = self._send_immediate(
                    text, generation, allow_unverified=allow_stopping, queued_at=queued_at, **kwargs,
                )
                if completion is not None:
                    completion.set()
                return written is not False
            except Exception as exc:
                self._log_outbound(
                    "error", text, generation, "failed", "socket_write",
                    type(exc).__name__,
                )
                if completion is not None:
                    completion.set()
                if not self._stopping and not self._stop_event.is_set():
                    self._disconnect_generation(generation, "send_failure")
                return False
        try:
            self._outbound_queue.put_nowait(
                (generation, text, completion, allow_stopping, trusted_command, memory_receipt, queued_at)
            )
            self._log_outbound("debug", text, generation, "queued", "accepted")
            return True
        except queue.Full:
            if completion is not None:
                completion.set()
            log.warning("[IRC] Outbound queue full; message dropped")
            self._log_outbound("warning", text, generation, "dropped", "queue_full")
            return False

    def _sender_loop(self) -> None:
        import queue

        while True:
            if self._stop_event.is_set() and self._outbound_queue.empty():
                return
            try:
                item = self._outbound_queue.get(timeout=0.1)
            except queue.Empty:
                self._refresh_memory_control()
                continue
            try:
                if item is None:
                    return
                generation, text, completion, allow_stopping = item[:4]
                trusted_command = len(item) >= 5 and item[4] is True
                memory_receipt = item[5] if len(item) >= 6 else None
                queued_at = item[6] if len(item) >= 7 else self._chat_clock()
                with self._state_lock:
                    current = (
                        generation == self._generation
                        and self.connected
                        and (allow_stopping or not self._stop_event.is_set())
                    )
                if not current:
                    self._log_outbound(
                        "warning", text, generation, "dropped", "stale_or_inactive",
                    )
                    continue
                try:
                    kwargs = {"trusted_command": True} if trusted_command else {}
                    if memory_receipt is not None:
                        kwargs["memory_receipt"] = memory_receipt
                    self._send_immediate(
                        text,
                        generation,
                        allow_unverified=allow_stopping or self._stopping,
                        queued_at=queued_at,
                        **kwargs,
                    )
                except Exception as exc:
                    self._log_outbound(
                        "error", text, generation, "failed", "socket_write",
                        type(exc).__name__,
                    )
                    if not self._stopping and not self._stop_event.is_set():
                        log.error("[IRC] Send failed: %s", type(exc).__name__)
                        self._disconnect_generation(generation, "send_failure")
            finally:
                try:
                    if item is not None and item[2] is not None:
                        item[2].set()
                except Exception:
                    pass
                self._outbound_queue.task_done()

    def _enqueue_work(self, line: str, generation: int) -> bool:
        import queue

        if not self._generation_is_connected(generation):
            return False
        try:
            self._work_queue.put_nowait((generation, line))
            return True
        except queue.Full:
            log.warning("[IRC] Work queue full; incoming message dropped")
            return False

    def _enqueue_application_message(self, message: str, generation: int) -> bool:
        import queue

        if not self._generation_is_connected(generation):
            return False
        try:
            self._work_queue.put_nowait(
                (generation, ("send_message", message, self._chat_clock()))
            )
            return True
        except queue.Full:
            log.warning("[IRC] Work queue full; timer message dropped")
            return False

    def _worker_loop(self) -> None:
        import queue

        while not self._stop_event.is_set():
            try:
                item = self._work_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if item is None:
                    return
                generation, work = item
                if not self._generation_is_connected(generation):
                    continue
                self._work_local.generation = generation
                self._work_local.redirect_allowed = True
                if (
                    isinstance(work, tuple)
                    and len(work) in (2, 3)
                    and work[0] == "send_message"
                ):
                    created_at = work[2] if len(work) == 3 else self._chat_clock()
                    if self._chat_clock() - created_at > self.CHAT_MAX_QUEUE_AGE:
                        self._log_outbound("debug", f"PRIVMSG {CHANNEL} :{work[1]}", generation, "dropped", "queue_expired")
                        continue
                    self._work_local.outbound_created_at = created_at
                    self.send_message(work[1])
                else:
                    self._process_raw_message(work)
            except Exception as exc:
                log.error("[IRC] Message worker failed: %s", type(exc).__name__)
            finally:
                try:
                    del self._work_local.generation
                    del self._work_local.redirect_allowed
                    if hasattr(self._work_local, "outbound_created_at"):
                        del self._work_local.outbound_created_at
                except (AttributeError, KeyError):
                    pass
                self._work_queue.task_done()

    def _shutdown_background_workers(self, join_timeout: float = 1.0) -> None:
        current = threading.current_thread()
        for target_queue, count in (
            (self._work_queue, len(self._worker_threads)),
            (self._outbound_queue, 1),
        ):
            if target_queue is None:
                continue
            for _ in range(count):
                try:
                    target_queue.put_nowait(None)
                except Exception:
                    break
        threads = list(self._worker_threads)
        if self._sender_thread is not None:
            threads.append(self._sender_thread)
        for worker in threads:
            if worker is current:
                continue
            try:
                worker.join(join_timeout)
            except Exception:
                pass

    def _has_live_background_workers(self) -> bool:
        threads = list(self._worker_threads)
        if self._sender_thread is not None:
            threads.append(self._sender_thread)
        if self._timer_thread is not None:
            threads.append(self._timer_thread)
        return any(thread.is_alive() for thread in threads)

    def _socket_for_current_work(self):
        return self._GenerationSocket(self, self._origin_generation())

    def _init_emotes(self) -> None:
        try:
            self.available_emotes = []
            self.user_emotes = []
            try:
                log.info("[IRC] Emotes will be loaded from USERSTATE emote-sets for bot account")
            except Exception:
                pass
        except Exception:
            pass

    def _decorate_message(self, msg: str) -> str:
        base = self._normalize_chat_text(msg)
        decorated = base
        try:
            if os.getenv("ENABLE_EMOTE_RESPONSES", "true").lower() != "true":
                return self._fit_chat_body(base, CHANNEL)
            avail = self.user_emotes or []
            try:
                from MedlarTV.core.emotional_system import get_current_emotion
                from MedlarTV.core.emotion_emote_selector import add_emotion_emote
                emotion = get_current_emotion()
                decorated = add_emotion_emote(base, emotion, avail)
            except Exception:
                try:
                    from MedlarTV.core.twitch_events import add_random_emote
                    decorated = add_random_emote(base, avail)
                except Exception:
                    pass
        except Exception:
            pass
        if decorated.startswith(base):
            return self._fit_chat_body(base, CHANNEL, decorated[len(base):])
        return self._fit_chat_body(decorated, CHANNEL)

    def _load_timers(self) -> List[Dict[str, Any]]:
        try:
            return _load_timers_yaml()
        except Exception:
            return []

    def _stop_timers(self) -> None:
        timer_stop = self._timer_stop
        try:
            timer_stop.set()
        except Exception:
            pass
        timer_thread = self._timer_thread
        if timer_thread is not None and timer_thread is not threading.current_thread():
            try:
                timer_thread.join(1.0)
            except Exception:
                pass
        if timer_thread is None or not timer_thread.is_alive():
            self._timer_thread = None

    def _init_timers(self, generation: Optional[int] = None) -> None:
        try:
            self._stop_timers()
            if self._timer_thread is not None and self._timer_thread.is_alive():
                log.warning("[IRC] Previous timer worker is still shutting down")
                return
            now = time.time()
            self._chat_count = 0
            self._next_chat_due.clear()
            self._next_time_due.clear()
            for t in self._timers:
                if not t.get("enabled"):
                    continue
                typ = str(t.get("type", "")).lower()
                k = str(t.get("id", "")) or str(hash(t.get("message", "")))
                if typ == "chats":
                    n = int(t.get("interval_chats", 0))
                    if n > 0:
                        self._next_chat_due[k] = n
                elif typ == "time":
                    m = int(t.get("interval_minutes", 0))
                    if m > 0:
                        self._next_time_due[k] = now + m * 60
            if self._next_time_due:
                timer_stop = threading.Event()
                self._timer_stop = timer_stop
                self._timer_thread = threading.Thread(
                    target=self._timer_loop,
                    args=(generation, timer_stop),
                    name="TwitchIRC-timers",
                    daemon=True,
                )
                self._timer_thread.start()
        except Exception:
            pass

    def _timer_loop(
        self,
        generation: Optional[int] = None,
        timer_stop: Optional[threading.Event] = None,
    ) -> None:
        stop_event = timer_stop or self._timer_stop
        if generation is not None:
            self._work_local.generation = generation
        try:
            while not stop_event.is_set():
                if generation is not None and not self._generation_is_connected(generation):
                    return
                try:
                    now = time.time()
                    for t in self._timers:
                        if not t.get("enabled"):
                            continue
                        if str(t.get("type", "")).lower() != "time":
                            continue
                        k = str(t.get("id", "")) or str(hash(t.get("message", "")))
                        m = int(t.get("interval_minutes", 0))
                        if m <= 0:
                            continue
                        due_ts = self._next_time_due.get(k)
                        if due_ts is None:
                            self._next_time_due[k] = now + m * 60
                            continue
                        if now >= due_ts and is_enabled("timers"):
                            message = str(t.get("message", ""))
                            if generation is None:
                                self.send_message(message)
                            else:
                                self._enqueue_application_message(message, generation)
                            self._next_time_due[k] = now + m * 60
                except Exception:
                    pass
                if generation is None:
                    # Retain the direct-call behavior used by focused unit tests.
                    time.sleep(1.0)
                else:
                    stop_event.wait(1.0)
        finally:
            if generation is not None:
                try:
                    del self._work_local.generation
                except (AttributeError, KeyError):
                    pass

    # ------------------------------ LOOP ------------------------------
    def _wait_for_reconnect(self, delay: float) -> bool:
        return self._stop_event.wait(delay)

    @staticmethod
    def _incoming_lines(framer, data):
        lines, invalid, oversized = framer.feed(data)
        if invalid or oversized:
            log.warning("[IRC] Incoming frames dropped invalid_utf8=%d oversized=%d", invalid, oversized)
        return lines

    def _receive_loop(self, generation: int) -> None:
        from MedlarTV.core.irc_framing import IRCLineBuffer
        with self._state_lock:
            if generation != self._generation or not self.connected:
                raise ConnectionError("IRC transport was invalidated")
            pending = list(self._pending_lines)
            self._pending_lines = []
            framer = self._incoming_framer
            if framer is None:
                framer = IRCLineBuffer(self.MAX_INCOMING_LINE_BYTES)
                self._incoming_framer = framer
        for line in pending:
            self._handle_raw_message(line, generation)

        while not self._stop_event.is_set():
            with self._state_lock:
                if generation != self._generation or not self.connected:
                    raise ConnectionError("IRC transport was invalidated")
                transport = self.sock
            if transport is None:
                raise ConnectionError("IRC socket is unavailable")
            try:
                data = transport.recv(2048)
            except TimeoutError:
                continue
            if not data:
                raise EOFError("IRC socket closed by peer")

            for line in self._incoming_lines(framer, data):
                self._handle_raw_message(line, generation)

        raise self.Cancelled("IRC listener stopped")

    def listen(self):
        self._ensure_background_workers()
        log.info("[IRC] State: DISCONNECTED reason=starting")
        delay = self.RECONNECT_INITIAL_DELAY
        while not self._stop_event.is_set():
            generation = None
            connected_at = None
            try:
                generation = self.connect()
                connected_at = self._monotonic()
                self._receive_loop(generation)
            except self.Cancelled:
                break
            except self.AuthenticationError as exc:
                log.error("[IRC] State: AUTH_FAILED reason=%s", str(exc))
            except self.JoinError as exc:
                log.error("[IRC] State: JOIN_FAILED reason=%s", str(exc))
            except Exception as e:
                log.error("[IRC] Transport error: %s", type(e).__name__)
            finally:
                if generation is not None:
                    self._disconnect_generation(generation, "transport_failure")

            if self._stop_event.is_set():
                break
            if connected_at is not None:
                lifetime = self._monotonic() - connected_at
                if lifetime >= self.RECONNECT_RESET_AFTER:
                    delay = self.RECONNECT_INITIAL_DELAY
            log.warning("[IRC] State: RETRYING delay=%.1fs", delay)
            if self._wait_for_reconnect(delay):
                break
            delay = min(self.RECONNECT_MAX_DELAY, max(
                self.RECONNECT_INITIAL_DELAY, delay * 2,
            ))

        self._stop_timers()
        self._shutdown_background_workers()

    def stop(self):
        with self._state_lock:
            if self._stopping:
                return
            self._stopping = True
            generation = self._generation
            was_connected = self.connected

        if was_connected and os.getenv(
            "ENABLE_ONLINE_OFFLINE_MSGS", "true"
        ).lower() == "true":
            try:
                flushed = threading.Event()
                if self._queue_outbound(
                    f"PRIVMSG {CHANNEL} :MedlarTV offline",
                    generation,
                    completion=flushed,
                    allow_stopping=True,
                ):
                    flushed.wait(1.0)
            except Exception:
                pass

        self.stop_flag = True
        self._stop_event.set()
        self._disconnect_generation(generation, "stopped")
        self._stop_timers()
        self._shutdown_background_workers()

    # ------------------------------------------------------------------
    def _handle_raw_message(self, line: str, generation: Optional[int] = None):
        generation = self._origin_generation() if generation is None else generation
        if line.startswith("PING"):
            try:
                self._send_immediate("PONG" + line[4:], generation)
            except Exception as exc:
                log.error("[IRC] PONG failed: %s", type(exc).__name__)
                self._disconnect_generation(generation, "pong_failure")
            return

        if self._irc_command(line) == "NOTICE":
            self._log_chat_notice(line)
            return

        if self._workers_started and threading.current_thread() not in self._worker_threads:
            self._enqueue_work(line, generation)
            return
        self._process_raw_message(line)

    def _log_chat_notice(self, line: str) -> None:
        # NOTICE bodies can contain quoted private text. Log only known IDs and
        # fixed explanations; even an unknown/malicious tag is never echoed.
        notice_id = ""
        if line.startswith("@") and " " in line:
            for tag in line[1:].split(" ", 1)[0].split(";"):
                key, _, value = tag.partition("=")
                if key == "msg-id":
                    notice_id = value
                    break
        reason = self._CHAT_REJECTION_REASONS.get(notice_id)
        if reason is not None:
            log.warning(
                "[IRC] Chat notice outcome=not_sent reason=%s msg_id=%s",
                reason, notice_id,
            )
        elif self._authentication_rejected(line):
            log.warning("[IRC] NOTICE reason=authentication_rejected")
        else:
            log.debug("[IRC] NOTICE reason=unclassified")

    def _process_raw_message(self, line: str):
        command = self._irc_command(line)
        if command == "NOTICE":
            self._log_chat_notice(line)
            return

        try:
            if command == "USERSTATE" and "emote-sets=" in line:
                idx = line.find("emote-sets=")
                if idx != -1:
                    seg = line[idx:].split(";", 1)[0]
                    val = seg.split("=", 1)[1]
                    sets = [s.strip() for s in val.split(",") if s.strip()]
                    if sets:
                        self._fetch_emote_sets(sets)
            if command == "GLOBALUSERSTATE" and "emote-sets=" in line:
                idx = line.find("emote-sets=")
                if idx != -1:
                    seg = line[idx:].split(";", 1)[0]
                    val = seg.split("=", 1)[1]
                    sets = [s.strip() for s in val.split(",") if s.strip()]
                    if sets:
                        self._fetch_emote_sets(sets)
        except Exception:
            pass

        if command == "JOIN":
            try:
                prefix = line.split(" JOIN ", 1)[0]
                if "!" in prefix:
                    username = prefix.split("!", 1)[0].lstrip(":")
                    key = username.lower()
                    if key not in _arrived_users:
                        _arrived_users.add(key)
                        _apply_event_rules(
                            "viewer_arrived",
                            username,
                            self.send_message,
                            self._socket_for_current_work(),
                        )
            except Exception:
                pass
            return

        self._process_incoming(line)

    def _fetch_emote_sets(self, set_ids: List[str]) -> None:
        generation = self._origin_generation()
        try:
            token = os.getenv("DEVILMEDLAR_TWITCH_TOKEN", "").replace("oauth:", "")
            client_id = os.getenv("APP_TWITCH_CLIENT_ID", "")
            if not token or not client_id:
                return
            import requests
            headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
            names: List[str] = []
            for sid in set_ids:
                try:
                    r = requests.get(
                        "https://api.twitch.tv/helix/chat/emotes",
                        headers=headers,
                        params={"emote_set_id": sid},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        data = r.json().get("data", [])
                        for em in data:
                            n = em.get("name")
                            if n:
                                names.append(str(n))
                except Exception:
                    pass
            if names and self._generation_is_current(generation):
                # Use only the bot's accessible emotes from USERSTATE
                self.user_emotes = sorted(set(names))
                self.available_emotes = list(self.user_emotes)
                try:
                    log.info(f"[IRC] Bot emotes loaded from emote-sets, total available={len(self.user_emotes)}")
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _claim_usernotice(self, tags, source_channel) -> bool:
        """Suppress repeated event IDs with bounded in-memory, channel-scoped state."""
        from collections import OrderedDict
        notice_id = tags.get("id", "")
        if not notice_id:
            return True
        key = (source_channel, notice_id)
        with self._state_lock:
            seen = getattr(self, "_seen_usernotices", None)
            if seen is None:
                seen = self._seen_usernotices = OrderedDict()
            if key in seen:
                return False
            seen[key] = None
            while len(seen) > 256:
                seen.popitem(last=False)
        return True

    def _process_incoming(self, raw: str):
        # Server events need neither a nick prefix nor optional trailing chat text.
        if self._irc_command(raw) == "USERNOTICE":
            from MedlarTV.core.twitch_events import parse_usernotice, get_sub_response
            notice = parse_usernotice(raw)
            if notice is None:
                return
            tags, source_channel = notice
            raid_info = detect_raid(raw)
            if raid_info:
                if self._claim_usernotice(tags, source_channel):
                    self.send_message(
                        f"⚡ RAID from {raid_info['raider']} with {raid_info['viewer_count']} viewers!"
                    )
                return
            sub_info = detect_subscription(raw)
            if sub_info and self._claim_usernotice(tags, source_channel):
                self.send_message(get_sub_response(sub_info))
            # Unknown/disabled/malformed notices never become ordinary viewer chat.
            return

        # Parse tags & username
        tags, username, message, source_channel = self._parse_message(raw)
        log.debug("[DEBUG] Parsed IRC chat for user=%s", username)

        if username is None or message is None or source_channel is None:
            return

        try:
            key = username.lower()
            if key not in _arrived_users:
                _arrived_users.add(key)
                _apply_event_rules(
                    "viewer_arrived",
                    username,
                    self.send_message,
                    self._socket_for_current_work(),
                )
        except Exception:
            pass

        try:
            self._learn_emotes_from_message(tags, message)
        except Exception:
            pass

        ctx: Dict[str, Any] = {
            "bot_nick": BOT_NICK,
            "socket_connected": self.connected,
            "bridge_connected": False,
            "message_timestamp": time.time(),
            # Preserve IRC identity before any display-name alias is applied.
            "channel": source_channel,
            "login": username.lower(),
            "user_id": tags.get("user-id", "").strip(),
            "room_id": tags.get("room-id", "").strip(),
            "message_id": tags.get("id", "").strip(),
        }

        # ------------------ EVENT DETECTION ------------------

        # Legacy channel-points detector is not a general IRC redemption feed.
        # Supporting all redemptions needs a separate EventSub feature decision.
        redeem = detect_channel_point_redemption(raw)
        if redeem:
            from MedlarTV.core.twitch_events import get_channel_point_response
            self.send_message(get_channel_point_response(redeem))
            return

        # Bits
        bits = detect_bits(raw)
        if bits:
            self.send_message(f"💎 {bits['user']} cheered {bits['amount']} bits!")
            return

        # ------------------ CHAT MESSAGE ------------------

        if message.startswith("!"):
            self._handle_command(username, message, ctx, tags)
        else:
            self._handle_regular_message(username, message, ctx, tags)

    # ----------------------------------------------------------------------------
    def _parse_message(self, raw: str):
        # Twitch IRC with tags
        # Format: @tags :username!something PRIVMSG #chan :message
        try:
            tags = {}
            username = None
            message = None
            source_channel = None

            if raw.startswith("@"):  # has tags
                tag_str, rest = raw[1:].split(" ", 1)
                for t in tag_str.split(";"):
                    if "=" in t:
                        k, v = t.split("=", 1)
                        tags[k] = v
                raw = rest

            if " PRIVMSG " in raw:
                prefix, msg_part = raw.split(" PRIVMSG ", 1)

                # Extract username
                if "!" in prefix:
                    username = prefix.split("!", 1)[0].lstrip(":")

                # Extract the source channel and message from this PRIVMSG.
                if " :" in msg_part:
                    target, message = msg_part.split(" :", 1)
                    from MedlarTV.command_authority import normalize_channel
                    source_channel = normalize_channel(target)

            return tags, username, message, source_channel

        except Exception:
            return {}, None, None, None

    def _learn_emotes_from_message(self, tags: Dict[str, Any], message: str) -> None:
        try:
            emtag = tags.get("emotes", "")
            if not emtag:
                return
            pieces: List[str] = []
            for grp in emtag.split("/"):
                if ":" not in grp:
                    continue
                _, ranges = grp.split(":", 1)
                for r in ranges.split(","):
                    if "-" not in r:
                        continue
                    s, e = r.split("-", 1)
                    try:
                        start = int(s)
                        end = int(e)
                        if 0 <= start <= end < len(message):
                            pieces.append(message[start:end+1])
                    except Exception:
                        continue
            # Do not learn emotes from others unless the bot can use them
            if pieces:
                allowed = set(self.user_emotes)
                learned = [p for p in pieces if p in allowed]
                if learned:
                    merged = sorted(set(self.user_emotes + learned))
                    self.user_emotes = merged
                    self.available_emotes = list(self.user_emotes)
                    try:
                        log.info(f"[IRC] Learned permitted emotes from chat, total available={len(self.user_emotes)}")
                    except Exception:
                        pass
        except Exception:
            pass

    # ------------------- REGULAR CHAT ----------------------
    def _handle_regular_message(self, username: str, msg: str, ctx: Dict[str, Any], tags: Dict[str, Any]):
        log.debug("[DEBUG] Incoming chat from @%s", username)
        try:
            if username and username.lower() == BOT_NICK.lower():
                return
        except Exception:
            pass
        try:
            if is_enabled("timers"):
                self._chat_count += 1
                for t in self._timers:
                    if not t.get("enabled"):
                        continue
                    if str(t.get("type", "")).lower() == "chats":
                        k = str(t.get("id", "")) or str(hash(t.get("message", "")))
                        n = int(t.get("interval_chats", 0))
                        if n > 0:
                            due = self._next_chat_due.get(k, n)
                            if self._chat_count >= due and is_enabled("timers"):
                                self.send_message(str(t.get("message", "")))
                                self._next_chat_due[k] = due + n
        except Exception:
            pass

        # Auto-moderation disabled for user messages; only Medlar follows rules
        result = {"is_allowed": True}

        try:
            if username and username.lower() == "pokemoncommunitygame":
                ml = msg.strip().lower()
                bot_mention = f"@{BOT_NICK.lower()}"
                mentions_bot = (bot_mention in ml) or ("@medlartv" in ml)
                if not is_enabled("pcg_auto_catch"):
                    return
                # Ignore catch-success announcements
                catch_phrases = [
                    "has been caught by",
                    "has been caught",
                    "was caught by",
                    "catch successful",
                    "has been captured",
                ]
                if any(p in ml for p in catch_phrases):
                    return
                if ("a wild" in ml) and ("catch it using !pokecatch" in ml):
                    self._send_operational("!pokecatch ultraball")
                    return
                if ("don’t own that ball" in ml) or ("don't own that ball" in ml):
                    if mentions_bot:
                        self._send_operational("!pokeshop ultraball 1")
                    return
                if "purchase successful" in ml:
                    if mentions_bot:
                        self._send_operational("!pokecatch ultraball")
                    return
                # Ignore any other PokemonCommunityGame messages
                return
        except Exception:
            pass

        try:
            if _is_ignored(username):
                return
            lookup, location = should_lookup_time(msg)
        except Exception:
            lookup, location = False, None

        source_channel, channel_key, stable_viewer, viewer_key = self._memory_identity(username, ctx)
        profile_channel = str(ctx.get("room_id") or "").strip()
        try:
            from MedlarTV.core.conversation_context import (
                is_explicit_personal_run_query, memory_query_topics,
                profile_query_topics,
            )
            requested_memory_topics = memory_query_topics(msg)
            requested_profile_topics = profile_query_topics(msg)
            explicit_personal_run_query = is_explicit_personal_run_query(msg)
        except Exception:
            requested_memory_topics = set()
            requested_profile_topics = set()
            explicit_personal_run_query = False
        # Existing send_message destinations are fixed to CHANNEL. Do not use
        # another joined channel's messages or profiles in that public reply.
        memory_enabled = source_channel == CHANNEL.lower()
        conversation = None
        profiles = None
        observation = None
        memory_blocked = False
        remembering = True
        reply_receipt = None
        if memory_enabled:
            # Serialize admission and session intake across this client's workers.
            # This admission section performs no model or socket operation.
            with self._memory_lock:
                remembering, _ = self._refresh_memory_control()
                try:
                    conversation = self._conversation_store()
                except Exception as exc:
                    log.warning("[MEMORY] Conversation store unavailable: %s", type(exc).__name__)
                try:
                    profiles = self._profile_store()
                    observation = profiles.observe(
                        profile_channel, stable_viewer or None, msg,
                        message_id=str(ctx.get("message_id") or "") or None,
                        intake_enabled=remembering,
                    )
                    memory_blocked = (not remembering or observation.kind in {"excluded", "forgotten"}
                                      or profiles.is_excluded(profile_channel, stable_viewer or None))
                except Exception as exc:
                    log.warning("[MEMORY] Profile store unavailable: %s", type(exc).__name__)
                    # Unknown exclusion state cannot authorize session memory.
                    memory_blocked = True
                if memory_blocked:
                    if conversation is not None:
                        try:
                            conversation.forget_viewer(channel_key, viewer_key)
                        except Exception as exc:
                            log.warning("[MEMORY] Conversation clearing unavailable: %s", type(exc).__name__)
                    conversation = None
                    profiles = None
                if conversation is not None:
                    try:
                        private_claim = observation is not None and observation.kind in {
                            "saved", "clarify", "forgotten",
                        }
                        source_observation = conversation.observe_user(
                            channel_key, viewer_key, username, msg,
                            share_public=not private_claim,
                        )
                        reply_receipt = self._make_reply_receipt(
                            conversation, source_observation, channel_key, viewer_key,
                            profile_channel, stable_viewer or None,
                        )
                    except Exception as exc:
                        log.warning("[MEMORY] Conversation observation unavailable: %s", type(exc).__name__)
                        conversation = None
                        profiles = None
                        memory_blocked = True
            if observation is not None:
                if observation.kind == "forgotten":
                    self.send_message(_prepare_msg(f"@{username} I've forgotten that from your profile."))
                    return
                if observation.kind == "clarify" and observation.clarification:
                    reply = observation.clarification
                    self._send_memory_reply(_prepare_msg(_prefix_for_llm_reply(username) + reply), reply_receipt)
                    return
                if observation.kind == "limit_reached":
                    reply = "I couldn't save that detail because my profile memory is full. Your earlier saved details are still there."
                    self._send_memory_reply(_prepare_msg(f"@{username} {reply}"), reply_receipt)
                    return
        if lookup and location:
            try:
                ti = get_times_for_location(location)
            except Exception:
                ti = "Time lookup unavailable"
            if ti:
                final = _prepare_msg(_role_prefix(username) + ti)
                self.send_message(final)
                try:
                    log_interaction(username, msg, ti)
                except Exception:
                    pass
                return
            else:
                not_found = f"Location not found: {location}"
                final = _prepare_msg(_role_prefix(username) + not_found)
                self.send_message(final)
                try:
                    log_interaction(username, msg, not_found)
                except Exception:
                    pass
                return

        if lookup and not location:
            try:
                ti = get_default_local_time()
            except Exception:
                ti = None
            ti = ti or "Unable to get local time"
            if ti:
                final = _prepare_msg(_role_prefix(username) + ti)
                self.send_message(final)
                try:
                    log_interaction(username, msg, ti)
                except Exception:
                    pass
                return

        try:
            ml = msg.strip().lower()
            kwords = ["medlar", "medlartv", BOT_NICK.lower()]
            asks = ["do you have", "any", "what", "show", "list"]
            if ("command" in ml) and any(a in ml for a in asks) and any(k in ml for k in kwords):
                link = _get_commands_link()
                text = f"Yeah i have commands, here is the link {link} — if you use !commands i will send link as well"
                final = _prepare_msg(_role_prefix(username) + text)
                self.send_message(final)
                try:
                    log_interaction(username, msg, text)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ------------------------------------------
        # Fuzzy-triggered Medlar response (IMPORTANT)
        # ------------------------------------------

        log.debug("[DEBUG] Running should_respond()")

        decision = is_enabled("fuzzy_trigger") and should_respond(msg)

        log.debug(f"[DEBUG] should_respond() returned: {decision}")

        if decision:
            if not is_enabled("llm_brain"):
                return

            if not remembering and (requested_profile_topics or requested_memory_topics):
                reply = "Viewer memory is off, so I can't recall saved details. You can still ask me to forget them."
                self.send_message(_prepare_msg(_prefix_for_llm_reply(username) + reply))
                return

            if memory_blocked and requested_profile_topics:
                from MedlarTV.core.viewer_profiles import ViewerProfileStore
                missing_reply = ViewerProfileStore.missing_answer_for_topics(requested_profile_topics)
                if missing_reply:
                    self.send_message(_prepare_msg(_prefix_for_llm_reply(username) + missing_reply))
                    return

            if profiles is not None and stable_viewer and profile_channel:
                profile_lookup_succeeded = False
                try:
                    fact_reply = profiles.get_answer(profile_channel, stable_viewer, msg)
                    profile_lookup_succeeded = True
                except Exception as exc:
                    log.warning("[MEMORY] Profile lookup unavailable: %s", type(exc).__name__)
                    fact_reply = None
                if fact_reply:
                    final = _prepare_msg(_prefix_for_llm_reply(username) + fact_reply)
                    self._send_memory_reply(final, reply_receipt)
                    try:
                        log_interaction(username, msg, fact_reply)
                    except Exception:
                        pass
                    return
                if profile_lookup_succeeded:
                    missing_reply = profiles.missing_answer_for_topics(
                        requested_profile_topics,
                    )
                    if missing_reply:
                        final = _prepare_msg(
                            _prefix_for_llm_reply(username) + missing_reply,
                        )
                        self._send_memory_reply(final, reply_receipt)
                        try:
                            log_interaction(username, msg, missing_reply)
                        except Exception:
                            pass
                        return

            memory_context = ""
            source_list_recall = False
            # Never fall back to llm_brain's old shared Twitch history.
            scoped_history = []
            conversation_lookup_succeeded = False
            if conversation is not None:
                try:
                    scoped_history = conversation.history_for(channel_key, viewer_key, query=msg)
                    memory_context, source_list_recall = conversation.context_for(
                        channel_key, viewer_key, msg, return_source_match=True,
                    )
                    conversation_lookup_succeeded = True
                except Exception as exc:
                    log.warning("[MEMORY] Conversation lookup unavailable: %s", type(exc).__name__)
                    scoped_history = []
                    memory_context = ""
                    source_list_recall = False
            if (conversation_lookup_succeeded and explicit_personal_run_query
                    and not source_list_recall):
                missing_reply = "I don't have a saved sequence for that run."
                final = _prepare_msg(
                    _prefix_for_llm_reply(username) + missing_reply,
                )
                self._send_memory_reply(final, reply_receipt)
                try:
                    log_interaction(username, msg, missing_reply)
                except Exception:
                    pass
                return
            if profiles is not None and stable_viewer and profile_channel:
                try:
                    profile_context = profiles.format_profile_context(
                        profile_channel, stable_viewer, query=msg,
                    )
                    if profile_context:
                        memory_context = profile_context + "\n" + memory_context
                except Exception as exc:
                    log.warning("[MEMORY] Profile context unavailable: %s", type(exc).__name__)
            if reply_receipt is not None and not self._memory_receipt_current(reply_receipt):
                return
            log.debug("[DEBUG] Calling generate_response(user=%s)", username)
            reply = generate_response(
                msg, _username_for_model(username),
                history=scoped_history,
                search_context="",
                memory_context=memory_context,
            )
            had_reply = bool(reply)
            protected_names = set()
            if observation is not None and observation.fact_key == "real_name" \
                    and observation.value:
                protected_names.add(observation.value)
            if reply and profiles is not None and stable_viewer and profile_channel:
                try:
                    name_fact = profiles.get_facts(profile_channel, stable_viewer).get("real_name")
                    if name_fact:
                        protected_names.add(name_fact.value)
                except Exception as exc:
                    log.warning("[MEMORY] Real-name address guard unavailable: %s", type(exc).__name__)
            if reply:
                try:
                    from MedlarTV.core.viewer_profiles import _clean_message, _real_name
                    declared_name = _real_name(_clean_message(msg))
                    if declared_name:
                        protected_names.add(declared_name)
                except Exception:
                    pass
                for real_name in protected_names:
                    reply = self._use_twitch_handle(reply, username, real_name)
            if had_reply and not reply:
                reply = "Noted."
            log.debug("[DEBUG] generate_response() returned text=%s", bool(reply))

            if reply:
                composed = _sanitize_reply_text(reply, _username_for_model(username)) or "Noted."
                final = _prepare_msg(_prefix_for_llm_reply(username) + composed)
                self._send_memory_reply(final, reply_receipt)
                try:
                    log_interaction(username, msg, composed if composed else reply)
                except Exception:
                    pass

        # viewer_arrived now handled via JOIN and gated by _arrived_users

    # ------------------- COMMAND HANDLING ----------------------
    def _handle_command(self, username: str, full: str, ctx: Dict[str, Any], tags: Dict[str, Any]):
        parts = full.split(" ", 1)
        cmd = parts[0].lstrip("!").lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd in {"forget", "forgetme"}:
            source_channel, channel_key, stable_viewer, viewer_key = self._memory_identity(username, ctx)
            if source_channel != CHANNEL.lower():
                # Replies still target the configured primary channel.
                return
            requested_fact = args.strip().casefold()
            forget_all = cmd == "forgetme" or requested_fact in {
                "all", "everything", "all my facts", "everything about me",
            }
            if not forget_all and not requested_fact:
                self.send_message(_prepare_msg(
                    f"@{username} Use !forget <fact> or !forgetme to clear everything."
                ))
                return
            try:
                with self._memory_lock:
                    self._conversation_store().forget_viewer(channel_key, viewer_key)
                    profile_channel = str(ctx.get("room_id") or "").strip()
                    if stable_viewer and profile_channel:
                        removed = self._profile_store().forget(
                            profile_channel, stable_viewer,
                            None if forget_all else requested_fact,
                        )
                if stable_viewer and profile_channel:
                    if forget_all:
                        text = "I cleared your saved facts and this chat's memory."
                    elif removed:
                        text = "I removed that saved fact and cleared this chat's memory."
                    else:
                        text = "I couldn't find that saved fact; I cleared this chat's memory."
                else:
                    text = "I cleared this chat's memory, but Twitch did not provide stable IDs for saved facts."
            except Exception as exc:
                log.warning("[MEMORY] Forget unavailable: %s", type(exc).__name__)
                text = "I couldn't clear that memory right now. Please try again later."
            self.send_message(_prepare_msg(f"@{username} {text}"))
            return

        from MedlarTV.command_authority import command_authority, normalize_login
        try:
            copilots = _get_active_copilots()
        except Exception:
            copilots = []
        authority = command_authority(username, ctx, tags, CHANNEL, copilots)
        role = authority.role
        ctx = dict(ctx) if isinstance(ctx, dict) else {}
        ctx["command_authority"] = authority

        if cmd in {"addcopilot", "removecopilot", "listcopilots"}:
            if cmd != "listcopilots" and not authority.permits_primary_write(username, role):
                self.send_message(f"@{username} This command requires verified primary-channel identity.")
                return
            if cmd == "addcopilot":
                target = normalize_login(args, mention=True)
                if role not in {"pilot", "mod"}:
                    self.send_message(f"@{username} ❌ Only Broadcaster and Mods can add co-pilots.")
                    return
                if not target:
                    self.send_message(f"@{username} ❌ Usage: !addcopilot <username>")
                    return
                ok = _add_copilot(target)
                if ok:
                    self.send_message(f"Co-Pilot {target} registered! Welcome to the squad! 🚀")
                else:
                    self.send_message(f"@{username} ⚠️ {target} is already a Co-Pilot.")
                return
            if cmd == "removecopilot":
                target = normalize_login(args, mention=True)
                if role not in {"pilot", "mod", "copilot"}:
                    self.send_message(f"@{username} ❌ Only Broadcaster, Mods, or Co-Pilots can remove co-pilots.")
                    return
                if not target:
                    self.send_message(f"@{username} ❌ Usage: !removecopilot <username>")
                    return
                ok = _remove_copilot(target)
                if ok:
                    self.send_message(f"Co-Pilot {target} unregistered. Thanks for flying with us! o7")
                else:
                    self.send_message(f"@{username} ⚠️ {target} is not currently a Co-Pilot.")
                return
            if cmd == "listcopilots":
                names = _get_active_copilots()
                listing = ", ".join(names) if names else "none"
                self.send_message(f"Active Co-Pilots: {listing}")
                return

        if cmd in {"pokecatch", "pokeshop"} and username.lower() != BOT_NICK.lower() and is_enabled("ignore_viewer_pokecatch"):
            return

        response = execute_command(cmd, username, role, args, ctx)

        # An unregistered bang command is still a command attempt, not chat.
        if response is None:
            return

        if response:
            try:
                if cmd == "roulette" and ("BANG" in response or response.startswith("💥")):
                    try:
                        from MedlarTV.core.moderation import execute_timeout
                        execute_timeout(
                            self._socket_for_current_work(),
                            CHANNEL,
                            username,
                            30,
                            "roulette",
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            final = _prepare_msg(_role_prefix(username) + response)
            self.send_message(final)
            try:
                log_interaction(username, full, response)
            except Exception:
                pass


# ----------------------------------------------------------------------------
# THREAD CONTROL
# ----------------------------------------------------------------------------

_listener_instance: Optional[TwitchIRC] = None
_listener_thread: Optional[threading.Thread] = None
_listener_lock = threading.RLock()


def start_listener():
    global _listener_instance, _listener_thread

    with _listener_lock:
        if _listener_thread is not None and _listener_thread.is_alive():
            log.info("[Listener] Already running.")
            return
        if _listener_instance is not None:
            _listener_instance.stop()
            if _listener_instance._has_live_background_workers():
                raise RuntimeError(
                    "Previous Twitch listener workers are still shutting down"
                )

        try:
            _arrived_users.clear()
            _last_audio_play.clear()
            log.info("[Listener] Session state cleared (arrivals, audio)")
        except Exception:
            pass

        _listener_instance = TwitchIRC()
        _listener_thread = threading.Thread(
            target=_listener_instance.listen,
            name="TwitchIRC-manager",
            daemon=True,
        )
        _listener_thread.start()

    log.info("[Listener] Twitch listener manager started; awaiting IRC verification.")


def stop_listener():
    global _listener_instance, _listener_thread
    with _listener_lock:
        instance = _listener_instance
        manager = _listener_thread
    if instance is None:
        return True
    deadline = time.monotonic() + 10.0
    instance.stop()
    if manager is not None and manager is not threading.current_thread():
        manager.join(max(0.0, deadline - time.monotonic()))
    # Never close a profile store while in-flight work still uses it.
    while instance._has_live_background_workers() and time.monotonic() < deadline:
        time.sleep(0.05)
    manager_alive = manager is not None and manager.is_alive()
    workers_alive = instance._has_live_background_workers()
    store_closed = True
    if not manager_alive and not workers_alive:
        store = getattr(instance, "_viewer_profiles", None)
        if store is not None:
            try:
                store.close()
                instance._viewer_profiles = None
            except Exception as exc:
                store_closed = False
                log.warning("[Listener] Profile store close failed: %s", type(exc).__name__)
    with _listener_lock:
        if not manager_alive and not workers_alive and store_closed:
            if _listener_instance is instance:
                _listener_instance = None
            if _listener_thread is manager:
                _listener_thread = None
    if manager_alive or workers_alive:
        log.warning("[Listener] Stop requested; background work is still exiting.")
    elif not store_closed:
        log.warning("[Listener] Stop requested; profile store cleanup is incomplete.")
    else:
        log.info("[Listener] Stopped.")
    return not manager_alive and not workers_alive and store_closed

def trigger_event_for_testing(username: str) -> None:
    class Dummy:
        def send(self, b):
            print(b.decode())
    sock = Dummy()
    def send_msg(m):
        print(f"CHAT_SEND: {m}")
    _apply_event_rules("viewer_arrived", username, send_msg, sock)

_arrived_users: set[str] = set()
_audio_lock = threading.Lock()
_last_audio_play: dict[str, float] = {}
