"""Ordinary Twitch conversation stays local; external lookup is an explicit command."""
from __future__ import annotations

import ast
from difflib import SequenceMatcher
from pathlib import Path
import re
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
feature_fixture = runpy.run_path(str(ROOT / "tests/test_twitch_feature_toggles.py"))
command_fixture = runpy.run_path(str(ROOT / "tests/test_twitch_command_routing.py"))
search_fixture = runpy.run_path(str(ROOT / "tests/test_web_search_privacy.py"))


def load_trigger():
    source = ROOT / "MedlarTV/core/fuzzy_trigger.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name not in {"load_trigger_keywords", "test_fuzzy_trigger"}]
    namespace = {"re": re, "SequenceMatcher": SequenceMatcher,
                 "os": SimpleNamespace(getenv=lambda _key, default=None: default),
                 "load_trigger_keywords": lambda: ["medlar", "medlartv"]}
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["should_respond"]


class TwitchAutosearchTests(unittest.TestCase):
    def setUp(self):
        self.chat = feature_fixture["TwitchFeatureToggleTests"]()
        self.chat.setUp()
        self.chat.client._learn_emotes_from_message = Mock()
        # Classifier imports remain available to the guarded runner. Stores here
        # are deliberately unavailable; real memory wiring has its own suite.
        self.chat.client._conversation_store = Mock(side_effect=RuntimeError("inert store"))
        self.chat.client._profile_store = Mock(side_effect=RuntimeError("inert store"))
        self.chat.namespace["should_respond"] = load_trigger()
        self.providers = {
            name: Mock(side_effect=AssertionError("Unexpected automatic external lookup"))
            for name in ("search_intelligently", "search_web", "search_wikipedia", "search_news")
        }
        self.chat.namespace.update(self.providers)

        self.command = command_fixture["TwitchCommandRoutingTests"]()
        self.command.setUp()
        self.command.client._conversation_store = Mock(side_effect=RuntimeError("inert store"))
        self.command.client._profile_store = Mock(side_effect=RuntimeError("inert store"))
        self.search = search_fixture["load_search_functions"]()
        self.ddgs = Mock()
        self.ddgs.text.return_value = [{"title": "Synthetic result", "href": "https://example.invalid/result"}]
        self.backend = Mock(return_value=self.ddgs)
        self.wiki_page = Mock(return_value=SimpleNamespace(
            exists=lambda: True, title="Synthetic topic", summary="A synthetic summary.",
            fullurl="https://example.invalid/topic",
        ))
        self.search.update(DDGS=self.backend, wiki=SimpleNamespace(page=self.wiki_page))
        self.command.commands.update(
            search_web=self.search["search_web"], search_wikipedia=self.search["search_wikipedia"],
            handle_t_command=Mock(return_value="synthetic translation"),
        )
        source = command_fixture["COMMANDS"]
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for name, command in (("handle_search", "search"), ("handle_wiki", "wiki"), ("handle_t", "t")):
            command_fixture["_load_definition"](source, tree, name, self.command.commands)
            self.command.commands["COMMAND_REGISTRY"][command]["handler"] = self.command.commands[name]

    def incoming_chat(self, message):
        self.chat.client._process_incoming(f":viewer!viewer@synthetic PRIVMSG #testchannel :{message}")

    def assert_no_automatic_lookup(self):
        for provider in self.providers.values():
            provider.assert_not_called()

    def test_question_words_with_address_before_and_after_generate_without_search(self):
        questions = (
            "who discovered penicillin?", "what is the latest astronomy news?",
            "where is the tallest mountain?", "why does ice float?",
            "when did Apollo 11 land?", "how does a Python module work?",
        )
        for question in questions:
            for message in (f"Medlar, {question}", f"{question} Medlar"):
                with self.subTest(message=message):
                    self.chat.generate_response.reset_mock()
                    self.chat.client.send_message.reset_mock()
                    self.incoming_chat(message)
                    self.chat.generate_response.assert_called_once()
                    self.assertEqual(message, self.chat.generate_response.call_args.args[0])
                    self.assertEqual("", self.chat.generate_response.call_args.kwargs["search_context"])
                    self.chat.client.send_message.assert_called_once_with("mock reply")
                    self.assert_no_automatic_lookup()

    def test_conversational_platform_requests_do_not_lookup_or_append_links(self):
        for message in (
            "Medlar, find example_user on Twitch", "Find example_user on Twitch, Medlar",
            "Medlar, search YouTube for Python tutorials", "Lookup a Reddit channel, Medlar",
            "Medlar, who is on Twitch?", "Who is on Twitch, Medlar?",
        ):
            with self.subTest(message=message):
                self.chat.client.send_message.reset_mock()
                self.incoming_chat(message)
                self.chat.client.send_message.assert_called_once_with("mock reply")
                self.assert_no_automatic_lookup()

    def test_recall_questions_before_and_after_address_stay_on_model_path(self):
        for message in (
            "Medlar, what test word did I tell you?", "What test word did I tell you, Medlar?",
            "Medlar, repeat my earlier sequence", "What did we discuss earlier, Medlar?",
        ):
            with self.subTest(message=message):
                self.chat.generate_response.reset_mock()
                self.incoming_chat(message)
                self.chat.generate_response.assert_called_once()
                self.assertEqual("", self.chat.generate_response.call_args.kwargs["search_context"])
                self.assert_no_automatic_lookup()

    def test_classifier_unavailable_does_not_enable_factual_or_recall_search(self):
        with patch.dict("sys.modules", {"MedlarTV.core.conversation_context": None}):
            for message in ("Medlar, latest astronomy news?", "What did I tell you, Medlar?"):
                self.chat.generate_response.reset_mock()
                self.incoming_chat(message)
                self.chat.generate_response.assert_called_once()
                self.assertEqual("", self.chat.generate_response.call_args.kwargs["search_context"])
        self.assert_no_automatic_lookup()

    def test_empty_model_reply_does_not_fall_back_to_search(self):
        self.chat.generate_response.return_value = ""
        self.incoming_chat("Medlar, latest news about a Twitch channel?")
        self.chat.generate_response.assert_called_once()
        self.chat.client.send_message.assert_not_called()
        self.assert_no_automatic_lookup()

    def test_disabled_fuzzy_or_brain_suppresses_conversation_without_search(self):
        for feature in ("fuzzy_trigger", "llm_brain"):
            with self.subTest(feature=feature):
                self.chat.enabled.update(fuzzy_trigger=True, llm_brain=True)
                self.chat.enabled[feature] = False
                self.chat.generate_response.reset_mock()
                self.chat.client.send_message.reset_mock()
                self.incoming_chat("What is the latest news on Twitch, Medlar?")
                self.chat.generate_response.assert_not_called()
                self.chat.client.send_message.assert_not_called()
                self.assert_no_automatic_lookup()

    def test_time_lookup_remains_independent_of_brain_and_external_search(self):
        self.chat.enabled["llm_brain"] = False
        self.chat.namespace["should_lookup_time"] = Mock(return_value=(True, "Paris"))
        self.incoming_chat("Medlar, what time is it in Paris?")
        self.chat.namespace["get_times_for_location"].assert_called_once_with("Paris")
        self.chat.client.send_message.assert_called_once_with("mock local time")
        self.chat.generate_response.assert_not_called()
        self.assert_no_automatic_lookup()

    def test_explicit_search_uses_existing_handler_with_fuzzy_and_brain_disabled(self):
        self.command.enabled.update(fuzzy_trigger=False, llm_brain=False)
        self.assertEqual(["everybody"], self.command.commands["COMMAND_REGISTRY"]["search"]["permissions"])
        self.command.incoming("!search latest astronomy news")
        self.ddgs.text.assert_called_once_with("latest astronomy news", max_results=2, backend="auto")
        self.command.client.send_message.assert_called_once_with("Synthetic result — https://example.invalid/result")
        self.command.generate_response.assert_not_called()
        self.command.should_respond.assert_not_called()

    def test_search_disabled_and_empty_query_do_not_reach_provider(self):
        self.command.incoming("!search")
        self.command.client.send_message.assert_called_with("Usage: !search <query>")
        self.backend.assert_not_called()
        self.search["ENABLE_WEB_SEARCH"] = False
        self.command.incoming("!search latest astronomy news")
        self.command.client.send_message.assert_called_with("No results for: latest astronomy news")
        self.backend.assert_not_called()
        self.command.generate_response.assert_not_called()

    def test_explicit_search_still_obeys_permission_gate(self):
        # Restrict only this synthetic registry to exercise the real permission gate.
        self.command.commands["COMMAND_REGISTRY"]["search"]["permissions"] = ["mod"]
        self.command.incoming("!search latest astronomy news")
        self.assertIn("Permission denied", self.command.client.send_message.call_args.args[0])
        self.backend.assert_not_called()
        self.command.client._process_incoming(
            "@mod=1 :viewer!viewer@synthetic PRIVMSG #testchannel :!search latest astronomy news"
        )
        self.ddgs.text.assert_called_once_with("latest astronomy news", max_results=2, backend="auto")
        self.command.generate_response.assert_not_called()

    def test_explicit_wiki_and_its_configuration_switch_remain(self):
        self.command.incoming("!wiki synthetic topic")
        self.wiki_page.assert_called_once_with("Synthetic Topic")
        self.assertIn("Synthetic topic", self.command.client.send_message.call_args.args[0])
        self.search["ENABLE_WIKIPEDIA"] = False
        self.wiki_page.reset_mock()
        self.command.incoming("!wiki synthetic topic")
        self.wiki_page.assert_not_called()
        self.command.client.send_message.assert_called_with("No Wikipedia page found for: synthetic topic")
        self.command.generate_response.assert_not_called()

    def test_translation_command_remains_separate_from_conversation_and_search(self):
        self.command.incoming("!t es hello")
        self.command.commands["handle_t_command"].assert_called_once_with("es hello", "viewer")
        self.command.client.send_message.assert_called_once_with("synthetic translation")
        self.command.generate_response.assert_not_called()
        self.backend.assert_not_called()
        self.wiki_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
