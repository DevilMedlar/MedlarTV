"""Synthetic counter persistence; no production counter/config reads or writes."""
import ast
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
from threading import RLock
import unittest
from unittest.mock import Mock, patch
import yaml
ROOT=Path(__file__).resolve().parents[1]

class CounterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"config/counters.yaml";self.path.parent.mkdir()
        tree=ast.parse((ROOT/"MedlarTV/core/command_handlers.py").read_text(encoding="utf-8"))
        names={"_load_counters","_save_counters","handle_death","handle_deathreset","handle_deathhash"}
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        future=ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0)
        self.ns={"_COUNTERS_FILE":self.path,"_COUNTERS_LOCK":RLock(),"yaml":yaml,"os":os,"log":Mock()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[])),"counter-fixture","exec"),self.ns)
    def call(self,name):return self.ns[name]("synthetic","",{})
    def put(self,text="counters:\n  death: 4\n"):
        self.path.write_text(text,encoding="utf-8");return self.path.read_bytes()
    def value(self):return yaml.safe_load(self.path.read_text(encoding="utf-8"))["counters"]["death"]
    def test_increment_reports_saved_value(self):
        self.put();self.assertEqual(self.call("handle_death"),"Has died 5 times devilmeSAD");self.assertEqual(self.value(),5)
    def test_reset_reports_saved_zero(self):
        self.put();self.assertEqual(self.call("handle_deathreset"),"Death Count has been set to 0 devilmeSUS");self.assertEqual(self.value(),0)
    def test_query_reports_existing_value_without_write(self):
        old=self.put();self.assertIn("died 4 so far",self.call("handle_deathhash"));self.assertEqual(self.path.read_bytes(),old)
    def test_missing_is_zero_without_query_creation(self):
        self.assertIn("died 0 so far",self.call("handle_deathhash"));self.assertFalse(self.path.exists())
    def test_missing_increment_creates_one(self):
        self.assertIn("Has died 1 times",self.call("handle_death"));self.assertEqual(self.value(),1)
    def test_empty_valid_file_remains_default(self):
        self.put("");self.assertIn("Has died 1 times",self.call("handle_death"));self.assertEqual(self.value(),1)
    def test_unrelated_counter_fields_preserved(self):
        self.put("counters: {death: 4, rounds: 9}\nextra: {flag: true}\n");self.call("handle_death")
        data=yaml.safe_load(self.path.read_text(encoding="utf-8"));self.assertEqual(data,{"counters":{"death":5,"rounds":9},"extra":{"flag":True}})
    def test_int_compatible_values_keep_existing_behavior(self):
        for value in ('"4"','4.5','true','-2'):
            self.put("counters: {death: "+value+"}\n");old=int(self.value());self.assertIn("Has died",self.call("handle_death"));self.assertEqual(self.value(),old+1)
    def test_malformed_yaml_never_becomes_zero_or_overwritten(self):
        old=self.put("counters: [");
        for name in ("handle_death","handle_deathreset","handle_deathhash"):
            self.assertIn("unavailable",self.call(name));self.assertEqual(self.path.read_bytes(),old)
    def test_invalid_mapping_never_overwritten(self):
        for text in ('[]','counters: []','counters: null','counters: {death: nope}'):
            old=self.put(text);self.assertIn("unavailable",self.call("handle_deathreset"));self.assertEqual(self.path.read_bytes(),old)
    def test_read_failure_is_not_zero(self):
        self.put()
        with patch.object(Path,"open",side_effect=PermissionError("synthetic")):
            self.assertIn("unavailable",self.call("handle_deathhash"))
    def test_read_failure_never_confirms_increment(self):
        self.put()
        with patch.object(Path,"open",side_effect=PermissionError("synthetic")):
            self.assertIn("unavailable",self.call("handle_death"))
    def test_replace_failure_preserves_prior_file_and_no_success(self):
        old=self.put()
        with patch.object(os,"replace",side_effect=PermissionError("synthetic")):
            self.assertIn("unavailable",self.call("handle_death"))
        self.assertEqual(self.path.read_bytes(),old);self.assertEqual(list(self.path.parent.glob("*.tmp")),[])
    def test_flush_failure_preserves_prior_file(self):
        old=self.put()
        with patch.object(os,"fsync",side_effect=OSError("synthetic")):
            self.assertIn("unavailable",self.call("handle_deathreset"))
        self.assertEqual(self.path.read_bytes(),old);self.assertEqual(list(self.path.parent.glob("*.tmp")),[])
    def test_serialization_failure_never_truncates_prior_file(self):
        old=self.put()
        with patch.object(yaml,"safe_dump",side_effect=ValueError("synthetic")):
            self.assertIn("unavailable",self.call("handle_deathreset"))
        self.assertEqual(self.path.read_bytes(),old)
    def test_same_process_increments_do_not_lose_updates(self):
        self.put("counters: {death: 0}\n")
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.call("handle_death"),range(12)))
        self.assertEqual(self.value(),12);self.assertTrue(all("Has died" in r for r in results))
