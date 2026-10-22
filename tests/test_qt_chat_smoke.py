"""Optional real-Qt smoke test; all brain calls are mocked."""

import os
import sys
import time
import types
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class QtChatSmokeTest(unittest.TestCase):
    def test_send_reply_and_new_chat(self):
        from MedlarTV.app import medlar_app

        app = QApplication.instance() or QApplication([])
        defaults = {
            "llm_brain": True,
            "pcg_auto_catch": True,
            "ignore_viewer_pokecatch": True,
            "content_filter": True,
            "timers": True,
            "fuzzy_trigger": True,
        }

        def respond(message, username, *, history, context):
            self.assertEqual((message, username, context), ("hello", "Operator", "desktop"))
            history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": "mock reply"}])
            return "mock reply"

        fake_core = types.ModuleType("MedlarTV.core")
        fake_core.__path__ = []
        fake_brain = types.ModuleType("MedlarTV.core.llm_brain")
        fake_brain.generate_response = respond
        fake_credentials = types.ModuleType("MedlarTV.core.local_credentials")
        fake_credentials.CREDENTIAL_FIELDS = {}
        fake_credentials.credential_present = lambda _key: False
        fake_credentials.save_credential = lambda *_args: self.fail("Smoke test must not write credentials")
        window = None
        with (
            patch.dict(sys.modules, {
                "MedlarTV.core": fake_core,
                "MedlarTV.core.llm_brain": fake_brain,
                "MedlarTV.core.local_credentials": fake_credentials,
            }),
            patch.object(medlar_app, "load_dotenv", return_value=False),
            patch.object(medlar_app.ControlCenter, "_load_settings", return_value=defaults),
            patch.object(medlar_app.ControlCenter, "_save_settings", return_value=None),
            patch.object(medlar_app.ControlCenter, "_start_ollama", return_value=None),
            patch.object(medlar_app, "_ollama_endpoint_open", return_value=False),
            patch.object(medlar_app.subprocess, "Popen", side_effect=AssertionError("Smoke test must not start services")) as spawn,
        ):
            try:
                window = medlar_app.ControlCenter()
                window.ollama_proc = types.SimpleNamespace(poll=lambda: None)
                window._ollama_ready = True
                self.assertIsNone(window.launcher_proc)
                window.show()
                window.btn_home_chat.click()
                app.processEvents()
                window.chk_filter.setChecked(False)
                window.chk_llm.setChecked(True)
                window.chat_input.setText("hello")
                window.send_chat_message()
                for _ in range(100):
                    app.processEvents()
                    if not window._chat_busy:
                        break
                    time.sleep(0.01)
                self.assertFalse(window._chat_busy)
                self.assertIn("You: hello", window.chat_transcript.toPlainText())
                self.assertIn("Medlar: mock reply", window.chat_transcript.toPlainText())
                self.assertEqual(window._chat_history[-1]["content"], "mock reply")
                window.clear_chat()
                self.assertEqual(window.chat_transcript.toPlainText(), "")
                self.assertEqual(window._chat_history, [])
                self.assertIsNone(window.launcher_proc)
                spawn.assert_not_called()
            finally:
                if window is not None:
                    window.ollama_proc = None
                    window._ollama_ready = False
                    window.close()


if __name__ == "__main__":
    unittest.main()
