"""Bounded self-report intake with a durable, identity-only memory exclusion.

Exclusion is not age verification or moderation. Its presence survives fact
forgetting, with no automatic expiry or removal after another age claim. The
table is created lazily and stores only channel/viewer IDs. Legacy facts are
neither bulk migrated nor silently deleted.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
import sqlite3
from typing import TYPE_CHECKING, TypedDict, Unpack, cast

# This internal subclass deliberately shares the base store's parsing helpers.
# Keep their private names; these are five reviewed implementation-sharing imports.
from .viewer_profiles import (
    Observation, ProfileFact, ViewerProfileStore, _HEALTH_CONDITIONS, _clean_message,  # pyright: ignore[reportPrivateUsage]
    _identity, _unsafe_statement, _birthday,  # pyright: ignore[reportPrivateUsage]
)


if TYPE_CHECKING:
    # Existing forwarding kwargs; declarations create no runtime dependency.
    class _HealthKwargs(TypedDict, total=False):
        source_role: str
        message_id: str | None
        now: datetime | None

    class _ObserveKwargs(_HealthKwargs, total=False):
        viewer_timezone: str | None


MAX_HEALTH_CONDITIONS = 4
HEALTH_PREFIX = 'health_condition:'
CONDITIONS = dict(_HEALTH_CONDITIONS, lupus='lupus', hiv='HIV',
                  bipolar='bipolar disorder', **{'bipolar disorder': 'bipolar disorder'})


def canonical_condition(value: object) -> str | None:
    return CONDITIONS.get(' '.join(value.casefold().split())) if isinstance(value, str) else None


def condition_key(value: object) -> str | None:
    canonical = canonical_condition(value)
    return HEALTH_PREFIX + canonical.casefold().replace(' ', '_') if canonical else None


@dataclass(frozen=True)
class HealthRequest:
    action: str
    condition: str | None = None
    previous: str | None = None


def parse_health_request(message: object) -> HealthRequest | None:
    if not isinstance(message, str) or not message.strip() or message.strip().endswith('?') \
            or _unsafe_statement(message):
        return None
    raw = re.sub(r'^(?:hey\s+)?@?medlar(?:tv)?\s*[,!:]?\s*', '', message.strip(), flags=re.I)
    correction = bool(re.match(r'^(?:please\s+)?(?:actually|correction)\b', raw, re.I))
    text = _clean_message(message).casefold()
    if re.fullmatch(r'(?:please )?(?:forget|delete|remove) (?:my )?health(?: conditions?)?', text):
        return HealthRequest('clear')
    match = re.fullmatch(r'(?:please )?(?:forget|delete|remove) (?:my )?(?:health condition[: ]+|health[: ]+)(.+)', text)
    if match:
        value = canonical_condition(match.group(1))
        return HealthRequest('remove', value) if value else None
    match = re.fullmatch(r'i (?:do not|don\'t|no longer|never) have (.+)', text)
    if match:
        value = canonical_condition(match.group(1))
        return HealthRequest('remove', value) if value else None
    match = re.fullmatch(r'replace (?:my health condition )?(.+) with (.+)', text)
    if match:
        previous, value = map(canonical_condition, match.groups())
        return HealthRequest('replace', value, previous) if previous and value else None
    match = re.fullmatch(r'i (?:have|was diagnosed with) (.+)', text)
    value = canonical_condition(match.group(1)) if match else None
    if text in {'i am diabetic', "i'm diabetic", 'i\u2019m diabetic'}:
        value = 'diabetes'
    return HealthRequest('correct' if correction else 'add', value) if value else None


def known_under18(facts: Mapping[str, ProfileFact]) -> bool:
    today = datetime.now(timezone.utc).date()
    birthday, year = facts.get('birthday'), facts.get('birth_year')
    if birthday and year and not year.derived:
        try:
            born = date(int(year.value), *map(int, birthday.value.split('-')))
            current_age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            if 0 <= current_age < 18:
                return True
        except (ValueError, TypeError):
            pass
    age = facts.get('age')
    if not age or age.derived or not re.fullmatch(r'[0-9]{1,3}', age.value):
        return False
    current_age = int(age.value)
    if age.as_of_date:
        try:
            reported = date.fromisoformat(age.as_of_date)
            current_age += today.year - reported.year - ((today.month, today.day) < (reported.month, reported.day))
        except ValueError:
            pass
    return current_age < 18


def current_under18_report(message: object, now: datetime | None = None) -> bool:
    """Recognize a narrow current self-report without retaining age or text."""
    if not isinstance(message, str) or message.strip().endswith('?') or _unsafe_statement(message):
        return False
    text = _clean_message(message).casefold()
    match = re.fullmatch(r"(?:i am|i['\u2019]m) (\d{1,3})(?: years old)?|"
                         r"(?:i am|i['\u2019]m) turning (\d{1,3}) today|"
                         r'i turn (\d{1,3}) today', text)
    if match:
        return int(next(value for value in match.groups() if value is not None)) < 18
    instant = now or datetime.now(timezone.utc)
    today = instant.date()
    birthday = _birthday(text, latest_possible_date=today)
    if birthday and birthday[1] is not None:
        month, day = map(int, birthday[0].split('-'))
        age = today.year - birthday[1] - ((today.month, today.day) < (month, day))
        return 0 <= age < 18
    turned = re.fullmatch(r'i turned (\d{1,3}) on (.+)', text)
    if turned:
        birthday = _birthday('my birthday is ' + turned.group(2), latest_possible_date=today)
        if birthday and birthday[1] is not None and int(turned.group(1)) <= 120:
            month, day = map(int, birthday[0].split('-'))
            age = int(turned.group(1)) + today.year - birthday[1] - ((today.month, today.day) < (month, day))
            return 0 <= age < 18
    return False


def health_groups(facts: Mapping[str, ProfileFact]) -> dict[str, list[ProfileFact]]:
    groups: dict[str, list[ProfileFact]] = {}
    for key, fact in facts.items():
        value = canonical_condition(fact.value)
        if value and (key == 'health_condition' or key == condition_key(value)):
            groups.setdefault(value, []).append(fact)
    return groups


class HealthConditionStore(ViewerProfileStore):
    """Application profile store. Ordinary self-reports enter through observe."""

    def __init__(self, path: str | Path | None = None) -> None:
        super().__init__(path)
        # Retry an unsuccessful durable flag write before any subsequent memory
        # use in this process. This is no substitute for persistence on restart.
        self._pending_exclusions: set[tuple[str, str]] = set()

    def _raw_facts(self, channel_id: str, viewer_id: str | None) -> dict[str, ProfileFact]:
        return super().get_facts(channel_id, viewer_id)

    @staticmethod
    def _has_exclusion(db: sqlite3.Connection | None, ids: tuple[str, str]) -> bool:
        if db is None or not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_exclusions'").fetchone():
            return False
        return db.execute('SELECT 1 FROM memory_exclusions WHERE channel_id=? AND viewer_id=?', ids).fetchone() is not None

    @staticmethod
    def _mark_excluded(db: sqlite3.Connection, ids: tuple[str, str]) -> None:
        db.execute('CREATE TABLE IF NOT EXISTS memory_exclusions ('
                   'channel_id TEXT NOT NULL, viewer_id TEXT NOT NULL, '
                   'PRIMARY KEY(channel_id, viewer_id))')
        db.execute('INSERT OR IGNORE INTO memory_exclusions VALUES (?, ?)', ids)

    def is_excluded(self, channel_id: str, viewer_id: str | None) -> bool:
        ids = _identity(channel_id, viewer_id)
        if ids is None:
            return False
        with self._lock:
            if ids in self._pending_exclusions:
                self.exclude(*ids)
                return True
            db = self._db(create=False)
            if self._has_exclusion(db, ids):
                return True
            if db is None or not known_under18(self._raw_facts(*ids)):
                return False
            # A legacy age is inspected only when this exact viewer is used.
            # Preserve the exclusion before forget can remove that old fact.
            if db.in_transaction:
                self._mark_excluded(db, ids)
            else:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    self._mark_excluded(db, ids)
            return True

    def exclude(self, channel_id: str, viewer_id: str | None) -> bool:
        ids = _identity(channel_id, viewer_id)
        if ids is None:
            return False
        with self._lock:
            self._pending_exclusions.add(ids)
            db = self._db(create=True)
            if db.in_transaction:
                self._mark_excluded(db, ids)
            else:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    self._mark_excluded(db, ids)
                self._pending_exclusions.discard(ids)
        return True

    def get_facts(self, channel_id: str, viewer_id: str | None) -> dict[str, ProfileFact]:
        with self._lock:
            db = self._db(create=False)
            if db is None:
                return {}
            def read() -> dict[str, ProfileFact]:
                return {} if self.is_excluded(channel_id, viewer_id) else self._raw_facts(channel_id, viewer_id)
            if db.in_transaction:
                return read()
            with db:
                # Read admission and facts from one serialized snapshot.
                db.execute('BEGIN IMMEDIATE')
                return read()

    def observe(self, channel_id: str, viewer_id: str | None, message: str, *, intake_enabled: object = True, **kwargs: Unpack[_ObserveKwargs]) -> Observation:
        if kwargs.get('source_role', 'user') != 'user':
            return Observation('ignored')
        if current_under18_report(message, kwargs.get('now')):
            self.exclude(channel_id, viewer_id)
            return Observation('excluded')
        request = parse_health_request(message)
        if request is not None:
            return self.observe_health(channel_id, viewer_id, message,
                                       intake_authorized=intake_enabled is True,
                                       **cast('_HealthKwargs', {key: value for key, value in kwargs.items() if key != 'viewer_timezone'}))
        if self.is_excluded(channel_id, viewer_id):
            # Retain the established natural forget parser, but never admit an
            # ordinary write. _prepare_updates checks again under SQLite's lock.
            if not re.match(r'^(?:please )?(?:forget|delete|remove)\b', _clean_message(message), re.I):
                return Observation('excluded')
        if intake_enabled is not True and not re.match(
                r'^(?:please )?(?:forget|delete|remove)\b', _clean_message(message), re.I):
            return Observation('ignored')
        return super().observe(channel_id, viewer_id, message, **kwargs)

    def _parse_health_condition(self, text: str) -> str | None:
        request = parse_health_request(text)
        return request.condition if request and request.action == 'add' else None

    def _prepare_updates(
        self, db: sqlite3.Connection, ids: tuple[str, str],
        updates: list[tuple[str, str, str | None, bool, str | None]],
        message: str, instant: datetime,
    ) -> Observation | None:
        if self.is_excluded(*ids):
            return Observation('excluded')
        # Reuse already accepted age/date facts solely for admission. No new
        # child profile (including birthday or derived year) is written.
        for key, value, as_of, *_ in updates:
            if key == 'age' and int(value) < 18:
                reported = date.fromisoformat(as_of) if as_of else None
                current_age = (int(value) + instant.year - reported.year
                               - ((instant.month, instant.day) < (reported.month, reported.day))) if reported else int(value)
                if current_age < 18:
                    self.exclude(*ids)
                    return Observation('excluded')
        incoming = {key: value for key, value, *_ in updates}
        if 'birth_year' in incoming and 'birthday' in incoming:
            month, day = map(int, incoming['birthday'].split('-'))
            year = int(incoming['birth_year'])
            age = instant.year - year - ((instant.month, instant.day) < (month, day))
            if age < 18:
                self.exclude(*ids)
                return Observation('excluded')
        health_update = next((value for key, value, *_ in updates if key == 'health_condition'), None)
        if health_update is not None:
            groups = health_groups(self._raw_facts(*ids))
            if health_update not in groups and len(groups) >= MAX_HEALTH_CONDITIONS:
                return Observation('limit_reached', fact_key='health_condition')
            matches = groups.get(health_update, ())
            key = max(matches, key=lambda f: (f.observed_at, f.key)).key if matches else condition_key(health_update)
            # A legacy row for the same value is kept in place. Newly learned
            # conditions use separate keys, including in atomic compound claims.
            # Incoming health values are canonicalized by _parse_health_condition;
            # existing matches are ProfileFact rows with text keys.
            key = cast(str, key)
            updates[:] = [(key, value, as_of, derived, basis) if old_key == 'health_condition'
                          else (old_key, value, as_of, derived, basis)
                          for old_key, value, as_of, derived, basis in updates]
        return None

    def observe_health(self, channel_id: str, viewer_id: str | None, message: str, *, intake_authorized: object = False,
                       source_role: str = 'user', message_id: str | None = None, now: datetime | None = None) -> Observation:
        if source_role != 'user':
            return Observation('ignored')
        request = parse_health_request(message)
        ids = _identity(channel_id, viewer_id)
        if request is None or ids is None:
            return Observation('ignored' if ids else 'session_only')
        if request.action == 'clear':
            return Observation('forgotten', fact_key='health_condition', count=self.forget(*ids, 'health'))
        # parse_health_request returns no condition only for the handled clear action.
        condition = cast(str, request.condition)
        # Forget/retract stays available while intake is disabled or unknown.
        if request.action != 'remove' and intake_authorized is not True:
            return Observation('ignored')
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        stamp = instant.astimezone(timezone.utc).isoformat(timespec='microseconds')
        source_id = str(message_id)[:128] if message_id is not None else None
        with self._lock:
            existing = self._raw_facts(*ids)
            if request.action != 'remove' and self.is_excluded(*ids):
                return Observation('excluded')
            if request.action == 'remove' and not health_groups(existing).get(condition):
                return Observation('ignored')
            db = self._db(create=True)
            try:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    # Reread after acquiring the cross-connection write lock.
                    facts = self._raw_facts(*ids)
                    if request.action != 'remove' and self.is_excluded(*ids):
                        return Observation('excluded')
                    groups = health_groups(facts)
                    old = request.previous
                    if request.action == 'correct':
                        if condition in groups:
                            old = condition
                        elif len(groups) == 1:
                            old = next(iter(groups))
                        elif len(groups) > 1:
                            return Observation('clarify', fact_key='health_condition', clarification=
                                               'Which saved condition should this replace? Use: Correction: replace X with Y.')
                    elif request.action == 'remove':
                        old = condition
                    if request.action == 'replace' and old not in groups:
                        return Observation('ignored')
                    touched = list(groups.get(old, ()) if old is not None else ()) + list(groups.get(condition, ()))
                    if any(fact.observed_at > stamp for fact in touched):
                        return Observation('ignored')
                    remaining = set(groups) - ({old} if old else set())
                    if request.action != 'remove':
                        remaining.add(condition)
                    if len(remaining) > MAX_HEALTH_CONDITIONS:
                        return Observation('limit_reached', fact_key='health_condition')
                    removed = 0
                    if old and (request.action == 'remove' or old != condition):
                        for fact in groups.get(old, ()):
                            removed += db.execute('DELETE FROM facts WHERE channel_id=? AND viewer_id=? AND fact_key=?',
                                                  (*ids, fact.key)).rowcount
                    if request.action == 'remove':
                        return Observation('forgotten', fact_key='health_condition', count=removed)
                    # Update a legacy row in place if it already represents this
                    # condition. No eager conversion, duplicated grant or migration.
                    matches = groups.get(condition, ())
                    key = max(matches, key=lambda f: (f.observed_at, f.key)).key if matches else condition_key(condition)
                    # The canonical parser and ProfileFact keys establish text here.
                    key = cast(str, key)
                    if not self._put(db, ids, key, condition, stamp, source_id, basis='explicit self-report'):
                        db.rollback()
                        return Observation('limit_reached', fact_key='health_condition')
            except sqlite3.OperationalError as exc:
                if 'full' in str(exc).casefold():
                    return Observation('limit_reached', fact_key='health_condition')
                raise
        return Observation('saved', fact_key='health_condition', value=condition, count=1)

    def forget(self, channel_id: str, viewer_id: str | None, key: str | None = None) -> int:
        self.is_excluded(channel_id, viewer_id)
        text = ' '.join(str(key or '').casefold().replace('_', ' ').split())
        text = re.sub(r'^(?:my|the) ', '', text)
        clear_health = text in {'health', 'health condition', 'health conditions'}
        match = re.fullmatch(r'(?:health condition|health)[: ]+(.+)', text)
        condition = canonical_condition(match.group(1)) if match else None
        if not clear_health and condition is None:
            return super().forget(channel_id, viewer_id, key)
        ids = _identity(channel_id, viewer_id)
        if ids is None:
            return 0
        with self._lock:
            db = self._db(create=False)
            if db is None:
                return 0
            with db:
                db.execute('BEGIN IMMEDIATE')
                if clear_health:
                    return db.execute("DELETE FROM facts WHERE channel_id=? AND viewer_id=? AND "
                                      "(fact_key='health_condition' OR fact_key GLOB 'health_condition:*')", ids).rowcount
                removed = 0
                for fact in (health_groups(self._raw_facts(*ids)).get(condition, ()) if condition is not None else ()):
                    removed += db.execute('DELETE FROM facts WHERE channel_id=? AND viewer_id=? AND fact_key=?',
                                          (*ids, fact.key)).rowcount
                return removed

    def get_answer(self, channel_id: str, viewer_id: str | None, question: str) -> str | None:
        facts = self.get_facts(channel_id, viewer_id)
        if self.is_excluded(channel_id, viewer_id):
            return None
        text = _clean_message(question).casefold()
        if re.fullmatch(r"(?:what(?: is|'s)|do you remember|can you tell me) my health conditions?|"
                        r'what are my health conditions|what health conditions? did i tell you(?: about)?|'
                        r'what did i tell you about my health', text):
            values = sorted(health_groups(facts), key=str.casefold)
            if len(values) == 1:
                return f'You previously reported {values[0]} as a health condition.'
            if values:
                return 'You previously reported these health conditions: ' + ', '.join(values) + '.'
            return None
        return super().get_answer(channel_id, viewer_id, question)

    def format_profile_context(self, channel_id: str, viewer_id: str | None, query: str | None = None) -> str:
        if self.is_excluded(channel_id, viewer_id):
            return ''
        return super().format_profile_context(channel_id, viewer_id, query)
