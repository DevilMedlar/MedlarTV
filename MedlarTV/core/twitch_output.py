"""Twitch-only output policy snapshots; no incoming or desktop moderation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

import yaml


NEUTRAL_FALLBACK = "Tiny victory time: what's one good thing from today?"


class PolicyError(ValueError):
    """Missing or invalid outgoing policy. Messages must be withheld."""


def normalize_chat(text: str) -> str:
    if not isinstance(text, str):
        raise PolicyError("invalid_text")
    return " ".join(text.replace("\x00", "").split()).encode(
        "utf-8", errors="replace",
    ).decode("utf-8")


def _comparison(text: str) -> str:
    # Preserve the existing literal ASCII matching contract, with stable spacing.
    return " ".join(re.sub(r"[^a-z0-9\s]", "", text.lower()).split())


def _mapping(value):
    if not isinstance(value, dict):
        raise PolicyError("invalid_mapping")
    return value


def _bool(mapping, key, default=True):
    value = mapping.get(key, default)
    if type(value) is not bool:
        raise PolicyError("invalid_boolean")
    return value


def _integer(mapping, key, default, minimum=0):
    value = mapping.get(key, default)
    if type(value) is not int or not minimum <= value <= 10000:
        raise PolicyError("invalid_integer")
    return value


def _strings(mapping, key, *, required=False, comparable=False):
    if required and key not in mapping:
        raise PolicyError("missing_blocklist")
    values = mapping.get(key, [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip()
        or (comparable and not _comparison(value)) for value in values
    ):
        raise PolicyError("invalid_list")
    return values


def validate_config(config):
    config = _mapping(config)
    for key in ("blocked_words", "blocked_topics"):
        _strings(config, key, required=True, comparable=True)
    advanced = _mapping(config.get("advanced", {}))
    for key in ("check_blocked_words", "check_blocked_topics", "apply_emoji_limit",
                "enforce_message_length", "skip_censored_words"):
        _bool(advanced, key)
    _integer(advanced, "min_blocked_word_length", 4, 1)
    modes = _mapping(config.get("response_modes", {}))
    caps = _mapping(modes.get("all_caps", {}))
    emoji = _mapping(modes.get("emoji_limit", {}))
    _bool(caps, "enabled")
    _bool(emoji, "enabled")
    _integer(caps, "max_messages", 3, 1)
    _integer(emoji, "max_per_message", 3)
    for key in ("positive_triggers", "negative_phrases"):
        _strings(caps, key)
    safety = _mapping(config.get("safety", {}))
    _integer(safety, "max_message_length", 500, 1)
    _strings(safety, "protected_users")
    return config


@dataclass(frozen=True)
class TwitchPolicy:
    enabled: bool
    llm_enabled: bool
    revision: str
    config: dict

    def permits(self, text: str) -> bool:
        """Pure validation, including after formatting/truncation. No state changes."""
        text = normalize_chat(text)
        if not text:
            return False
        if not self.enabled:
            return True
        normalized = _comparison(text)
        advanced = self.config.get("advanced", {})
        if advanced.get("check_blocked_words", True):
            minimum = advanced.get("min_blocked_word_length", 4)
            for word in self.config["blocked_words"]:
                if "*" in word and advanced.get("skip_censored_words", True):
                    continue
                word = _comparison(word)
                if len(word) >= minimum and re.search(r"\b" + re.escape(word) + r"\b", normalized):
                    return False
        if advanced.get("check_blocked_topics", True):
            if any(_comparison(topic) in normalized for topic in self.config["blocked_topics"]):
                return False
        return True

    def format_body(self, text: str) -> str:
        text = normalize_chat(text)
        if not self.enabled:
            return text
        # Optional snapshots leave all existing non-Twitch callers unchanged.
        from MedlarTV.core.content_filter import apply_all_caps_mode, limit_emojis
        advanced = self.config.get("advanced", {})
        if advanced.get("apply_emoji_limit", True):
            text = limit_emojis(text, config=self.config)
        if self.config.get("response_modes", {}).get("all_caps", {}).get("enabled", True):
            text = apply_all_caps_mode(text, config=self.config)
        if advanced.get("enforce_message_length", True):
            maximum = self.config.get("safety", {}).get("max_message_length", 500)
            if len(text) > maximum:
                from MedlarTV.core.text_clusters import truncate_clusters
                text = truncate_clusters(text, maximum, marker="." * min(3, maximum))
        return normalize_chat(text)


def load_policy(settings_file: str | Path) -> TwitchPolicy:
    """Reload one coherent policy using the GUI/settings active config directory."""
    try:
        settings_file = Path(settings_file)
        # Match runtime fallback decisions, while memory remains a fresh reader.
        from MedlarTV.config_state import FEATURE_STATE
        settings, settings_revision, status = FEATURE_STATE.read(settings_file)
        if not status.ready:
            raise PolicyError("settings_unavailable")
        settings_bytes = settings_revision.encode("ascii")
        enabled = _bool(settings, "content_filter")
        llm_enabled = _bool(settings, "llm_brain")
        config_bytes = b""
        config = {}
        if enabled:
            config_bytes = (settings_file.parent / "content_filter.yaml").read_bytes()
            config = validate_config(yaml.safe_load(config_bytes))
        revision = hashlib.sha256(settings_bytes + b"\x00" + config_bytes).hexdigest()[:16]
        return TwitchPolicy(enabled, llm_enabled, revision, config)
    except Exception:
        # Neither parser errors nor file paths/content escape into diagnostics.
        raise PolicyError("policy_unavailable") from None
