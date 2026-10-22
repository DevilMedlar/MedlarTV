"""Offline Qt checks for the Command Center's page navigation."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


class FakeOwnedOllama:
    """A ready app-owned process for private chat, without starting one."""

    def poll(self):
        return None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class CommandCenterNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from MedlarTV.app import medlar_app
        from MedlarTV.core import local_credentials

        cls.medlar_app = medlar_app
        cls.local_credentials = local_credentials
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)
        self.settings_path = self.temp_path / "app_settings.yaml"
        self.settings = {
            "llm_brain": True,
            "pcg_auto_catch": True,
            "ignore_viewer_pokecatch": True,
            "content_filter": True,
            "timers": True,
            "fuzzy_trigger": True,
        }
        control_center = self.medlar_app.ControlCenter
        self.patches = [
            patch.object(self.medlar_app, "load_dotenv", return_value=False),
            patch.object(self.medlar_app, "_ollama_endpoint_open", return_value=False),
            patch.object(control_center, "_start_ollama", return_value=None),
            patch.object(self.medlar_app.subprocess, "Popen", side_effect=AssertionError("Navigation spawned a process")),
            patch.object(self.medlar_app.socket, "create_connection", side_effect=AssertionError("Navigation opened a socket")),
            patch.object(self.local_credentials, "credential_present", side_effect=AssertionError("Navigation read credentials")),
            patch.object(self.local_credentials, "save_credential", side_effect=AssertionError("Navigation wrote credentials")),
            patch.object(self.local_credentials, "apply_saved_credentials", side_effect=AssertionError("Navigation loaded credentials")),
            patch.object(Path, "cwd", return_value=self.temp_path),
            patch.object(control_center, "_settings_path", return_value=self.settings_path),
            patch.object(control_center, "_load_settings", autospec=True, side_effect=lambda _window: dict(self.settings)),
            patch.object(control_center, "_save_settings", autospec=True, side_effect=self._save_settings),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

        self.window = control_center()
        self.window.timer.stop()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        # No process was spawned; discard the fake readiness before close.
        self.window.ollama_proc = None
        self.window._ollama_ready = False
        self.window.close()
        self.app.processEvents()
        self.assertFalse(self.settings_path.exists(), "Settings test wrote a real file")

    def _save_settings(self, _window, data):
        self.settings.clear()
        self.settings.update(data)

    def assert_page(self, page):
        self.app.processEvents()
        self.assertIs(self.window.pages.currentWidget(), page)
        self.assertTrue(self.window.status_label.isVisibleTo(self.window))
        self.assertTrue(self.window.status_label.text())

    def test_home_back_repeated_navigation_keeps_status_and_does_not_run_lifecycle(self):
        window = self.window
        window.status_label.setText("Status: Starting...")
        self.assert_page(window.home_page)
        for name in ("btn_nav_home", "btn_nav_chat", "btn_nav_twitch", "btn_nav_settings"):
            self.assertFalse(hasattr(window, name), f"The old top navigation still exposes {name}")

        with patch.object(type(window), "on_start", autospec=True) as start, \
             patch.object(type(window), "on_stop", autospec=True) as stop:
            window.btn_home_chat.click()
            self.assert_page(window.chat_page)
            self.assertTrue(window.btn_chat_home.isVisibleTo(window))
            window.btn_chat_home.click()
            self.assert_page(window.home_page)
            window.btn_home_twitch.click()
            self.assert_page(window.twitch_page)
            self.assertTrue(window.btn_twitch_home.isVisibleTo(window))
            window.btn_twitch_home.click()
            self.assert_page(window.home_page)
            window.btn_home_settings.click()
            self.assert_page(window.settings_page)
            self.assertTrue(window.btn_settings_home.isVisibleTo(window))
            window.btn_settings_home.click()
            self.assert_page(window.home_page)
            window.btn_home_chat.click()
            self.assert_page(window.chat_page)
            window.btn_chat_home.click()
            self.assert_page(window.home_page)
            window.btn_home_settings.click()
            self.assert_page(window.settings_page)
            window.btn_settings_home.click()
            self.assert_page(window.home_page)
            window.btn_home_twitch.click()
            self.assert_page(window.twitch_page)
            window.btn_twitch_home.click()
            self.assert_page(window.home_page)

            start.assert_not_called()
            stop.assert_not_called()

        self.assertIsNone(window.launcher_proc)
        self.assertEqual(window.status_label.text(), "Status: Starting...")

    def test_home_shortcuts_and_back_links_work_at_narrow_and_wide_sizes(self):
        window = self.window
        for width, height in ((640, 540), (1120, 760)):
            with self.subTest(size=(width, height)):
                window.resize(width, height)
                self.app.processEvents()
                for shortcut, page, back in (
                    (window.btn_home_chat, window.chat_page, window.btn_chat_home),
                    (window.btn_home_twitch, window.twitch_page, window.btn_twitch_home),
                    (window.btn_home_settings, window.settings_page, window.btn_settings_home),
                ):
                    self.assert_page(window.home_page)
                    self.assertTrue(shortcut.isVisibleTo(window))
                    shortcut.click()
                    self.assert_page(page)
                    self.assertTrue(back.isVisibleTo(window))
                    back.click()
                    self.assert_page(window.home_page)

    def test_home_shortcut_and_sidebar_accept_keyboard_activation(self):
        window = self.window
        window.btn_home_settings.setFocus()
        self.assertEqual(self.app.focusWidget(), window.btn_home_settings)
        QTest.keyClick(window.btn_home_settings, Qt.Key_Space)
        self.assert_page(window.settings_page)

        personality = window._settings_buttons["personality"]
        personality.setFocus()
        self.assertEqual(self.app.focusWidget(), personality)
        QTest.keyClick(personality, Qt.Key_Space)
        self.app.processEvents()
        self.assertIs(window.settings_stack.currentWidget(), window._settings_sections["personality"])

        window.btn_settings_home.setFocus()
        self.assertEqual(self.app.focusWidget(), window.btn_settings_home)
        QTest.keyClick(window.btn_settings_home, Qt.Key_Space)
        self.assert_page(window.home_page)

    def test_chat_draft_transcript_and_history_survive_navigation(self):
        window = self.window
        window.btn_home_chat.click()
        window.chat_input.setText("draft message")
        window.chat_transcript.appendPlainText("You: earlier\nMedlar: previous reply")
        window._chat_history = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "previous reply"},
        ]
        history = window._chat_history
        chat_page = window.chat_page

        window.btn_chat_home.click()
        window.btn_home_twitch.click()
        self.assert_page(window.twitch_page)
        window.btn_twitch_home.click()
        window.btn_home_settings.click()
        self.assert_page(window.settings_page)
        window.btn_settings_home.click()
        self.assert_page(window.home_page)
        window.btn_home_chat.click()
        self.assert_page(chat_page)

        self.assertEqual(window.chat_input.text(), "draft message")
        self.assertIn("Medlar: previous reply", window.chat_transcript.toPlainText())
        self.assertIs(window._chat_history, history)

    def test_in_flight_reply_completes_while_chat_page_is_hidden(self):
        window = self.window
        window.ollama_proc = FakeOwnedOllama()
        window._ollama_ready = True
        window.btn_home_chat.click()
        window.chat_input.setText("hello")
        with patch.object(self.medlar_app.threading, "Thread") as thread:
            window.send_chat_message()
            thread.return_value.start.assert_called_once_with()

        self.assertTrue(window._chat_busy)
        self.assertFalse(window.btn_chat_send.isEnabled())
        window.btn_chat_home.click()
        self.assert_page(window.home_page)
        window.btn_home_twitch.click()
        self.assert_page(window.twitch_page)

        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "offline reply"},
        ]
        window.chat_response.emit("offline reply", "", history)
        self.app.processEvents()
        self.assert_page(window.twitch_page)

        window.btn_twitch_home.click()
        window.btn_home_chat.click()
        self.assert_page(window.chat_page)
        self.assertFalse(window._chat_busy)
        self.assertTrue(window.btn_chat_send.isEnabled())
        self.assertTrue(window.btn_chat_clear.isEnabled())
        self.assertIn("You: hello", window.chat_transcript.toPlainText())
        self.assertIn("Medlar: offline reply", window.chat_transcript.toPlainText())
        self.assertEqual(window._chat_history, history)

    def test_private_chat_sends_with_twitch_stopped_when_owned_ollama_is_ready(self):
        window = self.window
        window.ollama_proc = FakeOwnedOllama()
        window._ollama_ready = True
        self.assertIsNone(window.launcher_proc)
        window.refresh_status()
        self.assertIn("Twitch stopped", window.status_label.text())
        self.assertIn("Ollama ready", window.status_label.text())

        window.btn_home_chat.click()
        window.chat_input.setText("hello from private chat")
        with patch.object(self.medlar_app.threading, "Thread") as thread:
            window.send_chat_message()
            thread.return_value.start.assert_called_once_with()

        self.assertIsNone(window.launcher_proc)
        self.assertTrue(window._chat_busy)
        self.assertIn("You: hello from private chat", window.chat_transcript.toPlainText())

    def test_settings_checkboxes_save_their_existing_keys(self):
        window = self.window
        window.btn_home_settings.click()
        self.assert_page(window.settings_page)

        for widget, key in (
            (window.chk_llm, "llm_brain"),
            (window.chk_pcg, "pcg_auto_catch"),
            (window.chk_filter, "content_filter"),
            (window.chk_timers, "timers"),
            (window.chk_fuzzy, "fuzzy_trigger"),
        ):
            self.assertTrue(widget.isVisibleTo(window))
            before = widget.isChecked()
            widget.click()
            self.assertEqual(self.settings[key], not before)
            self.assertEqual(widget.isChecked(), not before)

        window.btn_settings_home.click()
        window.btn_home_settings.click()
        self.assert_page(window.settings_page)
        self.assertFalse(window.chk_llm.isChecked())
        self.assertFalse(window.chk_filter.isChecked())


if __name__ == "__main__":
    unittest.main()
