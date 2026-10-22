"""
MedlarTV Stream Management (fixed tokens & headers)
---------------------------------------------------
Uses:
  - APP_TWITCH_CLIENT_ID + APP_SECRET_ID for APP access token
  - DEVILMEDLAR_TWITCH_CLIENT_ID + DEVILMEDLAR_TWITCH_TOKEN for
    broadcaster actions (title/category updates)
Matches the behaviour of test_api_direct.py you ran.
"""

from __future__ import annotations

import os
import time
import logging
from typing import Optional, Dict, Any

import requests

log = logging.getLogger("stream")
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
if DEBUG:
    print("[DEBUG stream_management] Loaded stream_management.py")

TWITCH_AUTH_BASE = "https://id.twitch.tv/oauth2"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

# --- ENV / CONFIG -----------------------------------------------------------

APP_CLIENT_ID = os.getenv("APP_TWITCH_CLIENT_ID", "").strip()
APP_CLIENT_SECRET = os.getenv("APP_SECRET_ID", "").strip()

BROADCASTER_CLIENT_ID = os.getenv("DEVILMEDLAR_TWITCH_CLIENT_ID", "").strip()
BROADCASTER_TOKEN_RAW = os.getenv("DEVILMEDLAR_TWITCH_TOKEN", "").strip()

if DEBUG:
    print(f"[DEBUG stream_management] ENV loaded: "
          f"APP_CLIENT_ID_set={bool(APP_CLIENT_ID)} "
          f"APP_CLIENT_SECRET_set={bool(APP_CLIENT_SECRET)} "
          f"BROADCASTER_CLIENT_ID_set={bool(BROADCASTER_CLIENT_ID)} "
          f"BROADCASTER_TOKEN_RAW_set={bool(BROADCASTER_TOKEN_RAW)}")

# Strip leading "oauth:" if present
if BROADCASTER_TOKEN_RAW.startswith("oauth:"):
    BROADCASTER_TOKEN = BROADCASTER_TOKEN_RAW.split("oauth:", 1)[1]
    if DEBUG:
        print("[DEBUG stream_management] Stripped 'oauth:' prefix from BROADCASTER_TOKEN_RAW")
else:
    BROADCASTER_TOKEN = BROADCASTER_TOKEN_RAW
    if DEBUG:
        print("[DEBUG stream_management] BROADCASTER_TOKEN_RAW used as-is (no 'oauth:' prefix)")

CHANNEL_NAME = os.getenv("TWITCH_CHANNEL", "#devilmedlar").lstrip("#")
if DEBUG:
    print(f"[DEBUG stream_management] CHANNEL_NAME set={bool(CHANNEL_NAME)}")

# --- INTERNAL STATE ---------------------------------------------------------

_app_token: Optional[str] = None
_app_token_expiry: float = 0.0
_broadcaster_id: Optional[str] = None


# --- HELPERS ----------------------------------------------------------------

def _safe_exception_class(exception):
    name = type.__dict__["__name__"].__get__(type(exception))
    return name if len(name) <= 64 and name in {
        "SSLError", "SSLCertVerificationError", "CertificateError", "MaxRetryError",
        "ProxyError", "ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout",
        "TimeoutError", "OSError", "ValueError", "TypeError", "KeyError",
        "AttributeError", "JSONDecodeError", "RequestException", "RuntimeError",
    } else "UnknownError"


