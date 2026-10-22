"""Local-clock, DST, privacy and routing checks with all network calls blocked.

Load source directly to avoid credential/startup imports. Listener sends are
mocked, and no Medlar, Ollama or Twitch session is started.
"""

import ast
import importlib.metadata
import importlib.util
import io
import logging
import re
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfoNotFoundError

import requests
import test_twitch_feature_toggles as listener_fixture

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "MedlarTV/core/time_lookup.py"
spec = importlib.util.spec_from_file_location("time_lookup_under_test", MODULE_PATH)
time_lookup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(time_lookup)

NOW = datetime(2026, 10, 2, 0, 42, tzinfo=timezone.utc)


class FrozenDatetime(datetime):
    instant = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        FrozenDatetime.instant = NOW
        self.addCleanup(setattr, FrozenDatetime, "instant", NOW)
        self.clock_patch = patch.object(time_lookup, "datetime", FrozenDatetime)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.network = []
        for target in ("requests.sessions.Session.request", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("Network forbidden in time lookup tests"))
            self.network.append(guard.start())
            self.addCleanup(guard.stop)

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def assert_diagnostic(self, captured, operation, reason, exception="None"):
        logs = "\n".join(captured.output)
        self.assertIn("source=local", logs)
        self.assertIn("operation=" + operation, logs)
        self.assertIn("reason=" + reason, logs)
        self.assertIn("exception=" + exception, logs)
        self.assertRegex(logs, r"\belapsed_ms=\d+\b")
        for private in ("Tokyo", "Asia/Tokyo", "America/New_York", "PRIVATE_QUERY", "PRIVATE_CREDENTIAL", "PRIVATE_ERROR"):
            self.assertNotIn(private, logs)
        return logs


