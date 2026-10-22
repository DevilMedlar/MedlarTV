"""Synthetic checks that interaction analytics never retain chat content."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from MedlarTV.core import interaction_logger


class InteractionLoggerPrivacyTests(unittest.TestCase):
    def test_contact_and_health_text_is_absent_from_persisted_interactions(self):
        with TemporaryDirectory() as temporary, \
                patch.object(interaction_logger, "LOG_DIRECTORY", temporary), \
                patch.object(interaction_logger, "ENABLE_LOGGING", True), \
                patch.object(interaction_logger, "DEBUG", False):
            interaction_logger.log_interaction(
                "mira@example.invalid",
                "Email mira@example.invalid, call 555-0199, or visit 18 Cedar Street. I have diabetes.",
                "Write to mira@example.invalid",
                context={"raw": "18 Cedar Street"},
            )
            interaction_logger.log_command(
                "SyntheticViewer", "!safe",
                args={"contact": "mira@example.invalid"},
                error="555-0199",
            )
            interaction = json.loads((Path(temporary) / "interactions.jsonl").read_text())
            command = json.loads((Path(temporary) / "commands.jsonl").read_text())
            self.assertIsNone(interaction["message"])
            self.assertIsNone(interaction["response"])
            self.assertIsNone(interaction["user"])
            self.assertNotIn("context", interaction)
            self.assertEqual(command["command"], "!safe")
            self.assertNotIn("args", command)
            self.assertNotIn("error", command)
            serialized = repr((interaction, command))
            for detail in ("mira@example.invalid", "555-0199", "18 Cedar Street", "diabetes"):
                self.assertNotIn(detail, serialized)


if __name__ == "__main__":
    unittest.main()
