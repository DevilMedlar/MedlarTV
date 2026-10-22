"""Synthetic startup preservation and source-isolated non-credential close intent."""
import ast
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from MedlarTV.app import config_editor_store as store

ROOT=Path(__file__).resolve().parents[1]
MARKER='synthetic-sensitive-draft-not-for-dialogs'


class Field:
    def __init__(self,value=''):self.value=value
    def text(self):return self.value
    def toPlainText(self):return self.value
    def setText(self,value):self.value=value
    def setPlainText(self,value):self.value=value


class Dialog:
    AcceptRole,DestructiveRole,RejectRole=range(3)
    choice='Cancel'
    dialogs=[]
    warnings=[]
    def __init__(self,*args):self.buttons={};self.messages=[];self.default=None;type(self).dialogs.append(self)
    def setWindowTitle(self,value):self.messages.append(value)
    def setText(self,value):self.messages.append(value)
    def setInformativeText(self,value):self.messages.append(value)
    def addButton(self,label,role):self.buttons[label]=object();return self.buttons[label]
    def setDefaultButton(self,value):self.default=value
    def exec(self):pass
    def clickedButton(self):return self.buttons.get(self.choice)
    @classmethod
    def warning(cls,*args):cls.warnings.append(args[1:])


class Event:
    def __init__(self):self.ignored=False;self.accepted=False
    def ignore(self):self.ignored=True
    def accept(self):self.accepted=True


class Base:
    def closeEvent(self,event):event.accept()


def window_type(root):
    source=ROOT/'MedlarTV/app/medlar_app.py';tree=ast.parse(source.read_text(encoding='utf-8'))
    owner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ControlCenter')
    names={'_editor_contents','_restore_close_drafts','_confirm_close_settings','closeEvent',
           '_config_file','_settings_path','save_personality','save_moderation','reload_personality',
           'reload_moderation','retry_feature_changes','_save_settings'}
    # Prepatch controls deliberately supply only the methods they actually contain.
    methods=[n for n in owner.body if isinstance(n,ast.FunctionDef) and n.name in names]
    cls=ast.ClassDef(name='Window',bases=[ast.Name(id='Base',ctx=ast.Load())],keywords=[],body=methods,decorator_list=[])
    ns={'Base':Base,'QMessageBox':Dialog,'Path':Path,'os':SimpleNamespace(getenv=lambda key,default='':str(root) if key=='MEDLARTV_ROOT' else default),
        '__file__':str(source)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(source),'exec'),ns)
    return ns['Window']


class CloseIntentTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='close-intent-');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        config=self.root/'MedlarTV/config';config.mkdir(parents=True)
        (config/'personality.yaml').write_text('personality:\n  name: "Before"\n  tone: "warm"\n  trigger_keywords: ["medlar"]\n',encoding='utf-8')
        (config/'content_filter.yaml').write_text('blocked_words:\n  - oldword\nblocked_topics:\n  - oldtopic\n',encoding='utf-8')
        (config/'app_settings.yaml').write_text('llm_brain: true\ncontent_filter: true\ncustom: keep\n',encoding='utf-8')
        self.window=window_type(self.root)();w=self.window
        for name in ('personality_name','personality_tone','personality_triggers','moderation_words','moderation_topics','personality_status','moderation_status','features_status'):
            setattr(w,name,Field())
        w._editor_revisions={'personality':None,'moderation':None};w._editor_snapshots={};w._editor_loaded=set()
        w.reload_personality();w.reload_moderation()
        w._feature_path=w._settings_path().resolve();w._feature_values,w._feature_revision=store.load_features(w._feature_path)
        w._feature_pending={};w._feature_lock_cleanup_failed=False
        w._apply_feature_values=mock.Mock();w._feature_request_summary=lambda:'Feature switches';w.refresh_settings=mock.Mock(return_value=True)
        w._close_ready=False;w._close_pending=False;w._chat_closed=False;w._shutdown_failure=None
        w._shutdown_all=mock.Mock(return_value=True)
        w.save_connection=mock.Mock(side_effect=AssertionError('credential save forbidden'))
        # A connection field would fail if its content were inspected by this flow.
        w.connection_inputs={'synthetic':SimpleNamespace(text=mock.Mock(side_effect=AssertionError('credential draft read forbidden')))}
        Dialog.choice='Cancel';Dialog.dialogs=[];Dialog.warnings=[]

    def dirty(self,kind):
        w=self.window
        if kind in ('personality','all'):w.personality_name.setText(MARKER);w.personality_triggers.setPlainText('newtrigger\n')
        if kind in ('moderation','all'):w.moderation_words.setPlainText('newword\n');w.moderation_topics.setPlainText('newtopic\n')
        if kind in ('features','all'):w._feature_pending={'llm_brain':False}

    def intent(self):
        w=self.window
        return (w._editor_contents('personality'),w._editor_contents('moderation'),dict(w._feature_pending))

    def files(self):return {p.name:p.read_bytes() for p in (self.root/'MedlarTV/config').iterdir() if p.is_file()}

    def close(self,choice):
        Dialog.choice=choice;event=Event();self.window.closeEvent(event);return event

    def test_clean_close_uses_existing_shutdown_without_prompt(self):
        event=self.close('Cancel');self.assertTrue(event.accepted);self.assertFalse(Dialog.dialogs)
        self.window._shutdown_all.assert_called_once()

    def test_cancel_each_draft_type_preserves_files_intent_and_running_app(self):
        for kind in ('personality','moderation','features'):
            with self.subTest(kind=kind):
                self.setUp();self.dirty(kind);before=self.intent();files=self.files();event=self.close('Cancel')
                self.assertTrue(event.ignored);self.assertFalse(self.window._chat_closed)
                self.window._shutdown_all.assert_not_called();self.assertEqual(self.intent(),before);self.assertEqual(self.files(),files)

    def test_dismissed_dialog_is_cancel(self):
        self.dirty('all');before=self.intent();event=self.close('dismissed')
        self.assertTrue(event.ignored);self.assertEqual(self.intent(),before);self.window._shutdown_all.assert_not_called()

    def test_cancel_is_default_and_dialog_contains_only_categories(self):
        self.dirty('all');self.close('Cancel');dialog=Dialog.dialogs[-1]
        self.assertIs(dialog.default,dialog.buttons['Cancel'])
        text=' '.join(dialog.messages);self.assertNotIn(MARKER,text)
        for category in ('Personality','Blocked lists','Feature switches','Connection values are excluded'):self.assertIn(category,text)

    def test_discard_each_draft_type_never_saves_or_clears_existing_files(self):
        for kind in ('personality','moderation','features','all'):
            with self.subTest(kind=kind):
                self.setUp();self.dirty(kind);before=self.intent();files=self.files();event=self.close('Discard')
                self.assertTrue(event.accepted);self.assertEqual(self.files(),files);self.assertEqual(self.intent(),before)
                self.window.save_connection.assert_not_called()

    def test_save_all_reuses_revision_checked_editors_and_feature_retry(self):
        self.dirty('all');event=self.close('Save');w=self.window
        self.assertTrue(event.accepted, (w.personality_status.text(), w.moderation_status.text(), w.features_status.text(), Dialog.warnings));w._shutdown_all.assert_called_once()
        self.assertEqual(store.load_personality(w._config_file('personality.yaml'))[0]['name'],MARKER)
        self.assertEqual(store.load_moderation(w._config_file('content_filter.yaml'))[0]['blocked_words'],['newword'])
        self.assertFalse(store.load_features(w._feature_path)[0]['llm_brain']);self.assertEqual(w._feature_pending,{})
        w.save_connection.assert_not_called();w.connection_inputs['synthetic'].text.assert_not_called()

    def test_save_only_dirty_sections(self):
        self.dirty('features');w=self.window
        w.save_personality=mock.Mock(side_effect=AssertionError('clean personality saved'))
        w.save_moderation=mock.Mock(side_effect=AssertionError('clean moderation saved'))
        self.assertTrue(self.close('Save').accepted)
        w.save_personality.assert_not_called();w.save_moderation.assert_not_called()

    def test_personality_validation_failure_keeps_app_open_and_all_drafts(self):
        self.dirty('all');self.window.personality_name.setText('');before=self.intent();files=self.files()
        event=self.close('Save');self.assertTrue(event.ignored);self.assertEqual(self.intent(),before);self.assertEqual(self.files(),files)
        self.window._shutdown_all.assert_not_called();self.assertTrue(Dialog.warnings)

    def test_later_revision_conflict_retains_drafts_after_earlier_section_saved(self):
        self.dirty('all');w=self.window;before=self.intent();path=w._config_file('content_filter.yaml')
        path.write_bytes(path.read_bytes()+b'# synthetic external edit\n');external=path.read_bytes()
        event=self.close('Save');self.assertTrue(event.ignored);w._shutdown_all.assert_not_called()
        self.assertEqual(self.intent(),before);self.assertEqual(path.read_bytes(),external)
        self.assertEqual(store.load_personality(w._config_file('personality.yaml'))[0]['name'],MARKER)
        self.assertIn('Some sections may already be saved',str(Dialog.warnings))

    def test_feature_save_failure_retains_pending_requests(self):
        self.dirty('all');w=self.window;before=self.intent();w._save_settings=mock.Mock(side_effect=OSError(MARKER))
        event=self.close('Save');self.assertTrue(event.ignored);self.assertEqual(self.intent(),before);w._shutdown_all.assert_not_called()

    def test_feature_post_save_reload_failure_restores_requests_and_keeps_saved_revision(self):
        self.dirty('features');w=self.window;before=self.intent();w.refresh_settings.return_value=False
        event=self.close('Save');self.assertTrue(event.ignored);self.assertEqual(self.intent(),before)
        values,revision=store.load_features(w._feature_path);self.assertFalse(values['llm_brain']);self.assertEqual(w._feature_revision,revision)
        w._shutdown_all.assert_not_called()

    def test_unexpected_save_exception_preserves_drafts_without_echoing_exception(self):
        self.dirty('all');w=self.window;before=self.intent();w.save_personality=mock.Mock(side_effect=RuntimeError(MARKER))
        event=self.close('Save');self.assertTrue(event.ignored);self.assertEqual(self.intent(),before)
        self.assertNotIn(MARKER,str(Dialog.warnings));w._shutdown_all.assert_not_called()

    def test_save_false_after_widget_mutation_restores_exact_visible_text(self):
        self.dirty('all');w=self.window;before=self.intent()
        def incomplete_save():w.personality_name.setText('normalized');w.personality_triggers.setPlainText('normalized');return False
        w.save_personality=incomplete_save
        self.assertTrue(self.close('Save').ignored);self.assertEqual(self.intent(),before)

    def test_credentials_only_keep_existing_close_behavior_without_combined_save(self):
        event=self.close('Save');self.assertTrue(event.accepted);self.assertFalse(Dialog.dialogs)
        self.window.save_connection.assert_not_called();self.window.connection_inputs['synthetic'].text.assert_not_called()

    def test_discard_then_incomplete_shutdown_keeps_drafts_and_existing_async_path(self):
        self.dirty('all');w=self.window;before=self.intent();w._shutdown_all.return_value=False
        event=self.close('Discard');self.assertTrue(event.ignored);self.assertTrue(w._close_pending);self.assertTrue(w._chat_closed)
        self.assertEqual(self.intent(),before);self.assertFalse(w._close_ready)

    def test_repeated_close_during_pending_shutdown_does_not_repeat_draft_prompt(self):
        self.dirty('all');w=self.window;w._shutdown_all.return_value=False
        self.close('Discard');count=len(Dialog.dialogs);self.close('Cancel');self.assertEqual(len(Dialog.dialogs),count)

    def test_shutdown_failure_after_discard_keeps_app_open_and_drafts(self):
        self.dirty('all');w=self.window;before=self.intent();w._shutdown_all.return_value=False;w._shutdown_failure='synthetic failure'
        event=self.close('Discard');self.assertTrue(event.ignored);self.assertFalse(w._chat_closed);self.assertFalse(w._close_pending)
        self.assertEqual(self.intent(),before)

    def test_ready_close_keeps_existing_acceptance_path(self):
        self.dirty('all');self.window._close_ready=True
        self.assertTrue(self.close('Cancel').accepted);self.assertFalse(Dialog.dialogs);self.window._shutdown_all.assert_not_called()


class StartupPreservationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='startup-preserve-');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        for rel in ('logs/core.log','logs/unrelated.keep','db/sessions/session-owned.json','db/sessions/unrelated.keep'):
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic saved evidence',encoding='utf-8')
        self.python=self.root/'.venv/Scripts/python.exe';self.python.parent.mkdir(parents=True);self.python.write_bytes(b'inert marker')
        source=ROOT/'launcher.py';tree=ast.parse(source.read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_run_services')
        class StartupError(RuntimeError):pass
        self.Error=StartupError
        self.calls=[]
        def call(name,result=None):
            def invoke(*args,**kwargs):self.calls.append(name);return result
            return invoke
        def getenv(key,default=''):
            if key=='MEDLARTV_SESSION_KEEP':raise AssertionError('Automatic retention setting was read')
            return default
        self.ns={'StartupError':StartupError,'_stop_requested':lambda:False,'ROOT':self.root,'VENV_PYTHON':self.python,
                 'Colors':SimpleNamespace(green='',reset='',red='',yellow=''),'os':SimpleNamespace(getenv=getenv,environ={}),
                 'clear_logs':call('delete logs'),'clear_old_sessions':call('delete sessions'),
                 '_load_env_file':call('synthetic env'),'_load_saved_connections':call('synthetic connections'),
                 'banner':call('banner'),'ollama_api_ready':call('api',True),'_validate_owned_ollama_endpoint':call('endpoint'),
                 'start_ollama':call('ollama',True),'_monitor_processes':call('monitor'),
                 'start_libretranslate':call('translation'),'libretranslate_home':lambda:self.root,
                 'start_avatar_bridge':call('bridge',False),'start_process':call('core',True)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(source),'exec'),self.ns)

    def evidence(self):return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
    def run_mode(self,mode):
        with redirect_stdout(io.StringIO()):self.ns['_run_services'](mode)

    def assert_preserved(self,before):
        self.assertEqual(self.evidence(),before);self.assertNotIn('delete logs',self.calls);self.assertNotIn('delete sessions',self.calls)

    def test_all_startup_preserves_every_existing_log_and_session(self):
        before=self.evidence();self.run_mode('all');self.assert_preserved(before)

    def test_twitch_startup_preserves_every_existing_log_and_session(self):
        before=self.evidence();self.run_mode('twitch');self.assert_preserved(before)

    def test_ollama_startup_preserves_every_existing_log_and_session(self):
        before=self.evidence();self.run_mode('ollama');self.assert_preserved(before)

    def test_missing_interpreter_prerequisite_preserves_failed_start_evidence(self):
        self.ns['VENV_PYTHON']=self.root/'missing/python.exe';before=self.evidence()
        with self.assertRaises(self.Error):self.run_mode('all')
        self.assert_preserved(before);self.assertNotIn('ollama',self.calls)

    def test_failed_service_start_preserves_failed_start_evidence(self):
        self.ns['start_ollama']=lambda:False;before=self.evidence()
        with self.assertRaises(self.Error):self.run_mode('all')
        self.assert_preserved(before)

    def test_failed_synthetic_connection_loader_preserves_evidence(self):
        self.ns['_load_saved_connections']=mock.Mock(side_effect=self.Error('synthetic connection failure'));before=self.evidence()
        with self.assertRaises(self.Error):self.run_mode('all')
        self.assert_preserved(before)

    def test_already_stopping_does_not_read_configuration_or_cleanup(self):
        self.ns['_stop_requested']=lambda:True;before=self.evidence();self.run_mode('all')
        self.assert_preserved(before);self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
