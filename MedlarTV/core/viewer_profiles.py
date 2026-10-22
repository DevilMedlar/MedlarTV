"""Selective, viewer-authored Twitch facts stored outside the checkout.

The caller must supply a stable Twitch user ID, never a display alias or login,
for durable facts. This module does not read chat logs or invoke a model.
Storage is capped by facts per viewer, total facts, and SQLite page count. At
capacity, new facts are rejected; existing facts can still be corrected or
forgotten. No automatic profile eviction or deletion takes place.
"""

from __future__ import annotations

from typing import Literal, overload
import calendar
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .memory_privacy import has_contact_detail, has_health_detail


MAX_FACTS_PER_VIEWER = 16
MAX_TOTAL_FACTS = 20_000
MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_CONTEXT_CHARS = 700
FAVORITE_CATEGORIES = {
    "color", "food", "number", "game", "movie", "book", "music",
    "song", "sport", "animal", "drink",
}
_COLOR_MODIFIERS = frozenset(
    "light dark pale deep bright navy sky royal forest sea olive mint slate "
    "dusty electric hot burnt warm cool soft pastel".split()
)
_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"), ("february", "feb"), ("march", "mar"),
            ("april", "apr"), ("may",), ("june", "jun"),
            ("july", "jul"), ("august", "aug"),
            ("september", "sep", "sept"), ("october", "oct"),
            ("november", "nov"), ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_NUMBER_WORDS = {
    "a": 1, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10,
}
_HEALTH_CONDITIONS = {
    "asthma": "asthma", "diabetes": "diabetes",
    "type 1 diabetes": "type 1 diabetes", "type 2 diabetes": "type 2 diabetes",
    "celiac disease": "celiac disease", "epilepsy": "epilepsy",
    "arthritis": "arthritis", "multiple sclerosis": "multiple sclerosis",
    "cancer": "cancer", "allergies": "allergies",
}
_UNCERTAIN_CLAIM = re.compile(
    r"\b(?:maybe|probably|perhaps|possibly|not sure|unsure|"
    r"i think|i guess|i suppose|might be|could be|"
    r"allegedly|supposedly|apparently|uncertain|who knows)\b",
    re.I,
)


@dataclass(frozen=True)
class ProfileFact:
    key: str
    value: str
    observed_at: str
    source_message_id: str | None
    as_of_date: str | None = None
    derived: bool = False
    basis: str | None = None


@dataclass(frozen=True)
class Observation:
    kind: str  # ignored, session_only, saved, forgotten, clarify, limit_reached, excluded
    fact_key: str | None = None
    value: str | None = None
    clarification: str | None = None
    count: int = 0


