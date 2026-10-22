"""Search diagnostics must not copy synthetic chat text to captured stdout."""

from __future__ import annotations

import ast
import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import FunctionType, SimpleNamespace
from collections.abc import Mapping
from typing import NoReturn, Protocol, cast, runtime_checkable
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "MedlarTV" / "core" / "web_search.py"
FUNCTIONS = {
    "_positive_result_limit", "search_web", "search_wikipedia", "should_search_web",
    "should_search_wikipedia", "_asks_about_chat_history",
    "search_intelligently", "search_news", "format_search_context",
}


@runtime_checkable
class _SearchBackend(Protocol):
    def text(self, text: str, **kwargs: object) -> list[dict[str, str]]: ...
    def news(self, text: str, **kwargs: object) -> list[dict[str, str]]: ...


@runtime_checkable
class _BackendFactory(Protocol):
    def __call__(self) -> _SearchBackend: ...


def call_search(namespace: dict[str, object], name: str, *args: object) -> object:
    function = namespace[name]
    assert isinstance(function, FunctionType), f"Expected extracted function: {name}"
    result: object = function(*args)
    return result


def text_result(value: object) -> str:
    assert isinstance(value, str), "Expected search context text"
    return value


def result_title(value: object) -> str:
    assert value is not None, "Expected a Wikipedia result"
    assert isinstance(value, dict), "Expected a Wikipedia result dictionary"
    # The runtime dictionary check establishes its shape; validate the accessed value.
    result = cast(Mapping[object, object], value)
    title = result["title"]
    assert isinstance(title, str), "Expected a Wikipedia title"
    return title


def always_search(_query: str) -> bool:
    return True


def load_search_functions() -> dict[str, object]:
    """Compile functions without importing dotenv, DDGS, or Wikipedia."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected: list[ast.stmt] = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    # Parsed functions already have complete source locations. The fixture module
    # inherits this test module's future-annotations compiler flag.
    module = ast.Module(body=selected, type_ignores=[])
    namespace: dict[str, object] = {
        "re": re, "DEBUG": False, "ENABLE_WEB_SEARCH": True,
        "ENABLE_WIKIPEDIA": True, "WEB_SEARCH_MAX_RESULTS": 3,
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    def configure_search() -> None:
        pass  # This fixture supplies synthetic configuration.

    def get_ddgs() -> _SearchBackend:
        factory = namespace["DDGS"]
        assert isinstance(factory, _BackendFactory), "Expected a synthetic backend factory"
        backend: object = factory()
        assert isinstance(backend, _SearchBackend), "Expected text/news backend methods"
        return backend

    namespace["_configure_search"] = configure_search
    namespace["_get_ddgs"] = get_ddgs  # Read the same globals at each lazy call.
    return namespace


class WebSearchPrivacyTests(unittest.TestCase):
    def test_personal_memory_queries_never_reach_search_backends(self) -> None:
        search = load_search_functions()
        web = Mock(return_value=[{"title": "unexpected"}])
        wikipedia = Mock(return_value={"title": "unexpected"})
        search.update(
            search_web=web,
            search_wikipedia=wikipedia,
            should_search_web=always_search,
            should_search_wikipedia=always_search,
        )

        for query in (
            "What is my favorite number?",
            "Give me the same four-word sequence in reverse order.",
            "What is my corrected four-word test sequence in original order?",
        ):
            with self.subTest(query=query):
                self.assertEqual("", call_search(search, "search_intelligently", query))

        web.assert_not_called()
        wikipedia.assert_not_called()

    def test_query_result_and_exception_text_stay_out_of_stdout(self) -> None:
        query = "latest map for 17 Oak Place via example.com?"
        results: list[dict[str, str]] = [{
            "title": "17 Oak Place", "body": "example.com",
            "href": "https://example.com/17-oak-place",
        }]
        for debug in (False, True):
            with self.subTest(debug=debug):
                calls: list[tuple[str, str]] = []

                class FakeDDGS:
                    def text(self, text: str, **_kwargs: object) -> list[dict[str, str]]:
                        calls.append(("web", text))
                        return results

                    def news(self, text: str, **_kwargs: object) -> list[dict[str, str]]:
                        calls.append(("news", text))
                        return results

                def page(text: str) -> SimpleNamespace:
                    calls.append(("wiki", text))
                    return SimpleNamespace(exists=lambda: False)

                search = load_search_functions()
                search.update(DEBUG=debug, DDGS=FakeDDGS,
                              wiki=SimpleNamespace(page=page))
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(results, call_search(search, "search_web", query, 2))
                    self.assertEqual(results, call_search(search, "search_news", query, 2))
                    self.assertIsNone(call_search(search, "search_wikipedia", query))
                    def found_page(_text: str) -> SimpleNamespace:
                        return SimpleNamespace(
                            exists=lambda: True, title="17 Oak Place",
                            summary="A synthetic example.", fullurl="https://example.com",
                        )

                    search["wiki"] = SimpleNamespace(page=found_page)
                    self.assertEqual("17 Oak Place", result_title(call_search(search, "search_wikipedia", query)))
                    call_search(search, "search_intelligently", query)
                    call_search(search, "search_intelligently", "Check 8 Birch Terrace?")
                    self.assertIn(query, text_result(call_search(search, "format_search_context", results, query)))
                printed = output.getvalue()
                for private in ("17 Oak Place", "8 Birch Terrace", "example.com"):
                    self.assertNotIn(private, printed)
                self.assertIn(("web", query), calls)
                self.assertIn(("news", query), calls)
                self.assertTrue(any(kind == "wiki" for kind, _ in calls))

                class FailingDDGS:
                    def text(self, _text: str, **_kwargs: object) -> NoReturn:
                        raise RuntimeError(query)

                    def news(self, _text: str, **_kwargs: object) -> NoReturn:
                        raise RuntimeError(query)

                search["DDGS"] = FailingDDGS
                failure_output = io.StringIO()
                with redirect_stdout(failure_output):
                    self.assertEqual([], call_search(search, "search_web", query))
                    self.assertEqual([], call_search(search, "search_news", query))
                self.assertNotIn(query, failure_output.getvalue())


if __name__ == "__main__":
    unittest.main()
