"""B6 synthetic collection, evidence and concurrent-editor contracts."""
import ast
import io
import json
import logging
import socket
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from collections import Counter, defaultdict

import test_medlar_diagnostics as fixture
from test_medlar_diagnostics import diagnostics as d, synthetic_gate, _FakeResponse

ROOT = Path(__file__).resolve().parents[1]
MARKER = "synthetic-private-evidence-76543"


def setUpModule():
    global _catalog_patch
    _catalog_patch = mock.patch.object(d, "_scope_catalog", return_value={"chat:read": "Read", "chat:edit": "Write"})
    _catalog_patch.start()


def tearDownModule():
    _catalog_patch.stop()


def extracted(relative, names, namespace):
    source = (ROOT / relative).read_text(encoding="utf-8")
    nodes = [n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    class NoImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node): return ast.copy_location(ast.Pass(),node)
        def visit_Import(self, node): return ast.copy_location(ast.Pass(),node)
    nodes = [NoImports().visit(n) for n in nodes]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[]))
    exec(compile(module,relative,"exec"),namespace)
    return namespace


class B6CollectionTests(unittest.TestCase):
    def test_default_cli_collects_and_saves_full_private_report(self):
        from test_medlar_diagnostics_accuracy import cli_fixture
        run=d.DiagnosticRun([d.Check("Synthetic section", "Full evidence", d.READY, MARKER)])
        with cli_fixture(run) as (root,collect,editor,out,err):
            self.assertEqual(d.main([]),0)
            self.assertTrue(collect.call_args.kwargs["live_twitch"])
            self.assertTrue(collect.call_args.kwargs["include_secret_values"])
            self.assertIn(MARKER,(root/"logs/medlar_diagnostics.txt").read_text())
            self.assertNotIn(MARKER,out.getvalue()+err.getvalue())
            editor.assert_not_called()

    def test_source_syntax_failure_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            p=root/d.REQUIRED_SOURCE_FILES[0];p.parent.mkdir(parents=True,exist_ok=True);p.write_text("syntax !")
            rows=d.check_source_syntax(root)
            self.assertTrue(any(c.status!=d.READY for c in rows))

    def test_report_reparse_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock.patch.object(d,"_is_link_or_reparse_point",return_value=True):
                with self.assertRaises(ValueError): d.ensure_output_allowed(root/"report.txt")

    def test_operational_arguments_are_preserved_without_profile_defaults(self):
        with mock.patch.object(d,"_collect_diagnostics_into_run") as collect:
            d.collect_diagnostics(root=ROOT,environ={"TWITCH_NICK":MARKER},live_twitch=True,
                                  probe_services=True,probe_writes=True,include_secret_values=True)
        self.assertTrue(collect.call_args.kwargs["probe_writes"])
        self.assertTrue(collect.call_args.kwargs["probe_services"])
        self.assertTrue(collect.call_args.kwargs["live_twitch"])
        self.assertTrue(collect.call_args.kwargs["include_secret_values"])
        self.assertNotIn("profile",collect.call_args.kwargs)

    def test_invalid_and_conflicting_modes_abort_before_collection(self):
        with mock.patch.object(d,"reexec_in_launcher_python",return_value=None), \
                mock.patch.object(d,"collect_diagnostics") as collect, redirect_stderr(io.StringIO()):
            for args in (["--profile","static"],["--offline","--live-twitch"],["--offline","--exercise-twitch"],["--exercise-twitch"]):
                with self.assertRaises(SystemExit):d.main(args)
            collect.assert_not_called()

    def test_missing_gate_direct_exercise_makes_zero_calls(self):
        helper=fixture.TwitchExerciseTests("runTest")
        http=mock.Mock();irc=mock.Mock()
        checks=d.run_live_twitch_checks(helper._environment(),exercise=helper._exercise(),http_json=http,irc_sender=irc)
        self.assertTrue(any(c.status!=d.READY for c in checks))
        http.assert_not_called();irc.assert_not_called()

    def test_full_private_cli_warns_before_collection(self):
        events=[]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"local.txt"
            with mock.patch.object(d,"reexec_in_launcher_python",return_value=None), \
                    mock.patch.object(d,"ensure_sensitive_output_allowed"),mock.patch.object(d,"write_report") as write, \
                    mock.patch.object(d,"collect_diagnostics",side_effect=lambda **kw:events.append("collect") or d.DiagnosticRun([])) as collect, \
                    mock.patch.object(d,"print",side_effect=lambda *a,**kw:events.append("warning" if "WARNING" in str(a[0]) else "print"),create=True):
                self.assertEqual(d.main(["--output",str(path)]),0)
                self.assertLess(events.index("warning"),events.index("collect"))
                self.assertTrue(collect.call_args.kwargs["include_secret_values"])
                self.assertIn("SENSITIVE",write.call_args.args[1])


