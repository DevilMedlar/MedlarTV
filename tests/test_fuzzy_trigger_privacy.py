"""Offline check that trigger diagnostics never echo a chat message."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from MedlarTV.core import fuzzy_trigger


class FuzzyTriggerPrivacyTests(unittest.TestCase):
    def test_trigger_diagnostics_omit_contact_message(self):
        message = "medlar my contact number: 55 50123"
        output = io.StringIO()
        with patch.object(fuzzy_trigger, "load_trigger_keywords", return_value=["medlar"]):
            with redirect_stdout(output):
                self.assertTrue(fuzzy_trigger.should_respond(message))
        self.assertNotIn(message, output.getvalue())
        self.assertNotIn("55 50123", output.getvalue())


if __name__ == "__main__":
    unittest.main()
