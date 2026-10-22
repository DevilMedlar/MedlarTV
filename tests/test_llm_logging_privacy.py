"""Offline model-error diagnostics omit synthetic message content."""

from __future__ import annotations

import ast
import io
import logging
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "MedlarTV" / "core" / "llm_brain.py"


def load_model_call(namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    node = next(item for item in tree.body
                if isinstance(item, ast.FunctionDef) and item.name == "_call_ollama_chat")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["_call_ollama_chat"]


class LlmLoggingPrivacyTests(unittest.TestCase):
    def test_errors_do_not_log_prompt_echoes(self):
        private = "17 Oak Place example.com"
        captured = io.StringIO()
        logger = logging.Logger("synthetic_llm_privacy", level=logging.DEBUG)
        logger.addHandler(logging.StreamHandler(captured))
        namespace = {
            "DEBUG": True, "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OLLAMA_MODEL": "synthetic", "log": logger,
        }
        call = load_model_call(namespace)

        def fail(*_args, **_kwargs):
            raise RuntimeError(private)

        namespace["requests"] = SimpleNamespace(post=fail)
        with redirect_stdout(captured):
            call([{"role": "user", "content": private}])

        namespace["requests"] = SimpleNamespace(post=lambda *_args, **_kwargs:
            SimpleNamespace(status_code=503, text=private))
        with redirect_stdout(captured):
            call([{"role": "user", "content": private}])
        self.assertNotIn(private, captured.getvalue())


if __name__ == "__main__":
    unittest.main()
