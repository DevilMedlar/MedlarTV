"""Synthetic URL precedence/parity tests; no real environment or network reads."""
import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from MedlarTV import commands_link as links
ROOT = Path(__file__).resolve().parents[1]

def definition(relative, name, env):
    tree=ast.parse((ROOT/relative).read_text(encoding="utf-8"))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    future=ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0)
    ns={"os":SimpleNamespace(getenv=lambda key,default=None:env.get(key,default))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,node],type_ignores=[])),name,"exec"),ns)
    return ns[name]

class LinkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=Path(self.temp.name)/"commands.yaml"
        self.config.write_text('commands:\n  commands:\n    response: "Catalog https://yaml.example.invalid/catalog"\n',encoding="utf-8")
        p=patch.object(links,"COMMANDS_CONFIG_PATH",self.config);p.start();self.addCleanup(p.stop)
    def check(self, env, expected):
        self.assertEqual(links.resolve_commands_link(env),expected)
        bang=definition("MedlarTV/core/command_handlers.py","handle_commands",env)
        conversation=definition("MedlarTV/tools/twitch_listener.py","_get_commands_link",env)
        self.assertEqual(bang("synthetic","",{}),"📋 Command catalog: "+expected)
        self.assertEqual(conversation(),expected)
    def test_yaml_default_used_by_both_paths(self): self.check({},"https://yaml.example.invalid/catalog")
    def test_primary_override_wins_all_conflicts(self):
        self.check({"COMMANDS_URL":"http://primary.example.invalid/a?x=1", "MEDLAR_COMMANDS_URL":"https://alias.example.invalid/", "COMMANDS_HOST":"192.0.2.8", "COMMANDS_PORT":"9999"},"http://primary.example.invalid/a?x=1")
    def test_compatibility_alias_wins_yaml_and_legacy(self):
        self.check({"MEDLAR_COMMANDS_URL":"http://192.0.2.7/custom/", "COMMANDS_HOST":"localhost"},"http://192.0.2.7/custom/")
    def test_explicit_legacy_pair_kept(self):self.check({"COMMANDS_HOST":"192.0.2.8","COMMANDS_PORT":"7777"},"http://192.0.2.8:7777/")
    def test_explicit_host_uses_old_port_default(self):self.check({"COMMANDS_HOST":"catalog.local"},"http://catalog.local:8080/")
    def test_explicit_port_uses_old_host_default(self):self.check({"COMMANDS_PORT":"7777"},"http://127.0.0.1:7777/")
    def test_explicit_loopback_does_not_guess_lan(self):self.check({"COMMANDS_HOST":"localhost"},"http://localhost:8080/")
    def test_empty_overrides_are_unset(self):self.check({key:"" for key in links.OVERRIDE_KEYS},"https://yaml.example.invalid/catalog")
    def test_whitespace_is_unset(self):self.check({"COMMANDS_URL":"  ","MEDLAR_COMMANDS_URL":"\t"},"https://yaml.example.invalid/catalog")
    def test_explicit_url_preserves_path_query_fragment(self):self.check({"COMMANDS_URL":"http://localhost:9988/c?q=synthetic#x"},"http://localhost:9988/c?q=synthetic#x")
    def test_explicit_url_never_reads_yaml(self):
        self.config.unlink();self.check({"COMMANDS_URL":"https://override.example.invalid/"},"https://override.example.invalid/")
    def test_missing_yaml_public_fallback(self):
        self.config.unlink();self.check({},links.DEFAULT_COMMANDS_URL)
    def test_invalid_yaml_public_fallback(self):
        self.config.write_text("commands: [",encoding="utf-8");self.check({},links.DEFAULT_COMMANDS_URL)
    def test_missing_yaml_url_public_fallback(self):
        self.config.write_text("commands: {commands: {response: no-link}}",encoding="utf-8");self.check({},links.DEFAULT_COMMANDS_URL)
    def test_unrelated_settings_not_consulted(self):
        class NamedOnly(dict):
            def get(self,key,default=None):
                if key not in links.OVERRIDE_KEYS:raise AssertionError("Unrelated configuration read")
                return super().get(key,default)
        self.check(NamedOnly(),"https://yaml.example.invalid/catalog")
