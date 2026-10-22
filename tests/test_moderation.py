"""Offline checks for policy-independent moderation helpers and readiness."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from MedlarTV.core import command_handlers, moderation


ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class ModerationPathAndDomainTests(unittest.TestCase):
    def setUp(self):
        moderation._timeouts.clear()
        self.addCleanup(moderation._timeouts.clear)

    def test_default_config_paths_are_repository_anchored(self):
        expected = ROOT / "MedlarTV" / "config"
        self.assertEqual(moderation.CONFIG_DIR, expected)
        self.assertEqual(moderation.LINK_WHITELIST_PATH, expected / "link_whitelist.yaml")
        self.assertEqual(moderation.CONTENT_FILTER_PATH, expected / "content_filter.yaml")
        self.assertTrue(moderation.CONFIG_DIR.is_absolute())

    def test_config_loading_is_independent_of_process_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            elsewhere = root / "elsewhere"
            config.mkdir()
            elsewhere.mkdir()
            whitelist = config / "link_whitelist.yaml"
            content_filter = config / "content_filter.yaml"
            whitelist.write_text("whitelist:\n  - example.test\n", encoding="utf-8")
            content_filter.write_text(
                "blocked_words:\n  - blockedtoken\nblocked_topics: []\n",
                encoding="utf-8",
            )

            original_cwd = Path.cwd()
            try:
                os.chdir(elsewhere)
                with patch.object(moderation, "LINK_WHITELIST_PATH", whitelist), \
                        patch.object(moderation, "CONTENT_FILTER_PATH", content_filter), \
                        patch.dict(os.environ, {
                            "MOD_LINK_FILTER": "false",
                            "MOD_CAPS_FILTER": "false",
                            "MOD_SPAM_FILTER": "false",
                        }):
                    self.assertEqual(moderation.load_link_whitelist(), {"example.test"})
                    result = moderation.check_message("viewer", "a blockedtoken appears")
            finally:
                os.chdir(original_cwd)

        self.assertFalse(result["is_allowed"])
        self.assertEqual(result["reason"], "blocked word")

    def test_domain_normalization_removes_only_a_literal_www_prefix(self):
        with patch.object(
            moderation,
            "load_link_whitelist",
            return_value={"wikipedia.org", "wow.com"},
        ), patch.dict(os.environ, {
            "MOD_LINK_FILTER": "true",
            "MOD_CAPS_FILTER": "false",
            "MOD_SPAM_FILTER": "false",
        }):
            for message in ("https://wikipedia.org/wiki/Test", "www.wow.com/news"):
                with self.subTest(message=message):
                    self.assertTrue(
                        moderation.check_message("viewer", message)["is_allowed"]
                    )

            rejected = moderation.check_message("viewer", "https://notallowed.example/page")

        self.assertFalse(rejected["is_allowed"])
        self.assertEqual(rejected["reason"], "link not allowed")


class ModerationStatusTests(unittest.TestCase):
    def test_status_does_not_claim_inbound_moderation_is_ready(self):
        emotional_system = types.SimpleNamespace()
        dependencies = {
            "MedlarTV.avatar.bridge.client": _module(
                "MedlarTV.avatar.bridge.client", is_bridge_available=lambda: True,
            ),
            "MedlarTV.core.emotional_system": _module(
                "MedlarTV.core.emotional_system",
                get_emotional_system=lambda: emotional_system,
            ),
            "MedlarTV.core.memory": _module(
                "MedlarTV.core.memory", load_memory=lambda: {"loaded": True},
            ),
            "MedlarTV.core.translation": _module(
                "MedlarTV.core.translation", get_supported_languages=lambda: ["en"],
            ),
            "MedlarTV.core.stream_management": _module(
                "MedlarTV.core.stream_management", get_access_token=lambda: "test-token",
            ),
        }
        with patch.dict(sys.modules, dependencies):
            response = command_handlers.handle_status(
                "viewer", "", {"socket_connected": True, "bot_nick": "MedlarTV"},
            )

        self.assertIn("Moderation:NOT_INTEGRATED", response)
        self.assertNotIn("Moderation:OK", response)


if __name__ == "__main__":
    unittest.main()
