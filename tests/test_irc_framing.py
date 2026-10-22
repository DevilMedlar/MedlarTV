"""Synthetic byte streams through the pure framer and actual listener methods."""
import threading
import unittest
from unittest.mock import Mock

from MedlarTV.core.irc_framing import IRCLineBuffer
import test_twitch_listener_reliability as reliability

WELCOME=b':tmi.twitch.tv 001 TestBot :Welcome\r\n'
JOIN=b':TestBot!testbot@local JOIN #testchannel\r\n'
PREFIX='@user-id=100;room-id=200;id=fixture :Ada!ada@local PRIVMSG #testchannel :'
SAMPLES=('café','中文 日本語','한글','😀','🇵🇭','👩🏽‍💻','e\u0301')


class IRCFramingTests(unittest.TestCase):
    def client(self,actions=()):
        return reliability.ListenerHarness().client_with_socket(reliability.ScriptedSocket(actions))

    def test_every_utf8_byte_split_preserves_complete_frames(self):
        for sample in SAMPLES:
            text=PREFIX+sample
            data=(text+'\r\n').encode('utf-8')
            for split in range(len(data)+1):
                with self.subTest(sample=sample,split=split):
                    f=IRCLineBuffer()
                    first,invalid,oversized=f.feed(data[:split])
                    second,bad,large=f.feed(data[split:])
                    self.assertEqual([text],first+second)
                    self.assertEqual((0,0,0,0),(invalid,oversized,bad,large))
                    self.assertEqual(b'',f.pending)

    def test_byte_at_a_time_and_multiple_frames_keep_crlf_boundaries(self):
        expected=[PREFIX+'café',PREFIX+'👩🏽‍💻','PING :synthetic']
        data=('\r\n'.join(expected)+'\r\n').encode('utf-8')
        f=IRCLineBuffer()
        actual=[]
        for value in data:
            lines,bad,large=f.feed(bytes([value]))
            self.assertEqual((0,0),(bad,large))
            actual.extend(lines)
        self.assertEqual(expected,actual)
        self.assertEqual(expected,IRCLineBuffer().feed(data)[0])

    def test_incomplete_line_or_utf8_is_never_dispatched(self):
        f=IRCLineBuffer()
        self.assertEqual(([],0,0),f.feed(b'prefix \xf0\x9f'))
        self.assertEqual(([],0,0),f.feed(b''))
        self.assertEqual(b'prefix \xf0\x9f',f.pending)

    def test_invalid_utf8_discards_whole_frame_and_resynchronizes(self):
        f=IRCLineBuffer()
        self.assertEqual((['PING :ok'],1,0),f.feed(b'PRIVATE\xfftail\r\nPING :ok\r\n'))
        self.assertEqual(([],1,0),f.feed(b'overlong \xc0\xaf\r\n'))

    def test_oversize_fragment_discards_until_split_crlf_then_recovers(self):
        f=IRCLineBuffer(8)
        self.assertEqual(([],0,1),f.feed(b'x'*100+b'\r'))
        self.assertLessEqual(len(f.pending),1)
        self.assertTrue(f.discarding)
        self.assertEqual((['ok'],0,0),f.feed(b'\nok\r\n'))
        self.assertFalse(f.discarding)

    def test_exact_size_line_with_split_delimiter_is_valid(self):
        f=IRCLineBuffer(8)
        self.assertEqual(([],0,0),f.feed(b'12345678\r'))
        self.assertEqual((['12345678'],0,0),f.feed(b'\n'))
        self.assertEqual((['ok'],0,1),f.feed(b'123456789\r\nok\r\n'))

    def test_continuous_unterminated_input_has_bounded_retained_state(self):
        f=IRCLineBuffer(32)
        dropped=0
        for _ in range(100):
            lines,bad,large=f.feed(b'x'*31)
            self.assertEqual([],lines)
            self.assertFalse(bad)
            dropped+=large
            self.assertLessEqual(len(f.pending),33)
        self.assertEqual(1,dropped)
        self.assertEqual((['next'],0,0),f.feed(b'\r\nnext\r\n'))

    def test_established_listener_dispatches_identical_text_at_every_split(self):
        for sample in SAMPLES:
            text=PREFIX+sample
            data=(text+'\r\n').encode('utf-8')
            for split in range(len(data)+1):
                h=self.client([part for part in (data[:split],data[split:]) if part]+[b''])
                reliability.ListenerHarness.activate(h.client,h.tls)
                h.client._handle_raw_message=Mock()
                with self.assertRaises(EOFError):
                    h.client._receive_loop(1)
                h.client._handle_raw_message.assert_called_once_with(text,1)
                tags,user,message,channel=h.client._parse_message(text)
                self.assertEqual(('Ada',sample,'#testchannel'),(user,message,channel))
                self.assertEqual('100',tags['user-id'])

    def test_handshake_handoff_preserves_every_partial_payload_split(self):
        text=PREFIX+'café 中文 한글 👩🏽‍💻 🇵🇭'
        data=(text+'\r\n').encode('utf-8')
        for split in range(len(data)+1):
            h=self.client([WELCOME+JOIN+data[:split]]+([data[split:]] if data[split:] else [])+[b''])
            generation=h.client.connect()
            h.client._handle_raw_message=Mock()
            with self.assertRaises(EOFError):
                h.client._receive_loop(generation)
            h.client._handle_raw_message.assert_called_once_with(text,generation)

    def test_reconnect_discards_old_partial_bytes_and_keeps_new_frame_clean(self):
        h=self.client()
        reliability.ListenerHarness.activate(h.client,h.tls)
        h.client._incoming_framer=IRCLineBuffer()
        h.client._incoming_framer.feed(b'OLD PRIVATE \xf0\x9f')
        generation=h.client._begin_transport()
        self.assertIsNone(h.client._incoming_framer)
        transport=reliability.ScriptedSocket([b'PING :new\r\n',b''])
        reliability.ListenerHarness.activate(h.client,transport,generation)
        h.client._handle_raw_message=Mock()
        with self.assertRaises(EOFError):
            h.client._receive_loop(generation)
        h.client._handle_raw_message.assert_called_once_with('PING :new',generation)
        h.client._disconnect_generation(generation,'synthetic')
        self.assertIsNone(h.client._incoming_framer)

    def test_stale_reader_cannot_take_new_generations_pending_input(self):
        h=self.client()
        reliability.ListenerHarness.activate(h.client,h.tls,2)
        h.client._pending_lines=['new pending']
        with self.assertRaises(ConnectionError):
            h.client._receive_loop(1)
        self.assertEqual(['new pending'],h.client._pending_lines)

    def test_handshake_pending_messages_are_bounded_without_blocking_ping(self):
        messages=b''.join((PREFIX+str(i)+'\r\n').encode() for i in range(70))
        h=self.client([messages+b'PING :synthetic\r\n'+WELCOME+JOIN,b''])
        with self.assertLogs('test_twitch_listener_reliability',level='WARNING') as captured:
            generation=h.client.connect()
        self.assertEqual(h.client.WORK_QUEUE_SIZE,len(h.client._pending_lines))
        self.assertIn(b'PONG :synthetic\r\n',h.tls.sent)
        self.assertNotIn('Ada','\n'.join(captured.output))
        h.client._handle_raw_message=Mock()
        with self.assertRaises(EOFError):
            h.client._receive_loop(generation)
        self.assertEqual(64,h.client._handle_raw_message.call_count)

    def test_invalid_input_diagnostics_contain_counts_not_frame_payloads(self):
        h=self.client([b'PRIVATE\xff\r\n'+b'x'*80+b'\r\nPING :ok\r\n',b''])
        h.client.MAX_INCOMING_LINE_BYTES=64
        reliability.ListenerHarness.activate(h.client,h.tls)
        h.client._handle_raw_message=Mock()
        with self.assertLogs('test_twitch_listener_reliability',level='WARNING') as captured:
            with self.assertRaises(EOFError):
                h.client._receive_loop(1)
        h.client._handle_raw_message.assert_called_once_with('PING :ok',1)
        output='\n'.join(captured.output)
        self.assertIn('invalid_utf8=1 oversized=1',output)
        self.assertNotIn('PRIVATE',output)
        self.assertNotIn('x'*10,output)

    def test_decoded_ping_remains_responsive_while_worker_is_blocked(self):
        started,release=threading.Event(),threading.Event()
        def slow(_line):
            started.set()
            release.wait(2)
        def ping(_socket):
            self.assertTrue(started.wait(2),'synthetic worker did not start')
            return b'PING :synthetic\r\n'
        h=self.client([(PREFIX+'slow').encode()+b'\r\n',ping,b''])
        reliability.ListenerHarness.activate(h.client,h.tls)
        h.client._process_raw_message=slow
        h.client._ensure_background_workers()
        try:
            with self.assertRaises(EOFError):
                h.client._receive_loop(1)
            self.assertTrue(h.tls.wait_for_sent(1))
            self.assertIn(b'PONG :synthetic\r\n',h.tls.sent)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            h.client.stop()