class B6EvidenceTests(unittest.TestCase):
    def test_full_private_report_retains_labels_details_urls_headers_and_bodies(self):
        exchange=d.HttpExchangeEvidence(1,"GET","https://example.invalid/?login="+MARKER,
                                       (("Authorization",MARKER),),MARKER,200,(("X-User",MARKER),),MARKER,"raw",False,20,None)
        run=d.DiagnosticRun([d.Check(MARKER,MARKER,d.READY,MARKER)],http_exchanges=[exchange])
        report=d.render_report(run)
        self.assertIn(MARKER,report)
        self.assertIn("Response status: 200",report)
        self.assertIn("[READY]",report)
        self.assertIn(exchange.url,report)
        self.assertIn("<<<REQUEST BODY",report)
        self.assertIn("<<<RESPONSE BODY",report)

    def test_full_transport_retains_complete_body_headers_and_payload(self):
        body=json.dumps({"value":MARKER*100}).encode()
        response=_FakeResponse(body,headers={"X-Synthetic":MARKER})
        result=d.request_json("GET","https://example.invalid",opener=lambda *_a,**_k:response)
        self.assertEqual(response.read_limits,[None])
        self.assertFalse(result.body_truncated)
        self.assertEqual(result.body_bytes_observed,len(body))
        self.assertEqual(result.payload,{"value":MARKER*100})
        self.assertEqual(result.raw_body,body.decode())
        self.assertEqual(result.response_headers,(("X-Synthetic",MARKER),))

    def test_private_transport_retains_complete_synthetic_body(self):
        body=json.dumps({"value":MARKER*100}).encode();response=_FakeResponse(body)
        result=d.request_json("GET","https://example.invalid",opener=lambda *_a,**_k:response)
        self.assertEqual(response.read_limits,[None]);self.assertEqual(result.raw_body,body.decode())
        self.assertFalse(result.body_truncated)

    def test_full_report_does_not_omit_or_shorten_rows(self):
        run=d.DiagnosticRun([d.Check("Synthetic","Safe",d.READY,MARKER)]*10000)
        report=d.render_report(run)
        self.assertEqual(report.count("- [READY] Safe: "+MARKER),10000)
        self.assertGreater(len(report.encode()),262144)

    def test_missing_duplicate_or_failed_mandatory_rows_are_incomplete(self):
        rows=[d.Check("Synthetic","required",d.READY,"safe")]
        required=[("Synthetic","required")]
        self.assertTrue(d._mandatory_pass(rows,required))
        for bad in ([],rows+rows,[d.Check("Synthetic","required",d.SKIPPED,"safe")]):
            self.assertFalse(d._mandatory_pass(bad,required))

    def test_malformed_token_response_publishes_no_partial_facts(self):
        base={"login":"bot","client_id":"client","user_id":"id","scopes":["chat:read"],"expires_in":3600}
        for key,value in (("login",None),("client_id",MARKER*0),("user_id",1),("scopes",MARKER),("expires_in",True)):
            with self.subTest(field=key):
                payload={**base,key:value}
                # Validate through the public live facade with entirely synthetic HTTP.
                checks=d.run_live_twitch_checks({"TWITCH_NICK":"bot","MEDLARTV_TWITCH_TOKEN":MARKER},
                    http_json=lambda *_a,**_k:d.HttpJsonResult(200,payload=payload),root=Path(tempfile.gettempdir()))
                self.assertEqual(next(c for c in checks if c.name=="Bot token validation").status,d.UNAVAILABLE)

    def test_app_exchange_failure_keeps_dependent_coverage_rows(self):
        checks=d.run_live_twitch_checks({"APP_TWITCH_CLIENT_ID":"client","APP_SECRET_ID":MARKER},
            http_json=lambda *_a,**_k:d.HttpJsonResult(403,error="rejected"),root=Path(tempfile.gettempdir()))
        for name in (*d.READ_ONLY_TWITCH_API_NAMES,"Account-age source data"):
            self.assertTrue(any(c.name==name and c.status==d.SKIPPED for c in checks))


