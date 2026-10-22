"""Synthetic owned-endpoint alignment and current-attempt bounded status contracts."""
import ast
import importlib.util
import ipaddress
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit

from MedlarTV.log_paths import resolve_log_directory
import test_b7_lifecycle_intent as lifecycle
from test_command_center_verification_status import source

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('b7_endpoint',ROOT/'MedlarTV/core/ollama_endpoint.py')
endpoint=importlib.util.module_from_spec(spec);spec.loader.exec_module(endpoint)
UP=b'[IRC] State: CONNECTED\n';DOWN=b'[IRC] State: DISCONNECTED\n'

def validate(values):
    tree=ast.parse((ROOT/'launcher.py').read_text(encoding='utf-8'))
    nodes=[n for n in tree.body if getattr(n,'name',None) in ('_endpoint_host_key','_validate_owned_ollama_endpoint')]
    ns={'ipaddress':ipaddress,'urlsplit':urlsplit,'StartupError':RuntimeError,
        '_ollama_bind_environment':lambda:{'OLLAMA_HOST':values.get('OLLAMA_HOST','127.0.0.1:11434')},
        'ollama_probe_target':lambda env=None:endpoint.ollama_probe_target(values if env is None else env),
        'ollama_base_url':lambda env=None:endpoint.ollama_base_url(values if env is None else env)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'owned-endpoint','exec'),ns)
    ns['_validate_owned_ollama_endpoint']()

class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.fixture=lifecycle.StartupPreservationTests();self.fixture.addCleanup=self.addCleanup;self.fixture.setUp()
    def test_matching_default_bind_and_client(self):validate({})
    def test_loopback_aliases_remain_accepted(self):
        validate({'OLLAMA_HOST':'0.0.0.0:11434','OLLAMA_URL':'http://localhost:11434/'})
        validate({'OLLAMA_HOST':'[::1]:11434','OLLAMA_URL':'http://127.0.0.1:11434'})
    def test_matching_named_host_remains_accepted(self):validate({'OLLAMA_HOST':'box:1234','OLLAMA_URL':'http://box:1234'})
    def test_client_https_cannot_match_raw_owned_serve(self):
        with self.assertRaisesRegex(RuntimeError,'HTTP'):validate({'OLLAMA_URL':'https://localhost:11434'})
    def test_client_base_path_cannot_match_raw_owned_serve(self):
        with self.assertRaisesRegex(RuntimeError,'base path'):validate({'OLLAMA_URL':'http://localhost:11434/proxy'})
    def test_bind_https_or_path_rejected_without_echo(self):
        for value in ('https://localhost:11434','localhost:11434/synthetic-marker'):
            with self.subTest(value=value), self.assertRaises(RuntimeError) as raised:validate({'OLLAMA_HOST':value})
            self.assertNotIn('synthetic-marker',str(raised.exception))
    def test_conflicting_host_or_port_stays_rejected(self):
        for value in ('http://remote:11434','http://localhost:1234'):
            with self.subTest(value=value),self.assertRaises(RuntimeError):validate({'OLLAMA_URL':value})
    def test_client_resolver_retains_remote_https_and_path_semantics(self):
        self.assertEqual(endpoint.ollama_base_url({'OLLAMA_URL':'https://remote/proxy/'}),'https://remote/proxy')
    def test_all_mode_alignment_precedes_service_probe_and_spawn(self):
        f=self.fixture;f.ns['_validate_owned_ollama_endpoint']=Mock(side_effect=f.Error('synthetic mismatch'))
        before=f.evidence()
        with self.assertRaises(f.Error):f.run_mode('all')
        f.assert_preserved(before);self.assertNotIn('ollama',f.calls);self.assertNotIn('api',f.calls)
    def test_ollama_mode_alignment_precedes_spawn(self):
        f=self.fixture;f.ns['_validate_owned_ollama_endpoint']=Mock(side_effect=f.Error('synthetic mismatch'))
        with self.assertRaises(f.Error):f.run_mode('ollama')
        self.assertNotIn('ollama',f.calls)
    def test_split_twitch_never_validates_or_starts_owned_server(self):
        f=self.fixture;f.ns['_validate_owned_ollama_endpoint']=Mock(side_effect=AssertionError('owned validation forbidden'))
        f.run_mode('twitch');self.assertIn('api',f.calls);self.assertNotIn('ollama',f.calls)
    def test_all_mode_validates_loaded_endpoint_values(self):
        f=self.fixture;seen=[];f.ns['os'].environ={}
        f.ns['_load_env_file']=lambda:f.ns['os'].environ.update(OLLAMA_URL='http://synthetic:1234')
        f.ns['_validate_owned_ollama_endpoint']=lambda:seen.append(dict(f.ns['os'].environ))
        f.run_mode('all');self.assertEqual(seen,[{'OLLAMA_URL':'http://synthetic:1234'}])
    def test_split_env_loader_cannot_retarget_inherited_endpoint_or_log_path(self):
        f=self.fixture;f.ns['os'].environ={'OLLAMA_URL':'https://remote/proxy','LOG_DIRECTORY':'selected-logs'}
        f.ns['_load_env_file']=lambda:f.ns['os'].environ.update(OLLAMA_URL='http://other',OLLAMA_HOST='other',LOG_DIRECTORY='other-logs')
        f.run_mode('twitch');self.assertEqual(f.ns['os'].environ,{'OLLAMA_URL':'https://remote/proxy','LOG_DIRECTORY':'selected-logs'})

class IRCStatusTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='b7-status-');self.addCleanup(temp.cleanup);self.root=Path(temp.name);self.path=self.root/'irc.txt'
        tree=ast.parse((ROOT/'MedlarTV/app/medlar_app.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if getattr(n,'name',None) in ('_IRCLog','_irc_connected_after_log_chunk')]
        self.ns={'os':SimpleNamespace(fstat=os.fstat)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'irc-cursor','exec'),self.ns)
    def reader(self):return self.ns['_IRCLog'](self.path)
    def append(self,data):
        with self.path.open('ab') as f:f.write(data)
    def test_historical_success_is_not_current_evidence(self):
        self.path.write_bytes(UP);r=self.reader();self.assertFalse(r.read_status());self.append(DOWN);self.assertFalse(r.read_status())
    def test_fresh_success_and_failure_order(self):
        r=self.reader();self.append(UP);self.assertTrue(r.read_status());self.append(DOWN+UP);self.assertTrue(r.read_status());self.append(DOWN);self.assertFalse(r.read_status())
    def test_partial_record_waits_for_newline(self):
        r=self.reader();self.append(UP[:-1]);self.assertFalse(r.read_status());self.append(b'\n');self.assertTrue(r.read_status())
    def test_missing_file_reports_off_then_recovers(self):
        r=self.reader();self.append(UP);self.assertTrue(r.read_status());self.path.rename(self.root/'away');self.assertFalse(r.read_status());self.append(DOWN);self.assertFalse(r.read_status())
    def test_rotation_resets_prior_connected_state(self):
        r=self.reader();self.append(UP);self.assertTrue(r.read_status());self.path.rename(self.root/'old');self.path.write_bytes(b'new run\n');self.assertFalse(r.read_status())
    def test_truncation_resets_state_and_pending_bytes(self):
        r=self.reader();self.append(UP+b'partial');self.assertTrue(r.read_status());self.path.write_bytes(b'x\n');self.assertFalse(r.read_status());self.assertEqual(r.pending,b'')
    def test_same_size_rewrite_resets_without_replaying_success(self):
        r=self.reader();self.append(UP);self.assertTrue(r.read_status());self.path.write_bytes(b'x'*(len(UP)-1)+b'\n');self.assertFalse(r.read_status())
    def test_bounded_reads_do_not_report_success_before_backlog_failure(self):
        r=self.reader();self.append(UP+b'x'*70000+b'\n'+DOWN);self.assertFalse(r.read_status());self.assertEqual(r.offset,65536);self.assertLessEqual(len(r.pending),2048);self.assertFalse(r.read_status())
    def test_new_start_rebaselines_existing_success(self):
        r=self.reader();self.append(UP);self.assertTrue(r.read_status());self.assertFalse(self.reader().read_status())
    def test_missing_baseline_gui_does_not_search_historical_logs(self):
        (self.root/'logs').mkdir();(self.root/'logs/twitch_listener_log.txt').write_bytes(UP)
        ns=source({'os':SimpleNamespace(environ={},fstat=os.fstat,SEEK_END=os.SEEK_END),'Path':Path})
        w=SimpleNamespace(_root=self.root,launcher_proc=Mock(poll=Mock(return_value=None)),ollama_proc=None,_ollama_ready=True,_ollama_starting=False,_shutdown_failure=None,status_label=Mock())
        ns['refresh_status'](w);self.assertIn('IRC=OFF',w.status_label.setText.call_args.args[0])

class LogPathTests(unittest.TestCase):
    def test_default_relative_and_absolute_resolution(self):
        root=Path(tempfile.gettempdir())/'synthetic-root'
        self.assertEqual(resolve_log_directory(root,{}),root/'logs')
        self.assertEqual(resolve_log_directory(root,{'LOG_DIRECTORY':'custom'}),root/'custom')
        absolute=root.parent/'absolute';self.assertEqual(resolve_log_directory(root,{'LOG_DIRECTORY':str(absolute)}),absolute)
    def test_empty_value_preserves_existing_core_root_semantics(self):
        root=Path(tempfile.gettempdir());self.assertEqual(resolve_log_directory(root,{'LOG_DIRECTORY':''}),root)
    def test_spawn_uses_same_log_path_and_baselines_before_child_records(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        env={'LOG_DIRECTORY':'custom'};logs=root/'custom'
        def spawn(*args,**kwargs):
            (logs/'twitch_listener_log.txt').write_bytes(UP);(logs/'stream_management.txt').write_bytes(b'[TwitchVerify] stage=complete outcome=ok\n');return Mock()
        ns=source({'os':SimpleNamespace(environ=env,getpid=lambda:99,name='posix',fstat=os.fstat),'Path':Path,
                   'tempfile':SimpleNamespace(gettempdir=lambda:str(root)),'uuid':SimpleNamespace(uuid4=lambda:SimpleNamespace(hex='synthetic')),
                   'subprocess':SimpleNamespace(Popen=spawn,DEVNULL=-3)})
        w=SimpleNamespace(_service_root=lambda:root);ns['_spawn_launcher_mode'](w,'twitch')
        self.assertEqual(w._twitch_irc_log.path,logs/'twitch_listener_log.txt');self.assertTrue(w._twitch_irc_log.read_status());self.assertEqual(w._twitch_verification_log.read_status(),'OK')

if __name__=='__main__':unittest.main()
