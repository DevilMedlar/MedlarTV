"""Approved chat pacing via virtual clock; no HTTP, sockets or real sleeps."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import test_twitch_outgoing
import test_written_receipts


class ChatPacingTests(unittest.TestCase):
    def setUp(self):
        self.h = test_twitch_outgoing.TwitchOutgoingTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.client = self.h.client
        self.clock = self.client._pacing_fixture
        self.writes = []
        original = self.client.sock.sendall
        def sent(payload):
            self.writes.append((self.clock.now, payload))
            original(payload)
        self.client.sock.sendall = sent
        self.client._outbound_queue = queue.Queue(maxsize=128)
        self.client._sender_thread = SimpleNamespace(is_alive=lambda: True)

    def drain(self):
        self.client._outbound_queue.put(None)
        self.client._sender_loop()
        self.assertEqual(self.client._outbound_queue.unfinished_tasks, 0)

    def bodies(self):
        return [(at, payload.decode().partition(' :')[2].removesuffix('\r\n'))
                for at,payload in self.writes if payload.startswith(b'PRIVMSG ')]

    def test_burst_fifo_and_thirty_second_expiry(self):
        for text in ('command confirmation', 'event reply', 'timer greeting', 'model reply', 'expired confirmation'):
            self.assertTrue(self.client.send_message(text))
        self.drain()
        bodies = self.bodies()
        self.assertEqual([at for at,_ in bodies], [0, 8, 16, 24])
        self.assertEqual([body for _,body in bodies],
                         [text+' Kappa' for text in ('command confirmation','event reply','timer greeting','model reply')])
        self.assertLessEqual(self.clock.now,30)

    def test_all_rooms_and_trusted_operational_chat_share_gap(self):
        for text,trusted in [('PRIVMSG #one :one',False),
                             ('PRIVMSG #two :two',False),
                             ('PRIVMSG #devilmedlar :!pokecatch ultraball',True)]:
            self.assertTrue(self.client._queue_outbound(text,1,trusted_command=trusted))
        self.drain()
        self.assertEqual([at for at,_ in self.bodies()],[0,8,16])

    def test_pong_is_immediate_during_wait_and_takes_no_chat_slot(self):
        self.client.send_message('one')
        self.client.send_message('two')
        def pong():
            self.assertFalse(self.client._send_lock.locked())
            self.assertFalse(self.client._state_lock._is_owned())
            self.assertFalse(self.client._memory_lock._is_owned())
            self.client._send_immediate('PONG :synthetic',1)
        self.clock.on_wait=pong
        self.drain()
        self.assertEqual([at for at,_ in self.bodies()],[0,8])
        pong_at=next(at for at,payload in self.writes if payload.startswith(b'PONG'))
        self.assertLess(pong_at,8)

    def test_policy_change_while_paced_prevents_write(self):
        self.client.send_message('one')
        self.client.send_message('violet bridge')
        self.clock.on_wait=lambda: self.h.config.update(blocked_topics=['violet bridge'])
        self.drain()
        self.assertEqual(len(self.bodies()),1)
        self.assertTrue(self.client.connected)

    def test_stop_interrupts_wait_and_completes_dropped_items(self):
        done=threading.Event()
        self.client.send_message('one')
        self.client._queue_outbound('PRIVMSG #devilmedlar :two',1,completion=done)
        self.clock.on_wait=self.client._stop_event.set
        self.drain()
        self.assertEqual(len(self.bodies()),1)
        self.assertLess(self.clock.now,1)
        self.assertTrue(done.is_set())

    def test_reconnect_invalidates_wait_and_keeps_account_gap(self):
        self.client.send_message('one')
        self.client.send_message('old reply')
        self.clock.on_wait=lambda: setattr(self.client,'_generation',2)
        self.drain()
        self.assertEqual(len(self.bodies()),1)
        self.client.send_message('new reply')
        self.drain()
        self.assertEqual([at for at,_ in self.bodies()],[0,8])
        self.assertNotIn('old reply',self.bodies()[-1][1])

    def test_expired_item_does_not_delay_newer_fifo_item(self):
        self.client.send_message('expired timer')
        self.clock.now=31
        self.client.send_message('fresh greeting')
        self.drain()
        self.assertEqual(self.bodies(),[(31,'fresh greeting Kappa')])

    def test_exact_age_boundary_can_write_but_over_age_cannot(self):
        self.client.send_message('boundary')
        self.clock.now=30
        self.drain()
        self.assertEqual(self.bodies(),[(30,'boundary Kappa')])
        self.client.send_message('expired')
        self.clock.now=60.001
        self.drain()
        self.assertEqual(len(self.bodies()),1)

    def test_queue_overflow_remains_bounded(self):
        self.client._outbound_queue=queue.Queue(maxsize=1)
        self.assertTrue(self.client.send_message('one'))
        self.assertFalse(self.client.send_message('two'))
        self.assertEqual(self.client._outbound_queue.qsize(),1)

    def test_rate_limit_notice_does_not_replay(self):
        self.client.send_message('one')
        self.drain()
        self.client._log_chat_notice('@msg-id=msg_ratelimit :server NOTICE #devilmedlar :synthetic')
        self.assertEqual(len(self.bodies()),1)
        self.assertTrue(self.client._outbound_queue.empty())

    def test_shutdown_operational_chat_never_bypasses_gap(self):
        self.client.send_message('one')
        self.drain()
        self.client._stopping=True
        self.client._queue_outbound('PRIVMSG #devilmedlar :offline',1,allow_stopping=True)
        self.drain()
        self.assertEqual(len(self.bodies()),1)
        self.assertEqual(self.clock.waits,[])

    def test_blocked_write_does_not_consume_slot(self):
        self.h.config['blocked_topics']=['blocked']
        self.assertFalse(self.client._send_immediate('PRIVMSG #devilmedlar :blocked',1))
        self.assertTrue(self.client._send_immediate('PRIVMSG #devilmedlar :valid',1))
        self.assertEqual([at for at,_ in self.bodies()],[0])

    def test_final_age_check_prevents_write_after_slow_policy_reload(self):
        original=self.client._load_output_policy
        def policy():
            self.clock.now=31
            return original()
        self.client._load_output_policy=policy
        self.assertFalse(self.client._send_immediate('PRIVMSG #devilmedlar :expired',1,queued_at=0))
        self.assertEqual(self.writes,[])
        self.assertIsNone(self.client._last_chat_write_at)

    def test_failed_socket_attempt_keeps_gap_without_replay(self):
        self.client.sock.failure=OSError('synthetic socket failure')
        with self.assertRaises(OSError):
            self.client._send_immediate('PRIVMSG #devilmedlar :one',1)
        self.client.sock.failure=None
        self.client._generation=2
        self.assertTrue(self.client._send_immediate('PRIVMSG #devilmedlar :two',2))
        self.assertEqual([at for at,_ in self.writes],[0,8])
        self.assertEqual(len(self.client.sock.sent),1)

    def test_wall_clock_jump_does_not_change_chat_schedule(self):
        self.client.send_message('one')
        self.client.send_message('two')
        with patch.object(self.h.ns['time'],'time',return_value=-99999999):
            self.drain()
        self.assertEqual([at for at,_ in self.bodies()],[0,8])

    def test_sender_failure_does_not_wait_or_bypass_queue(self):
        self.client._workers_started=True
        self.client._sender_thread=SimpleNamespace(is_alive=lambda:False)
        self.assertFalse(self.client.send_message('one'))
        self.assertEqual(self.writes,[])
        self.assertEqual(self.clock.waits,[])

    def test_ready_timer_expires_in_work_queue_before_model_work(self):
        self.client._work_queue=queue.Queue()
        self.client._enqueue_application_message('stale timer',1)
        self.clock.now=31
        self.client._work_queue.put(None)
        self.client._worker_loop()
        self.assertTrue(self.client._outbound_queue.empty())
        self.assertFalse(hasattr(self.client._work_local,'outbound_created_at'))

    def test_ready_timer_age_survives_worker_to_sender_handoff(self):
        self.client._work_queue=queue.Queue()
        self.client._enqueue_application_message('timer',1)
        self.clock.now=29
        self.client._work_queue.put(None)
        self.client._worker_loop()
        self.clock.now=31
        self.drain()
        self.assertEqual(self.writes,[])

    def test_invalidated_memory_reply_is_not_written_after_wait(self):
        h=test_written_receipts.WrittenReceiptTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        h.queued()
        h.chat('Medlar, one story')
        h.chat('Medlar, another story')
        clock=h.client._pacing_fixture
        clock.on_wait=lambda: h.toggle(False)
        h.drain()
        self.assertEqual(len(h.sock.sent),1)
        self.assertEqual(h.history(),[])

    def test_expired_memory_reply_creates_no_assistant_history(self):
        h=test_written_receipts.WrittenReceiptTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        h.queued()
        h.chat()
        h.client._pacing_fixture.now=31
        h.drain()
        self.assertEqual(h.sock.sent,[])
        self.assertFalse(any(message['role']=='assistant' for message in h.history()))
