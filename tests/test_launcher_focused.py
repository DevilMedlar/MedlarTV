"""Source-isolated launcher regression checks: fake processes and clocks only."""
import ast
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
source = (ROOT / 'launcher.py').read_text(encoding='utf-8')
tree = ast.parse(source, filename='launcher.py')
ns = {'__name__': 'synthetic_launcher', 'ROOT': ROOT, 'LOGS_DIR': HERE,
      'VENV_PYTHON': ROOT / '.venv/Scripts/python.exe', 'DEBUG': False}
keep_globals = {'processes', 'STOP_FILE_ENV', 'OWNER_PID_ENV', 'CORE_STOP_FILE_ENV',
                'SHUTDOWN_GRACE_SECONDS', 'SHUTDOWN_TOTAL_SECONDS',
                '_windows_job_handle', '_owner_pid', '_owner_handle', '_owner_missing', '_avatar_session'}
nodes = []
for n in tree.body:
    if isinstance(n, ast.Import) or (isinstance(n, ast.ImportFrom) and not n.module.startswith('MedlarTV')):
        nodes.append(n)
    elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        nodes.append(n)
    elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in keep_globals for t in n.targets):
        nodes.append(n)
    elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in keep_globals:
        nodes.append(n)
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'launcher.py', 'exec'), ns)
# Preload mock machinery before denying every real service/native operation.
import socket
import subprocess
DENIALS = []
def audit(event, args):
    if event.startswith(('socket.', 'subprocess.', 'os.spawn', 'os.exec')) or event in {'os.kill', 'os.killpg', 'os.system', 'ctypes.dlopen'}:
        DENIALS.append(event)
        raise AssertionError('Real system operation forbidden: ' + event)
sys.addaudithook(audit)

class Clock:
    def __init__(self): self.now = 0.0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds
class Child:
    pid = 43219
    def __init__(self):
        self.exited = False
        self.stdin = io.BytesIO()
        self.signals = []
    def poll(self): return 0 if self.exited else None
    def wait(self, timeout):
        if not self.exited: raise AssertionError('Fake force did not exit child')
        return 0
    def send_signal(self, sig): self.signals.append(sig)

