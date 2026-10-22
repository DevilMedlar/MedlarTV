"""Command diagnostics omit synthetic query text while preserving replies."""

from __future__ import annotations

import ast
import io
import logging
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "MedlarTV" / "core" / "command_handlers.py"


def load_executor(namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    node = next(item for item in tree.body
                if isinstance(item, ast.FunctionDef) and item.name == "execute_command")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["execute_command"]


class CommandQueryPrivacyTests(unittest.TestCase):
    def test_query_and_exception_text_do_not_enter_command_logs(self):
        query = "find 17 Oak Place on example.com"
        output = io.StringIO()
        logger = logging.Logger("synthetic_command_privacy", level=logging.DEBUG)
        logger.addHandler(logging.StreamHandler(output))

        def reply(_user, args, _context):
            return f"No results for: {args}"

        namespace = {
            "COMMAND_REGISTRY": {"search": {"permissions": [], "handler": reply}},
            "check_permission": lambda *_args: True,
            "log": logger, "log_command": lambda *_args, **_kwargs: None,
        }
        execute = load_executor(namespace)
        self.assertIn(query, execute("search", "Ada", "user", query, {"query": query}))
        self.assertNotIn(query, output.getvalue())

        def fail(_user, args, _context):
            raise RuntimeError(args)

        namespace["COMMAND_REGISTRY"]["search"]["handler"] = fail
        self.assertIn("Command failed: search", execute(
            "search", "Ada", "user", query, {"query": query},
        ))
        self.assertNotIn(query, output.getvalue())


if __name__ == "__main__":
    unittest.main()
