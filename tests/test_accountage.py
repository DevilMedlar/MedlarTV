"""Offline, deterministic checks for Twitch account-age calendar arithmetic."""

import ast
import calendar
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


HANDLERS = Path(__file__).resolve().parents[1] / "MedlarTV/core/command_handlers.py"
tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
names = {"_calendar_anniversary", "_fmt_calendar_age", "handle_accountage"}
nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert {node.name for node in nodes} == names
future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
module = ast.fix_missing_locations(ast.Module(body=[future] + nodes, type_ignores=[]))


class FrozenDatetime(datetime):
    instant = datetime(2026, 10, 2, 15, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


namespace = {
    "calendar": calendar,
    "datetime": FrozenDatetime,
    "timezone": timezone,
    "_twitch_headers_app": lambda: {},
}
exec(compile(module, str(HANDLERS), "exec"), namespace)
format_age = namespace["_fmt_calendar_age"]
handle_accountage = namespace["handle_accountage"]


def api_response(status, data):
    response = Mock(status_code=status)
    response.json.return_value = {"data": data}
    return response


class CalendarAgeTests(unittest.TestCase):
    def test_ordinary_calendar_span_matches_day_component(self):
        created = datetime(2020, 4, 10, tzinfo=timezone.utc)
        now = datetime(2026, 10, 2, 15, tzinfo=timezone.utc)
        self.assertEqual(format_age(created, now), "6 years 5 months 22 days 15 hours")

    def test_monthly_and_yearly_anniversary_boundaries_include_time_of_day(self):
        cases = [
            (datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
             datetime(2026, 2, 15, 11, 59, tzinfo=timezone.utc), "30 days 23 hours"),
            (datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
             datetime(2026, 2, 15, 12, tzinfo=timezone.utc), "1 month"),
            (datetime(2025, 10, 2, 15, tzinfo=timezone.utc),
             datetime(2026, 10, 2, 14, 59, tzinfo=timezone.utc), "11 months 29 days 23 hours"),
            (datetime(2025, 10, 2, 15, tzinfo=timezone.utc),
             datetime(2026, 10, 2, 15, tzinfo=timezone.utc), "1 year"),
        ]
        for created, now, expected in cases:
            with self.subTest(created=created, now=now):
                self.assertEqual(format_age(created, now), expected)

    def test_month_end_clamps_from_original_day_without_drift(self):
        cases = [
            (datetime(2024, 1, 31, 12, tzinfo=timezone.utc),
             datetime(2024, 2, 29, 12, tzinfo=timezone.utc), "1 month"),
            (datetime(2023, 1, 31, 12, tzinfo=timezone.utc),
             datetime(2023, 2, 28, 12, tzinfo=timezone.utc), "1 month"),
            (datetime(2023, 1, 31, 12, tzinfo=timezone.utc),
             datetime(2023, 3, 30, 12, tzinfo=timezone.utc), "1 month 30 days"),
            (datetime(2023, 1, 31, 12, tzinfo=timezone.utc),
             datetime(2023, 3, 31, 12, tzinfo=timezone.utc), "2 months"),
        ]
        for created, now, expected in cases:
            with self.subTest(created=created, now=now):
                self.assertEqual(format_age(created, now), expected)

    def test_february_29_anniversary_clamps_in_nonleap_years(self):
        created = datetime(2020, 2, 29, 12, tzinfo=timezone.utc)
        cases = [
            (datetime(2021, 2, 27, 12, tzinfo=timezone.utc), "11 months 29 days"),
            (datetime(2021, 2, 28, 12, tzinfo=timezone.utc), "1 year"),
            (datetime(2024, 2, 29, 12, tzinfo=timezone.utc), "4 years"),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(format_age(created, now), expected)

    def test_new_accounts_keep_zero_hour_style_and_future_is_rejected(self):
        now = datetime(2026, 10, 2, 15, tzinfo=timezone.utc)
        self.assertEqual(format_age(now, now), "0 hours")
        self.assertEqual(format_age(now, now + timedelta(minutes=59)), "0 hours")
        with self.assertRaises(ValueError):
            format_age(now + timedelta(seconds=1), now)
        with self.assertRaises(ValueError):
            format_age(now.replace(tzinfo=None), now)


class AccountAgeHandlerTests(unittest.TestCase):
    def setUp(self):
        FrozenDatetime.instant = datetime(2026, 10, 2, 15, tzinfo=timezone.utc)
        self.api_get = Mock(side_effect=AssertionError("Unexpected API call"))
        namespace["requests"] = SimpleNamespace(get=self.api_get)

    def test_default_target_uses_twitch_utc_creation_time(self):
        self.api_get.side_effect = None
        self.api_get.return_value = api_response(200, [{"created_at": "2020-04-10T00:00:00Z"}])
        self.assertEqual(
            handle_accountage("DevilMedlar", "", {}),
            "devilmedlar was created 6 years 5 months 22 days 15 hours ago.",
        )
        self.api_get.assert_called_once_with(
            "https://api.twitch.tv/helix/users",
            headers={}, params={"login": "devilmedlar"}, timeout=10,
        )

    def test_explicit_at_target_and_new_account(self):
        self.api_get.side_effect = None
        self.api_get.return_value = api_response(200, [{"created_at": "2026-10-02T14:30:00Z"}])
        self.assertEqual(
            handle_accountage("viewer", "  @OtherUser  ", {}),
            "otheruser was created 0 hours ago.",
        )
        self.assertEqual(self.api_get.call_args.kwargs["params"], {"login": "otheruser"})

    def test_missing_malformed_and_future_creation_time_are_unavailable(self):
        for created_at in (None, "not-a-timestamp", "2026-10-03T15:00:00Z"):
            with self.subTest(created_at=created_at):
                self.api_get.reset_mock(side_effect=True)
                self.api_get.side_effect = None
                self.api_get.return_value = api_response(200, [{"created_at": created_at}])
                self.assertEqual(
                    handle_accountage("viewer", "", {}),
                    "Account age unavailable for viewer",
                )

    def test_api_failure_and_unknown_user_keep_distinct_replies(self):
        self.api_get.side_effect = None
        self.api_get.return_value = api_response(503, [])
        self.assertEqual(handle_accountage("viewer", "", {}), "Unable to fetch account age for viewer")
        self.api_get.return_value = api_response(200, [])
        self.assertEqual(handle_accountage("viewer", "", {}), "User not found: viewer")
        self.api_get.side_effect = RuntimeError("offline")
        self.assertEqual(handle_accountage("viewer", "", {}), "Unable to fetch account age for viewer")


if __name__ == "__main__":
    unittest.main()
