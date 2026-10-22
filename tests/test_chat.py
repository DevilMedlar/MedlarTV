"""Offline tests for the command center chat boundary and brain history."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Widget:
    def __init__(self, value=""):
        self.value = value
        self.enabled = True
        self.focused = False
        self.lines = []

    def text(self):
        return self.value

    def clear(self):
        self.value = ""
        self.lines.clear()

    def setEnabled(self, enabled):
        self.enabled = enabled

    def setFocus(self):
        self.focused = True

    def setText(self, value):
        self.value = value

    def appendPlainText(self, value):
        self.lines.append(value)

    def isChecked(self):
        return self.value


class Emitter:
    def __init__(self, window):
        self.window = window

    def emit(self, reply, error, history):
        self.window._on_chat_response(reply, error, history)


class BaseWindow:
    def closeEvent(self, event):
        event.closed = True


class FakeProcess:
    def poll(self):
        return None


class ChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qtwidgets = module("PySide6.QtWidgets", QApplication=type("QApplication", (), {"instance": staticmethod(lambda: None)}))
        for name in ("QWidget", "QVBoxLayout", "QPushButton", "QHBoxLayout", "QLabel", "QCheckBox", "QLineEdit", "QPlainTextEdit", "QFrame", "QStackedWidget", "QScrollArea", "QMessageBox"):
            setattr(qtwidgets, name, type(name, (), {}))
        qtwidgets.QMainWindow = BaseWindow
        qtcore = module("PySide6.QtCore", Qt=types.SimpleNamespace(), QTimer=type("QTimer", (), {}), Signal=lambda *args: None, QEvent=type("QEvent", (), {}))
        fake_modules = {
            "PySide6": module("PySide6"),
            "PySide6.QtWidgets": qtwidgets,
            "PySide6.QtCore": qtcore,
            "PySide6.QtGui": module("PySide6.QtGui", QIcon=type("QIcon", (), {})),
            "dotenv": module("dotenv", load_dotenv=lambda: None),
            "yaml": module("yaml", safe_load=lambda stream: {}, safe_dump=lambda *args, **kwargs: ""),
            "requests": module("requests"),
            "MedlarTV": module("MedlarTV"),
            "MedlarTV.core": module("MedlarTV.core"),
            "MedlarTV.core.ollama_endpoint": module(
                "MedlarTV.core.ollama_endpoint",
                ollama_base_url=lambda: "http://127.0.0.1:11434",
            ),
            "MedlarTV.core.emotional_system": module(
                "MedlarTV.core.emotional_system",
                get_emotional_system=lambda: None,
                get_emotion_state=lambda: {},
                get_current_emotion=lambda: "neutral",
            ),
            "MedlarTV.core.mood_system": module("MedlarTV.core.mood_system", compute_mood=lambda _: {}, get_mood_label=lambda *_: "Neutral"),
            "MedlarTV.core.sentiment_advanced": module(
                "MedlarTV.core.sentiment_advanced",
                analyze_sentiment_advanced=lambda _: (0.0, {}),
                detect_emotional_keywords=lambda _: {},
            ),
            "MedlarTV.core.emotion_emote_selector": module("MedlarTV.core.emotion_emote_selector", get_multiple_emotion_emotes=lambda *_: []),
        }
        # Permit reviewed leaf modules without executing eager package startup.
        fake_modules["MedlarTV"].__path__ = [str(ROOT / "MedlarTV")]
        fake_modules["MedlarTV.core"].__path__ = [str(ROOT / "MedlarTV/core")]
        cls.modules_patch = patch.dict(sys.modules, fake_modules)
        cls.modules_patch.start()
        cls.addClassCleanup(cls.modules_patch.stop)
        cls.app = load_source("medlar_app_under_test", "MedlarTV/app/medlar_app.py")
        cls.brain = load_source("llm_brain_under_test", "MedlarTV/core/llm_brain.py")

    def window(self):
        window = self.app.ControlCenter.__new__(self.app.ControlCenter)
        window.chat_input = Widget()
        window.btn_chat_send = Widget()
        window.btn_chat_clear = Widget()
        window.chat_transcript = Widget()
        window.chat_status = Widget()
        window.chk_llm = Widget(True)
        window.chk_filter = Widget(True)
        window._chat_history = []
        window._chat_busy = False
        window._chat_closed = False
        window.launcher_proc = None
        window._stop_file = None
        window.ollama_proc = FakeProcess()
        window._ollama_ready = True
        window._ollama_error = None
        window._shutdown_failure = None
        window.status_label = Widget("Status: Idle")
        window.chat_response = Emitter(window)
        return window

    def test_private_chat_sends_without_twitch_launcher_when_ollama_ready(self):
        window = self.window()
        window.chat_input.value = "private hello"
        pending = []

        class FakeThread:
            def __init__(self, **kwargs):
                pending.append(kwargs)

            def start(self):
                pass

        self.assertIsNone(window.launcher_proc)
        with patch.object(self.app.threading, "Thread", FakeThread):
            window.send_chat_message()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["args"][0], "private hello")
        self.assertEqual(window.chat_transcript.lines, ["You: private hello"])
        self.assertTrue(window._chat_busy)

    def test_busy_prevents_duplicate_send_and_clear(self):
        window = self.window()
        window.chat_input.value = "  hello  "
        pending = []

        class FakeThread:
            def __init__(self, **kwargs):
                pending.append(kwargs)

            def start(self):
                pass

        with patch.object(self.app.threading, "Thread", FakeThread):
            window.send_chat_message()
            window.chat_input.value = "another"
            window.send_chat_message()
            window.clear_chat()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["args"][0], "hello")
        self.assertTrue(pending[0]["daemon"])
        self.assertEqual(window.chat_transcript.lines, ["You: hello"])
        self.assertFalse(window.btn_chat_send.enabled)
        self.assertFalse(window.btn_chat_clear.enabled)

    def test_settings_and_empty_input_block_send(self):
        window = self.window()
        window.chat_input.value = "   "
        window.send_chat_message()
        self.assertFalse(window._chat_busy)
        window.chat_input.value = "hello"
        window.chk_llm.value = False
        window.send_chat_message()
        self.assertIn("Enable LLM Brain", window.chat_status.value)
        self.assertEqual(window.chat_transcript.lines, [])

    def test_filtered_reply_replaces_history_and_clear_starts_fresh(self):
        window = self.window()

        def respond(message, username, *, history, context):
            self.assertEqual((message, username, context), ("hello", "Operator", "desktop"))
            history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": "raw"}])
            return "raw"

        fake_brain = module("MedlarTV.core.llm_brain", generate_response=respond)
        fake_filter = module("MedlarTV.core.content_filter", filter_message=lambda text: (True, "safe", None), get_safety_response=lambda: "blocked")
        with patch.dict(sys.modules, {"MedlarTV.core.llm_brain": fake_brain, "MedlarTV.core.content_filter": fake_filter}):
            window._chat_busy = True
            window._generate_chat_reply("hello", [], True)

        self.assertEqual(window.chat_transcript.lines, ["Medlar: safe\n"])
        self.assertEqual(window._chat_history[-1]["content"], "safe")
        self.assertFalse(window._chat_busy)
        window.clear_chat()
        self.assertEqual(window._chat_history, [])
        self.assertEqual(window.chat_transcript.lines, [])

    def test_filter_toggle_and_blocked_reply(self):
        def respond(message, username, *, history, context):
            history.append({"role": "assistant", "content": "raw"})
            return "raw"

        fake_brain = module("MedlarTV.core.llm_brain", generate_response=respond)
        fake_filter = module("MedlarTV.core.content_filter", filter_message=lambda text: (False, None, "blocked"), get_safety_response=lambda: "safe fallback")
        with patch.dict(sys.modules, {"MedlarTV.core.llm_brain": fake_brain, "MedlarTV.core.content_filter": fake_filter}):
            filtered = self.window()
            filtered._generate_chat_reply("hello", [], True)
            self.assertEqual(filtered._chat_history[-1]["content"], "safe fallback")
            unfiltered = self.window()
            unfiltered._generate_chat_reply("hello", [], False)
            self.assertEqual(unfiltered._chat_history[-1]["content"], "raw")

    def test_thread_start_failure_restores_controls(self):
        window = self.window()
        window.chat_input.value = "hello"

        class BrokenThread:
            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread failed")

        with patch.object(self.app.threading, "Thread", BrokenThread):
            window.send_chat_message()
        self.assertFalse(window._chat_busy)
        self.assertTrue(window.btn_chat_send.enabled)
        self.assertIn("thread failed", window.chat_status.value)

    def test_failure_restores_controls_without_committing_history(self):
        window = self.window()
        window._chat_busy = True
        window._chat_history = [{"role": "assistant", "content": "old"}]
        fake_brain = module("MedlarTV.core.llm_brain", generate_response=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("backend failed")))
        with patch.dict(sys.modules, {"MedlarTV.core.llm_brain": fake_brain}):
            window._generate_chat_reply("hello", list(window._chat_history), False)
        self.assertIn("backend failed", window.chat_status.value)
        self.assertEqual(window._chat_history, [{"role": "assistant", "content": "old"}])
        self.assertTrue(window.btn_chat_send.enabled)

    def test_close_during_request_ignores_late_reply(self):
        window = self.window()
        window._chat_busy = True
        event = types.SimpleNamespace(closed=False)
        with patch.object(self.app.ControlCenter, "_shutdown_all", return_value=True) as shutdown:
            window.closeEvent(event)
        shutdown.assert_called_once_with()
        window._on_chat_response("late", "", [{"role": "assistant", "content": "late"}])
        self.assertTrue(event.closed)
        self.assertEqual(window.chat_transcript.lines, [])
        self.assertEqual(window._chat_history, [])

    def test_brain_keeps_desktop_and_twitch_histories_separate(self):
        brain = self.brain
        brain.clear_history()
        desktop = []
        calls = []

        def reply(messages):
            calls.append(messages)
            return "hello back"

        with patch.multiple(
            brain,
            _load_personality=lambda: {},
            get_emotional_system=lambda: types.SimpleNamespace(process_message=lambda _: None),
            get_emotion_state=lambda: {},
            get_current_emotion=lambda: "neutral",
            compute_mood=lambda _: {"label": "Neutral"},
            analyze_sentiment_advanced=lambda _: (0.0, {}),
            check_ollama_health=lambda: True,
            _call_ollama_chat=reply,
        ):
            brain.generate_response("desktop prompt", "Operator", history=desktop, context="desktop", memory_context="PRIVATE_VIEWER_MEMORY")
            self.assertEqual(brain._conversation_history, [])
            self.assertEqual([item["role"] for item in desktop], ["user", "assistant"])
            self.assertIn("Control Center", calls[0][0]["content"])
            self.assertNotIn("PRIVATE_VIEWER_MEMORY", str(calls[0]))
            brain.generate_response("twitch prompt", "viewer")
            self.assertEqual(len(brain._conversation_history), 2)
            self.assertNotIn("desktop prompt", str(calls[1]))
            self.assertIn("live Twitch chat", str(calls[1]))
        brain.clear_history()

    def test_twitch_history_keeps_user_statement_with_false_assistant_recap(self):
        brain = self.brain
        brain.clear_history()
        self.addCleanup(brain.clear_history)
        calls = []
        first_reply = "You told me your favorite food is pizza."
        replies = iter([first_reply, "You said gnocchi."])

        def reply(messages):
            calls.append([dict(item) for item in messages])
            return next(replies)

        with patch.multiple(
            brain,
            _load_personality=lambda: {},
            get_emotional_system=lambda: types.SimpleNamespace(process_message=lambda _: None),
            get_emotion_state=lambda: {},
            get_current_emotion=lambda: "neutral",
            compute_mood=lambda _: {"label": "Neutral"},
            analyze_sentiment_advanced=lambda _: (0.0, {}),
            check_ollama_health=lambda: True,
            _call_ollama_chat=reply,
        ):
            brain.generate_response("My favorite food is gnocchi.", "viewer")
            brain.generate_response("What food did I say was my favorite?", "viewer")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][-3:], [
            {"role": "user", "content": "viewer: My favorite food is gnocchi."},
            {"role": "assistant", "content": first_reply},
            {"role": "user", "content": "viewer: What food did I say was my favorite?"},
        ])
        self.assertNotIn("gnocchi", calls[1][-2]["content"])
        self.assertEqual(brain._conversation_history[:3], calls[1][-3:])

    def test_scoped_memory_and_search_context_are_model_only(self):
        brain = self.brain
        brain.clear_history()
        self.addCleanup(brain.clear_history)
        calls = []
        question = "What fruit did I mention?"
        memory_context = "Earlier viewer statement: my favorite fruit is peach."
        search_context = "CURRENT WEB INFORMATION:\nFruit market mentions papaya."

        def reply(messages):
            calls.append([dict(item) for item in messages])
            return "A reply."

        with patch.multiple(
            brain,
            _load_personality=lambda: {},
            get_emotional_system=lambda: types.SimpleNamespace(process_message=lambda _: None),
            get_emotion_state=lambda: {},
            get_current_emotion=lambda: "neutral",
            compute_mood=lambda _: {"label": "Neutral"},
            analyze_sentiment_advanced=lambda _: (0.0, {}),
            check_ollama_health=lambda: True,
            _call_ollama_chat=reply,
        ):
            brain.generate_response(
                question, "viewer", search_context=search_context,
                memory_context=memory_context,
            )
            self.assertEqual(brain._conversation_history, [
                {"role": "user", "content": "viewer: " + question},
                {"role": "assistant", "content": "A reply."},
            ])
            brain.generate_response("How are you?", "viewer")

        self.assertEqual(len(calls), 2)
        scoped = [item for item in calls[0] if item["role"] == "system"
                  and "Scoped Twitch memory data follows" in item["content"]]
        self.assertEqual(len(scoped), 1)
        self.assertIn(memory_context, scoped[0]["content"])
        self.assertIn("prefer their own latest", scoped[0]["content"])
        self.assertIn("quoted lines were written by the current asking viewer",
                      scoped[0]["content"])
        self.assertIn("answer directly and apply any requested ordering",
                      scoped[0]["content"])
        self.assertNotIn(search_context, scoped[0]["content"])
        self.assertEqual("viewer: " + question, calls[0][-1]["content"])
        search = [item for item in calls[0][:-1] if item["role"] == "user"
                  and "External search data (not viewer speech)" in item["content"]]
        self.assertEqual(1, len(search))
        self.assertIn(search_context, search[0]["content"])
        self.assertTrue(any("not words from the current viewer" in item["content"]
                            for item in calls[0] if item["role"] == "system"))
        self.assertNotIn(memory_context, calls[0][-1]["content"])
        self.assertNotIn(memory_context, str(brain._conversation_history))
        self.assertNotIn(search_context, str(brain._conversation_history))
        self.assertEqual(calls[1][-3]["content"], "viewer: " + question)
        self.assertNotIn(memory_context, str(calls[1]))
        self.assertNotIn(search_context, str(calls[1]))

    def test_factual_search_context_stays_out_of_twitch_history(self):
        brain = self.brain
        brain.clear_history()
        self.addCleanup(brain.clear_history)
        calls = []
        search_context = "CURRENT WEB INFORMATION:\n[Result 1] AI research update"
        question = "Medlar, latest news about AI?"

        def reply(messages):
            calls.append([dict(item) for item in messages])
            return "Here's a short update."

        with patch.multiple(
            brain,
            _load_personality=lambda: {},
            get_emotional_system=lambda: types.SimpleNamespace(process_message=lambda _: None),
            get_emotion_state=lambda: {},
            get_current_emotion=lambda: "neutral",
            compute_mood=lambda _: {"label": "Neutral"},
            analyze_sentiment_advanced=lambda _: (0.0, {}),
            check_ollama_health=lambda: True,
            _call_ollama_chat=reply,
        ):
            brain.generate_response(question, "viewer", search_context=search_context)
            self.assertEqual(brain._conversation_history[0],
                             {"role": "user", "content": "viewer: " + question})
            brain.generate_response("Medlar, how are you?", "viewer")

        self.assertEqual(len(calls), 2)
        self.assertEqual("viewer: " + question, calls[0][-1]["content"])
        self.assertTrue(any(search_context in item["content"]
                            for item in calls[0][:-1] if item["role"] == "user"))
        self.assertEqual(calls[1][-3]["content"], "viewer: " + question)
        self.assertNotIn(search_context, str(calls[1]))

    def test_recall_question_skips_search_while_current_facts_still_search(self):
        fake_ddgs = module("ddgs", DDGS=type("NoNetworkDDGS", (), {}))
        fake_wiki = module("wikipediaapi", Wikipedia=lambda **kwargs: types.SimpleNamespace())
        with patch.dict(sys.modules, {"ddgs": fake_ddgs, "wikipediaapi": fake_wiki}):
            search = load_source("web_search_recall_under_test", "MedlarTV/core/web_search.py")
        search.ENABLE_WEB_SEARCH = True
        search.ENABLE_WIKIPEDIA = True
        result = {"title": "AI research update", "body": "A current factual result."}
        with patch.object(search, "search_web", return_value=[result]) as web, \
             patch.object(search, "search_wikipedia", side_effect=AssertionError("Unexpected Wikipedia lookup")) as wiki:
            self.assertEqual(search.search_intelligently("Medlar, what test word did I tell you?"), "")
            self.assertEqual(search.search_intelligently("What were the four words?"), "")
            self.assertEqual(search.search_intelligently(
                "Can you list the four words I gave you in reverse order?"), "")
            self.assertEqual(search.search_intelligently(
                "Give the four words in reverse order"), "")
            self.assertEqual(search.search_intelligently(
                "What were my group's four words?"), "")
            self.assertEqual(search.search_intelligently(
                "What were the group words?"), "")
            self.assertEqual(search.search_intelligently(
                "What were my four words in reverse order?"), "")
            self.assertEqual(search.search_intelligently(
                "What were my four words in reverse order please?"), "")
            self.assertEqual(search.search_intelligently(
                "What were my four words backwards?"), "")
            self.assertEqual(search.search_intelligently(
                "Could you remind me of my four words?"), "")
            self.assertEqual(search.search_intelligently(
                "What were my four code words?"), "")
            self.assertEqual(search.search_intelligently(
                "What were the four words we said?"), "")
            web.assert_not_called()
            wiki.assert_not_called()

            for factual in (
                "What are the words to my country's national anthem?",
                "What are the four words in this historic motto?",
                "What words did Shakespeare mention?",
            ):
                with self.subTest(factual=factual):
                    self.assertIn("AI research update",
                                  search.search_intelligently(factual))
                    web.assert_called_once_with(factual, max_results=2)
                    web.reset_mock()

            context = search.search_intelligently("latest news about AI?")
            web.assert_called_once_with("latest news about AI?", max_results=3)
            wiki.assert_not_called()
            self.assertIn("AI research update", context)
            self.assertIn("A current factual result.", context)

    def test_search_migration_uses_supported_ddgs_calls(self):
        calls = []

        class FakeDDGS:
            def text(self, query, **kwargs):
                calls.append(("text", query, kwargs))
                return [{"title": "result", "href": "https://example.test", "body": "summary"}]

            def news(self, query, **kwargs):
                calls.append(("news", query, kwargs))
                return [{"title": "article"}]

        fake_ddgs = module("ddgs", DDGS=FakeDDGS)
        fake_wiki = module("wikipediaapi", Wikipedia=lambda **kwargs: types.SimpleNamespace())
        with patch.dict(sys.modules, {"ddgs": fake_ddgs, "wikipediaapi": fake_wiki}):
            search = load_source("web_search_under_test", "MedlarTV/core/web_search.py")
            self.assertEqual(len(search.search_web("query", max_results=2)), 1)
            self.assertEqual(len(search.search_news("news", max_results=2)), 1)
        self.assertEqual(calls[0], ("text", "query", {"max_results": 2, "backend": "auto"}))
        self.assertEqual(calls[1], ("news", "news", {"max_results": 2}))


if __name__ == "__main__":
    unittest.main()