def _ssl_exception_details(exception):
    """Inspect at most eight exception nodes; never format messages or arguments."""
    reasons = {
        "CERTIFICATE_VERIFY_FAILED", "WRONG_VERSION_NUMBER", "UNSUPPORTED_PROTOCOL",
        "UNEXPECTED_EOF_WHILE_READING", "EOF_OCCURRED_IN_VIOLATION_OF_PROTOCOL",
        "SSLV3_ALERT_HANDSHAKE_FAILURE", "TLSV1_ALERT_UNKNOWN_CA",
        "TLSV1_ALERT_PROTOCOL_VERSION", "TLSV1_ALERT_INTERNAL_ERROR",
        "TLSV1_ALERT_BAD_CERTIFICATE", "TLSV13_ALERT_CERTIFICATE_REQUIRED",
        "NO_SHARED_CIPHER", "NO_CIPHERS_AVAILABLE", "DECRYPTION_FAILED_OR_BAD_RECORD_MAC",
    }
    classes, symbols, libraries, verification_codes, error_codes = set(), set(), set(), set(), set()
    pending, seen, has_ssl = [exception], set(), False
    while pending and len(seen) < 8:
        current = pending.pop()
        if not issubclass(type(current), BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        name = _safe_exception_class(current)
        classes.add(name)
        has_ssl |= name in {"SSLError", "SSLCertVerificationError", "CertificateError"}
        # Native descriptors bypass user-defined getters; dictionary fields cannot
        # invoke properties. String arguments are neither inspected nor formatted.
        attributes = BaseException.__dict__["__dict__"].__get__(current)
        if type(attributes) is not dict:
            attributes = {}
        reason = attributes.get("reason")
        if type(reason) is str and len(reason) <= 64 and reason in reasons:
            symbols.add(reason)
            has_ssl = True
        library = attributes.get("library")
        if type(library) is str and len(library) <= 4 and library in {"SSL", "X509", "PEM", "ASN1", "BIO"}:
            libraries.add(library)
        code = attributes.get("verify_code")
        if type(code) is int and 0 <= code <= 65535:
            verification_codes.add(code)
        code = OSError.errno.__get__(current) if issubclass(type(current), OSError) else attributes.get("errno")
        if type(code) is int and 0 <= code <= 65535:
            error_codes.add(code)
        children = [BaseException.__cause__.__get__(current),
                    BaseException.__context__.__get__(current), reason,
                    attributes.get("original_error")]
        arguments = BaseException.args.__get__(current)
        if type(arguments) is tuple:
            children.extend(arguments[:4])
        pending.extend(child for child in children if issubclass(type(child), BaseException)
                       and id(child) not in seen)
    if not has_ssl:
        return None
    return " ".join(
        key + "=" + (",".join(str(value) for value in sorted(values)) if values else "unknown")
        for key, values in (("classes", classes), ("reason", symbols), ("library", libraries),
                            ("verify_code", verification_codes), ("error_code", error_codes))
    )


def _log_verification_stage(stage, outcome, *, status=None, exception=None):
    """Record only fixed stages/outcomes, numeric status and exception class."""
    if stage not in {"broadcaster_validation", "broadcaster_scope", "app_token",
                     "channel_lookup", "complete"} or outcome not in {"ok", "failed"}:
        return
    detail = ""
    if type(status) is int:
        detail += f" http_status={status}"
    if exception is not None:
        detail += f" exception={_safe_exception_class(exception)}"
    log.info("[TwitchVerify] stage=%s outcome=%s%s", stage, outcome, detail)
    if exception is not None:
        tls = _ssl_exception_details(exception)
        if tls is not None:
            log.info("[TwitchTLS] stage=%s %s", stage, tls)


def _ensure_app_token() -> Optional[str]:
    """
    Get (and cache) an APP access token using client_credentials.

    Uses APP_TWITCH_CLIENT_ID + APP_SECRET_ID exactly like test_api_direct.py.
    """
    if DEBUG:
        print("[DEBUG stream_management] _ensure_app_token() called")
    global _app_token, _app_token_expiry

    now = time.time()
    if DEBUG:
        print(f"[DEBUG stream_management] _app_token_set={bool(_app_token)} "
              f"now={now} expiry={_app_token_expiry}")

    if _app_token and now < _app_token_expiry - 60:
        if DEBUG:
            print("[DEBUG stream_management] Using cached APP token")
        _log_verification_stage("app_token", "ok")
        return _app_token

    if not APP_CLIENT_ID or not APP_CLIENT_SECRET:
        if DEBUG:
            print("[DEBUG stream_management] Missing APP_CLIENT_ID or APP_CLIENT_SECRET")
        log.error("[Stream] Missing APP_TWITCH_CLIENT_ID or APP_SECRET_ID in environment")
        _log_verification_stage("app_token", "failed")
        return None

    data = {
        "client_id": APP_CLIENT_ID,
        "client_secret": APP_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }

    if DEBUG:
        print(f"[DEBUG stream_management] Requesting new APP token at {TWITCH_AUTH_BASE}/token")
    try:
        resp = requests.post(f"{TWITCH_AUTH_BASE}/token", data=data, timeout=15)
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception while requesting app token: {_safe_exception_class(e)}")
        log.error("[Stream] Error requesting app token: %s", _safe_exception_class(e))
        _log_verification_stage("app_token", "failed", exception=e)
        return None

    if DEBUG:
        print(f"[DEBUG stream_management] App token response status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] App token failure status={resp.status_code}")
        log.error("[Stream] App token failed: status=%s", resp.status_code)
        _log_verification_stage("app_token", "failed", status=resp.status_code)
        return None

    try:
        body = resp.json()
        # Validate the whole response before publishing either cached field.
        if not isinstance(body, dict):
            raise ValueError("Invalid app-token response mapping")
        candidate = body.get("access_token")
        expires_in = body.get("expires_in", 3600)
        if not isinstance(candidate, str) or not candidate.strip() or type(expires_in) is not int or expires_in <= 0:
            raise ValueError("Invalid app-token response fields")
        new_expiry = now + expires_in
        _app_token, _app_token_expiry = candidate, new_expiry
    except Exception as exc:
        _log_verification_stage("app_token", "failed", status=resp.status_code, exception=exc)
        raise
    _log_verification_stage("app_token", "ok" if _app_token else "failed", status=resp.status_code)

    if DEBUG:
        print(f"[DEBUG stream_management] New APP token acquired: token_set={bool(_app_token)} "
              f"expires_in={expires_in}s new_expiry={_app_token_expiry}")
    log.info("[Stream] Got new APP access token")
    return _app_token


def get_access_token() -> Optional[str]:
    """
    Public helper used by !status.
    Returns current APP token or None.
    """
    if DEBUG:
        print("[DEBUG stream_management] get_access_token() called")
    token = _ensure_app_token()
    if DEBUG:
        print(f"[DEBUG stream_management] get_access_token() returning token_set={bool(token)}")
    return token


def _app_headers(token: Optional[str] = None) -> Dict[str, str]:
    if DEBUG:
        print("[DEBUG stream_management] _app_headers() called")
    tok = token or _ensure_app_token()
    if DEBUG:
        print(f"[DEBUG stream_management] _app_headers() tok_set={bool(tok)}")
    if not tok:
        return {}
    headers = {
        "Client-ID": APP_CLIENT_ID,
        "Authorization": f"Bearer {tok}",
    }
    if DEBUG:
        print(f"[DEBUG stream_management] _app_headers() built headers with Client-ID_set={bool(APP_CLIENT_ID)}")
    return headers


def _broadcaster_headers() -> Dict[str, str]:
    if DEBUG:
        print("[DEBUG stream_management] _broadcaster_headers() called")
    if not BROADCASTER_CLIENT_ID or not BROADCASTER_TOKEN:
        if DEBUG:
            print("[DEBUG stream_management] Missing BROADCASTER_CLIENT_ID or BROADCASTER_TOKEN")
        return {}
    headers = {
        "Client-ID": BROADCASTER_CLIENT_ID,
        "Authorization": f"Bearer {BROADCASTER_TOKEN}",
        "Content-Type": "application/json",
    }
    if DEBUG:
        print(f"[DEBUG stream_management] _broadcaster_headers() built headers with "
              f"Client-ID_set={bool(BROADCASTER_CLIENT_ID)} token_set={bool(BROADCASTER_TOKEN)}")
    return headers


# --- BROADCASTER / CHANNEL LOOKUPS -----------------------------------------

def get_broadcaster_id() -> Optional[str]:
    """Get numeric broadcaster_id for CHANNEL_NAME via APP token."""
    if DEBUG:
        print("[DEBUG stream_management] get_broadcaster_id() called")
    global _broadcaster_id

    if _broadcaster_id:
        if DEBUG:
            print("[DEBUG stream_management] Using cached broadcaster id")
        _log_verification_stage("channel_lookup", "ok")
        return _broadcaster_id

    token = _ensure_app_token()
    if DEBUG:
        print(f"[DEBUG stream_management] get_broadcaster_id() token_set={bool(token)}")
    if not token:
        _log_verification_stage("channel_lookup", "failed")
        return None

    headers = _app_headers(token)
    params = {"login": CHANNEL_NAME}
    if DEBUG:
        print("[DEBUG stream_management] Requesting /users for channel login")

    try:
        resp = requests.get(f"{TWITCH_API_BASE}/users", headers=headers, params=params, timeout=15)
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception fetching broadcaster id: {_safe_exception_class(e)}")
        log.error("[Stream] Error fetching broadcaster id: %s", _safe_exception_class(e))
        _log_verification_stage("channel_lookup", "failed", exception=e)
        return None

    if DEBUG:
        print(f"[DEBUG stream_management] /users status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] /users error status={resp.status_code}")
        log.error("[Stream] Failed to get broadcaster id: status=%s", resp.status_code)
        _log_verification_stage("channel_lookup", "failed", status=resp.status_code)
        return None

    try:
        data = resp.json().get("data", [])
        if DEBUG:
            print(f"[DEBUG stream_management] /users returned {len(data)} records")
    except Exception as exc:
        _log_verification_stage("channel_lookup", "failed", status=resp.status_code, exception=exc)
        raise
    if not data:
        log.error("[Stream] No user data returned for channel")
        _log_verification_stage("channel_lookup", "failed", status=resp.status_code)
        return None

    try:
        candidate = data[0].get("id") if isinstance(data[0], dict) else None
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("Invalid broadcaster identity response")
        _broadcaster_id = candidate
    except Exception as exc:
        _log_verification_stage("channel_lookup", "failed", status=resp.status_code, exception=exc)
        raise
    _log_verification_stage("channel_lookup", "ok" if _broadcaster_id else "failed", status=resp.status_code)
    if DEBUG:
        print("[DEBUG stream_management] broadcaster id resolved")
    log.info("[Stream] Broadcaster id resolved")
    return _broadcaster_id


def get_channel_info() -> Optional[Dict[str, Any]]:
    """Return /channels info for the broadcaster (title, game, tags, etc.)."""
    if DEBUG:
        print("[DEBUG stream_management] get_channel_info() called")
    token = _ensure_app_token()
    bid = get_broadcaster_id()
    if DEBUG:
        print(f"[DEBUG stream_management] get_channel_info() token_set={bool(token)} broadcaster_id_set={bool(bid)}")
    if not (token and bid):
        return None

    headers = _app_headers(token)
    params = {"broadcaster_id": bid}
    if DEBUG:
        print("[DEBUG stream_management] Requesting /channels")

    try:
        resp = requests.get(f"{TWITCH_API_BASE}/channels", headers=headers, params=params, timeout=15)
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception fetching channel info: {type(e).__name__}")
        log.error("[Stream] Error fetching channel info: %s", type(e).__name__)
        return None

    if DEBUG:
        print(f"[DEBUG stream_management] /channels status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] /channels error status={resp.status_code}")
        log.error("[Stream] Failed to get channel info: status=%s", resp.status_code)
        return None

    items = resp.json().get("data", [])
    if DEBUG:
        print(f"[DEBUG stream_management] /channels returned {len(items)} records")
    return items[0] if items else None


def get_stream_info() -> Optional[Dict[str, Any]]:
    """Return /streams info (live status, viewer count, etc.)."""
    if DEBUG:
        print("[DEBUG stream_management] get_stream_info() called")
    token = _ensure_app_token()
    bid = get_broadcaster_id()
    if DEBUG:
        print(f"[DEBUG stream_management] get_stream_info() token_set={bool(token)} broadcaster_id_set={bool(bid)}")
    if not (token and bid):
        return None

    headers = _app_headers(token)
    params = {"user_id": bid}
    if DEBUG:
        print("[DEBUG stream_management] Requesting /streams")

    try:
        resp = requests.get(f"{TWITCH_API_BASE}/streams", headers=headers, params=params, timeout=15)
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception fetching stream info: {type(e).__name__}")
        log.error("[Stream] Error fetching stream info: %s", type(e).__name__)
        return None

    if DEBUG:
        print(f"[DEBUG stream_management] /streams status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] /streams error status={resp.status_code}")
        log.error("[Stream] Failed to get stream info: status=%s", resp.status_code)
        return None

    items = resp.json().get("data", [])
    if DEBUG:
        print(f"[DEBUG stream_management] /streams returned {len(items)} records")
    return items[0] if items else None

# --- USER LOOKUP & SHOUTOUT -------------------------------------------------

def get_user_id(login: str) -> Optional[str]:
    """Resolve a user's numeric id by login using APP token."""
    if DEBUG:
        print("[DEBUG stream_management] get_user_id() called")
    token = _ensure_app_token()
    if DEBUG:
        print(f"[DEBUG stream_management] get_user_id() token_set={bool(token)}")
    if not token:
        return None
    headers = _app_headers(token)
    params = {"login": login}
    if DEBUG:
        print("[DEBUG stream_management] Requesting /users for login")
    try:
        resp = requests.get(f"{TWITCH_API_BASE}/users", headers=headers, params=params, timeout=15)
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG stream_management] Exception fetching user id: {type(e).__name__}")
        log.error("[Stream] Error fetching user id: %s", type(e).__name__)
        return None
    if DEBUG:
        print(f"[DEBUG stream_management] /users status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] /users error status={resp.status_code}")
        log.error("[Stream] Failed to get user id: status=%s", resp.status_code)
        return None
    data = resp.json().get("data", [])
    if DEBUG:
        print(f"[DEBUG stream_management] /users returned {len(data)} records")
    return (data[0]["id"] if data else None)

