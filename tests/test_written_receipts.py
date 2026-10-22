"""Real listener/policy/queue logic with a recording fake socket and synthetic DBs."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
from functools import partial
from datetime import datetime, timedelta, timezone
import queue
import sys
import threading
from types import ModuleType, SimpleNamespace
from pathlib import Path
from typing import Callable, Protocol, TypedDict, TypeAlias, runtime_checkable
import unittest
from unittest.mock import Mock, patch

# Load this pure parser before the output fixture installs synthetic packages.
# Standalone receipt tests must not depend on another suite priming imports.
import MedlarTV.command_authority
assert MedlarTV.command_authority is sys.modules["MedlarTV.command_authority"]
from MedlarTV.app import config_editor_store as editor
from MedlarTV.core.conversation_context import ConversationContextStore
from MedlarTV.core.memory_control import read_memory_control
from MedlarTV.core.memory_receipts import ReplyReceipt, SourceObservation, WrittenReply
from MedlarTV.core.viewer_profiles import ProfileFact
import test_twitch_memory_wiring as wiring
import test_twitch_outgoing as outgoing


# These AST-built fixtures intentionally have no statically imported listener
# class. Describe only the real members this white-box receipt suite exercises.
_ControlItem: TypeAlias = tuple[int, str, threading.Event | None, bool]
_ChatItem: TypeAlias = tuple[
    int, str, threading.Event | None, bool, bool, ReplyReceipt | None, float,
]
_QueueItem: TypeAlias = _ControlItem | _ChatItem


class _OutboundQueue(Protocol):
    queue: deque[_QueueItem | None]

    def put_nowait(self, item: _QueueItem | None) -> None: ...
    def get_nowait(self) -> _QueueItem | None: ...
    def task_done(self) -> None: ...
    def qsize(self) -> int: ...


@runtime_checkable
class _RecordingSocket(Protocol):
    sent: list[bytes]
    failure: Exception | None
    closed: bool

    def sendall(self, payload: bytes) -> None: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...


class _WorkLocal(Protocol):
    generation: int
    redirect_allowed: bool


class _SenderThread(Protocol):
    def is_alive(self) -> bool: ...


class _AliveSender:
    """Synthetic ready sender; never creates or starts a thread."""
    def is_alive(self) -> bool:
        return True


class _SendLock(Protocol):
    def acquire(self) -> bool: ...
    def release(self) -> None: ...


class _OutputPolicy(Protocol):
    def permits(self, text: str) -> bool: ...
    def format_body(self, text: str) -> str: ...


class _GateOutbound(Protocol):
    def __call__(self, text: str, generation: int, *, trusted_command: bool = False) -> str | None: ...


@runtime_checkable
class _ReceiptClient(Protocol):
    sock: _RecordingSocket | None
    connected: bool
    _work_local: _WorkLocal
    _outbound_queue: _OutboundQueue | None
    _sender_thread: _SenderThread | None
    _send_lock: _SendLock
    _load_output_policy: Callable[[], _OutputPolicy]
    _decorate_message: Callable[[str], str]
    _gate_outbound: _GateOutbound
    send_message: Callable[[str], bool]

    def _fit_chat_body(self, body: str, channel: str, decoration: str) -> str: ...
    def _sender_loop(self) -> None: ...
    def _begin_transport(self) -> int: ...
    def _send_immediate(self, text: str, generation: int, *, memory_receipt: ReplyReceipt | None = None) -> bool: ...
    def _record_written(self, written: WrittenReply) -> None: ...


class _ClientClass(Protocol):
    # Reading the function from the AST-built class returns an unbound sender.
    @property
    def send_message(self) -> Callable[[_ReceiptClient, str], bool]: ...


class _Profiles(Protocol):
    def is_excluded(self, channel: str, viewer: str) -> bool: ...
    def get_facts(self, channel: str, viewer: str) -> dict[str, ProfileFact]: ...


@runtime_checkable
class _MemoryHarness(Protocol):
    client: _ReceiptClient
    client_class: _ClientClass
    profile_path: Path
    namespace: dict[str, object]
    conversation: ConversationContextStore
    profiles: _Profiles
    generate_response: Mock

    def tearDown(self) -> None: ...
    def chat(self, username: str, message: str, *, user_id: str) -> None: ...


class _FilterConfig(TypedDict):
    blocked_words: list[str]
    blocked_topics: list[str]


class _PolicyModule(Protocol):
    NEUTRAL_FALLBACK: str


@runtime_checkable
class _OutputHarness(Protocol):
    client: _ReceiptClient
    config: _FilterConfig
    flags: dict[str, bool]
    policy_module: _PolicyModule

    def doCleanups(self) -> bool: ...


class _BrainModule(ModuleType):
    generate_twitch_redirect: Mock


def _memory_harness(value: object) -> _MemoryHarness:
    assert isinstance(value, _MemoryHarness), 'memory fixture interface changed'
    return value


def _output_harness(value: object) -> _OutputHarness:
    assert isinstance(value, _OutputHarness), 'output fixture interface changed'
    return value


def _receipt_client(value: object) -> _ReceiptClient:
    assert isinstance(value, _ReceiptClient), 'receipt client interface changed'
    return value


def _recording_socket(value: object) -> _RecordingSocket:
    assert isinstance(value, _RecordingSocket), 'recording socket interface changed'
    return value


def _chat_item(item: _QueueItem | None) -> _ChatItem:
    assert item is not None, 'expected queued reply, not shutdown sentinel'
    assert len(item) == 7, 'expected full chat queue item, not legacy control item'
    return item


def _receipt(item: _ChatItem) -> ReplyReceipt:
    receipt = item[5]
    assert receipt is not None, 'expected reply with a memory receipt'
    return receipt


# This suite deliberately tests receipt admission, transport locking and transient
# memory internals. The reviewed inline exemptions below cover reportPrivateUsage
# only; all other strict diagnostics remain enabled and the accesses stay explicit.
class WrittenReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        output=outgoing.TwitchOutgoingTests('runTest')
        output.setUp()
        self.output=_output_harness(output)
        self.addCleanup(self.output.doCleanups)
        harness=wiring.TwitchMemoryWiringTests('runTest')
        harness.setUp()
        self.h=_memory_harness(harness)
        self.addCleanup(self.h.tearDown)
        self.client=_receipt_client(self.h.client)
        self.path=self.h.profile_path.with_name('app_settings.yaml')
        self.path.write_text('remember_users: true\n',encoding='utf-8')
        self.h.namespace['get_memory_control']=lambda: read_memory_control(self.path)
        self.h.namespace['socket']=SimpleNamespace(SHUT_RDWR=2)
        self.sock=_recording_socket(outgoing.RecordingSocket())
        self.client.sock=self.sock
        self.client._load_output_policy=self.output.client._load_output_policy  # pyright: ignore[reportPrivateUsage]  # inject the checked output policy fixture
        self.client._decorate_message=lambda body: self.client._fit_chat_body(body,'#testchannel',' Kappa')  # pyright: ignore[reportPrivateUsage]  # exercise final wire formatting, not draft text
        self.client.send_message=partial(self.h.client_class.send_message,self.client)
        self.client._work_local.generation=1  # pyright: ignore[reportPrivateUsage]  # set the synthetic generation/redirect work context
        self.client._work_local.redirect_allowed=True  # pyright: ignore[reportPrivateUsage]  # set the synthetic generation/redirect work context
        self.brain=_BrainModule('MedlarTV.core.llm_brain')
        self.brain.generate_twitch_redirect=Mock(return_value='Try a calm tea break.')
        self.modules=patch.dict(sys.modules,{self.brain.__name__:self.brain})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def chat(self,message: str = 'Medlar, tell me a story',viewer: str = '100') -> None:
        return self.h.chat('Ada' if viewer=='100' else 'Bex',message,user_id=viewer)

    def toggle(self,value: bool) -> None:
        _,revision=editor.load_features(self.path)
        editor.save_features(self.path,{'remember_users':value},revision)

    def queued(self,maxsize: int = 100) -> None:
        self.client._outbound_queue=queue.Queue[_QueueItem | None](maxsize=maxsize)  # pyright: ignore[reportPrivateUsage]  # exercise queued admission and the idle sender fixture
        self.client._sender_thread=_AliveSender()  # pyright: ignore[reportPrivateUsage]  # provide synthetic sender readiness

    def outbound_queue(self) -> _OutboundQueue:
        outbound=self.client._outbound_queue  # pyright: ignore[reportPrivateUsage]  # exercise queued admission and the idle sender fixture
        assert outbound is not None, 'test requires queued output'
        return outbound

    def drain(self) -> None:
        self.outbound_queue().put_nowait(None)
        self.client._sender_loop()  # pyright: ignore[reportPrivateUsage]  # exercise the real final-write queue path

    def bodies(self) -> list[str]:
        return [frame.decode('utf-8').partition(' :')[2].removesuffix('\r\n') for frame in self.sock.sent]

    def history(self,viewer: str = '100') -> list[dict[str, str]]:
        return self.h.conversation.history_for('#testchannel',viewer)

    def test_queue_acceptance_creates_no_assistant_history_until_actual_write(self) -> None:
        self.queued()
        self.chat()
        self.assertEqual([],self.sock.sent)
        self.assertEqual([],self.history())
        self.assertEqual(1,len(self.h.conversation._viewers[('testchannel','100')].evidence))  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence
        self.drain()
        self.assertEqual('mock model reply Kappa',self.history()[-1]['content'])
        self.assertEqual(self.bodies()[-1],self.history()[-1]['content'])

    def test_formatted_limited_final_text_not_generated_candidate_is_remembered(self) -> None:
        self.h.generate_response.return_value='a ' * 600
        self.chat()
        self.assertEqual(self.bodies()[-1],self.history()[-1]['content'])
        self.assertLessEqual(len(self.sock.sent[-1]),512)
        self.assertNotEqual(self.h.generate_response.return_value,self.history()[-1]['content'])

    def test_complete_written_preserves_exact_final_whitespace(self) -> None:
        store=ConversationContextStore()
        observed=store.observe_user('room','100','Ada','Hello there')
        assert isinstance(observed, SourceObservation)
        self.assertTrue(store.complete_written(observed,'Answer  with\tspacing'))
        self.assertEqual('Answer  with\tspacing',store.history_for('room','100')[-1]['content'])

    def test_generated_redirect_is_only_remembered_after_its_final_write(self) -> None:
        self.output.flags['llm_brain']=True
        self.output.config['blocked_topics']=['violet bridge']
        self.h.generate_response.return_value='Original violet bridge candidate'
        self.queued()
        self.chat()
        self.assertEqual([],self.history())
        self.brain.generate_twitch_redirect.assert_called_once_with()
        self.drain()
        self.assertEqual('Try a calm tea break. Kappa',self.history()[-1]['content'])
        self.assertNotIn('violet',repr(self.history()))

    def test_checked_fallback_not_rejected_draft_enters_history(self) -> None:
        self.output.config['blocked_topics']=['violet bridge']
        self.h.generate_response.return_value='violet bridge'
        self.chat()
        self.assertEqual(self.output.policy_module.NEUTRAL_FALLBACK+' Kappa',self.history()[-1]['content'])

    def test_suppression_preserves_user_source_without_assistant_history(self) -> None:
        self.output.config['blocked_topics']=['violet bridge','tiny victory']
        self.h.generate_response.return_value='violet bridge'
        self.chat('My test word is cobalt')
        self.assertEqual([],self.sock.sent)
        self.assertEqual([],self.history())
        self.assertIn('cobalt',repr(self.h.conversation._viewers))  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_queue_full_does_not_commit_but_preserves_source(self) -> None:
        self.queued(maxsize=1)
        self.outbound_queue().put_nowait((1,'PONG :synthetic',None,False))
        self.chat('My test word is cobalt')
        self.assertEqual([],self.history())
        self.assertIn('cobalt',repr(self.h.conversation._viewers))  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence
        self.assertEqual([],self.sock.sent)

    def test_send_failure_keeps_user_source_and_does_not_commit_or_claim_delivery(self) -> None:
        self.sock.failure=OSError('synthetic PRIVATE payload')
        with self.assertLogs('test_twitch_memory_wiring',level='WARNING') as captured:
            self.chat('My test word is cobalt')
        self.assertEqual([],self.history())
        self.assertIn('cobalt',repr(self.h.conversation._viewers))  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence
        self.assertNotIn('PRIVATE','\n'.join(captured.output))
        self.assertFalse(self.client.connected)

    def test_legacy_completion_event_on_policy_drop_is_not_a_written_receipt(self) -> None:
        self.queued()
        self.chat()
        item=_chat_item(list(self.outbound_queue().queue)[0])
        completed=threading.Event()
        self.outbound_queue().queue[0]=(*item[:2],completed,*item[3:])
        self.output.config['blocked_words']=['mock']
        self.drain()
        self.assertTrue(completed.is_set())
        self.assertEqual([],self.sock.sent)
        self.assertEqual([],self.history())
        self.assertTrue(self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_repeated_identical_messages_complete_out_of_order_by_observation_id(self) -> None:
        self.queued()
        self.h.generate_response.side_effect=['First answer','Second answer']
        self.chat('Medlar, hello again')
        self.chat('Medlar, hello again')
        first=_chat_item(self.outbound_queue().get_nowait())
        second=_chat_item(self.outbound_queue().get_nowait())
        self.outbound_queue().task_done()
        self.outbound_queue().task_done()
        first_observation=_receipt(first).observation
        second_observation=_receipt(second).observation
        assert first_observation is not None
        assert second_observation is not None
        self.assertNotEqual(first_observation.sequence,second_observation.sequence)
        self.outbound_queue().put_nowait(second)
        self.outbound_queue().put_nowait(first)
        self.drain()
        self.assertEqual(['Second answer Kappa','First answer Kappa'],self.bodies())
        self.assertEqual(['First answer Kappa','Second answer Kappa'],[x['content'] for x in self.history() if x['role']=='assistant'])

    def test_forget_and_identical_reobserve_cannot_attach_old_reply_to_new_source(self) -> None:
        self.queued()
        self.h.generate_response.side_effect=['OLD ANSWER','NEW ANSWER']
        self.chat('Medlar, hello again')
        self.chat('!forgetme')
        self.chat('Medlar, hello again')
        self.drain()
        self.assertNotIn('OLD ANSWER',repr(self.bodies()))
        self.assertEqual(['NEW ANSWER Kappa'],[x['content'] for x in self.history() if x['role']=='assistant'])

    def test_identical_messages_completed_in_order_keep_each_source_association(self) -> None:
        self.queued()
        self.h.generate_response.side_effect=['First answer','Second answer']
        self.chat('Medlar, hello again')
        self.chat('Medlar, hello again')
        self.drain()
        self.assertEqual(['First answer Kappa','Second answer Kappa'],self.bodies())
        self.assertEqual(['First answer Kappa','Second answer Kappa'],[x['content'] for x in self.history() if x['role']=='assistant'])

    def test_source_eviction_drops_pending_reply_without_reconstructing_text(self) -> None:
        self.h.conversation.max_user_statements=1
        self.queued()
        self.h.generate_response.side_effect=['OLD ANSWER','NEW ANSWER']
        self.chat('Medlar, hello again')
        self.chat('Medlar, hello again')
        self.drain()
        self.assertEqual(['NEW ANSWER Kappa'],self.bodies())

    def test_viewer_capacity_eviction_invalidates_old_lease(self) -> None:
        self.h.conversation.max_viewers=1
        self.queued()
        self.chat(viewer='100')
        self.chat(viewer='200')
        self.drain()
        self.assertEqual(1,len(self.sock.sent))
        self.assertEqual([],self.history('100'))

    def test_expired_source_cannot_receive_late_reply(self) -> None:
        self.queued()
        self.chat()
        self.h.conversation._viewers[('testchannel','100')].evidence[0].when=datetime.now(timezone.utc)-timedelta(days=1)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence
        self.drain()
        self.assertEqual([],self.sock.sent)
        self.assertEqual([],self.history())

    def test_new_session_invalidates_old_receipts_even_with_identical_reobserve(self) -> None:
        self.queued()
        self.h.generate_response.side_effect=['OLD ANSWER','NEW ANSWER']
        self.chat('Medlar, hello again')
        self.h.conversation.clear_all()
        self.chat('Medlar, hello again')
        self.drain()
        self.assertEqual(['NEW ANSWER Kappa'],self.bodies())

    def test_reconnect_invalidates_queue_but_preserves_valid_user_evidence(self) -> None:
        self.queued()
        self.chat('My test word is cobalt')
        generation=self.client._begin_transport()  # pyright: ignore[reportPrivateUsage]  # invalidate transport generation without connecting
        self.sock=_recording_socket(outgoing.RecordingSocket())
        self.client.sock=self.sock
        self.client.connected=True
        self.client._work_local.generation=generation  # pyright: ignore[reportPrivateUsage]  # set the synthetic generation/redirect work context
        self.assertIn('cobalt',repr(self.h.conversation._viewers))  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence
        self.drain()
        self.assertEqual([],self.sock.sent)
        self.assertEqual([],self.history())

    def test_off_during_generation_drops_reply_and_clears_transient_sources(self) -> None:
        def generated(*_args: object, **_kwargs: object) -> str:
            self.toggle(False)
            return 'OLD memory answer'
        self.h.generate_response.side_effect=generated
        self.chat()
        self.assertEqual([],self.sock.sent)
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_off_then_on_during_generation_still_invalidates_earlier_reply(self) -> None:
        def generated(*_args: object, **_kwargs: object) -> str:
            self.toggle(False)
            self.toggle(True)
            return 'OLD memory answer'
        self.h.generate_response.side_effect=generated
        self.chat()
        self.h.generate_response.assert_called_once()
        self.assertTrue(read_memory_control(self.path).enabled)
        self.assertEqual([],self.sock.sent)
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_off_after_queue_acceptance_drops_at_last_write_gate(self) -> None:
        self.queued()
        self.chat()
        self.toggle(False)
        self.drain()
        self.assertEqual([],self.sock.sent)
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_idle_sender_observes_off_without_needing_another_chat_message(self) -> None:
        self.chat()
        self.assertTrue(self.history())
        self.toggle(False)
        fake=Mock()
        fake.get.side_effect=[queue.Empty,None]
        self.client._outbound_queue=fake  # pyright: ignore[reportPrivateUsage]  # exercise queued admission and the idle sender fixture
        self.client._sender_loop()  # pyright: ignore[reportPrivateUsage]  # exercise the real final-write queue path
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_off_during_final_policy_check_is_rechecked_before_socket_write(self) -> None:
        self.queued()
        self.chat()
        self.assertEqual(1,self.outbound_queue().qsize())
        original=self.client._gate_outbound  # pyright: ignore[reportPrivateUsage]  # intercept the final policy check for boundary races
        def gate(text: str, generation: int, *, trusted_command: bool = False) -> str | None:
            result=original(text,generation,trusted_command=trusted_command)
            self.toggle(False)
            return result
        self.client._gate_outbound=gate  # pyright: ignore[reportPrivateUsage]  # intercept the final policy check for boundary races
        self.drain()
        self.assertEqual([],self.sock.sent)
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_off_while_waiting_for_transport_lock_prevents_later_write(self) -> None:
        self.queued()
        self.chat()
        item=_chat_item(self.outbound_queue().get_nowait())
        self.outbound_queue().task_done()
        reached=threading.Event()
        original=self.client._gate_outbound  # pyright: ignore[reportPrivateUsage]  # intercept the final policy check for boundary races
        def gate(text: str, generation: int, *, trusted_command: bool = False) -> str | None:
            result=original(text,generation,trusted_command=trusted_command)
            reached.set()
            return result
        self.client._gate_outbound=gate  # pyright: ignore[reportPrivateUsage]  # intercept the final policy check for boundary races
        results: list[bool | str]=[]
        def write() -> None:
            try:
                results.append(self.client._send_immediate(item[1],item[0],memory_receipt=item[5]))  # pyright: ignore[reportPrivateUsage]  # test the real last-write receipt admission gate
            except Exception as exc:
                results.append(type(exc).__name__)
        self.client._send_lock.acquire()  # pyright: ignore[reportPrivateUsage]  # coordinate the transport-lock race deterministically
        worker=threading.Thread(target=write,daemon=True)
        try:
            worker.start()
            self.assertTrue(reached.wait(2),'writer did not reach policy gate')
            self.toggle(False)
        finally:
            self.client._send_lock.release()  # pyright: ignore[reportPrivateUsage]  # coordinate the transport-lock race deterministically
            worker.join(2)
        self.assertFalse(worker.is_alive(),'writer did not finish')
        self.assertEqual([False],results)
        self.assertEqual([],self.sock.sent)
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_exclusion_after_queue_acceptance_drops_health_recall(self) -> None:
        self.chat('I have HIV')
        self.sock.sent.clear()
        self.queued()
        self.chat('Medlar, what are my health conditions?')
        self.chat('I am 17')
        self.drain()
        self.assertNotIn('HIV',repr(self.bodies()))
        self.assertTrue(self.h.profiles.is_excluded('room-a','100'))
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_forget_after_queue_acceptance_drops_direct_profile_recall(self) -> None:
        self.chat('My favorite color is blue')
        self.sock.sent.clear()
        self.queued()
        self.chat('Medlar, what is my favorite color?')
        self.chat('!forget favorite color')
        self.drain()
        self.assertNotIn('blue',repr(self.bodies()))

    def test_sensitive_written_answer_never_enters_general_history(self) -> None:
        self.chat('I have HIV')
        self.chat('Medlar, what are my health conditions?')
        self.assertIn('HIV',self.bodies()[-1])
        self.assertNotIn('HIV',repr(self.history()))
        self.assertNotIn('HIV',repr(self.h.conversation._public))  # pyright: ignore[reportPrivateUsage]  # verify sensitive replies never enter public history

    def test_duplicate_queued_receipt_writes_at_most_once(self) -> None:
        self.queued()
        self.chat()
        item=_chat_item(list(self.outbound_queue().queue)[0])
        self.outbound_queue().put_nowait(item)
        self.drain()
        self.assertEqual(1,len(self.sock.sent))
        self.assertEqual(2,len(self.history()))

    def test_wrong_owner_or_transport_receipt_cannot_write_or_commit(self) -> None:
        self.queued()
        self.chat()
        item=_chat_item(self.outbound_queue().get_nowait())
        self.outbound_queue().task_done()
        for receipt in (replace(_receipt(item),owner=object()),replace(_receipt(item),generation=100)):
            self.assertFalse(self.client._send_immediate(item[1],1,memory_receipt=receipt))  # pyright: ignore[reportPrivateUsage]  # test the real last-write receipt admission gate
            self.client._record_written(WrittenReply(receipt,1,'forged'))  # pyright: ignore[reportPrivateUsage]  # verify forged receipts cannot commit history
        self.assertEqual([],self.sock.sent)
        self.assertEqual([],self.history())

    def test_control_or_timer_messages_remain_available_while_memory_is_off(self) -> None:
        self.toggle(False)
        self.assertTrue(self.client.send_message('Scheduled ordinary greeting'))
        self.assertEqual(['Scheduled ordinary greeting Kappa'],self.bodies())
        self.assertTrue(self.client._send_immediate('PONG :synthetic',1))  # pyright: ignore[reportPrivateUsage]  # test the real last-write receipt admission gate

    def test_unrelated_feature_edit_preserves_current_pending_reply(self) -> None:
        self.queued()
        self.chat()
        _,revision=editor.load_features(self.path)
        editor.save_features(self.path,{'timers':False},revision)
        self.drain()
        self.assertEqual(['mock model reply Kappa'],self.bodies())

    def test_receipt_failure_after_write_does_not_disconnect_or_log_payload(self) -> None:
        with patch.object(self.h.conversation,'complete_written',side_effect=RuntimeError('PRIVATE')):
            with self.assertLogs('test_twitch_memory_wiring',level='WARNING') as captured:
                self.chat()
        self.assertEqual(1,len(self.sock.sent))
        self.assertTrue(self.client.connected)
        self.assertEqual([],self.history())
        self.assertNotIn('PRIVATE','\n'.join(captured.output))

    def test_invalid_control_at_last_write_fails_closed_and_preserves_durable_fact(self) -> None:
        self.chat('My favorite color is blue')
        self.sock.sent.clear()
        self.queued()
        self.chat()
        self.path.write_text('remember_users: "true"\n',encoding='utf-8')
        self.drain()
        self.assertEqual([],self.sock.sent)
        self.assertEqual('blue',self.h.profiles.get_facts('room-a','100')['favorite_color'].value)
        self.assertEqual({},self.h.conversation._viewers)  # pyright: ignore[reportPrivateUsage]  # inspect or expire transient source evidence

    def test_channel_and_viewer_observation_isolation(self) -> None:
        store=ConversationContextStore()
        a=store.observe_user('room-a','100','Ada','Hello there')
        b=store.observe_user('room-b','100','Ada','Hello there')
        other=store.observe_user('room-a','200','Bex','Hello there')
        assert isinstance(a, SourceObservation)
        assert isinstance(b, SourceObservation)
        assert isinstance(other, SourceObservation)
        self.assertFalse(store.receipt_is_current(a.lease,b))
        self.assertFalse(store.receipt_is_current(a.lease,other))
        self.assertTrue(store.complete_written(b,'Room B answer'))
        self.assertEqual([],store.history_for('room-a','100'))
        self.assertEqual('Room B answer',store.history_for('room-b','100')[-1]['content'])