class LocalClockTests(OfflineCase):
    def test_shared_formatter_has_one_label_and_optional_local_date(self):
        dt = time_lookup._fetch_time("Asia/Tokyo")
        self.assertEqual(time_lookup._format_time("Tokyo", dt), "Tokyo: 09:42 AM on October 02, 2026")
        self.assertEqual(time_lookup._format_time("Tokyo", dt, include_date=False), "Tokyo: 09:42 AM")

    def test_tokyo_success_reply_and_sanitized_elapsed_log(self):
        with patch.object(time_lookup.time, "perf_counter", side_effect=[10, 10.125]), \
             self.assertLogs(time_lookup.log, level="INFO") as captured:
            reply = time_lookup.get_times_for_location("Tokyo")
        self.assertEqual(reply, "Tokyo: 09:42 AM on October 02, 2026")
        logs = self.assert_diagnostic(captured, "time_by_zone", "ok")
        self.assertIn("elapsed_ms=125", logs)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.INFO)

    def test_all_existing_aliases_resolve_to_their_original_zone(self):
        for alias, zone in time_lookup.TIMEZONE_ALIASES.items():
            with self.subTest(alias=alias):
                expected = time_lookup._fetch_time(zone)
                label = zone.split("/")[-1].replace("_", " ")
                self.assertEqual(time_lookup.get_times_for_location(alias), f"{label}: {expected.strftime('%I:%M %p')} on {expected.strftime('%B %d, %Y')}")
                self.assertEqual(time_lookup.resolve_timezones_for_query(alias), [zone])

    def test_alias_case_and_repeated_whitespace(self):
        self.assertEqual(time_lookup.get_times_for_location("  NeW   YoRk  "), "New York: 08:42 PM on October 01, 2026")

    def test_synonyms_keep_new_york_default_and_existing_mappings(self):
        cases = {"est": "America/New_York", "edt": "America/New_York", "eastern time": "America/New_York",
                 "united states": "America/New_York", "rusia": "Europe/Moscow", "united kingdom": "Europe/London"}
        for query, zone in cases.items():
            with self.subTest(query=query):
                self.assertEqual(time_lookup.resolve_timezones_for_query(query), [zone])

    def test_default_time_is_eastern_even_when_device_timezone_differs(self):
        with patch.dict("os.environ", {"TZ": "Pacific/Auckland"}):
            self.assertEqual(time_lookup.get_default_local_time(),
                             "Eastern Time (US & Canada): 08:42 PM on October 01, 2026")

    def test_known_city_legacy_entrypoint_uses_shared_dated_reply(self):
        self.assertEqual(time_lookup.get_current_time("Tokyo"),
                         "Tokyo: 09:42 AM on October 02, 2026")

    def test_non_alias_city_and_case_insensitive_iana_path(self):
        self.assertEqual(time_lookup.get_times_for_location("Paris"),
                         "Paris: 02:42 AM on October 02, 2026")
        self.assertEqual(time_lookup.resolve_timezones_for_query("asia/tokyo"), ["Asia/Tokyo"])
        self.assertEqual(time_lookup.get_times_for_location("asia/tokyo"),
                         "Tokyo: 09:42 AM on October 02, 2026")

    def test_unknown_city_returns_no_match_without_network(self):
        self.assertIsNone(time_lookup.get_times_for_location("Atlantis"))
        self.assertEqual(time_lookup.resolve_timezones_for_query("Atlantis"), [])

    def test_malformed_locations_and_queries_do_not_raise(self):
        for value in (None, 42, True, [], {}, "", "   ", "\x00", "../PRIVATE_QUERY", "https://PRIVATE_CREDENTIAL@host/"):
            with self.subTest(kind=type(value).__name__):
                self.assertIsNone(time_lookup.get_times_for_location(value))
                self.assertIsNone(time_lookup.get_current_time(value))
                self.assertEqual(time_lookup.resolve_timezones_for_query(value), [])

    def test_timezone_list_is_sorted_unique_and_available_offline(self):
        zones = time_lookup.list_timezones()
        self.assertEqual(zones, sorted(set(zones)))
        self.assertIn("Asia/Tokyo", zones)
        self.assertIn("America/New_York", zones)
        self.assertGreater(len(zones), 400)

    def test_timezone_filter_keeps_case_insensitive_matching(self):
        self.assertEqual(time_lookup.list_timezones("TOKYO"), ["Asia/Tokyo"])
        self.assertEqual(time_lookup.list_timezones("PRIVATE_QUERY"), [])
        for value in (42, True, [], {}):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(time_lookup.list_timezones(value), [])

    def test_world_clock_keeps_all_seventeen_labels(self):
        summary = time_lookup.world_clock_summary()
        self.assertEqual(len(summary.split(" | ")), 17)
        self.assertIn("Tokyo: 09:42 AM", summary)
        self.assertIn("New York: 08:42 PM", summary)

    def test_multi_country_labels_and_alias_priority_are_preserved(self):
        self.assertEqual(time_lookup.get_times_for_location("america"),
                         "Eastern: 08:42 PM | Central: 07:42 PM | Mountain: 06:42 PM | Pacific: 05:42 PM")
        self.assertEqual(time_lookup.get_times_for_location("usa"), "New York: 08:42 PM on October 01, 2026")

    def test_multiple_matches_keep_eight_zone_limit(self):
        zones = time_lookup.list_timezones("America/")[:12]
        with patch.object(time_lookup, "resolve_timezones_for_query", return_value=zones):
            self.assertEqual(len(time_lookup.get_times_for_location("test region").split(" | ")), 8)

    def test_new_year_date_boundaries_in_both_directions(self):
        FrozenDatetime.instant = datetime(2026, 12, 31, 18, 10, tzinfo=timezone.utc)
        self.assertEqual(time_lookup.get_current_time("Tokyo"),
                         "Tokyo: 03:10 AM on January 01, 2027")
        FrozenDatetime.instant = datetime(2026, 1, 1, 2, 10, tzinfo=timezone.utc)
        self.assertEqual(time_lookup.get_current_time("Los Angeles"),
                         "Los Angeles: 06:10 PM on December 31, 2025")

    def test_fractional_hour_offsets_and_leap_day(self):
        FrozenDatetime.instant = datetime(2028, 2, 28, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(time_lookup.get_current_time("India"),
                         "Kolkata: 01:30 AM on February 29, 2028")
        dt = time_lookup._fetch_time("Asia/Kathmandu")
        self.assertEqual((dt.hour, dt.minute), (1, 45))

    def test_dst_summer_and_winter_offsets(self):
        for zone, summer, winter in (("America/New_York", -4, -5), ("Europe/London", 1, 0)):
            for month, expected in ((7, summer), (1, winter)):
                with self.subTest(zone=zone, month=month):
                    FrozenDatetime.instant = datetime(2026, month, 2, 12, tzinfo=timezone.utc)
                    dt = time_lookup._fetch_time(zone)
                    self.assertEqual(dt.utcoffset().total_seconds(), expected * 3600)

    def test_dated_named_reply_follows_dst_and_local_calendar(self):
        cases = [(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), "New York: 01:59 AM on March 08, 2026"),
                 (datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc), "New York: 03:00 AM on March 08, 2026"),
                 (datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), "New York: 01:30 AM on November 01, 2026"),
                 (datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc), "New York: 01:30 AM on November 01, 2026")]
        for instant, expected in cases:
            with self.subTest(instant=instant):
                FrozenDatetime.instant = instant
                self.assertEqual(time_lookup.get_times_for_location("New York"), expected)

    def test_world_clock_compact_format_is_unchanged(self):
        FrozenDatetime.instant = datetime(2026, 10, 2, 3, 24, tzinfo=timezone.utc)
        expected = (
            "London: 04:24 AM | Berlin: 05:24 AM | Moscow: 06:24 AM | Johannesburg: 05:24 AM | "
            "Dubai: 07:24 AM | Delhi: 08:54 AM | Shanghai: 11:24 AM | Tokyo: 12:24 PM | "
            "Sydney: 01:24 PM | Auckland: 04:24 PM | S\u00e3o Paulo: 12:24 AM | New York: 11:24 PM | "
            "Chicago: 10:24 PM | Denver: 09:24 PM | Los Angeles: 08:24 PM | Anchorage: 07:24 PM | Honolulu: 05:24 PM"
        )
        self.assertEqual(time_lookup.world_clock_summary(), expected)

    def test_single_and_compact_summaries_fit_existing_limit_with_decoration(self):
        # Reserve a full-length username plus a long emote name without editing
        # the existing role/emote/filter mechanisms or omitting any selected zone.
        prefix = "Co-Pilot @" + "v" * 25 + " "
        suffix = " " + "E" * 50
        replies = [time_lookup.get_times_for_location(alias) for alias in time_lookup.TIMEZONE_ALIASES]
        replies += [time_lookup.get_default_local_time(), time_lookup.world_clock_summary(),
                    time_lookup.get_times_for_location("america")]
        longest = sorted(time_lookup.list_timezones(), key=lambda zone: len(zone.split("/")[-1]), reverse=True)[:8]
        with patch.object(time_lookup, "resolve_timezones_for_query", return_value=longest):
            summary = time_lookup.get_times_for_location("test region")
        self.assertEqual(len(summary.split(" | ")), 8)
        self.assertNotIn(" on ", summary)
        replies.append(summary)
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertLessEqual(len((prefix + reply + suffix).encode("utf-8")), 500)

    def test_new_york_spring_gap_and_fall_fold(self):
        cases = [(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), (1, 59), -5, 0),
                 (datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc), (3, 0), -4, 0),
                 (datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), (1, 30), -4, 0),
                 (datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc), (1, 30), -5, 1)]
        for instant, expected_time, offset, fold in cases:
            with self.subTest(instant=instant):
                FrozenDatetime.instant = instant
                dt = time_lookup._fetch_time("America/New_York")
                self.assertEqual((dt.hour, dt.minute), expected_time)
                self.assertEqual(dt.utcoffset().total_seconds(), offset * 3600)
                self.assertEqual(dt.fold, fold)

    def test_london_spring_gap_and_fall_fold(self):
        cases = [(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc), (0, 59), 0, 0),
                 (datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), (2, 0), 1, 0),
                 (datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), (1, 30), 1, 0),
                 (datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc), (1, 30), 0, 1)]
        for instant, expected_time, offset, fold in cases:
            with self.subTest(instant=instant):
                FrozenDatetime.instant = instant
                dt = time_lookup._fetch_time("Europe/London")
                self.assertEqual((dt.hour, dt.minute), expected_time)
                self.assertEqual(dt.utcoffset().total_seconds(), offset * 3600)
                self.assertEqual(dt.fold, fold)

    def test_missing_zone_data_raises_truthful_sanitized_failure(self):
        with patch.object(time_lookup, "ZoneInfo", side_effect=ZoneInfoNotFoundError("PRIVATE_ERROR")), \
             self.assertLogs(time_lookup.log, level="INFO") as captured, \
             self.assertRaises(time_lookup.TimeLookupUnavailable):
            time_lookup.get_times_for_location("Tokyo")
        self.assert_diagnostic(captured, "time_by_zone", "timezone_data_unavailable", "ZoneInfoNotFoundError")

    def test_missing_or_broken_catalog_is_distinct_from_no_match(self):
        for values in (set(), None, {None}):
            with self.subTest(values=values), patch.object(time_lookup, "available_timezones", return_value=values), \
                 self.assertLogs(time_lookup.log, level="INFO") as captured, \
                 self.assertRaises(time_lookup.TimeLookupUnavailable):
                time_lookup.list_timezones()
            self.assertIn("reason=timezone_data_unavailable", "\n".join(captured.output))

    def test_clock_error_does_not_expose_exception_message(self):
        with patch.object(FrozenDatetime, "now", side_effect=OSError("PRIVATE_ERROR")), \
             self.assertLogs(time_lookup.log, level="INFO") as captured, \
             self.assertRaises(time_lookup.TimeLookupUnavailable):
            time_lookup.get_default_local_time()
        self.assert_diagnostic(captured, "time_by_zone", "clock_error", "OSError")

    def test_world_clock_and_multi_country_keep_partial_results(self):
        original = time_lookup._fetch_time

        def one_failure(zone):
            if zone == "America/Chicago":
                raise time_lookup.TimeLookupUnavailable("test failure")
            return original(zone)

        with patch.object(time_lookup, "_fetch_time", side_effect=one_failure):
            self.assertEqual(len(time_lookup.world_clock_summary().split(" | ")), 16)
            self.assertEqual(time_lookup.get_times_for_location("america"),
                             "Eastern: 08:42 PM | Mountain: 06:42 PM | Pacific: 05:42 PM")

    def test_world_clock_all_failures_are_unavailable(self):
        with patch.object(time_lookup, "_fetch_time", side_effect=time_lookup.TimeLookupUnavailable()):
            self.assertEqual(time_lookup.world_clock_summary(), "World clock unavailable")
            with self.assertRaises(time_lookup.TimeLookupUnavailable):
                time_lookup.get_times_for_location("america")

    def test_declared_tzdata_matches_installed_verified_version(self):
        installed = importlib.metadata.version("tzdata")
        self.assertIn("tzdata==" + installed, (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())

    def test_all_public_lookups_work_with_http_and_dns_blocked(self):
        self.assertIsNotNone(time_lookup.get_current_time("Tokyo"))
        self.assertIsNotNone(time_lookup.get_times_for_location("Paris"))
        self.assertIsNotNone(time_lookup.get_default_local_time())
        self.assertTrue(time_lookup.list_timezones())
        self.assertNotEqual(time_lookup.world_clock_summary(), "World clock unavailable")
        self.assertTrue(time_lookup.enhance_time_query("Current time in Tokyo today?"))

    def test_debug_environment_does_not_print_raw_inputs(self):
        stdout = io.StringIO()
        with patch.dict("os.environ", {"MEDLARTV_DEBUG": "true"}), redirect_stdout(stdout), \
             self.assertLogs(time_lookup.log, level="INFO") as captured:
            time_lookup.get_current_time("Tokyo")
            time_lookup.list_timezones("PRIVATE_QUERY")
            time_lookup.resolve_timezones_for_query("PRIVATE_CREDENTIAL")
        self.assertEqual(stdout.getvalue(), "")
        self.assert_diagnostic(captured, "time_by_zone", "ok")


class TimeCommandTests(OfflineCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "MedlarTV/core/command_handlers.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ("handle_time", "handle_timezones", "handle_worldclock")]
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        selected = ast.fix_missing_locations(ast.Module(body=[future] + nodes, type_ignores=[]))
        cls.namespace = {name: getattr(time_lookup, name) for name in
                         ("get_times_for_location", "list_timezones", "world_clock_summary", "TimeLookupUnavailable")}
        exec(compile(selected, str(path), "exec"), cls.namespace)

    def test_bare_command_keeps_eastern_reply(self):
        with patch.dict(sys.modules, {"MedlarTV.core.time_lookup": time_lookup}):
            self.assertEqual(self.namespace["handle_time"]("viewer", "", {}),
                             "Eastern Time (US & Canada): 08:42 PM on October 01, 2026")

    def test_known_city_command_reply_convention(self):
        self.assertEqual(self.namespace["handle_time"]("viewer", "Tokyo", {}),
                         "Tokyo: 09:42 AM on October 02, 2026")

    def test_all_city_and_country_aliases_return_one_dated_resolved_label(self):
        for alias, zone in time_lookup.TIMEZONE_ALIASES.items():
            with self.subTest(alias=alias):
                label = zone.split("/")[-1].replace("_", " ")
                reply = self.namespace["handle_time"]("viewer", alias, {})
                self.assertEqual(reply.count(label + ": "), 1)
                self.assertEqual(reply, time_lookup.get_times_for_location(alias))
                self.assertRegex(reply, r"^[^:]+: \d{2}:\d{2} [AP]M on [A-Za-z]+ \d{2}, \d{4}$")

    def test_single_catalog_command_uses_canonical_label_without_query_wrapper(self):
        self.assertEqual(self.namespace["handle_time"]("viewer", "ASIA/TOKYO", {}),
                         "Tokyo: 09:42 AM on October 02, 2026")
        self.assertEqual(self.namespace["handle_time"]("viewer", "paris", {}),
                         "Paris: 02:42 AM on October 02, 2026")

    def test_country_and_multiple_match_commands_keep_compact_entries(self):
        expected = "Eastern: 08:42 PM | Central: 07:42 PM | Mountain: 06:42 PM | Pacific: 05:42 PM"
        self.assertEqual(self.namespace["handle_time"]("viewer", "america", {}), expected)
        reply = self.namespace["handle_time"]("viewer", "Europe", {})
        selected = time_lookup.resolve_timezones_for_query("Europe")[:8]
        expected_labels = [zone.split("/")[-1].replace("_", " ") for zone in selected]
        actual_labels = [entry.split(": ", 1)[0] for entry in reply.split(" | ")]
        self.assertEqual(actual_labels, expected_labels)
        self.assertNotIn(" on ", reply)
        self.assertNotIn("Current time in", reply)

    def test_named_city_date_rollover_through_explicit_handler(self):
        FrozenDatetime.instant = datetime(2026, 12, 31, 18, 10, tzinfo=timezone.utc)
        self.assertEqual(self.namespace["handle_time"]("viewer", "Tokyo", {}),
                         "Tokyo: 03:10 AM on January 01, 2027")
        FrozenDatetime.instant = datetime(2026, 1, 1, 2, 10, tzinfo=timezone.utc)
        self.assertEqual(self.namespace["handle_time"]("viewer", "Los Angeles", {}),
                         "Los Angeles: 06:10 PM on December 31, 2025")

    def test_unknown_city_command_is_truthful(self):
        self.assertEqual(self.namespace["handle_time"]("viewer", "Atlantis", {}), "Location not found: Atlantis")

    def test_data_failure_is_not_reported_as_unknown_city(self):
        with patch.object(time_lookup, "ZoneInfo", side_effect=ZoneInfoNotFoundError("PRIVATE_ERROR")), \
             self.assertLogs(time_lookup.log, level="WARNING"):
            self.assertEqual(self.namespace["handle_time"]("viewer", "Tokyo", {}), "Time lookup unavailable")
            with patch.dict(sys.modules, {"MedlarTV.core.time_lookup": time_lookup}):
                self.assertEqual(self.namespace["handle_time"]("viewer", "", {}), "Unable to get local time")

    def test_timezone_command_formats_matches_and_no_match(self):
        self.assertEqual(self.namespace["handle_timezones"]("viewer", "Tokyo", {}),
                         "1 zones match 'Tokyo': Asia/Tokyo")
        self.assertEqual(self.namespace["handle_timezones"]("viewer", "Atlantis", {}), "No timezones found")
        self.assertRegex(self.namespace["handle_timezones"]("viewer", "", {}), r"^\d+ zones available: ")

    def test_timezone_data_failure_is_unavailable(self):
        with patch.object(time_lookup, "available_timezones", return_value=set()), \
             self.assertLogs(time_lookup.log, level="WARNING"):
            self.assertEqual(self.namespace["handle_timezones"]("viewer", "", {}), "Timezone list unavailable")

    def test_worldclock_command_reply_preserved(self):
        self.assertIn("Tokyo: 09:42 AM", self.namespace["handle_worldclock"]("viewer", "", {}))


class NaturalTimeRoutingTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.fixture = listener_fixture.TwitchFeatureToggleTests()
        self.fixture.setUp()
        self.fixture.namespace.update(
            should_lookup_time=time_lookup.should_lookup_time,
            get_times_for_location=time_lookup.get_times_for_location,
            get_default_local_time=time_lookup.get_default_local_time,
        )

    def message(self, text):
        self.fixture.regular_message(text)

    def test_today_suffix_and_optional_the_are_parsed(self):
        cases = {"What time is it in Tokyo today?": "tokyo",
                 "Current time in the Philippines today!": "philippines",
                 "What's the time in New York today?": "new york",
                 "Time now in Asia/Tokyo today?": "asia/tokyo",
                 "Current time in Etc/GMT+5 today?": "etc/gmt+5"}
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(time_lookup.should_lookup_time(message), (True, expected))

    def test_default_non_time_and_malformed_messages(self):
        self.assertEqual(time_lookup.should_lookup_time("What's the time?"), (True, None))
        self.assertEqual(time_lookup.should_lookup_time("Who made you?"), (False, None))
        for value in (None, 42, [], {}, ""):
            self.assertEqual(time_lookup.should_lookup_time(value), (False, None))

    def test_natural_today_request_uses_local_lookup_and_skips_llm(self):
        self.message("What time is it in Tokyo today?")
        self.fixture.client.send_message.assert_called_once_with("Tokyo: 09:42 AM on October 02, 2026")
        self.fixture.generate_response.assert_not_called()

    def test_natural_city_country_alias_and_catalog_replies_match_commands(self):
        TimeCommandTests.setUpClass()
        for place in ("Tokyo", "Manila", "London", "Japan", "Philippines", "India", "USA", "Paris", "Asia/Tokyo", "America", "Europe"):
            with self.subTest(place=place):
                self.fixture.client.send_message.reset_mock()
                self.message("What time is it in " + place + " today?")
                expected = TimeCommandTests.namespace["handle_time"]("viewer", place, {})
                self.fixture.client.send_message.assert_called_once_with(expected)
        self.fixture.generate_response.assert_not_called()

    def test_natural_request_date_crosses_year_boundary(self):
        FrozenDatetime.instant = datetime(2026, 12, 31, 18, 10, tzinfo=timezone.utc)
        self.message("What time is it in Tokyo today?")
        self.fixture.client.send_message.assert_called_once_with("Tokyo: 03:10 AM on January 01, 2027")

    def test_existing_role_and_emote_delivery_wrap_the_shared_dated_reply(self):
        FrozenDatetime.instant = datetime(2026, 10, 2, 3, 24, tzinfo=timezone.utc)
        path = ROOT / "MedlarTV/tools/twitch_listener.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        role = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_role_prefix")
        selected = ast.fix_missing_locations(ast.Module(body=[role], type_ignores=[]))
        self.fixture.namespace["CHANNEL"] = "#viewer"
        exec(compile(selected, str(path), "exec"), self.fixture.namespace)
        self.message("Hey Medlar, what time is it in Tokyo today?")
        base = "Pilot Tokyo: 12:24 PM on October 02, 2026"
        self.fixture.client.send_message.assert_called_once_with(base)
        self.fixture.namespace["os"] = types.SimpleNamespace(getenv=lambda _key, default: default)
        self.fixture.client.connected = True
        self.fixture.client.user_emotes = ["CoolCat"]
        self.fixture.client._send_raw = Mock()
        from MedlarTV.core.twitch_output import TwitchPolicy
        policy = TwitchPolicy(True, False, "synthetic-clock", {
            "blocked_words": ["forbiddenword"], "blocked_topics": [],
            "response_modes": {"all_caps": {"enabled": True}},
        })
        self.fixture.client._load_output_policy = Mock(return_value=policy)
        emoji_formatter = Mock(side_effect=lambda text, *, config: text)
        caps_formatter = Mock(side_effect=lambda text, *, config: text)
        modules = {}
        for name, values in {
            "MedlarTV.core.content_filter": {"limit_emojis": emoji_formatter, "apply_all_caps_mode": caps_formatter},
            "MedlarTV.core.emotional_system": {"get_current_emotion": lambda: "neutral"},
            "MedlarTV.core.emotion_emote_selector": {"add_emotion_emote": lambda text, _emotion, available: text + " " + available[0]},
        }.items():
            module = types.ModuleType(name)
            module.__dict__.update(values)
            modules[name] = module
        with patch.dict(sys.modules, modules):
            type(self.fixture.client).send_message(self.fixture.client, base)
        emoji_formatter.assert_called_once_with(base, config=policy.config)
        caps_formatter.assert_called_once_with(base, config=policy.config)
        self.assertTrue(policy.enabled)
        self.assertFalse(policy.permits("forbiddenword"))
        self.fixture.client._load_output_policy.assert_called_once_with()
        self.fixture.client._send_raw.assert_called_once_with(
            "PRIVMSG #viewer :Pilot Tokyo: 12:24 PM on October 02, 2026 CoolCat")

    def test_natural_default_keeps_eastern_and_skips_llm(self):
        self.message("What's the time?")
        self.fixture.client.send_message.assert_called_once_with(
            "Eastern Time (US & Canada): 08:42 PM on October 01, 2026")
        self.fixture.generate_response.assert_not_called()

    def test_natural_unknown_city_is_truthful_without_network(self):
        self.message("Current time in Atlantis?")
        self.fixture.client.send_message.assert_called_once_with("Location not found: atlantis")
        self.fixture.generate_response.assert_not_called()

    def test_natural_data_failure_is_unavailable_without_llm_fallback(self):
        with patch.object(time_lookup, "ZoneInfo", side_effect=ZoneInfoNotFoundError("PRIVATE_ERROR")), \
             self.assertLogs(time_lookup.log, level="WARNING"):
            self.message("Current time in Tokyo today?")
        self.fixture.client.send_message.assert_called_once_with("Time lookup unavailable")
        self.fixture.generate_response.assert_not_called()

    def test_natural_default_failure_replies_truthfully(self):
        with patch.object(time_lookup, "ZoneInfo", side_effect=ZoneInfoNotFoundError("PRIVATE_ERROR")), \
             self.assertLogs(time_lookup.log, level="WARNING"):
            self.message("What's the time?")
        self.fixture.client.send_message.assert_called_once_with("Unable to get local time")
        self.fixture.generate_response.assert_not_called()

    def test_enriched_context_format_is_preserved_with_today_suffix(self):
        self.assertEqual(time_lookup.enhance_time_query("Current time in Tokyo today?"),
                         "\n\n[REAL-TIME DATA]\nTokyo: 09:42 AM on October 02, 2026\n[END REAL-TIME DATA]\n\n")


if __name__ == "__main__":
    unittest.main()
