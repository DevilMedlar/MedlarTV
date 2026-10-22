"""Small, text-preserving stores for Command Center configuration editors.

Only the fields exposed by the editors are rewritten.  YAML is parsed before
and after each edit so unsupported layouts fail without changing the file.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, cast

import yaml


# Shared validation keeps the GUI and runtime on the same feature contract.
from MedlarTV.config_contract import ConfigFormatError, FEATURE_DEFAULTS, parse_features, parse_mapping


class ConfigConflictError(ValueError):
    """The file changed after the editor loaded it."""


class FeatureSaveCleanupError(OSError):
    """The atomic save committed, but the cooperating-writer lock remains."""
    def __init__(self, revision):
        super().__init__("Feature save completed, but its save lock could not be released")
        self.revision = revision


def _read_features(path):
    raw = path.read_bytes()  # Invalid/missing files are never defaults to save.
    data, values = parse_features(raw)
    return data, values, raw.decode("utf-8-sig").splitlines(keepends=True), hashlib.sha256(raw).hexdigest(), raw


def load_features(path: str | Path) -> tuple[dict[str, bool], str]:
    """Known switches plus existing runtime defaults for omitted keys; no writes."""
    _, values, _, revision, _ = _read_features(Path(path))
    return values, revision


@contextmanager
def _feature_save_lock(path):
    """Serialize cooperating writers; an existing/stale lock fails closed."""
    lock = path.with_name(f".{path.name}.features.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ConfigConflictError("Another feature save is in progress; retry after it finishes") from None
    identity = os.fstat(descriptor)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            current = lock.stat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass


def save_features(path: str | Path, updates: dict[str, bool], expected_revision: str) -> str:
    """Change only requested switches; preserve unknown fields and file layout."""
    if not isinstance(updates, dict) or any(key not in FEATURE_DEFAULTS or type(value) is not bool
                                          for key, value in updates.items()):
        raise ValueError("Feature edits must use known switch names and boolean values")
    path = Path(path)
    updated = None
    try:
        with _feature_save_lock(path):
            data, current, lines, revision, raw = _read_features(path)
            if revision != expected_revision:
                raise ConfigConflictError("Feature configuration changed; reload and review before retrying")
            expected = deepcopy(data)
            newline = "\r\n" if b"\r\n" in raw else "\n"
            memory_changed = 'remember_users' in updates and updates['remember_users'] != current['remember_users']
            for key, value in updates.items():
                if current[key] == value:
                    continue
                if key in data:
                    _replace_inline(lines, _key_at(lines, key, 0, 0, len(lines)), value)
                else:
                    if lines and not _line_ending(lines[-1]):
                        lines[-1] += newline
                    lines.append(f"{key}: {json.dumps(value)}{newline}")
                expected[key] = value
            if memory_changed:
                # A persistent, nonsecret revision distinguishes off/on cycles
                # even if the listener is busy while both saves occur.
                # parse_features already validates this optional revision as text.
                previous = cast(str, data.get('_memory_revision', 'initial'))
                memory_revision = hashlib.sha256(
                    (previous + ':' + revision + ':' + str(updates['remember_users'])).encode('utf-8')
                ).hexdigest()
                if '_memory_revision' in data:
                    _replace_inline(lines, _key_at(lines, '_memory_revision', 0, 0, len(lines)), memory_revision)
                else:
                    if lines and not _line_ending(lines[-1]):
                        lines[-1] += newline
                    lines.append(f'_memory_revision: "{memory_revision}"{newline}')
                expected['_memory_revision'] = memory_revision
            payload = _encode(lines, raw)
            if _parse_after(payload) != expected:
                raise ConfigFormatError("Edit would alter other feature configuration")
            updated = _atomic_save_locked(path, payload, expected_revision)
    except OSError:
        if updated is not None:
            raise FeatureSaveCleanupError(updated) from None
        raise
    return updated


_KEY_LINE = re.compile(r"^( *)([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")
_LIST_LINE = re.compile(r"^( *)-[ \t]+(.*)$")
_NORMALIZE = re.compile(r"[^a-z0-9\s]")
_MAX_ENTRIES = 500


def _normalized(value: str) -> str:
    return " ".join(_NORMALIZE.sub("", value.lower()).split())


def _line_body(line: str) -> str:
    return line.rstrip("\r\n")


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _read(path: Path) -> tuple[dict[str, Any], list[str], str, bytes]:
    raw = path.read_bytes()
    revision = hashlib.sha256(raw).hexdigest()
    data = parse_mapping(raw)
    text = raw.decode("utf-8-sig")
    return data, text.splitlines(keepends=True), revision, raw


def _key_at(lines: list[str], key: str, indent: int, start: int, end: int) -> int:
    matches = []
    for index in range(start, end):
        match = _KEY_LINE.fullmatch(_line_body(lines[index]))
        if match and len(match.group(1)) == indent and match.group(2) == key:
            matches.append(index)
    if len(matches) != 1:
        raise ConfigFormatError(f"Expected one editable {key!r} key")
    return matches[0]


def _block_end(lines: list[str], start: int, indent: int) -> int:
    for index in range(start + 1, len(lines)):
        body = _line_body(lines[index])
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        if len(body) - len(body.lstrip(" ")) <= indent:
            return index
    return len(lines)


def _comment_tail(value: str) -> str:
    """Retain a scalar's trailing YAML comment, including its spacing."""
    quote = None
    escaped = False
    for index, character in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = None
        elif quote == "'":
            if character == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    continue
                quote = None
        elif character in ("'", '"'):
            quote = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            prefix = value[:index]
            return prefix[len(prefix.rstrip(" \t")):] + value[index:]
    return ""