def default_profile_path() -> Path:
    """Choose local app data, never a path below the source checkout."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local)
    elif os.name == "nt":
        base = Path.home() / "AppData" / "Local"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "MedlarTV" / "viewer_profiles.sqlite3"


def _identity(channel_id: str, viewer_id: str | None) -> tuple[str, str] | None:
    channel = str(channel_id or "").strip().casefold()
    viewer = str(viewer_id or "").strip()
    if not channel or not viewer or len(channel) > 128 or len(viewer) > 128:
        return None
    if any(ord(char) < 32 for char in channel + viewer):
        return None
    return channel, viewer


def _normalize_forget_key(key: str) -> str | None:
    """Map natural command arguments to supported fact keys; None means all."""
    name = " ".join(str(key).strip().casefold().replace("_", " ").split())
    name = re.sub(r"^(?:my|the) ", "", name)
    if name in {"all", "everything", "all facts", "everything you know about me"}:
        return None
    favorite = re.fullmatch(r"favo[u]?rite (\w+)", name)
    if favorite:
        category = "color" if favorite.group(1) == "colour" else favorite.group(1)
        return f"favorite_{category}" if category in FAVORITE_CATEGORIES else ""
    aliases = {
        "name": "real_name", "real name": "real_name", "full name": "real_name",
        "health": "health_condition", "health condition": "health_condition",
        "birthday": "birthday", "birth date": "birthday",
        "birth year": "birth_year", "year of birth": "birth_year",
        "age": "age", "marital status": "marital_status",
        "marriage": "marital_status", "married": "marital_status",
        "parenthood": "parenthood", "kids": "parenthood",
        "children": "parenthood", "child": "parenthood",
        "children count": "children_count", "number of kids": "children_count",
        "number of children": "children_count",
    }
    return aliases.get(name, "")


def _local_date(now: datetime, viewer_timezone: str | None) -> date | None:
    if not viewer_timezone:
        return None
    try:
        zone = ZoneInfo(viewer_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None
    return now.astimezone(zone).date()


def _clean_message(message: str) -> str:
    text = str(message or "").strip()
    text = re.sub(r"^(?:hey\s+)?@?medlar(?:tv)?\s*[,!:]?\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:please\s+)?(?:actually|correction)\s*[,!:]?\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:please\s+)?remember(?:\s+that)?\s*[,!:]?\s*", "", text, flags=re.I)
    return text.rstrip(".!? ")


def _unsafe_statement(text: str) -> bool:
    """Reject common quoted, joking, hypothetical and instruction-like claims."""
    if len(text) > 180 or any(char in text for char in '\n\r"""`') \
            or has_contact_detail(text) or _UNCERTAIN_CLAIM.search(text):
        return True
    if re.search(
        r"\b(?:just kidding|kidding|jk|j/k|hypothetically|pretend|imagine|"
        r"used to|joking|sarcasm|according to|someone said|my friend says|if i|what if|"
        r"ignore (?:your|all)|system prompt)\b",
        text,
        re.I,
    ):
        return True
    return False


def _safe_value(value: str, *, category: str) -> str | None:
    """Accept one short favorite value, never a caveat or personal detail."""
    raw = value.strip()
    # A question or a quoted value is not a clear self-report.
    if "?" in raw or raw.startswith(("'", "‘", "“")) or raw.endswith(("'", "’", "”")):
        return None
    # A second sentence may retract the claim or disclose an unrelated fact.
    # Do not split and save the first clause: the whole self-report is unclear.
    if re.search(r"[.!?](?:\s+\S|(?=[A-Z]))", raw):
        return None
    value = raw.rstrip(".! ")
    if not value or len(value) > 64 or len(value.split()) > 8 \
            or any(char in value for char in ".!?"):
        return None
    # A comma often starts a correction, joke, or unrelated personal clause.
    if re.search(r"[,\n\r;:{}()<>@\\/]", value):
        return None
    if re.search(
        r"\b[\w-]+\s+(?:likes?|loves?|has|had|is|was|works?|"
        r"attends?|studies|lives|plays?)\b",
        value, re.I,
    ):
        return None
    if re.search(
        r"\b(?:not|never|no longer|neither|maybe|probably|perhaps|possibly|"
        r"anything but|everything but|nothing but|other than|instead of|"
        r"but|except|unless|rather than|or|because|since|actually|"
        r"no wait|wait|i lied|lied|take it back|scratch that|"
        r"changed my mind|never mind|nevermind|correction|"
        r"and (?:i|my|our|you|we|he|she|they)|"
        r"just kidding|kidding|joking|joke|lol|lmao|haha|"
        r"i think|i guess|who knows|"
        r"password|address|phone|email|social security|"
        r"ignore|prompt|instruction|assistant|system)\b",
        value, re.I,
    ):
        return None
    # Child names, ages, and schools do not belong in a favorite-value field.
    if re.search(
        r"\b(?:son|sons|daughter|daughters|child|children|kid|kids|"
        r"baby|babies|toddler|teenager|grandchild|grandson|granddaughter|"
        r"stepchild|stepson|stepdaughter|nephew|niece|little one|"
        r"parent|mother|father|spouse|husband|wife|friend|"
        r"my|our|his|her|their|named|name|school|preschool|daycare|"
        r"elementary|academy|university|college|campus|grade|classroom|teacher|"
        r"years?[- ]old|\d{1,2}\s?yo)\b",
        value, re.I,
    ):
        return None
    if re.search(r"\b[\w-]+['’]s\b", value):
        return None
    if re.search(
        r"\b\d{1,5}\s+(?:[\w.-]+\s+){0,3}"
        r"(?:st|street|rd|road|ln|lane|ave|avenue|dr|drive|way|blvd|boulevard)\b",
        value, re.I,
    ):
        return None
    if category in {"color", "number"} and re.search(r"\b[A-Z][a-z]+\b", value):
        words = value.split()
        if len(words) > 1 and (
            category != "color"
            or any(word.casefold() not in _COLOR_MODIFIERS for word in words[:-1])
        ):
            return None
    if category in {"color", "number"} and re.search(r"\band\b", value, re.I):
        return None
    if has_contact_detail(value) or has_health_detail(value):
        return None
    if category == "number" and not re.fullmatch(r"\d{1,8}|[a-z]+(?:[- ][a-z]+){0,2}", value, re.I):
        return None
    if not re.fullmatch(r"[\w &'.,!?+()-]+", value, re.UNICODE):
        return None
    return value


def _favorite_update(text: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"my favo[u]?rite (color|colour|food|number|game|movie|book|music|song|sport|animal|drink) "
        r"(?:is|:) (.+)", text, re.I,
    )
    if not match:
        return None
    category = match.group(1).casefold().replace("colour", "color")
    value = _safe_value(match.group(2), category=category)
    return (f"favorite_{category}", value) if value is not None else None


def _real_name(text: str) -> str | None:
    match = re.fullmatch(r"my (?:(?:real|full) )?name is (.+)", text, re.I)
    if not match:
        return None
    value = match.group(1).strip()
    if _UNCERTAIN_CLAIM.search(value):
        return None
    if len(value) > 60 or not re.fullmatch(
        r"[^\W\d_]+(?:[-'\u2019][^\W\d_]+)*(?: [^\W\d_]+(?:[-'\u2019][^\W\d_]+)*){0,3}",
        value, re.UNICODE,
    ):
        return None
    # Extra lowercase words after a proper name are more likely caveats than
    # additional name parts. Prefer declining an unclear declaration.
    if len(value.split()) > 1 and any(
        not part[0].isupper() for part in value.split()
    ):
        return None
    if re.search(
        r"\b(?:i|my|me|you|your|our|his|her|their|son|daughter|kid|child|"
        r"friend|spouse|husband|wife|mother|father|mom|dad|sister|brother|"
        r"and|or|but|not|wait|joke|lied|actually|likes|loves|has|had|"
        r"is|was|lives|works|school|email|phone|address)\b",
        value, re.I,
    ):
        return None
    if re.search(r"\b(?:lol|lmao|haha|hehe|rofl|joking|kidding|jk)\b|\bj/k\b",
                 value, re.I):
        return None
    return value


def _health_condition(text: str) -> str | None:
    lowered = text.casefold()
    match = re.fullmatch(r"i (?:have|was diagnosed with) (.+)", lowered)
    if match:
        return _HEALTH_CONDITIONS.get(match.group(1))
    if lowered in {"i am diabetic", "i'm diabetic", "i\u2019m diabetic"}:
        return "diabetes"
    return None


def _retracted_health_condition(text: str) -> str | None:
    match = re.fullmatch(
        r"i (?:do not|don't|no longer|never) have (.+)", text.casefold(),
    )
    return _HEALTH_CONDITIONS.get(match.group(1)) if match else None


def _birthday(
    text: str, *, latest_possible_date: date | None = None,
) -> tuple[str, int | None] | None:
    match = re.fullmatch(
        r"my birthday is (" + "|".join(sorted(_MONTHS, key=len, reverse=True))
        + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?",
        text,
        re.I,
    )
    if not match:
        return None
    month = _MONTHS[match.group(1).casefold()]
    day = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else None
    try:
        date(year or 2000, month, day)
    except ValueError:
        return None
    if year is not None:
        if year < 1900:
            return None
        if latest_possible_date is not None and date(year, month, day) > latest_possible_date:
            return None
    return f"{month:02d}-{day:02d}", year


def _birth_year(age: int, age_date: date, birthday: str) -> int | None:
    month, day = (int(part) for part in birthday.split("-"))
    # February 28 before a leap-day birthday has differing legal age conventions.
    if (month, day) == (2, 29) and (age_date.month, age_date.day) == (2, 28) \
            and not calendar.isleap(age_date.year):
        return None
    year = age_date.year - age - ((age_date.month, age_date.day) < (month, day))
    try:
        date(year, month, day)
    except ValueError:
        return None
    return year


def _year_conflict(facts: dict[str, ProfileFact]) -> bool:
    """Compare a direct year with dated age, including impossible leap candidates."""
    direct = facts.get("birth_year")
    age = facts.get("age")
    birthday = facts.get("birthday")
    if not direct or direct.derived or not age or not age.as_of_date or not birthday:
        return False
    try:
        age_date = date.fromisoformat(age.as_of_date)
        month, day = map(int, birthday.value.split("-"))
        expected_age = age_date.year - int(direct.value) - (
            (age_date.month, age_date.day) < (month, day)
        )
        if (month, day) == (2, 29) and (age_date.month, age_date.day) == (2, 28) \
                and not calendar.isleap(age_date.year):
            # Jurisdictions differ on whether a leap-day birthday is observed
            # on February 28 or March 1 in non-leap years.
            return int(age.value) not in {expected_age, expected_age + 1}
        return expected_age != int(age.value)
    except ValueError:
        return False


class ViewerProfileStore:
    """A small, durable store of clear viewer self-reports.

    `viewer_id` must be a stable platform ID; callers pass None when it is
    unavailable. The store does not persist or retrieve facts for None.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_profile_path()
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @overload
    def _db(self, *, create: Literal[True]) -> sqlite3.Connection: ...

    @overload
    def _db(self, *, create: Literal[False]) -> sqlite3.Connection | None: ...

    @overload
    def _db(self, *, create: bool) -> sqlite3.Connection | None: ...

    def _db(self, *, create: bool) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        if not create and not self.path.exists():
            return None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS facts ("
            "channel_id TEXT NOT NULL, viewer_id TEXT NOT NULL, "
            "fact_key TEXT NOT NULL, fact_value TEXT NOT NULL, "
            "observed_at TEXT NOT NULL, source_message_id TEXT, "
            "as_of_date TEXT, derived INTEGER NOT NULL DEFAULT 0, basis TEXT, "
            "PRIMARY KEY (channel_id, viewer_id, fact_key))"
        )
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
        self._connection = connection
        return connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> ViewerProfileStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _put(
        self, db: sqlite3.Connection, ids: tuple[str, str], key: str, value: str,
        observed_at: str, source_message_id: str | None,
        *, as_of_date: str | None = None, derived: bool = False,
        basis: str | None = None,
    ) -> bool:
        count = db.execute(
            "SELECT count(*) FROM facts WHERE channel_id=? AND viewer_id=?",
            ids,
        ).fetchone()[0]
        exists = db.execute(
            "SELECT 1 FROM facts WHERE channel_id=? AND viewer_id=? AND fact_key=?",
            (*ids, key),
        ).fetchone()
        if not exists:
            if count >= MAX_FACTS_PER_VIEWER:
                return False
            total = db.execute("SELECT count(*) FROM facts").fetchone()[0]
            if total >= MAX_TOTAL_FACTS:
                return False
        db.execute(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(channel_id, viewer_id, fact_key) DO UPDATE SET "
            "fact_value=excluded.fact_value, observed_at=excluded.observed_at, "
            "source_message_id=excluded.source_message_id, "
            "as_of_date=excluded.as_of_date, derived=excluded.derived, "
            "basis=excluded.basis",
            (*ids, key, value, observed_at, source_message_id,
             as_of_date, int(derived), basis),
        )
        return True

    def _recompute_year(
        self, db: sqlite3.Connection, ids: tuple[str, str],
        observed_at: str, source_message_id: str | None,
    ) -> bool:
        direct = db.execute(
            "SELECT derived FROM facts WHERE channel_id=? AND viewer_id=? "
            "AND fact_key='birth_year'",
            ids,
        ).fetchone()
        if direct is not None and not direct["derived"]:
            return True
        rows = db.execute(
            "SELECT fact_key, fact_value, as_of_date FROM facts "
            "WHERE channel_id=? AND viewer_id=? AND fact_key IN ('age','birthday')",
            ids,
        ).fetchall()
        facts = {row["fact_key"]: row for row in rows}
        age = facts.get("age")
        birthday = facts.get("birthday")
        db.execute(
            "DELETE FROM facts WHERE channel_id=? AND viewer_id=? "
            "AND fact_key='birth_year' AND derived=1",
            ids,
        )
        if not age or not birthday or not age["as_of_date"]:
            return True
        age_date = date.fromisoformat(age["as_of_date"])
        year = _birth_year(int(age["fact_value"]), age_date, birthday["fact_value"])
        if year is not None and year >= 1900:
            return self._put(
                db, ids, "birth_year", str(year), observed_at,
                source_message_id, as_of_date=age_date.isoformat(),
                derived=True, basis="age+birthday",
            )
        return True

    def get_facts(self, channel_id: str, viewer_id: str | None) -> dict[str, ProfileFact]:
        ids = _identity(channel_id, viewer_id)
        if ids is None:
            return {}
        with self._lock:
            db = self._db(create=False)
            if db is None:
                return {}
            rows = db.execute(
                "SELECT fact_key, fact_value, observed_at, source_message_id, "
                "as_of_date, derived, basis FROM facts "
                "WHERE channel_id=? AND viewer_id=? ORDER BY fact_key LIMIT ?",
                (*ids, MAX_FACTS_PER_VIEWER),
            ).fetchall()
        return {
            row["fact_key"]: ProfileFact(
                row["fact_key"], row["fact_value"], row["observed_at"],
                row["source_message_id"], row["as_of_date"],
                bool(row["derived"]), row["basis"],
            )
            for row in rows
        }

    def forget(self, channel_id: str, viewer_id: str | None, key: str | None = None) -> int:
        """Delete this viewer's facts in this channel; `key=None` deletes all."""
        ids = _identity(channel_id, viewer_id)
        if ids is None:
            return 0
        fact_key = None if key is None else _normalize_forget_key(key)
        if key is not None and fact_key == "":
            return 0
        keys = None if fact_key is None else {fact_key}
        if keys is not None:
            if fact_key == "birthday":
                keys.add("birth_year")
            elif fact_key == "parenthood":
                keys.add("children_count")
        with self._lock:
            db = self._db(create=False)
            if db is None:
                return 0
            with db:
                if keys is None:
                    result = db.execute(
                        "DELETE FROM facts WHERE channel_id=? AND viewer_id=?", ids
                    )
                    return result.rowcount
                removed = 0
                for fact_key in keys:
                    result = db.execute(
                        "DELETE FROM facts WHERE channel_id=? AND viewer_id=? "
                        "AND fact_key=?",
                        (*ids, fact_key),
                    )
                    removed += result.rowcount
                if fact_key == "age":
                    result = db.execute(
                        "DELETE FROM facts WHERE channel_id=? AND viewer_id=? "
                        "AND fact_key='birth_year' AND derived=1",
                        ids,
                    )
                    removed += result.rowcount
                return removed

    def _parse_health_condition(self, text: str) -> str | None:
        return _health_condition(text)

    def _prepare_updates(
        self, db: sqlite3.Connection, ids: tuple[str, str],
        updates: list[tuple[str, str, str | None, bool, str | None]],
        message: str, instant: datetime,
    ) -> Observation | None:
        """Policy hook under the same write lock as the subsequent fact writes."""
        return None

    def observe(
        self, channel_id: str, viewer_id: str | None, message: str, *,
        message_id: str | None = None, now: datetime | None = None,
        viewer_timezone: str | None = None,
        source_role: str = "user",
    ) -> Observation:
        """Save only unambiguous self-reports and handle this viewer's forget requests."""
        if source_role != "user":
            return Observation("ignored")
        ids = _identity(channel_id, viewer_id)
        text = _clean_message(message)
        if not text or str(message or "").strip().endswith("?") or _unsafe_statement(text):
            return Observation("session_only" if ids is None else "ignored")
        lowered = text.casefold()

        if re.fullmatch(
            r"(?:please\s+)?(?:forget|delete|remove)\s+(?:everything|all|"
            r"everything you know|all (?:my )?(?:facts|info))\s+about me",
            lowered,
        ):
            return Observation("forgotten", count=self.forget(channel_id, viewer_id))
        forget = re.fullmatch(
            r"(?:please\s+)?(?:forget|delete|remove)\s+my\s+"
            r"(?:favo[u]?rite\s+(\w+)|"
            r"(birthday|birth date|age|birth year|year of birth|"
            r"marital status|marriage|kids|children|"
            r"name|real name|full name|health|health condition))",
            lowered,
        )
        if forget:
            category = forget.group(1)
            if category:
                category = "color" if category == "colour" else category
                if category not in FAVORITE_CATEGORIES:
                    return Observation("ignored")
                key = f"favorite_{category}"
            else:
                key = {
                    "birth date": "birthday", "birth year": "birth_year",
                    "year of birth": "birth_year", "marital status": "marital_status",
                    "marriage": "marital_status", "kids": "parenthood",
                    "children": "parenthood",
                    "name": "real_name", "real name": "real_name",
                    "full name": "real_name", "health": "health_condition",
                    "health condition": "health_condition",
                }.get(forget.group(2), forget.group(2))
            return Observation("forgotten", fact_key=key,
                               count=self.forget(channel_id, viewer_id, key))

        if ids is None:
            return Observation("session_only")

        retracted_health = _retracted_health_condition(text)
        if retracted_health:
            with self._lock:
                current = self.get_facts(channel_id, viewer_id).get("health_condition")
                if current is None or current.value != retracted_health:
                    return Observation("ignored")
                return Observation(
                    "forgotten", fact_key="health_condition",
                    count=self.forget(channel_id, viewer_id, "health condition"),
                )

        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        instant = instant.astimezone(timezone.utc)
        observed_at = instant.isoformat(timespec="microseconds")
        local_date = _local_date(instant, viewer_timezone)
        # Any viewer-local date can be at most one calendar day ahead of UTC.
        latest_possible_date = local_date or (instant + timedelta(hours=14)).date()
        source_id = str(message_id)[:128] if message_id is not None else None

        updates: list[tuple[str, str, str | None, bool, str | None]] = []
        clauses = re.split(r"[.!](?:\s+|(?=[A-Z]))", text)
        if len(clauses) > 1:
            # Accept only two complete independent reports. Do not save a
            # favorite from a joke, contact detail, or unrelated second clause.
            if len(clauses) != 2:
                return Observation("ignored")
            favorite_clause = _favorite_update(clauses[0]) or _favorite_update(clauses[1])
            health_clause = self._parse_health_condition(clauses[0]) or self._parse_health_condition(clauses[1])
            if not favorite_clause or not health_clause:
                return Observation("ignored")
            updates.extend((
                (favorite_clause[0], favorite_clause[1], None, False, "explicit self-report"),
                ("health_condition", health_clause, None, False, "explicit self-report"),
            ))
        turned_on = re.fullmatch(r"i turned (\d{1,3}) on (.+)", lowered) if not updates else None
        if turned_on:
            stated_age = int(turned_on.group(1))
            dated_birthday = _birthday(
                "my birthday is " + turned_on.group(2),
                latest_possible_date=latest_possible_date,
            )
            if dated_birthday and dated_birthday[1] is not None and stated_age <= 120:
                month_day, year = dated_birthday
                assert year is not None
                stated_date = date(year, *map(int, month_day.split("-")))
                if stated_date <= latest_possible_date:
                    updates.extend((
                        ("birthday", month_day, None, False, None),
                        ("age", str(stated_age), stated_date.isoformat(), False, None),
                    ))
        elif not updates and lowered in {"today is my birthday", "it's my birthday today", "it is my birthday today", "my birthday is today"}:
            if local_date is None:
                return Observation("clarify", fact_key="birthday", clarification=
                                   "Please tell me your local month and day, for example: My birthday is October 2.")
            updates.append(("birthday", local_date.strftime("%m-%d"), None, False, None))
        else:
            birthday = _birthday(text, latest_possible_date=latest_possible_date) if not updates else None
            if birthday:
                month_day, year = birthday
                updates.append(("birthday", month_day, None, False, None))
                if year is not None:
                    updates.append(("birth_year", str(year), None, False, None))

        turning = re.fullmatch(r"(?:i am|i['']m) turning (\d{1,3}) today|i turn (\d{1,3}) today", lowered) if not updates else None
        if turning:
            if local_date is None:
                return Observation("clarify", fact_key="birthday", clarification=
                                   "What is the full date where you are? For example: I turned 30 on October 2, 2026.")
            age = int(turning.group(1) or turning.group(2))
            if age > 120:
                return Observation("ignored")
            updates.extend((
                ("birthday", local_date.strftime("%m-%d"), None, False, None),
                ("age", str(age), local_date.isoformat(), False, None),
            ))
        elif not updates:
            age_match = re.fullmatch(r"(?:i am|i['’]m) (\d{1,3})(?: years old)?", lowered)
            if age_match and int(age_match.group(1)) <= 120:
                updates.append(("age", age_match.group(1),
                                local_date.isoformat() if local_date else None,
                                False, None))

        if not updates:
            marital = re.fullmatch(r"(?:i am|i['’]m) (married|not married|single|no longer married)", lowered)
            if marital:
                updates.append(("marital_status", "married" if marital.group(1) == "married" else "not married", None, False, None))

        if not updates:
            no_children = re.fullmatch(
                r"i (?:do not|don't) have (?:any )?(?:kids|children|a kid|a child)|"
                r"i have no (?:kids|children)", lowered,
            )
            children = re.fullmatch(
                r"i have (a|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
                r"\d{1,2}) (?:kids?|child|children)", lowered,
            )
            if no_children:
                updates.append(("parenthood", "no", None, False, None))
            elif children:
                count_text = children.group(1)
                count = _NUMBER_WORDS.get(count_text, int(count_text) if count_text.isdigit() else -1)
                if 0 <= count <= 20:
                    updates.append(("parenthood", "yes" if count else "no", None, False, None))
                    if count:
                        updates.append(("children_count", str(count), None, False, None))

        if not updates:
            favorite = _favorite_update(text)
            if favorite:
                updates.append((favorite[0], favorite[1], None, False, None))

        if not updates:
            name = _real_name(text)
            if name:
                updates.append(("real_name", name, None, False, "explicit self-report"))

        if not updates:
            condition = self._parse_health_condition(text)
            if condition:
                updates.append(("health_condition", condition, None, False, "explicit self-report"))

        if not updates:
            return Observation("ignored")

        with self._lock:
            try:
                db = self._db(create=True)
                assert db is not None
                with db:
                    # Serialize capacity checks across store instances/processes.
                    db.execute("BEGIN IMMEDIATE")
                    boundary = self._prepare_updates(db, ids, updates, message, instant)
                    if boundary is not None:
                        return boundary
                    for key, *_ in updates:
                        old = db.execute(
                            "SELECT observed_at FROM facts WHERE channel_id=? AND viewer_id=? "
                            "AND fact_key=?",
                            (*ids, key),
                        ).fetchone()
                        if old is not None and observed_at < old["observed_at"]:
                            return Observation("ignored")
                    health_update = next(
                        (value for key, value, *_ in updates if key == "health_condition"), None,
                    )
                    if health_update:
                        old_health = db.execute(
                            "SELECT fact_value FROM facts WHERE channel_id=? AND viewer_id=? "
                            "AND fact_key='health_condition'", ids,
                        ).fetchone()
                        correction = re.match(
                            r"^(?:please\s+)?(?:actually|correction)\b",
                            str(message or "").strip(), re.I,
                        )
                        if old_health and old_health["fact_value"] != health_update and not correction:
                            return Observation(
                                "clarify", fact_key="health_condition",
                                clarification=(
                                    "Please clarify whether this replaces the health condition "
                                    "you previously reported. If it does, start with 'Correction:'."
                                ),
                            )
                    birthday_update = next(
                        (value for key, value, *_ in updates if key == "birthday"), None,
                    )
                    explicit_year_update = any(key == "birth_year" for key, *_ in updates)
                    if birthday_update:
                        # A month/day correction alone does not erase a directly
                        # reported year. A new full birth date replaces that year.
                        old_year = db.execute(
                            "SELECT fact_value, derived FROM facts WHERE "
                            "channel_id=? AND viewer_id=? AND fact_key='birth_year'",
                            ids,
                        ).fetchone()
                        if old_year and not old_year["derived"] and not explicit_year_update:
                            month, day = map(int, birthday_update.split("-"))
                            try:
                                date(int(old_year["fact_value"]), month, day)
                            except ValueError:
                                return Observation(
                                    "clarify", fact_key="birthday",
                                    clarification="That date conflicts with the birth year you told me. Which full birth date is correct?",
                                )
                        db.execute(
                            "DELETE FROM facts WHERE channel_id=? AND viewer_id=? "
                            "AND fact_key='birth_year' AND derived=1",
                            ids,
                        )
                    for key, value, as_of, derived, basis in updates:
                        if not self._put(db, ids, key, value, observed_at, source_id,
                                         as_of_date=as_of, derived=derived, basis=basis):
                            db.rollback()
                            return Observation("limit_reached", fact_key=key)
                    if any(key in {"birthday", "age"} for key, *_ in updates) and not explicit_year_update:
                        if not self._recompute_year(db, ids, observed_at, source_id):
                            db.rollback()
                            return Observation("limit_reached", fact_key="birth_year")
                    if any(key == "parenthood" and value == "no" for key, value, *_ in updates):
                        db.execute(
                            "DELETE FROM facts WHERE channel_id=? AND viewer_id=? "
                            "AND fact_key='children_count'",
                            ids,
                        )
            except sqlite3.OperationalError as exc:
                if "full" in str(exc).casefold():
                    return Observation("limit_reached", fact_key=updates[0][0])
                raise
        key, value, *_ = updates[0]
        return Observation("saved", fact_key=key, value=value, count=len(updates))

    @staticmethod
    def missing_answer_for_topics(topics: set[str]) -> str | None:
        """Return a direct no-memory answer for one recognized durable fact topic."""
        labels = {
            "birthday": "birthday information",
            "age": "age",
            "marital": "marital status",
            "children": "parenthood information",
            "real_name": "real name",
            "health_condition": "health condition",
        }
        direct = [
            topic for topic in topics
            if topic in labels or topic.startswith("favorite_")
        ]
        if len(direct) != 1:
            return None
        topic = direct[0]
        label = (
            "favorite " + topic.removeprefix("favorite_").replace("_", " ")
            if topic.startswith("favorite_") else labels[topic]
        )
        return f"I don't have a saved {label} for you."

    def get_answer(self, channel_id: str, viewer_id: str | None, question: str) -> str | None:
        """Answer direct questions about this viewer's saved facts only."""
        text = _clean_message(question).casefold()
        facts = self.get_facts(channel_id, viewer_id)
        if not facts:
            return None
        if re.fullmatch(
            r"(?:what(?: is|'s)|do you remember|can you tell me) my "
            r"(?:(?:real|full) )?name|what name did i tell you",
            text,
        ):
            fact = facts.get("real_name")
            if fact:
                return f"You told me your real name is {fact.value}."
        if re.fullmatch(
            r"(?:what(?: is|'s)|do you remember|can you tell me) my "
            r"health condition|what health condition did i tell you(?: about)?|"
            r"what did i tell you about my health",
            text,
        ):
            fact = facts.get("health_condition")
            if fact:
                return f"You previously reported {fact.value} as a health condition."
        favorite = re.fullmatch(
            r"(?:what(?:'s| is| was)|do you remember|can you tell me|"
            r"what did i (?:say|tell you)) my favo[u]?rite "
            r"(color|colour|food|number|game|movie|book|music|song|sport|animal|drink)"
            r"(?: (?:is|was|again))?",
            text,
        )
        if favorite:
            category = favorite.group(1)
            category = "color" if category == "colour" else category
            fact = facts.get(f"favorite_{category}")
            if fact:
                return f"You told me your favorite {category} is {fact.value}."
        if re.fullmatch(
            r"(?:when(?: is|'s)|what(?: is|'s)|do you remember|"
            r"what did i tell you) my birthday(?: is)?",
            text,
        ):
            fact = facts.get("birthday")
            if fact:
                month, day = map(int, fact.value.split("-"))
                return f"You told me your birthday is {date(2000, month, day).strftime('%B')} {day}."
        if re.fullmatch(r"how old am i|what(?: is|'s) my age|what age did i tell you", text):
            fact = facts.get("age")
            if fact:
                if fact.as_of_date:
                    return f"You told me you were {fact.value} on {fact.as_of_date}."
                stamp = fact.observed_at.replace("T", " ").split("+")[0][:16]
                return f"You told me you were {fact.value} at {stamp} UTC."
        if re.fullmatch(r"what year was i born|when was i born|what(?: is|'s) my birth year", text):
            if _year_conflict(facts):
                return "Your reported age and birth date point to different birth years. Please restate the correct age or full birth date."
            fact = facts.get("birth_year")
            if fact:
                lead = "Based on your birthday and age, " if fact.derived else "You told me "
                return f"{lead}your birth year is {fact.value}."
        if re.fullmatch(r"am i married|what(?: is|'s) my marital status|did i tell you i(?: am|'m) married", text):
            fact = facts.get("marital_status")
            if fact:
                return f"You told me you are {fact.value}."
        if re.fullmatch(r"how many (?:kids|children) do i have|do i have (?:kids|children)|am i a parent", text):
            count = facts.get("children_count")
            if count:
                return f"You told me you have {count.value} children."
            fact = facts.get("parenthood")
            if fact:
                return "You told me you have children." if fact.value == "yes" else "You told me you do not have children."
        return None

    def format_profile_context(
        self, channel_id: str, viewer_id: str | None, query: str | None = None,
    ) -> str:
        """Short source-labeled facts relevant to a viewer's question."""
        # Direct name and health lookups use get_answer. Keep these sensitive
        # values out of broad recall and free-form model prompts entirely.
        facts = {
            key: fact for key, fact in self.get_facts(channel_id, viewer_id).items()
            if key not in {"real_name", "health_condition"} and not key.startswith("health_condition:")
        }
        if not facts:
            return ""
        year_conflict = _year_conflict(facts)
        if query is not None:
            text = _clean_message(query).casefold()
            if not re.match(
                r"^(?:please )?(?:what|when|how|am|do|did|is|are|can|could|would|"
                r"tell me|remind me)\b", text,
            ) or not re.search(r"\b(?:i|me|my|mine)\b", text):
                return ""
            if not re.fullmatch(
                r"(?:what (?:do you )?(?:remember|know) about me|"
                r"tell me what you (?:remember|know) about me)", text,
            ):
                selected: set[str] = set()
                if re.search(
                    r"\bmy (?:birthday|birth date|birth year|year of birth)\b|"
                    r"\bwhen was i born\b|\bwhat year was i born\b", text,
                ):
                    selected.update({"birthday", "birth_year"})
                if re.search(r"\bmy age\b|\bhow old am i\b", text):
                    selected.update({"age", "birthday", "birth_year"})
                if re.search(
                    r"\bmy (?:marital status|marriage)\b|\bam i married\b|"
                    r"\bdid i tell you i(?: am|'m| was) married\b", text,
                ):
                    selected.add("marital_status")
                if re.search(
                    r"\bmy (?:kids?|children|parenthood)\b|"
                    r"\b(?:do i have (?:kids?|children)|am i a parent|"
                    r"how many (?:kids?|children) do i have)\b", text,
                ):
                    selected.update({"parenthood", "children_count"})
                for category in FAVORITE_CATEGORIES:
                    spelling = r"colou?r" if category == "color" else re.escape(category)
                    if re.search(r"\bmy favo[u]?rite " + spelling + r"\b", text):
                        selected.add(f"favorite_{category}")
                if re.search(r"\bmy favo[u]?rites\b", text):
                    selected.update(key for key in facts if key.startswith("favorite_"))
                facts = {key: fact for key, fact in facts.items() if key in selected}
                if not facts:
                    return ""
        conflict_visible = year_conflict and "birth_year" in facts
        if conflict_visible:
            facts = {key: fact for key, fact in facts.items() if key != "birth_year"}
        parts: list[str] = []
        for key, fact in facts.items():
            label = key.replace("_", " ")
            value = fact.value
            if key == "age":
                as_of = fact.as_of_date or fact.observed_at + " UTC"
                value += f" (stated as of {as_of})"
            elif fact.derived:
                value += " (derived from age and birthday)"
            parts.append(f"{label}: {value}")
        if conflict_visible:
            parts.append("birth year: unresolved (this viewer's dated reports conflict)")
        result = "This viewer's own previously reported facts in this channel: " + "; ".join(parts)
        return result[:MAX_CONTEXT_CHARS]
