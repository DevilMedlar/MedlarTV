"""Offline tests for LibreTranslate-backed Twitch commands."""

import ast
import json
import logging
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from MedlarTV.core import command_handlers, translation, translation_command


LANGUAGES = {"en", "es", "fr", "ja", "zh-hans"}
LISTENER = Path(__file__).resolve().parents[1] / "MedlarTV" / "tools" / "twitch_listener.py"


def response(status, body):
    result = Mock(status_code=status)
    result.json.return_value = body
    return result


def listener_command_handler(namespace):
    """Load the real listener command method without importing Twitch I/O."""
    tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
    client = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TwitchIRC")
    method = next(node for node in client.body if isinstance(node, ast.FunctionDef) and node.name == "_handle_command")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, method], type_ignores=[]))
    exec(compile(module, str(LISTENER), "exec"), namespace)
    return namespace["_handle_command"]


class TranslationServiceTests(unittest.TestCase):
    def test_commands_use_a_real_loopback_http_endpoint(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.assert_path("/languages")
                self.send_json([{"code": "en"}, {"code": "es"}])

            def do_POST(self):
                self.assert_path("/translate")
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(payload)
                self.send_json({"translatedText": "Hola mundo"})

            def assert_path(self, expected):
                if self.path != expected:
                    raise AssertionError((self.path, expected))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/translate"
            with patch.dict("os.environ", {"LIBRETRANSLATE_URL": url}):
                self.assertEqual(translation_command.handle_tlang_command(), "Supported: en, es")
                self.assertIn("Hola mundo", translation_command.handle_t_command("es en: Hello world", "viewer"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(received, [{"q": "Hello world", "source": "en", "target": "es", "format": "text"}])

    def test_language_lookup_recovers_after_service_starts(self):
        with patch.dict("os.environ", {"LIBRETRANSLATE_URL": "http://127.0.0.1:5055/translate"}), \
             patch.object(translation.requests, "get", side_effect=[
                 requests.ConnectionError("offline"),
                 response(200, [{"code": "EN"}, {"code": "es"}]),
             ]) as get:
            self.assertEqual(translation.get_supported_languages(), set())
            self.assertEqual(translation.get_supported_languages(), {"en", "es"})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.args[0], "http://127.0.0.1:5055/languages")

    def test_language_lookup_rejects_malformed_list(self):
        with patch.object(translation.requests, "get", return_value=response(200, {"code": "en"})):
            self.assertEqual(translation.get_supported_languages(), set())

    def test_aliases_follow_languages_actually_served(self):
        self.assertEqual(translation.normalize_lang("SPANISH", LANGUAGES), "es")
        self.assertEqual(translation.normalize_lang("jp", LANGUAGES), "ja")
        self.assertEqual(translation.normalize_lang("cn", LANGUAGES), "zh-hans")
        self.assertIsNone(translation.normalize_lang("tagalog", LANGUAGES))

    def test_translate_posts_normalized_payload_to_configured_endpoint(self):
        with patch.dict("os.environ", {"LIBRETRANSLATE_URL": "http://127.0.0.1:5055/translate/"}), \
             patch.object(translation.requests, "post", return_value=response(200, {"translatedText": "Hola"})) as post:
            self.assertEqual(
                translation.translate_text("Hello", "sp", "EN", supported={"en", "es"}),
                (True, "Hola"),
            )
        post.assert_called_once_with(
            "http://127.0.0.1:5055/translate",
            json={"q": "Hello", "source": "en", "target": "es", "format": "text"},
            timeout=7,
        )

    def test_translate_rejects_unsupported_languages_without_post(self):
        with patch.object(translation.requests, "post") as post:
            self.assertEqual(
                translation.translate_text("Hello", "ko", supported={"en", "es"}),
                (False, "Unsupported language: ko"),
            )
            self.assertEqual(
                translation.translate_text("Hello", "es", "ko", supported={"en", "es"}),
                (False, "Unsupported source language: ko"),
            )
        post.assert_not_called()

    def test_translate_reports_offline_and_bad_server_results(self):
        with patch.object(translation.requests, "post", side_effect=requests.ConnectionError("offline")):
            ok, message = translation.translate_text("Hello", "es", supported={"en", "es"})
            self.assertFalse(ok)
            self.assertIn("Translator offline", message)

        for result, expected in (
            (response(503, {}), "Translation server error: 503"),
            (response(200, {"translatedText": ""}), "Translation failed (empty result)."),
            (response(200, ["unexpected"]), "Translation failed (empty result)."),
        ):
            with self.subTest(expected=expected), patch.object(translation.requests, "post", return_value=result):
                self.assertEqual(
                    translation.translate_text("Hello", "es", supported={"en", "es"}),
                    (False, expected),
                )

        invalid_json = response(200, {})
        invalid_json.json.side_effect = ValueError("invalid JSON")
        with patch.object(translation.requests, "post", return_value=invalid_json):
            self.assertEqual(
                translation.translate_text("Hello", "es", supported={"en", "es"}),
                (False, "Translation failed (invalid server response)."),
            )

    def test_language_list_never_claims_support_when_offline(self):
        with patch.object(translation, "get_supported_languages", return_value=set()):
            self.assertEqual(translation.supported_list_human(), translation.SERVICE_UNAVAILABLE)
        with patch.object(translation, "get_supported_languages", return_value={"es", "en"}):
            self.assertEqual(translation.supported_list_human(), "Supported: en, es")


class TranslationCommandTests(unittest.TestCase):
    def test_parse_target_source_override_and_free_text(self):
        self.assertEqual(
            translation_command._parse_args("  EN   tl: mahal kita  "),
            ("en", "tl", "mahal kita"),
        )
        self.assertEqual(
            translation_command._parse_args("es Hello, world!"),
            ("es", None, "Hello, world!"),
        )
        self.assertEqual(
            translation_command._parse_args("es Hello: world!"),
            ("es", None, "Hello: world!"),
        )
        self.assertEqual(translation_command._parse_args("es"), (None, None, None))

    def test_missing_args_return_help_without_service_lookup(self):
        with patch.object(translation_command, "get_supported_languages") as supported:
            self.assertEqual(translation_command.handle_t_command("es", "viewer"), translation_command.HELP)
        supported.assert_not_called()

    def test_command_rejects_offline_and_unsupported_languages_without_post(self):
        with patch.object(translation_command, "get_supported_languages", side_effect=[set(), {"en", "es"}, {"en", "es"}]), \
             patch.object(translation_command, "translate_text") as translate:
            self.assertIn("Translator offline", translation_command.handle_t_command("es Hello", "viewer"))
            self.assertIn("Unsupported language 'ko'", translation_command.handle_t_command("ko Hello", "viewer"))
            self.assertIn("Unsupported source language 'ko'", translation_command.handle_t_command("es ko: Hello", "viewer"))
        translate.assert_not_called()

    def test_command_passes_aliases_and_source_override_to_translator(self):
        languages = {"en", "es"}
        with patch.object(translation_command, "get_supported_languages", return_value=languages), \
             patch.object(translation_command, "translate_text", return_value=(True, "Hello")) as translate:
            answer = translation_command.handle_t_command("english sp: Hola", "viewer")
        translate.assert_called_once_with("Hola", "en", "es", supported=languages)
        self.assertIn("@viewer", answer)
        self.assertIn("[en] Hello", answer)
        self.assertIn("from [es] to [en]", answer)

    def test_command_uses_server_auto_detection_without_override(self):
        with patch.object(translation_command, "get_supported_languages", return_value={"en", "es"}), \
             patch.object(translation_command, "translate_text", return_value=(True, "Hello")) as translate:
            answer = translation_command.handle_t_command("en Hola", "viewer")
        self.assertIsNone(translate.call_args.args[2])
        self.assertIn("from [auto] to [en]", answer)

    def test_registry_dispatches_all_translation_aliases(self):
        with patch.object(command_handlers, "handle_t_command", return_value="mock translation") as handle, \
             patch.object(command_handlers, "log_command"):
            for alias in ("t", "trans", "translate"):
                with self.subTest(alias=alias):
                    self.assertEqual(
                        command_handlers.execute_command(alias, "viewer", "user", "es Hello", {}),
                        "mock translation",
                    )
        self.assertEqual(handle.call_count, 3)
        self.assertTrue(all(call.args == ("es Hello", "viewer") for call in handle.call_args_list))

    def test_translation_text_is_omitted_from_command_debug_logs(self):
        with patch.object(command_handlers, "handle_t_command", return_value="private translation"), \
             patch.object(command_handlers, "log_command"), \
             self.assertLogs(command_handlers.log, level="DEBUG") as captured:
            command_handlers.execute_command("t", "viewer", "user", "es private source", {})
        logs = "\n".join(captured.output)
        self.assertNotIn("private source", logs)
        self.assertNotIn("private translation", logs)

    def test_listener_routes_chat_aliases_without_twitch_connection(self):
        namespace = {
            "CHANNEL": "#testchannel",
            "BOT_NICK": "TestBot",
            "_get_active_copilots": lambda: [],
            "_role_prefix": lambda _user: "",
            "_prepare_msg": lambda message: message,
            "log_interaction": Mock(),
            "generate_response": Mock(side_effect=AssertionError("unexpected LLM fallback")),
            "execute_command": command_handlers.execute_command,
            "log": logging.getLogger("test_translation_listener"),
        }
        handle_chat_command = listener_command_handler(namespace)
        client = types.SimpleNamespace(send_message=Mock())
        with patch.object(command_handlers, "handle_t_command", return_value="mock translation") as handle, \
             patch.object(command_handlers, "log_command"):
            for alias in ("t", "trans", "translate"):
                handle_chat_command(client, "viewer", f"!{alias} es Hello", {}, {})
        self.assertEqual(handle.call_count, 3)
        self.assertEqual(client.send_message.call_count, 3)
        client.send_message.assert_any_call("mock translation")
        namespace["generate_response"].assert_not_called()

    def test_language_list_and_help_are_registered(self):
        with patch.object(command_handlers, "handle_tlang_command", return_value="Supported: en, es"), \
             patch.object(command_handlers, "log_command"):
            self.assertEqual(
                command_handlers.execute_command("tlang", "viewer", "user", "", {}),
                "Supported: en, es",
            )
            self.assertEqual(
                command_handlers.execute_command("thelp", "viewer", "user", "", {}),
                translation_command.HELP,
            )

    def test_status_reflects_live_translator_availability(self):
        dependencies = {
            "MedlarTV.avatar.bridge.client": types.SimpleNamespace(is_bridge_available=lambda: False),
            "MedlarTV.core.emotional_system": types.SimpleNamespace(get_emotional_system=lambda: None),
            "MedlarTV.core.memory": types.SimpleNamespace(load_memory=lambda: {}),
            "MedlarTV.core.moderation": types.SimpleNamespace(check_message=lambda *_args: None),
            "MedlarTV.core.stream_management": types.SimpleNamespace(get_access_token=lambda: None),
        }
        with patch.dict(sys.modules, dependencies), \
             patch.object(translation, "get_supported_languages", side_effect=[set(), {"en", "es"}]):
            offline = command_handlers.handle_status("viewer", "", {"bot_nick": "TestBot"})
            online = command_handlers.handle_status("viewer", "", {"bot_nick": "TestBot"})
        self.assertIn("Translation:FAILED", offline)
        self.assertIn("Translation:OK", online)


if __name__ == "__main__":
    unittest.main()