# The separate21-case script also compares actual ctypes layouts. Windows
# ctypes import loads kernel32, so that case is deliberately outside this
# unchanged native-access-denying guard; its original evidence stays retained.
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.child = Clock(), Child()
        self.entry = {'proc': self.child, 'pid': self.child.pid, 'name': 'fake',
                      'log': io.StringIO(), 'stop_file': None, 'required': True, 'console_group': True}
        self.force = Mock(side_effect=lambda *args, **kwargs: setattr(self.child, 'exited', True))
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        for ctx in (patch.dict(ns, {'time': self.clock, 'processes': [self.entry], '_child_environment': lambda **kw: {}}),
                    patch.object(ns['platform'], 'system', return_value='Windows'),
                    patch.object(ns['subprocess'], 'run', self.force), patch('builtins.print')):
            self.stack.enter_context(ctx)
    def tearDown(self):
        self.assertEqual(DENIALS, [])
    def stop(self, **kw): return ns['_stop_owned_process'](self.child, **kw)
    def test_default_deadlines_and_cooperative_stop(self):
        self.assertTrue(self.stop())
        self.assertGreaterEqual(self.clock.now, 15)
        self.assertLessEqual(self.force.call_args.kwargs['timeout'], 5)
        self.assertEqual(self.child.signals, [signal.CTRL_BREAK_EVENT])
    def test_grace_only_gets_bounded_force_window(self):
        self.assertTrue(self.stop(grace_deadline=2))
        self.assertAlmostEqual(self.force.call_args.kwargs['timeout'], 4.999999999999999)
        self.assertEqual(self.child.signals, [])
    def test_explicit_total_deadline_preserved(self):
        self.assertFalse(self.stop(deadline=3))
        self.force.assert_not_called()
        self.assertEqual(len(self.child.signals), 1)
        self.assertEqual(self.clock.now, 3)
    def test_both_explicit_deadlines_preserved(self):
        self.assertTrue(self.stop(grace_deadline=1, deadline=8))
        self.assertLessEqual(self.force.call_args.kwargs['timeout'], 7)
        self.assertEqual(self.child.signals, [])
    def test_total_caps_supplied_later_grace(self):
        self.assertFalse(self.stop(grace_deadline=9, deadline=2.05))
        self.assertEqual(self.clock.now, 2.05)
        self.force.assert_not_called()
        self.assertEqual(self.child.signals, [])
    def test_expired_total_does_not_wait(self):
        self.assertFalse(self.stop(deadline=0))
        self.assertEqual(self.clock.now, 0)
        self.force.assert_not_called()
    def test_unowned_process_rejected(self):
        ns['processes'] = []
        self.assertFalse(self.stop())
        self.force.assert_not_called()
        self.assertEqual(self.child.signals, [])
    def test_changed_recorded_pid_rejected(self):
        self.entry['pid'] += 1
        self.assertFalse(self.stop())
        self.force.assert_not_called()
    def test_exited_child_needs_no_request(self):
        self.child.exited = True
        self.assertTrue(self.stop())
        self.force.assert_not_called()
    def test_posix_group_signals_preserved(self):
        killpg = Mock(side_effect=lambda pid, sig: setattr(self.child, 'exited', sig == 9))
        with patch.object(ns['platform'], 'system', return_value='Linux'), patch.object(os, 'killpg', killpg, create=True), patch.object(signal, 'SIGKILL', 9, create=True):
            self.assertTrue(self.stop())
        self.assertEqual(killpg.call_args_list[0].args, (self.child.pid, signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (self.child.pid, 9))
        self.force.assert_not_called()
    def test_none_interpreter_never_reaches_subprocess(self):
        with patch.dict(ns, {'_local_libretranslate_target': lambda: ('127.0.0.1',5000), 'libretranslate_python': lambda path: (None,None)}), patch.object(ns['socket'], 'create_connection', side_effect=OSError), patch.object(Path, 'is_dir', return_value=True):
            ns['start_libretranslate'](ROOT)
        self.force.assert_not_called()
    def test_missing_colorama_disables_all_colors(self):
        original_import = builtins.__import__
        def fake_import(name, *args, **kw):
            if name == 'colorama': raise ImportError('synthetic absent module')
            return original_import(name, *args, **kw)
        colors = ns['Colors']
        original = [getattr(colors, name) for name in ('red','green','yellow','cyan','reset')]
        try:
            with patch('builtins.__import__', fake_import): colors.init()
            self.assertTrue(all(getattr(colors,name) == '' for name in ('red','green','yellow','cyan','reset')))
        finally:
            for name, value in zip(('red','green','yellow','cyan','reset'), original): setattr(colors,name,value)
    def test_readiness_rejects_invalid_shapes(self):
        for payload, expected in ((b'[{"code":"en"}]',True),(b'[{"code":" "}]',False),(b'[{"code":1}]',False),(b'[null,1,"en"]',False),(b'{}',False),(b'[]',False)):
            response = io.BytesIO(payload)
            response.status = 200
            with patch.dict(ns, {'urlopen': Mock(return_value=response)}):
                self.assertIs(ns['_libretranslate_ready']('127.0.0.1',5000),expected)
    def test_dynamic_pid_values_remain_defensively_rejected(self):
        for pid in (True,False,None,'43219',43219.0,0,-1,os.getpid()):
            self.child.pid=pid
            self.assertIsNone(ns['_owned_entry'](self.child))
        self.child.pid=43219
        self.assertIs(ns['_owned_entry'](self.child),self.entry)
    def test_annotated_avatar_helpers_preserve_payloads_and_wire_bytes(self):
        module=types.ModuleType('synthetic_avatar_auth')
        sys.modules[module.__name__]=module
        try:
            exec(compile((ROOT/'MedlarTV/avatar/bridge/auth.py').read_text(encoding='utf-8'),'synthetic-auth','exec'),module.__dict__)
            with patch.object(module.secrets,'token_hex',side_effect=['a'*64,'b'*64]):
                session=module.create_session('Synthetic','ws://127.0.0.1:8765')
            self.assertEqual(session.channel,'#synthetic')
            for role in ('server','publisher','subscriber'):
                payload=module.child_payload(session,role)
                encoded=module.encode_bootstrap(payload)
                self.assertTrue(encoded.endswith(b'\n'))
                self.assertEqual(json.loads(encoded),payload)
                expected=module.parse_bootstrap(payload,role)
                self.assertEqual(expected.role,role)
                self.assertEqual(expected.channel,session.channel)
                self.assertEqual(expected.url,session.url)
                if role=='server':
                    self.assertEqual(expected.publisher_key,'a'*64)
                    self.assertEqual(expected.subscriber_key,'b'*64)
                else:
                    self.assertEqual(expected.key,('a' if role=='publisher' else 'b')*64)
            with self.assertRaises(module.BootstrapError): module.child_payload(session,'invalid')
            with self.assertRaises(module.BootstrapError): module.encode_bootstrap({'key':'a'*3000})
        finally:
            sys.modules.pop(module.__name__,None)
    def configure_pair(self, stdout, stderr):
        for stream in (stdout, stderr):
            ns['_configure_output_stream'](stream)
    def test_compatible_wrappers_are_both_configured(self):
        class Stream:
            def __init__(self): self.calls = []
            def reconfigure(self, *, encoding, errors): self.calls.append((encoding,errors))
        stdout, stderr = Stream(), Stream()
        self.configure_pair(stdout,stderr)
        self.assertEqual(stdout.calls,[('utf-8','replace')])
        self.assertEqual(stderr.calls,[('utf-8','replace')])
    def test_missing_reconfigure_does_not_skip_stderr(self):
        class Stream:
            def __init__(self): self.calls = []
            def reconfigure(self, *, encoding, errors): self.calls.append((encoding,errors))
        stderr=Stream()
        self.configure_pair(io.StringIO(),stderr)
        self.assertEqual(stderr.calls,[('utf-8','replace')])
    def test_raising_stdout_does_not_skip_stderr(self):
        class Stream:
            def __init__(self, raises=False): self.calls=[]; self.raises=raises
            def reconfigure(self, *, encoding, errors):
                self.calls.append((encoding,errors))
                if self.raises: raise OSError('synthetic stream failure')
        stdout,stderr=Stream(True),Stream()
        self.configure_pair(stdout,stderr)
        self.assertEqual(stdout.calls,[('utf-8','replace')])
        self.assertEqual(stderr.calls,[('utf-8','replace')])
    def test_both_missing_methods_are_supported(self):
        self.configure_pair(io.StringIO(),io.StringIO())
    def test_raising_stderr_does_not_undo_stdout(self):
        class Stream:
            def __init__(self, raises=False): self.calls=[]; self.raises=raises
            def reconfigure(self, *, encoding, errors):
                self.calls.append((encoding,errors))
                if self.raises: raise OSError('synthetic stream failure')
        stdout,stderr=Stream(),Stream(True)
        self.configure_pair(stdout,stderr)
        self.assertEqual(stdout.calls,[('utf-8','replace')])
        self.assertEqual(stderr.calls,[('utf-8','replace')])
