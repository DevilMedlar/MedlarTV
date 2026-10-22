"""Synthetic filenames and process/sound doubles; no playback or child launch."""
import ast
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from MedlarTV import audio_playback as audio
ROOT=Path(__file__).resolve().parents[1]

class LaunchTests(unittest.TestCase):
    def launch(self, filename, duration=10000):
        process=Mock()
        with patch.object(audio.subprocess,"Popen",return_value=process) as popen:
            self.assertTrue(audio.launch_media_player(filename,duration))
        args,kw=popen.call_args
        self.assertEqual(args[0][-1],audio.MEDIA_PLAYER_PROGRAM)
        self.assertNotIn(filename,args[0][-1])
        self.assertFalse(kw["shell"])
        self.assertEqual(kw["stdin"],audio.subprocess.PIPE)
        self.assertEqual(kw["encoding"],"ascii")
        self.assertEqual(json.loads(process.stdin.write.call_args.args[0]),{"path":filename,"duration_ms":duration})
        process.stdin.close.assert_called_once();process.wait.assert_not_called();process.communicate.assert_not_called()
        return args[0],kw
    def test_spaces_are_data(self):self.launch("C:/synthetic/two words.mp3")
    def test_apostrophe_is_data(self):self.launch("C:/synthetic/it's fine.m4a")
    def test_quotes_are_data(self):self.launch('C:/synthetic/"quoted".aac')
    def test_dollar_expression_is_data(self):self.launch("C:/synthetic/$(synthetic).mp3")
    def test_unicode_is_ascii_json_data(self):self.launch("C:/synthetic/音楽-é.mp3")
    def test_duration_is_data_not_program(self):
        args,kw=self.launch("C:/synthetic/audio.mp3",1234);self.assertNotIn("1234",args[-1])
    def test_hidden_noninteractive_launch(self):
        args,kw=self.launch("C:/synthetic/audio.mp3")
        self.assertIn("-NonInteractive",args);self.assertEqual(args[args.index("-WindowStyle")+1],"Hidden")
        self.assertEqual(kw["creationflags"],getattr(audio.subprocess,"CREATE_NO_WINDOW",0))
    def test_spawn_failure_returns_false(self):
        with patch.object(audio.subprocess,"Popen",side_effect=OSError("synthetic")):
            self.assertFalse(audio.launch_media_player("synthetic.mp3",10000))
    def test_pipe_failure_closes_and_returns_false(self):
        process=Mock();process.stdin.write.side_effect=BrokenPipeError("synthetic")
        with patch.object(audio.subprocess,"Popen",return_value=process):
            self.assertFalse(audio.launch_media_player("synthetic.mp3",10000))
        process.stdin.close.assert_called_once()
    def test_program_starts_with_valid_error_preference_assignment(self):
        # Independent expected syntax catches stray patch markers; process mocks
        # cannot parse PowerShell or prove its runtime/codecs are available.
        self.assertEqual(audio.MEDIA_PLAYER_PROGRAM.splitlines()[0], "$ErrorActionPreference = 'Stop'")
        self.assertFalse(any(line.startswith("+") for line in audio.MEDIA_PLAYER_PROGRAM.splitlines()))

class SoundTests(unittest.TestCase):
    def setUp(self):
        self.name="synthetic.wav";self.exists=True;self.clock=100.0
        self.path=SimpleNamespace(exists=lambda:self.exists,suffix=".wav",resolve=lambda:"C:/synthetic/absolute.wav")
        class Filename:
            def __str__(inner):return self.name
            def exists(inner):return self.exists
            @property
            def suffix(inner):return Path(self.name).suffix
            def resolve(inner):return "C:/synthetic/"+self.name
        self.wav=Mock();self.startfile=Mock()
        ns={"Path":lambda _:Filename(),"time":SimpleNamespace(time=lambda:self.clock),"_audio_lock":threading.Lock(),
            "_last_audio_play":{},"log":Mock(),"os":SimpleNamespace(startfile=self.startfile),
            "winsound":SimpleNamespace(PlaySound=self.wav,SND_FILENAME=1,SND_ASYNC=2)}
        tree=ast.parse((ROOT/"MedlarTV/tools/twitch_listener.py").read_text(encoding="utf-8"))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="_play_local_sound")
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),"sound-fixture","exec"),ns)
        self.play=ns["_play_local_sound"]
    def test_wav_async_flags_unchanged(self):
        self.assertTrue(self.play(self.name));self.wav.assert_called_once_with(self.name,3);self.startfile.assert_not_called()
    def test_missing_file_has_no_effect(self):
        self.exists=False;self.assertFalse(self.play(self.name));self.wav.assert_not_called();self.startfile.assert_not_called()
    def test_one_second_same_file_cooldown(self):
        self.assertTrue(self.play(self.name));self.clock+=.5;self.assertFalse(self.play(self.name));self.clock+=.5;self.assertTrue(self.play(self.name));self.assertEqual(self.wav.call_count,2)
    def test_separate_files_have_independent_cooldown(self):
        self.assertTrue(self.play(self.name));self.name="other.wav";self.assertTrue(self.play(self.name));self.assertEqual(self.wav.call_count,2)
    def test_wav_error_does_not_open_association(self):
        self.wav.side_effect=RuntimeError("synthetic");self.assertFalse(self.play(self.name));self.startfile.assert_not_called()
    def test_all_explicit_media_extensions_preserved(self):
        with patch.object(audio,"launch_media_player",return_value=True) as player:
            for ext in (".mp3",".m4a",".aac",".MP3"):
                self.name="synthetic"+ext;self.assertTrue(self.play(self.name))
            self.assertEqual(player.call_count,4);self.startfile.assert_not_called()
    def test_duration_default_and_override_preserved(self):
        with patch.object(audio,"launch_media_player",return_value=True) as player:
            for i,duration in enumerate((0,-1,1234)):
                self.name=f"synthetic{i}.mp3";self.assertTrue(self.play(self.name,duration))
            self.assertEqual([c.args[1] for c in player.call_args_list],[10000,10000,1234])
    def test_media_launch_failure_does_not_fall_back(self):
        self.name="synthetic.mp3"
        with patch.object(audio,"launch_media_player",return_value=False):self.assertFalse(self.play(self.name))
        self.startfile.assert_not_called()
    def test_unsupported_file_never_executes_association(self):
        for suffix in (".ps1",".exe",".cmd",".txt",""):
            self.name="synthetic"+suffix;self.assertFalse(self.play(self.name))
        self.startfile.assert_not_called();self.wav.assert_not_called()
    def test_filename_never_enters_program_from_listener(self):
        self.name='synthetic \"quote\" $(expression) 音楽.mp3'
        process=Mock()
        with patch.object(audio.subprocess,"Popen",return_value=process) as popen:
            self.assertTrue(self.play(self.name,1234))
        args,kw=popen.call_args
        self.assertNotIn(self.name,args[0][-1])
        self.assertEqual(args[0][-1],audio.MEDIA_PLAYER_PROGRAM)
        self.assertEqual(json.loads(process.stdin.write.call_args.args[0]),{"path":"C:/synthetic/"+self.name,"duration_ms":1234})
        self.startfile.assert_not_called()