class B6OwnershipTests(unittest.TestCase):
    def test_frozen_medlar_executable_requires_repository_ownership(self):
        for executable,completed,count in ((r"C:\synthetic\Medlar\Medlar.exe",True,1),(r"C:\other\Medlar.exe",False,0)):
            row={"pid":2,"parent":1,"name":"Medlar.exe","command":"Medlar.exe","executable":executable}
            result=d.classify_process_snapshot([row],Path(r"C:\synthetic\Medlar"))
            self.assertEqual(result.probe_completed,completed);self.assertEqual(result.medlar_process_count,count)

    def test_direct_module_relative_and_adapter_forms_use_repository_ownership(self):
        root=Path(r"C:\synthetic\Medlar")
        forms=(r'python "C:\synthetic\Medlar\launcher.py"',
               r'python -m MedlarTV.core.main',r'python MedlarTV/core/main.py',
               r'python MedlarTV/tools/python_service_entry.py --module MedlarTV.core.main')
        for command in forms:
            with self.subTest(command=command):
                run=d.classify_process_snapshot([{"pid":2,"parent":1,"name":"python.exe","command":command,
                    "executable":r"C:\synthetic\Medlar\.venv\Scripts\python.exe"}],root)
                self.assertTrue(run.probe_completed);self.assertEqual(run.medlar_process_count,1)

    def test_unowned_relative_module_and_missing_command_are_unknown(self):
        for command in ("python -m MedlarTV.core.main",None):
            result=d.classify_process_snapshot([{"pid":2,"parent":1,"name":"python.exe","command":command,"executable":r"C:\Python\python.exe"}],Path(r"C:\synthetic\Medlar"))
            self.assertFalse(result.probe_completed)

    def test_owned_adapter_parent_chain_and_sibling_path_are_distinct(self):
        root=Path(r"C:\synthetic\Medlar")
        rows=[{"pid":1,"parent":0,"name":"python.exe","command":r'python C:\synthetic\Medlar\launcher.py',"executable":r"C:\Python\python.exe"},
              {"pid":2,"parent":1,"name":"python.exe","command":"python MedlarTV/tools/python_service_entry.py","executable":r"C:\Python\python.exe"}]
        result=d.classify_process_snapshot(rows,root);self.assertTrue(result.probe_completed);self.assertEqual(result.medlar_process_count,2)
        rows[0]["command"]=r'python C:\synthetic\Medlar-copy\launcher.py'
        self.assertFalse(d.classify_process_snapshot(rows,root).probe_completed)

    def test_network_failure_is_unknown_except_explicit_refusal(self):
        for error,expected in ((PermissionError(),None),(socket.gaierror(),None),(TimeoutError(),None),(OSError(),None),(ConnectionRefusedError(),True)):
            state,_=d.probe_ollama_endpoint_stopped({},ROOT,connector=mock.Mock(side_effect=error))
            self.assertIs(state,expected)


class B6ConcurrentEditorTests(unittest.TestCase):
    def _run(self, field, phase):
        helper=fixture.TwitchExerciseTests("runTest");delegate,calls,state=helper._successful_http();reads=0
        def transport(method,url,**kwargs):
            nonlocal reads
            if method=="GET" and "/helix/channels?" in url:
                reads+=1
                trigger={"title":3,"game_id":5}[field] if phase=="before" else 7
                if reads==trigger:state[field]=MARKER
            return delegate(method,url,**kwargs)
        irc=mock.Mock()
        checks=d.run_live_twitch_checks(helper._environment(),root=Path(tempfile.gettempdir()),http_json=transport,
                                      exercise=helper._exercise(),irc_sender=irc,gate_context=synthetic_gate())
        return checks,calls,state,irc

    def test_title_changed_before_write_is_preserved(self):
        checks,calls,state,irc=self._run("title","before")
        self.assertEqual(state["title"],MARKER)
        self.assertFalse(any(m=="PATCH" for m,*_ in calls));irc.assert_not_called()

    def test_category_changed_before_write_is_preserved_and_only_title_restored(self):
        checks,calls,state,irc=self._run("game_id","before")
        self.assertEqual(state["game_id"],MARKER)
        self.assertFalse(any("game_id" in kw.get("json_body",{}) for m,u,kw in calls if m=="PATCH"))
        self.assertEqual(state["title"],"Original title");irc.assert_not_called()

    def test_editor_change_after_temporary_write_is_not_overwritten(self):
        checks,calls,state,irc=self._run("title","after")
        self.assertEqual(state["title"],MARKER)
        self.assertTrue(any(c.status!=d.READY for c in checks if c.name=="Full exercise result"));irc.assert_not_called()