def _one_line(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} must be 1–{limit} characters on one line")
    return value


def _list_of_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigFormatError(f"{field} must be a YAML list of strings")
    return list(value)


def _new_entries(value: Any, field: str, *, old: list[str] = (), min_length: int = 1) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_ENTRIES:
        raise ValueError(f"{field} must contain at most {_MAX_ENTRIES} entries")
    old_count = Counter(old)
    entries = []
    status_by_key: dict[str, list[bool]] = defaultdict(list)
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} must contain nonempty text entries")
        is_new = old_count[item] == 0
        entry = _one_line(item, field, 120) if is_new else item
        if old_count[item]:
            old_count[item] -= 1
        key = _normalized(entry)
        if is_new and len(key.replace(" ", "")) < min_length:
            raise ValueError(f"{field} contains a short new entry")
        if field == "blocked_topics" and not key:
            raise ValueError("blocked_topics cannot contain an empty normalized topic")
        status_by_key[key].append(is_new)
        entries.append(entry)
    if any(len(statuses) > 1 and any(statuses) for statuses in status_by_key.values()):
        raise ValueError(f"{field} contains a duplicate new entry")
    return entries


def _replace_inline(lines: list[str], index: int, value: Any) -> None:
    old = lines[index]
    match = _KEY_LINE.fullmatch(_line_body(old))
    if not match:
        raise ConfigFormatError("Editable field is not an inline YAML key")
    original = match.group(3)
    if not original or original.lstrip().startswith(("|", ">")):
        raise ConfigFormatError("Block scalar/list layout is not supported for this field")
    suffix = _comment_tail(original)
    rendered = json.dumps(value, ensure_ascii=False)
    lines[index] = f"{match.group(1)}{match.group(2)}: {rendered}{suffix}{_line_ending(old)}"


def _encode(lines: list[str], original: bytes) -> bytes:
    text = "".join(lines).encode("utf-8")
    return (b"\xef\xbb\xbf" + text) if original.startswith(b"\xef\xbb\xbf") else text


def _parse_after(payload: bytes) -> dict[str, Any]:
    # Recheck the same mapping contract before committing text-preserving edits.
    return parse_mapping(payload)


