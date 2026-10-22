"""Core runtime logging must not persist dependency request URLs."""

from __future__ import annotations

from collections.abc import Callable
from types import FunctionType
from typing import cast

import ast
import logging
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "MedlarTV" / "core" / "main.py"
DEPENDENCY_LOGGERS = (
    "httpx",
    "primp",
    "wikipediaapi._http_client.sync_http_client",
)


def load_dependency_logging_configurator() -> Callable[[], None]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "configure_dependency_logging"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace: dict[str, object] = {"logging": logging}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    result = namespace["configure_dependency_logging"]
    assert isinstance(result, FunctionType), "Expected extracted logging configurator"
    return cast(Callable[[], None], result)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class CoreDependencyLoggingTests(unittest.TestCase):
    def test_dependency_info_and_debug_urls_do_not_reach_core_handlers(self) -> None:
        configure = load_dependency_logging_configurator()
        root = logging.getLogger()
        capture = _Capture()
        original_root_level = root.level
        original_handlers = list(root.handlers)
        states = {
            name: (logging.getLogger(name).level, logging.getLogger(name).propagate)
            for name in DEPENDENCY_LOGGERS
        }
        marker = "MM-QUERY-LOG-20261005"
        try:
            root.handlers = [capture]
            root.setLevel(logging.DEBUG)
            for name in DEPENDENCY_LOGGERS:
                logger = logging.getLogger(name)
                logger.setLevel(logging.DEBUG)
                logger.propagate = True

            configure()

            for name in DEPENDENCY_LOGGERS:
                logger = logging.getLogger(name)
                self.assertFalse(logger.isEnabledFor(logging.INFO))
                self.assertTrue(logger.isEnabledFor(logging.WARNING))
                logger.debug("GET https://example.invalid/?q=%s", marker)
                logger.info("GET https://example.invalid/?q=%s", marker)
                logger.warning("request failed status=503")

            self.assertNotIn(marker, "\n".join(capture.messages))
            self.assertEqual(
                ["request failed status=503"] * len(DEPENDENCY_LOGGERS),
                capture.messages,
            )
        finally:
            root.handlers = original_handlers
            root.setLevel(original_root_level)
            for name, (level, propagate) in states.items():
                logger = logging.getLogger(name)
                logger.setLevel(level)
                logger.propagate = propagate


if __name__ == "__main__":
    unittest.main()