def send_shoutout(target_login: str) -> bool:
    """Send a Helix Shoutout using broadcaster token and required scope."""
    try:
        log.info("[Stream] send_shoutout requested")
        from_id = get_broadcaster_id()
        to_id = get_user_id(target_login)
        if DEBUG:
            print(f"[DEBUG stream_management] send_shoutout() ids_set={bool(from_id) and bool(to_id)}")
        if not (from_id and to_id):
            log.error("[Stream] Missing ids for shoutout")
            return False
        headers = _broadcaster_headers()
        if DEBUG:
            print(f"[DEBUG stream_management] send_shoutout() headers_set={bool(headers)}")
        if not headers:
            log.error("[Stream] Missing broadcaster headers (client id/token)")
            return False
        params = {
            "from_broadcaster_id": from_id,
            "to_broadcaster_id": to_id,
            "moderator_id": from_id,
        }
        if DEBUG:
            print("[DEBUG stream_management] POST /chat/shoutouts")
        try:
            resp = requests.post(f"{TWITCH_API_BASE}/chat/shoutouts", headers=headers, params=params, timeout=15)
        except Exception as e:
            if DEBUG:
                print(f"[DEBUG stream_management] Exception sending shoutout: {type(e).__name__}")
            log.error("[Stream] Error sending shoutout: %s", type(e).__name__)
            return False
        if DEBUG:
            print(f"[DEBUG stream_management] send_shoutout() status={resp.status_code}")
        if 200 <= resp.status_code < 300:
            log.info("[Stream] Helix shoutout success status=%s", resp.status_code)
            return True
        if DEBUG:
            print(f"[DEBUG stream_management] send_shoutout() error status={resp.status_code}")
        log.error("[Stream] Helix shoutout failed status=%s", resp.status_code)
        return False
    except Exception:
        return False


