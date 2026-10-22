"""Source-isolated root selection with inert widgets, timers and environment."""
import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'MedlarTV/app/medlar_app.py'


class InertAttribute:
    def __init__(self,owner,name):self.owner,self.name=owner,name
    def __getattr__(self,name):return InertAttribute(self.owner,self.name+'.'+name)
    def __call__(self,*args,**kwargs):self.owner.calls.append((self.name,args,kwargs))


class InertWidget:
    def __init__(self,*args,**kwargs):
        self.calls=[]
    def __getattr__(self,name):
        if name.startswith('__'):raise AttributeError(name)
        return InertAttribute(self,name)


def window_type(environment, source_root, executable):
    tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
    owner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ControlCenter')
    names={'__init__','_service_root','_settings_path'}
    methods=[n for n in owner.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods}==names
    cls=ast.ClassDef(name='ControlCenter',bases=[ast.Name(id='InertWidget',ctx=ast.Load())],keywords=[],body=methods,decorator_list=[])
    module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
    scheduled=[]
    class Timer(InertWidget):
        @staticmethod
        def singleShot(delay,callback):scheduled.append((delay,callback))
    fake_os=SimpleNamespace(getenv=lambda key,default='':environment.get(key,default),environ=environment,
                            path=SimpleNamespace(expanduser=lambda _:str(source_root/'home')))
    ns={'Path':Path,'os':fake_os,'sys':SimpleNamespace(executable=str(executable)),
        '__file__':str(source_root/'MedlarTV/app/medlar_app.py'),'InertWidget':InertWidget,
        'load_dotenv':lambda:None,'QTimer':Timer,'QApplication':SimpleNamespace(instance=lambda:None),
        'Qt':SimpleNamespace(AlignLeft=0)}
    for name in ('QIcon','QLabel','QPushButton','QCheckBox','QPlainTextEdit','QLineEdit'):ns[name]=InertWidget
    exec(compile(module,str(SOURCE),'exec'),ns)
    return ns['ControlCenter'],scheduled


class CommandCenterRootTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(prefix='root-contract-');self.addCleanup(folder.cleanup)
        self.folder=Path(folder.name)
        self.explicit=self.folder/'explicit checkout'
        self.fallback=self.folder/'fallback'
        self.cwd=self.folder/'unrelated cwd'
        for root in (self.explicit,self.fallback,self.cwd):
            (root/'MedlarTV/config').mkdir(parents=True)
            (root/'launcher.py').write_text('# synthetic launcher; never executed\n')
            python=root/'.venv/Scripts/python.exe';python.parent.mkdir(parents=True);python.write_bytes(b'synthetic inert marker')
        self.environment={}
        self.Window,self.scheduled=window_type(self.environment,self.fallback,self.folder/'interpreter/a/b/python.exe')

    def construct(self):
        exists=Path.exists
        def safe_exists(path):
            # Candidate icon/home paths never inspect the real host filesystem.
            return exists(path) if path.is_relative_to(self.folder) else False
        with patch.object(Path,'cwd',return_value=self.cwd),patch.object(Path,'exists',safe_exists):
            return self.Window()

    def test_constructor_preserves_explicit_root_even_when_discovery_candidates_exist(self):
        self.environment['MEDLARTV_ROOT']=str(self.explicit)
        self.construct()
        self.assertEqual(self.environment['MEDLARTV_ROOT'],str(self.explicit))

    def test_explicit_root_is_not_replaced_when_configuration_directory_is_missing(self):
        missing=self.folder/'missing checkout'
        self.environment['MEDLARTV_ROOT']=str(missing)
        self.construct()
        self.assertEqual(self.environment['MEDLARTV_ROOT'],str(missing))

    def test_constructor_avoids_configuration_discovery_for_explicit_root(self):
        self.environment['MEDLARTV_ROOT']=str(self.explicit);probed=[]
        exists=Path.exists
        def track(path):
            if path.name=='config':probed.append(path)
            return exists(path) if path.is_relative_to(self.folder) else False
        with patch.object(Path,'cwd',return_value=self.cwd),patch.object(Path,'exists',track):self.Window()
        self.assertEqual(probed,[])

    def test_service_root_and_settings_use_the_same_explicit_checkout(self):
        self.environment['MEDLARTV_ROOT']=str(self.explicit)
        window=self.construct()
        self.assertEqual(window._service_root(),self.explicit)
        self.assertEqual(window._settings_path(),self.explicit/'MedlarTV/config/app_settings.yaml')

    def test_missing_explicit_launcher_does_not_fall_back_to_valid_checkout(self):
        self.environment['MEDLARTV_ROOT']=str(self.folder/'no launcher')
        window=self.construct()
        with patch.object(Path,'cwd',return_value=self.cwd),self.assertRaisesRegex(FileNotFoundError,'Configured MEDLARTV_ROOT'):
            window._service_root()

    def test_missing_explicit_interpreter_does_not_fall_back(self):
        root=self.folder/'no interpreter';root.mkdir();(root/'launcher.py').write_text('# inert')
        self.environment['MEDLARTV_ROOT']=str(root)
        window=self.construct()
        with patch.object(Path,'cwd',return_value=self.cwd),self.assertRaisesRegex(FileNotFoundError,'Configured MEDLARTV_ROOT'):
            window._service_root()

    def test_explicit_root_wins_over_stale_active_root(self):
        self.environment['MEDLARTV_ROOT']=str(self.explicit)
        window=self.construct();window._root=self.fallback
        self.assertEqual(window._service_root(),self.explicit)

    def test_explicit_absolute_root_is_independent_of_caller_cwd(self):
        self.environment['MEDLARTV_ROOT']=str(self.explicit)
        window=self.construct()
        for cwd in (self.cwd,self.fallback):
            with self.subTest(cwd=cwd),patch.object(Path,'cwd',return_value=cwd):
                self.assertEqual(window._service_root(),self.explicit)
                self.assertEqual(window._settings_path().parent.parent.parent,self.explicit)

    def test_absent_or_blank_root_keeps_existing_constructor_discovery(self):
        for value in (None,'','   '):
            with self.subTest(value=value):
                self.environment.clear()
                if value is not None:self.environment['MEDLARTV_ROOT']=value
                self.construct()
                self.assertEqual(Path(self.environment['MEDLARTV_ROOT']),self.fallback)

    def test_absent_root_keeps_existing_service_discovery(self):
        window=self.Window.__new__(self.Window)
        with patch.object(Path,'cwd',return_value=self.cwd):
            self.assertEqual(window._service_root(),self.fallback)

    def test_configuration_and_service_selection_follow_explicit_root_change(self):
        self.environment['MEDLARTV_ROOT']=str(self.explicit);window=self.construct()
        self.environment['MEDLARTV_ROOT']=str(self.cwd)
        self.assertEqual(window._service_root(),self.cwd)
        self.assertEqual(window._settings_path().parent.parent.parent,self.cwd)

    def test_constructor_schedules_only_inert_callbacks_without_executing_services(self):
        self.environment['MEDLARTV_ROOT']=str(self.explicit);window=self.construct()
        self.assertEqual(len(self.scheduled),1)
        self.assertEqual(self.scheduled[0][0],0)
        # No event loop or callback invocation occurs in this source-isolated fixture.
        self.assertFalse(any(name=='_start_ollama' for name,args,kwargs in window.calls))


if __name__=='__main__':unittest.main()