def _atomic_save(path: Path, payload: bytes, expected_revision: str) -> str:
    # Use the existing per-file exclusive lock for every cooperating editor.
    # Arbitrary external editors do not participate in this lock protocol.
    with _feature_save_lock(path):
        return _atomic_save_locked(path, payload, expected_revision)


def _atomic_save_locked(path: Path, payload: bytes, expected_revision: str) -> str:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_revision:
        raise ConfigConflictError("Configuration changed; reload before saving")
    if hashlib.sha256(payload).hexdigest() == expected_revision:
        return expected_revision
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, stat.S_IMODE(path.stat().st_mode))
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_revision:
            raise ConfigConflictError("Configuration changed; reload before saving")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return hashlib.sha256(payload).hexdigest()


def load_personality(path: str | Path) -> tuple[dict[str, Any], str]:
    data, _, revision, _ = _read(Path(path))
    person = data.get("personality")
    if not isinstance(person, dict):
        raise ConfigFormatError("personality must be a YAML mapping")
    if not isinstance(person.get("name"), str) or not isinstance(person.get("tone"), str):
        raise ConfigFormatError("personality name and tone must be text")
    triggers = _list_of_strings(person.get("trigger_keywords"), "trigger_keywords")
    return {"name": person["name"], "tone": person["tone"], "trigger_keywords": triggers}, revision


def save_personality(
    path: str | Path,
    name: str,
    tone: str,
    trigger_keywords: list[str],
    expected_revision: str,
) -> str:
    path = Path(path)
    data, lines, revision, raw = _read(path)
    if revision != expected_revision:
        raise ConfigConflictError("Configuration changed; reload before saving")
    load_personality(path)
    name = _one_line(name, "name", 80)
    tone = _one_line(tone, "tone", 1000)
    triggers = _new_entries(
        trigger_keywords,
        "trigger_keywords",
        old=data["personality"]["trigger_keywords"],
        min_length=3,
    )
    if not triggers:
        raise ValueError("trigger_keywords must contain at least one trigger")
    parent = _key_at(lines, "personality", 0, 0, len(lines))
    if _KEY_LINE.fullmatch(_line_body(lines[parent])).group(3).strip():
        raise ConfigFormatError("personality must use a block mapping")
    end = _block_end(lines, parent, 0)
    indexes = {key: _key_at(lines, key, 2, parent + 1, end) for key in ("name", "tone", "trigger_keywords")}
    for key, value in (("name", name), ("tone", tone), ("trigger_keywords", triggers)):
        if data["personality"][key] != value:
            _replace_inline(lines, indexes[key], value)
    expected = deepcopy(data)
    expected["personality"].update(name=name, tone=tone, trigger_keywords=triggers)
    payload = _encode(lines, raw)
    if _parse_after(payload) != expected:
        raise ConfigFormatError("Edit would alter other personality configuration")
    return _atomic_save(path, payload, expected_revision)


def load_moderation(path: str | Path) -> tuple[dict[str, Any], str]:
    data, _, revision, _ = _read(Path(path))
    words = _list_of_strings(data.get("blocked_words"), "blocked_words")
    topics = _list_of_strings(data.get("blocked_topics"), "blocked_topics")
    return {"blocked_words": words, "blocked_topics": topics}, revision