def search_game(game_name: str) -> Optional[str]:
    """Search game/category by name and return its game_id."""
    if DEBUG:
        print(f"[DEBUG stream_management] search_game() name_length={len(game_name)}")
    token = _ensure_app_token()
    if DEBUG:
        print(f"[DEBUG stream_management] search_game() token_set={bool(token)}")
    if not token:
        return None

    headers = _app_headers(token)
    params = {"query": game_name, "first": 1}
    if DEBUG:
        print("[DEBUG stream_management] Requesting /search/categories")

    try:
        resp = requests.get(f"{TWITCH_API_BASE}/search/categories", headers=headers, params=params, timeout=15)
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception searching game: {type(e).__name__}")
        log.error("[Stream] Error searching game: %s", type(e).__name__)
        return None

    if DEBUG:
        print(f"[DEBUG stream_management] /search/categories status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] /search/categories error status={resp.status_code}")
        log.error("[Stream] Failed to search game: status=%s", resp.status_code)
        return None

    data = resp.json().get("data", [])
    if DEBUG:
        print(f"[DEBUG stream_management] /search/categories returned {len(data)} records")
    if not data:
        return None

    game_id = data[0]["id"]
    if DEBUG:
        print(f"[DEBUG stream_management] search_game() game_id_set={bool(game_id)}")
    return game_id


