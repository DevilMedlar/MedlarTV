"""Offline dispatch/catalog contracts; no handler imports or live readiness claims."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("b8_builder", ROOT / "MedlarTV/tools/build_commands_site.py")
assert spec is not None and spec.loader is not None, "Expected catalog spec and loader"
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
UNAVAILABLE = {"ban", "hello", "hype", "medlar", "modstats", "mood", "moodnumbers", "timeout", "unwarn", "warn", "whitelist"}

class CatalogTests(unittest.TestCase):
    def setUp(self): self.items = {c["name"]: c for c in builder.load_commands()}
    def test_all_implemented_names_included(self):
        self.assertEqual(len([c for c in self.items.values() if c["implemented"]]), 47)
        self.assertTrue({"forget", "forgetme", "help", "trans", "translate"} <= self.items.keys())
    def test_unavailable_retained_and_labelled(self):
        self.assertEqual({n for n,c in self.items.items() if not c["implemented"]}, UNAVAILABLE)
        for n in UNAVAILABLE: self.assertIn("Unavailable", self.items[n]["requirements"])
    def test_no_duplicate_names(self): self.assertEqual(len(builder.load_commands()), 58)
    def test_title_split_roles(self):
        for n in ("title", "game"):
            self.assertEqual(self.items[n]["allowed_roles"], ["everybody"])
            self.assertEqual(self.items[n]["set_roles"], ["pilot", "copilot"])
            self.assertIn("verified primary", self.items[n]["requirements"])
    def test_deathreset_does_not_grant_mod(self): self.assertEqual(self.items["deathreset"]["allowed_roles"], ["pilot"])
    def test_inline_copilot_permissions_retained(self):
        self.assertEqual(self.items["removecopilot"]["allowed_roles"], ["pilot", "mod", "copilot"])
        self.assertIn("verified primary", self.items["addcopilot"]["requirements"])
    def test_memory_scope_disclosed(self):
        for n in ("forget", "forgetme"): self.assertIn("primary channel", self.items[n]["requirements"])
    def test_render_uses_text_for_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            builder.build_once(Path(d));html=(Path(d)/"index.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)
        self.assertIn("node.textContent = text", html)
    def test_refresh_keeps_last_good_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            builder.build_once(Path(d));html=(Path(d)/"index.html").read_text(encoding="utf-8")
        self.assertIn("if (!r.ok)", html)
        self.assertIn("if (!validCatalog(j))", html)
        self.assertIn("last successfully loaded", html)
    def test_permissions_filter_includes_everybody(self):
        with tempfile.TemporaryDirectory() as d:
            builder.build_once(Path(d));html=(Path(d)/"index.html").read_text(encoding="utf-8")
        self.assertIn("roles.includes('everybody')", html)
        self.assertIn("state.role === 'pilot'", html)
    def test_dormant_catalog_shares_literal_renderer(self):
        source=(ROOT/"MedlarTV/core/main.py").read_text(encoding="utf-8")
        self.assertIn("render_commands_html(api=True)", source)
        self.assertIn('return JSONResponse({"commands": load_commands()}', source)
        self.assertIn("fetch('/api/commands'", builder.render_commands_html(api=True))
        self.assertNotIn("innerHTML", builder.render_commands_html(api=True))
    def test_malformed_build_metadata_rejected(self):
        invalid_commands: tuple[object, ...] = ([], {"bad": []}, {"bad": {"allowed_roles": "mod"}}, {"bad": {"response": {}}})
        for commands in invalid_commands:
            with patch.object(builder.yaml, "safe_load", return_value={"commands": commands}):
                with self.assertRaises(ValueError): builder.load_commands()
    def test_translation_alias_help_from_source(self):
        for n in ("trans", "translate"):
            self.assertIn("Optional source override", self.items[n]["usage"])