def _rewrite_block_list(lines: list[str], key: str, values: list[str], newline: str) -> list[str]:
    start = _key_at(lines, key, 0, 0, len(lines))
    end = _block_end(lines, start, 0)
    header = lines[start]
    header_match = _KEY_LINE.fullmatch(_line_body(header))
    if not header_match:
        raise ConfigFormatError(f"{key} is not an editable YAML list")
    header_value = header_match.group(3)
    suffix = _comment_tail(header_value)
    header_content = header_value[:-len(suffix)] if suffix else header_value
    if header_content.strip() not in ("", "[]"):
        raise ConfigFormatError(f"{key} must use a block list")
    if suffix.startswith("#"):
        suffix = " " + suffix
    slots: list[tuple[int, str]] = []
    added_heading = None
    for index in range(start + 1, end):
        body = _line_body(lines[index])
        if body.strip() == "# Added in Command Center":
            added_heading = index
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        match = _LIST_LINE.fullmatch(body)
        if not match or len(match.group(1)) != 2:
            raise ConfigFormatError(f"{key} contains an unsupported list layout")
        try:
            item = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise ConfigFormatError(f"{key} contains invalid YAML") from exc
        if not isinstance(item, list) or len(item) != 1 or not isinstance(item[0], str):
            raise ConfigFormatError(f"{key} must have one string per line")
        slots.append((index, item[0]))
    remaining = Counter(values)
    result = list(lines)
    for index, existing in slots:
        if remaining[existing]:
            remaining[existing] -= 1
        else:
            result[index] = ""
    additions = []
    for value in values:
        if remaining[value]:
            additions.append(f"  - {json.dumps(value, ensure_ascii=False)}{newline}")
            remaining[value] -= 1
    if additions:
        insertion = slots[-1][0] + 1 if slots else start + 1
        if not slots:
            for index in range(start + 1, end):
                if _line_body(lines[index]).startswith("  #"):
                    insertion = index + 1
        if added_heading is not None:
            insertion = max(insertion, added_heading + 1)
        else:
            additions.insert(0, f"  # Added in Command Center{newline}")
        if insertion and result[insertion - 1] and not _line_ending(result[insertion - 1]):
            result[insertion - 1] += newline
        result[insertion:insertion] = additions
    if not values or header_content.strip() == "[]":
        header_content = " []" if not values else ""
        result[start] = f"{key}:{header_content}{suffix}{_line_ending(header)}"
    return [line for line in result if line]


def save_moderation(
    path: str | Path,
    blocked_words: list[str],
    blocked_topics: list[str],
    expected_revision: str,
) -> str:
    path = Path(path)
    data, lines, revision, raw = _read(path)
    if revision != expected_revision:
        raise ConfigConflictError("Configuration changed; reload before saving")
    current, _ = load_moderation(path)
    advanced = data.get("advanced", {})
    if not isinstance(advanced, dict):
        raise ConfigFormatError("advanced must be a YAML mapping")
    min_length = advanced.get("min_blocked_word_length", 4)
    if not isinstance(min_length, int) or isinstance(min_length, bool) or not 1 <= min_length <= 120:
        raise ConfigFormatError("advanced.min_blocked_word_length must be 1–120")
    words = _new_entries(blocked_words, "blocked_words", old=current["blocked_words"], min_length=min_length)
    topics = _new_entries(blocked_topics, "blocked_topics", old=current["blocked_topics"])
    newline = "\r\n" if b"\r\n" in raw else "\n"
    # Work from the bottom so line indexes above the edited block stay valid.
    words_index = _key_at(lines, "blocked_words", 0, 0, len(lines))
    topics_index = _key_at(lines, "blocked_topics", 0, 0, len(lines))
    ordered = (("blocked_words", words), ("blocked_topics", topics))
    if words_index < topics_index:
        ordered = tuple(reversed(ordered))
    for key, values in ordered:
        lines = _rewrite_block_list(lines, key, values, newline)
    payload = _encode(lines, raw)
    after = _parse_after(payload)
    saved_words = _list_of_strings(after.get("blocked_words"), "blocked_words")
    saved_topics = _list_of_strings(after.get("blocked_topics"), "blocked_topics")
    if Counter(saved_words) != Counter(words) or Counter(saved_topics) != Counter(topics):
        raise ConfigFormatError("Edit did not save the requested moderation entries")
    expected = deepcopy(data)
    expected["blocked_words"] = saved_words
    expected["blocked_topics"] = saved_topics
    if after != expected:
        raise ConfigFormatError("Edit would alter other moderation configuration")
    return _atomic_save(path, payload, expected_revision)