def get_top_games(limit: int = 5) -> Optional[list[Dict[str, Any]]]:
    """Optional helper: /games/top"""
    if DEBUG:
        print(f"[DEBUG stream_management] get_top_games() called limit={limit}")
    token = _ensure_app_token()
    if DEBUG:
        print(f"[DEBUG stream_management] get_top_games() token_set={bool(token)}")
    if not token:
        return None

    headers = _app_headers(token)
    params = {"first": limit}
    if DEBUG:
        print(f"[DEBUG stream_management] Requesting /games/top first={limit}")

    try:
        resp = requests.get(f"{TWITCH_API_BASE}/games/top", headers=headers, params=params, timeout=15)
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception fetching top games: {type(e).__name__}")
        log.error("[Stream] Error fetching top games: %s", type(e).__name__)
        return None

    if DEBUG:
        print(f"[DEBUG stream_management] /games/top status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] /games/top error status={resp.status_code}")
        log.error("[Stream] Failed to get top games: status=%s", resp.status_code)
        return None

    data = resp.json().get("data", [])
    if DEBUG:
        print(f"[DEBUG stream_management] /games/top returned {len(data)} records")
    return data


# --- MUTATING OPERATIONS (TITLE / CATEGORY) ---------------------------------

def update_stream_title(new_title: str) -> bool:
    """
    Update stream title using BROADCASTER TOKEN.

    This mirrors your direct test:
      Client-ID: DEVILMEDLAR_TWITCH_CLIENT_ID
      Authorization: Bearer DEVILMEDLAR_TWITCH_TOKEN (no 'oauth:')
    """
    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_title() title_length={len(new_title)}")
    bid = get_broadcaster_id()
    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_title() broadcaster_id_set={bool(bid)}")
    if not bid:
        log.error("[Stream] Cannot update title: missing broadcaster id")
        return False

    headers = _broadcaster_headers()
    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_title() headers_set={bool(headers)}")
    if not headers:
        log.error("[Stream] Cannot update title: broadcaster headers missing")
        return False

    params = {"broadcaster_id": bid}
    json_body = {"title": new_title}
    if DEBUG:
        print("[DEBUG stream_management] PATCH /channels (title)")

    try:
        resp = requests.patch(
            f"{TWITCH_API_BASE}/channels",
            headers=headers,
            params=params,
            json=json_body,
            timeout=15,
        )
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception updating title: {type(e).__name__}")
        log.error("[Stream] Error updating title: %s", type(e).__name__)
        return False

    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_title() status={resp.status_code}")
    if resp.status_code in (204, 200):
        log.info("[Stream] Title updated")
        if DEBUG:
            print("[DEBUG stream_management] update_stream_title() SUCCESS")
        return True

    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_title() error status={resp.status_code}")
    log.error("[Stream] Failed to update title: status=%s", resp.status_code)
    return False