class B6ProductionTests(unittest.TestCase):
    def test_app_token_validation_preserves_cache_on_every_malformed_response(self):
        out=io.StringIO();logger=logging.Logger("b6-token");logger.addHandler(logging.StreamHandler(out))
        for body in ([MARKER],{"access_token":MARKER,"expires_in":MARKER},{"access_token":"","expires_in":3600},{"access_token":MARKER,"expires_in":True}):
            ns={"DEBUG":True,"time":SimpleNamespace(time=lambda:1000),"_app_token":"old", "_app_token_expiry":0,
                "APP_CLIENT_ID":"client","APP_CLIENT_SECRET":MARKER,"TWITCH_AUTH_BASE":"https://example.invalid",
                "requests":SimpleNamespace(post=lambda *_a,**_k:SimpleNamespace(status_code=200,json=lambda:body)),
                "log":logger,"_log_verification_stage":mock.Mock()}
            extracted("MedlarTV/core/stream_management.py",{"_ensure_app_token"},ns)
            with redirect_stdout(out),self.assertRaises(ValueError):ns["_ensure_app_token"]()
            self.assertEqual((ns["_app_token"],ns["_app_token_expiry"]),("old",0))
        self.assertNotIn(MARKER,out.getvalue())

    def test_malformed_broadcaster_response_publishes_no_identity(self):
        ns={"DEBUG":False,"_broadcaster_id":None,"_ensure_app_token":lambda:"synthetic",
            "_app_headers":lambda *_a:{},"CHANNEL_NAME":"channel","TWITCH_API_BASE":"https://example.invalid",
            "requests":SimpleNamespace(get=lambda *_a,**_k:SimpleNamespace(status_code=200,json=lambda:{"data":[{"id":123}]})),
            "log":mock.Mock(),"_log_verification_stage":mock.Mock()}
        extracted("MedlarTV/core/stream_management.py",{"get_broadcaster_id"},ns)
        with self.assertRaises(ValueError):ns["get_broadcaster_id"]()
        self.assertIsNone(ns["_broadcaster_id"])

    def test_outer_startup_catches_secret_bearing_exception(self):
        output=io.StringIO();logger=logging.Logger("b6");logger.addHandler(logging.StreamHandler(output))
        fail=mock.Mock(side_effect=RuntimeError(MARKER))
        ns={"log":logger,"os":SimpleNamespace(getenv=lambda *_a:"true"),"get_emotional_system":fail,
            "read_bootstrap":fail,"load_memory":fail,"verify_twitch_tokens":fail}
        extracted("MedlarTV/core/main.py",{"initialize_systems"},ns)
        with redirect_stdout(output):ns["initialize_systems"]()
        self.assertNotIn(MARKER,output.getvalue());self.assertIn("RuntimeError",output.getvalue())

    def test_generic_logging_keeps_only_types_and_counts(self):
        entries=[];out=io.StringIO()
        ns={"DEBUG":True,"ENABLE_LOGGING":True,"_now_iso":lambda:"synthetic", "_append_jsonl":lambda _name,row:entries.append(row)}
        extracted("MedlarTV/core/interaction_logger.py",{"log_error","log_mood_change"},ns)
        with redirect_stdout(out):
            ns["log_error"](MARKER,MARKER,{MARKER:MARKER})
            ns["log_mood_change"](MARKER,MARKER,MARKER,{MARKER:MARKER})
        self.assertNotIn(MARKER,out.getvalue()+json.dumps(entries))
        self.assertEqual(entries[0]["message_chars"],len(MARKER))

    def test_legacy_memory_diagnostics_keep_values_and_timestamps_private(self):
        output=io.StringIO();data={"moods":[{"mood":MARKER,"source":MARKER,"timestamp":MARKER}]}
        ns={"DEBUG":True,"load_memory":lambda:data,"save_memory":mock.Mock(),"datetime":datetime,
            "Counter":Counter,"defaultdict":defaultdict}
        extracted("MedlarTV/core/memory.py",{"get_dominant_mood","get_dominant_weighted_mood","reset_memory_on_shutdown"},ns)
        with redirect_stdout(output):
            self.assertEqual(ns["get_dominant_mood"](),MARKER)
            self.assertEqual(ns["get_dominant_weighted_mood"](),MARKER)
            ns["reset_memory_on_shutdown"]()
        self.assertNotIn(MARKER,output.getvalue())


if __name__=="__main__":unittest.main()
