"""Offline Qt checks for Command Center settings editors.

Every configuration path and credential call is redirected to synthetic test data.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
except ImportError:
    QApplication = None


def _set_text(widget, value):
    if hasattr(widget, "setPlainText"):
        widget.setPlainText(value)
    else:
        widget.setText(value)


def _text(widget):
    if hasattr(widget, "toPlainText"):
        return widget.toPlainText()
    return widget.text()


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class CommandCenterSettingsTests(unittest.TestCase):
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
        self.temp_root = Path(self.temp.name)
        self.config_dir = self.temp_root / "MedlarTV" / "config"
        self.config_dir.mkdir(parents=True)
        self.settings_path = self.config_dir / "app_settings.yaml"
        self.personality_path = self.config_dir / "personality.yaml"
        self.moderation_path = self.config_dir / "content_filter.yaml"
        self._write_yaml(
            self.settings_path,
            {"llm_brain": True, "content_filter": True, "timers": True,
             "pcg_auto_catch": True, "fuzzy_trigger": True},
        )
        # Match the editable layout of the actual files while keeping every
        # value synthetic. The store deliberately refuses ambiguous layouts.
        self.personality_path.write_text(
            'personality:\n'
            '  name: "Fixture Medlar"\n'
            '  tone: "warm and brief"\n'
            '  trigger_keywords: ["medlar"]\n'
            '  learning:\n'
            '    adapt_tone: true\n'
            '  response_templates:\n'
            '    greeting:\n'
            '      - "Hello {user}!"\n'
            'translator:\n'
            '  tone: neutral\n'
            '  rules:\n'
            '    - Keep meaning.\n',
            encoding="utf-8",
        )
        self.moderation_path.write_text(
            'blocked_words:\n'
            '  - fixtureword\n'
            'blocked_topics:\n'
            '  - fixturetopic\n'
            'response_modes:\n'
            '  emoji_limit:\n'
            '    enabled: true\n'
            '    max_per_message: 3\n'
            'safety:\n'
            '  max_message_length: 500\n',
            encoding="utf-8",
        )
        self.saved_credentials = {"MEDLARTV_TWITCH_TOKEN": "already-saved-dummy"}

        def save_dummy_credential(key, value):
            self.saved_credentials[key] = value
            return True

        patches = [
            patch.object(self.medlar_app, "load_dotenv", return_value=False),
            patch.object(self.medlar_app, "_ollama_endpoint_open", return_value=False),
            patch.object(self.medlar_app.ControlCenter, "_start_ollama", return_value=None),
            patch.object(self.medlar_app.subprocess, "Popen", side_effect=AssertionError("Settings spawned a process")),
            patch.object(self.medlar_app.socket, "create_connection", side_effect=AssertionError("Settings opened a socket")),
            patch.object(Path, "cwd", return_value=self.temp_root),
            patch.object(self.medlar_app.ControlCenter, "_settings_path", return_value=self.settings_path),
            patch.object(self.local_credentials, "save_credential", side_effect=save_dummy_credential),
            patch.object(self.local_credentials, "credential_present", side_effect=lambda key: key in self.saved_credentials),
            patch.object(self.local_credentials, "apply_saved_credentials", return_value=None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        self.window = self.medlar_app.ControlCenter()
        self.window.timer.stop()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    @staticmethod
    def _write_yaml(path, data):
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    @staticmethod
    def _read_yaml(path):
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def _open_section(self, section):
        if self.window.pages.currentWidget() is not self.window.home_page:
            self.window.btn_settings_home.click()
        self.window.btn_home_settings.click()
        self.window._settings_buttons[section].click()
        self.app.processEvents()

    @staticmethod
    def _reload_with_choice(reload_button, choice):
        def choose(dialog):
            next(button for button in dialog.buttons() if button.text().replace("&", "") == choice).click()
            return 0

        with patch.object(QMessageBox, "exec", new=choose):
            reload_button.click()

    def test_personality_save_reload_and_preserve_unrelated_yaml(self):
        self._open_section("personality")
        _set_text(self.window.personality_name, "New Medlar")
        _set_text(self.window.personality_tone, "calm and curious")
        _set_text(self.window.personality_triggers, "medlar\nnew alias")
        self.window.btn_personality_save.click()

        saved = self._read_yaml(self.personality_path)
        self.assertEqual(saved["personality"]["name"], "New Medlar")
        self.assertEqual(saved["personality"]["tone"], "calm and curious")
        self.assertEqual(saved["personality"]["trigger_keywords"], ["medlar", "new alias"])
        self.assertEqual(saved["personality"]["learning"], {"adapt_tone": True})
        self.assertEqual(saved["personality"]["response_templates"], {"greeting": ["Hello {user}!"]})
        self.assertEqual(saved["translator"], {"tone": "neutral", "rules": ["Keep meaning."]})

        _set_text(self.window.personality_name, "Unsaved name")
        self._reload_with_choice(self.window.btn_personality_reload, "Discard")
        self.assertEqual(_text(self.window.personality_name), "New Medlar")
        self.assertEqual(_text(self.window.personality_tone), "calm and curious")
        self.assertEqual(_text(self.window.personality_triggers).splitlines(), ["medlar", "new alias"])

    def test_personality_invalid_input_does_not_write(self):
        self._open_section("personality")
        before = self.personality_path.read_bytes()
        _set_text(self.window.personality_name, "   ")
        self.window.btn_personality_save.click()
        self.assertEqual(self.personality_path.read_bytes(), before)

    def test_personality_tone_over_300_characters_is_not_truncated(self):
        self._open_section("personality")
        long_tone = "patient and curious " * 20
        self.assertGreater(len(long_tone), 300)
        self.assertLess(len(long_tone), 1000)
        _set_text(self.window.personality_tone, long_tone)
        self.assertEqual(_text(self.window.personality_tone), long_tone)
        self.window.btn_personality_save.click()
        self.assertEqual(self._read_yaml(self.personality_path)["personality"]["tone"], long_tone.strip())
        _set_text(self.window.personality_tone, "unsaved")
        self._reload_with_choice(self.window.btn_personality_reload, "Discard")
        self.assertEqual(_text(self.window.personality_tone), long_tone.strip())

    def test_personality_stale_revision_does_not_overwrite_external_edit(self):
        self._open_section("personality")
        _set_text(self.window.personality_name, "Draft name")
        externally_changed = self._read_yaml(self.personality_path)
        externally_changed["personality"]["tone"] = "external revision"
        self._write_yaml(self.personality_path, externally_changed)
        external_bytes = self.personality_path.read_bytes()

        self.window.btn_personality_save.click()
        self.assertEqual(self.personality_path.read_bytes(), external_bytes)

    def test_settings_section_navigation_keeps_unsaved_personality_draft(self):
        self._open_section("personality")
        _set_text(self.window.personality_tone, "not yet saved")
        self.window._settings_buttons["features"].click()
        self.window.btn_settings_home.click()
        self.window.btn_home_settings.click()
        self.window._settings_buttons["personality"].click()
        self.assertEqual(_text(self.window.personality_tone), "not yet saved")
        self.assertEqual(self._read_yaml(self.personality_path)["personality"]["tone"], "warm and brief")

    def test_sidebar_stays_visible_and_marks_selected_category_at_both_sizes(self):
        window = self.window
        for width, height in ((640, 540), (1120, 760)):
            with self.subTest(size=(width, height)):
                window.resize(width, height)
                window.btn_home_settings.click()
                self.app.processEvents()
                self.assertTrue(window.settings_sidebar.isVisibleTo(window))
                self.assertIsNotNone(window.settings_sidebar.widget())
                self.assertNotEqual(window.settings_sidebar.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
                for category in ("personality", "features", "moderation", "connections"):
                    button = window._settings_buttons[category]
                    self.assertTrue(button.isVisibleTo(window), category)
                    button.click()
                    self.app.processEvents()
                    self.assertTrue(window.settings_sidebar.isVisibleTo(window))
                    self.assertIs(window.settings_stack.currentWidget(), window._settings_sections[category])
                    self.assertTrue(button.property("selected"), category)
                    for other, other_button in window._settings_buttons.items():
                        if other != category:
                            self.assertFalse(other_button.property("selected"), other)
                window.btn_settings_home.click()

    def test_search_finds_descriptions_without_indexing_credential_values(self):
        self._open_section("connections")
        window = self.window
        key = "MEDLARTV_TWITCH_TOKEN"
        dummy = "synthetic-unsaved-credential"
        field = window.connection_inputs[key]
        field.setText(dummy)
        search = window.edit_settings_search

        search.setText("blocked topics")
        self.app.processEvents()
        self.assertTrue(window._settings_buttons["moderation"].isVisibleTo(window))
        self.assertFalse(window._settings_buttons["connections"].isVisibleTo(window))
        self.assertIs(window.settings_stack.currentWidget(), window._settings_sections["moderation"])

        search.setText(dummy)
        self.app.processEvents()
        self.assertTrue(window.settings_empty_state.isVisibleTo(window))
        self.assertTrue(window.settings_search_status.isVisibleTo(window))
        self.assertFalse(window.settings_stack.isVisibleTo(window))
        self.assertFalse(any(button.isVisibleTo(window) for button in window._settings_buttons.values()))
        self.assertNotIn(dummy, window.settings_empty_state.text())
        self.assertNotIn(dummy, window.settings_search_status.text())

        search.clear()
        self.app.processEvents()
        self.assertTrue(window.settings_stack.isVisibleTo(window))
        self.assertTrue(all(button.isVisibleTo(window) for button in window._settings_buttons.values()))
        window._settings_buttons["connections"].click()
        self.assertEqual(field.echoMode(), QLineEdit.Password)
        self.assertEqual(field.text(), dummy)

    def test_failed_personality_and_moderation_saves_keep_drafts(self):
        self._open_section("personality")
        _set_text(self.window.personality_name, "Draft name")
        with patch("MedlarTV.app.config_editor_store.save_personality", side_effect=OSError("offline failure")):
            self.window.btn_personality_save.click()
        self.assertEqual(_text(self.window.personality_name), "Draft name")
        self.assertEqual(self._read_yaml(self.personality_path)["personality"]["name"], "Fixture Medlar")

        self.window._settings_buttons["moderation"].click()
        _set_text(self.window.moderation_words, "draftword")
        with patch("MedlarTV.app.config_editor_store.save_moderation", side_effect=OSError("offline failure")):
            self.window.btn_moderation_save.click()
        self.assertEqual(_text(self.window.moderation_words), "draftword")
        self.assertEqual(self._read_yaml(self.moderation_path)["blocked_words"], ["fixtureword"])

    def test_reload_guard_cancel_save_and_failed_save_preserve_draft(self):
        self._open_section("personality")
        _set_text(self.window.personality_name, "Draft name")

        self._reload_with_choice(self.window.btn_personality_reload, "Cancel")
        self.assertEqual(_text(self.window.personality_name), "Draft name")
        self.assertEqual(self._read_yaml(self.personality_path)["personality"]["name"], "Fixture Medlar")

        with patch("MedlarTV.app.config_editor_store.save_personality", side_effect=OSError("offline failure")):
            self._reload_with_choice(self.window.btn_personality_reload, "Save")
        self.assertEqual(_text(self.window.personality_name), "Draft name")
        self.assertEqual(self._read_yaml(self.personality_path)["personality"]["name"], "Fixture Medlar")

        self._reload_with_choice(self.window.btn_personality_reload, "Save")
        self.assertEqual(_text(self.window.personality_name), "Draft name")
        self.assertEqual(self._read_yaml(self.personality_path)["personality"]["name"], "Draft name")

    def test_failed_feature_save_reverts_checkbox_and_shows_error(self):
        self._open_section("features")
        before = self.window.chk_llm.isChecked()
        with patch.object(type(self.window), "_save_settings", side_effect=OSError("synthetic disk failure")):
            self.window.chk_llm.click()
        self.app.processEvents()

        self.assertEqual(self.window.chk_llm.isChecked(), before)
        self.assertEqual(self._read_yaml(self.settings_path)["llm_brain"], before)
        self.assertTrue(self.window.features_status.isVisibleTo(self.window))
        message = self.window.features_status.text().lower()
        self.assertIn("sav", message)
        self.assertTrue(
            any(word in message for word in ("fail", "unable", "could not", "not saved")),
            message,
        )

    def test_moderation_save_reload_and_preserve_unrelated_yaml(self):
        self._open_section("moderation")
        _set_text(self.window.moderation_words, "firstword\nsecondword")
        _set_text(self.window.moderation_topics, "first topic\nsecond topic")
        self.window.btn_moderation_save.click()

        saved = self._read_yaml(self.moderation_path)
        self.assertEqual(saved["blocked_words"], ["firstword", "secondword"])
        self.assertEqual(saved["blocked_topics"], ["first topic", "second topic"])
        self.assertEqual(saved["response_modes"], {"emoji_limit": {"enabled": True, "max_per_message": 3}})
        self.assertEqual(saved["safety"], {"max_message_length": 500})

        _set_text(self.window.moderation_words, "unsavedword")
        self._reload_with_choice(self.window.btn_moderation_reload, "Discard")
        self.assertEqual(_text(self.window.moderation_words).splitlines(), ["firstword", "secondword"])
        self.assertEqual(_text(self.window.moderation_topics).splitlines(), ["first topic", "second topic"])

    def test_moderation_stale_revision_does_not_overwrite_external_edit(self):
        self._open_section("moderation")
        _set_text(self.window.moderation_words, "draftword")
        externally_changed = self._read_yaml(self.moderation_path)
        externally_changed["blocked_topics"] = ["external topic"]
        self._write_yaml(self.moderation_path, externally_changed)
        external_bytes = self.moderation_path.read_bytes()

        self.window.btn_moderation_save.click()
        self.assertEqual(self.moderation_path.read_bytes(), external_bytes)

    def test_moderation_lists_can_be_cleared_and_reloaded(self):
        self._open_section("moderation")
        _set_text(self.window.moderation_words, "")
        _set_text(self.window.moderation_topics, "")
        self.window.btn_moderation_save.click()

        saved = self._read_yaml(self.moderation_path)
        self.assertEqual(saved["blocked_words"], [])
        self.assertEqual(saved["blocked_topics"], [])
        self.assertEqual(saved["safety"], {"max_message_length": 500})

        _set_text(self.window.moderation_words, "unsavedword")
        self._reload_with_choice(self.window.btn_moderation_reload, "Discard")
        self.assertEqual(_text(self.window.moderation_words), "")
        self.assertEqual(_text(self.window.moderation_topics), "")

    def test_connections_are_masked_empty_and_save_only_dummy_secret(self):
        self._open_section("connections")
        self.assertIn("MEDLARTV_TWITCH_TOKEN", self.window.connection_inputs)
        for key, widget in self.window.connection_inputs.items():
            self.assertEqual(widget.echoMode(), QLineEdit.Password, key)
            self.assertEqual(widget.text(), "", key)
            self.assertIn(key, self.window.connection_saved_labels)

        key = "MEDLARTV_TWITCH_TOKEN"
        dummy = "dummy-irc-token-for-offline-test"
        self.window.connection_inputs[key].setText(dummy)
        self.window.save_connection(key)
        self.assertEqual(self.saved_credentials[key], dummy)
        self.assertEqual(self.window.connection_inputs[key].text(), "")
        self.window.show_settings_section("features")
        self.window.show_settings_section("connections")
        self.assertEqual(self.window.connection_inputs[key].text(), "")
        self.assertNotIn(dummy, self.window.connection_saved_labels[key].text())

    def test_failed_credential_save_keeps_masked_input(self):
        self._open_section("connections")
        key = "MEDLARTV_TWITCH_TOKEN"
        dummy = "synthetic-unsaved-credential"
        field = self.window.connection_inputs[key]
        field.setText(dummy)
        with patch.object(self.local_credentials, "save_credential", side_effect=OSError("offline failure")):
            self.window.save_connection(key)
        self.assertEqual(field.echoMode(), QLineEdit.Password)
        self.assertEqual(field.text(), dummy)
        self.assertEqual(self.saved_credentials[key], "already-saved-dummy")
        self.assertNotIn(dummy, self.window.connection_status.text())
        self.assertNotIn(dummy, self.window.connection_saved_labels[key].text())


if __name__ == "__main__":
    unittest.main()