def update_stream_category(game_name: str) -> bool:
    """Update stream category/game by name."""
    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_category() name_length={len(game_name)}")
    bid = get_broadcaster_id()
    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_category() broadcaster_id_set={bool(bid)}")
    if not bid:
        log.error("[Stream] Cannot update category: missing broadcaster id")
        return False

    game_id = search_game(game_name)
    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_category() game_id_set={bool(game_id)}")
    if not game_id:
        log.error("[Stream] Could not find game/category")
        return False

    headers = _broadcaster_headers()
    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_category() headers_set={bool(headers)}")
    if not headers:
        log.error("[Stream] Cannot update category: broadcaster headers missing")
        return False

    params = {"broadcaster_id": bid}
    json_body = {"game_id": game_id}
    if DEBUG:
        print("[DEBUG stream_management] PATCH /channels (category)")

    try:
        resp = requests.patch(
            f"{TWITCH_API_BASE}/channels",
            headers=headers,
            params=params,
            json=json_body,
            timeout=15,
        )
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception updating category: {type(e).__name__}")
        log.error("[Stream] Error updating category: %s", type(e).__name__)
        return False

    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_category() status={resp.status_code}")
    if resp.status_code in (204, 200):
        log.info("[Stream] Category updated")
        if DEBUG:
            print("[DEBUG stream_management] update_stream_category() SUCCESS")
        return True

    if DEBUG:
        print(f"[DEBUG stream_management] update_stream_category() error status={resp.status_code}")
    log.error("[Stream] Failed to update category: status=%s", resp.status_code)
    return False


# --- FORMATTING & VALIDATION HELPERS ----------------------------------------

def format_stream_info(info: Optional[Dict[str, Any]]) -> str:
    """Nicely format stream/channel info for chat."""
    if DEBUG:
        print(f"[DEBUG stream_management] format_stream_info() called info_is_none={info is None}")
    if not info:
        return "Could not retrieve stream info."

    title = info.get("title") or info.get("game_name", "Unknown")
    game = info.get("game_name", "Unknown")
    lang = info.get("broadcaster_language", "??").upper()

    formatted = f"📺 Title: {title} | 🎮 Game: {game} | 🌐 Lang: {lang}"
    if DEBUG:
        print(f"[DEBUG stream_management] format_stream_info() result_length={len(formatted)}")
    return formatted


def verify_twitch_tokens() -> bool:
    """
    Called at startup: make sure broadcaster token is valid and has the right scopes,
    and that we can resolve broadcaster id with APP credentials.
    """
    if DEBUG:
        print("[DEBUG stream_management] verify_twitch_tokens() called")
    ok = True

    if not BROADCASTER_TOKEN:
        if DEBUG:
            print("[DEBUG stream_management] BROADCASTER_TOKEN missing")
        log.error("[Stream] Broadcaster token is missing")
        _log_verification_stage("broadcaster_validation", "failed")
        _log_verification_stage("complete", "failed")
        return False

    # Validate broadcaster token with Twitch
    try:
        headers = {"Authorization": f"OAuth {BROADCASTER_TOKEN}"}
        if DEBUG:
            print(f"[DEBUG stream_management] Validating broadcaster token at {TWITCH_AUTH_BASE}/validate")
        resp = requests.get(f"{TWITCH_AUTH_BASE}/validate", headers=headers, timeout=15)
    except Exception as e:  # pragma: no cover
        if DEBUG:
            print(f"[DEBUG stream_management] Exception validating broadcaster token: {_safe_exception_class(e)}")
        log.error("[Stream] Error validating broadcaster token: %s", _safe_exception_class(e))
        _log_verification_stage("broadcaster_validation", "failed", exception=e)
        _log_verification_stage("complete", "failed")
        return False

    if DEBUG:
        print(f"[DEBUG stream_management] /validate status={resp.status_code}")
    if resp.status_code != 200:
        if DEBUG:
            print(f"[DEBUG stream_management] /validate error status={resp.status_code}")
        log.error("[Stream] Broadcaster token validation failed: status=%s", resp.status_code)
        _log_verification_stage("broadcaster_validation", "failed", status=resp.status_code)
        ok = False
    else:
        _log_verification_stage("broadcaster_validation", "ok", status=resp.status_code)
        try:
            data = resp.json()
            scopes = data.get("scopes", [])
            if DEBUG:
                print(f"[DEBUG stream_management] /validate scope_count={len(scopes)}")
            scope_ok = "channel:manage:broadcast" in scopes
        except Exception as exc:
            _log_verification_stage("broadcaster_scope", "failed", status=resp.status_code, exception=exc)
            raise
        if not scope_ok:
            if DEBUG:
                print("[DEBUG stream_management] Missing 'channel:manage:broadcast' scope")
            log.error("[Stream] Broadcaster token is missing 'channel:manage:broadcast' scope")
            _log_verification_stage("broadcaster_scope", "failed", status=resp.status_code)
            ok = False
        else:
            _log_verification_stage("broadcaster_scope", "ok", status=resp.status_code)

    # Ensure we can fetch broadcaster id using APP token
    bid = get_broadcaster_id()
    if DEBUG:
        print(f"[DEBUG stream_management] verify_twitch_tokens() broadcaster_id_set={bool(bid)}")
    if not bid:
        ok = False

    if ok:
        log.info("[Stream] ✅ Twitch tokens verified")
        log.info("verify_twitch_tokens() -> True")
        if DEBUG:
            print("[DEBUG stream_management] verify_twitch_tokens() -> True")
    else:
        log.error("[Stream] ❌ Twitch token verification FAILED")
        if DEBUG:
            print("[DEBUG stream_management] verify_twitch_tokens() -> False")

    _log_verification_stage("complete", "ok" if ok else "failed")
    return ok
