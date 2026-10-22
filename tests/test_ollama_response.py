"""Guarded response-double tests; no application imports or HTTP adapter."""
from __future__ import annotations
import io
import logging
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import test_twitch_outgoing
load_definitions = test_twitch_outgoing.load_definitions

MARKER = 'SYNTHETIC_BACKEND_SECRET'
UNAVAILABLE = 'I tried to respond, but my brain backend is cranky right now.'


class OllamaResponseTests(unittest.TestCase):
    def setUp(self):
        self.post = Mock()
        self.stream = io.StringIO()
        self.logger = logging.getLogger('test_ollama_response')
        handler = logging.StreamHandler(self.stream)
        self.logger.addHandler(handler)
        self.addCleanup(self.logger.removeHandler, handler)
        self.ns = load_definitions('MedlarTV/core/llm_brain.py', {
            'DEBUG': False, 'log': self.logger,
            'OLLAMA_HOST': 'http://127.0.0.1:11434', 'OLLAMA_MODEL': 'fixture-model',
            'requests': SimpleNamespace(post=self.post),
        }, names={'_OllamaResponseUnavailable', '_parse_ollama_chat_response', '_call_ollama_chat'})

    def response(self, body=None, status=200, error=None, text=''):
        return SimpleNamespace(status_code=status, text=text,
                               json=Mock(return_value=body, side_effect=error))

    def call(self, *responses):
        self.post.reset_mock()
        self.post.side_effect = list(responses)
        result = self.ns['_call_ollama_chat']([{'role': 'user', 'content': 'fixture'}])
        self.assertNotIn(MARKER, result)
        self.assertNotIn(MARKER, self.stream.getvalue())
        return result

    def test_valid_primary_formats_preserve_stripping(self):
        for body in ({'message': {'content': '  hello 🌳  '}},
                     {'choices': [{'message': {'content': '  hello 🌳  '}}]}):
            with self.subTest(body=body):
                self.assertEqual(self.call(self.response(body)), 'hello 🌳')
                self.assertEqual(self.post.call_args.kwargs['timeout'], 120)
                self.assertEqual(self.post.call_args.kwargs['json']['model'], 'fixture-model')

    def test_invalid_json_is_typed_and_contained(self):
        response = self.response(error=ValueError(MARKER))
        with self.assertRaises(self.ns['_OllamaResponseUnavailable']):
            self.ns['_parse_ollama_chat_response'](response)
        self.assertEqual(self.call(response), UNAVAILABLE)

    def test_invalid_roots_and_unknown_body_never_echo(self):
        for body in (None, [], 7, MARKER, {'unexpected': MARKER}):
            with self.subTest(shape=type(body).__name__):
                self.assertEqual(self.call(self.response(body)), UNAVAILABLE)

    def test_invalid_message_shapes(self):
        for value in (None, [], 7, MARKER):
            with self.subTest(shape=type(value).__name__):
                self.assertEqual(self.call(self.response({'message': value})), UNAVAILABLE)

    def test_invalid_content_shapes(self):
        for value in (None, 7, [], {}, '', ' \t\n'):
            with self.subTest(shape=type(value).__name__):
                self.assertEqual(self.call(self.response({'message': {'content': value}})), UNAVAILABLE)

    def test_invalid_choices_shapes(self):
        for choices in (None, {}, [], [None], [7], [{}], [{'message': None}],
                        [{'message': {'content': [MARKER]}}]):
            with self.subTest(shape=type(choices).__name__):
                self.assertEqual(self.call(self.response({'choices': choices})), UNAVAILABLE)

    def test_message_precedence_preserved(self):
        body = {'message': {'content': ' main '}, 'choices': [{'message': {'content': 'other'}}]}
        self.assertEqual(self.call(self.response(body)), 'main')
        body['message'] = None
        self.assertEqual(self.call(self.response(body)), UNAVAILABLE)

    def test_valid_fallback_formats_keep_model_and_attempt_timeouts(self):
        for body in ({'message': {'content': ' fallback '}},
                     {'choices': [{'message': {'content': ' fallback '}}]}):
            with self.subTest(body=body):
                self.assertEqual(self.call(self.response(status=404, text='model not found'),
                                           self.response(body)), 'fallback')
                self.assertEqual([c.kwargs['json']['model'] for c in self.post.call_args_list],
                                 ['fixture-model', 'llama3'])
                self.assertEqual([c.kwargs['timeout'] for c in self.post.call_args_list], [120, 120])

    def test_malformed_fallback_is_contained(self):
        for response in (self.response(error=ValueError(MARKER)),
                         self.response({'unexpected': MARKER}), self.response({'message': None}),
                         self.response({'message': {'content': []}})):
            self.assertEqual(self.call(self.response(status=404, text='not found'), response), UNAVAILABLE)

    def test_http_errors_preserve_no_retry_behavior(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.assertEqual(self.call(self.response(status=status, text=MARKER)), UNAVAILABLE)
                self.assertEqual(self.post.call_count, 1)

    def test_default_model_does_not_retry_missing_model(self):
        self.ns['OLLAMA_MODEL'] = 'llama3'
        self.assertEqual(self.call(self.response(status=404, text='not found')), UNAVAILABLE)
        self.assertEqual(self.post.call_count, 1)

    def test_timeout_and_fallback_http_failure_are_safe(self):
        result = self.call(TimeoutError(MARKER))
        self.assertIn('brain server might be down', result)
        self.assertEqual(self.call(self.response(status=404, text='not found'),
                                   self.response(status=503, text=MARKER)), UNAVAILABLE)

    def test_same_parser_used_for_primary_and_fallback(self):
        parser = Mock(wraps=self.ns['_parse_ollama_chat_response'])
        self.ns['_parse_ollama_chat_response'] = parser
        self.call(self.response({'message': {'content': 'ok'}}))
        self.call(self.response(status=404, text='not found'),
                  self.response({'message': {'content': 'ok'}}))
        self.assertEqual(parser.call_count, 2)

    def test_debug_does_not_echo_backend_marker(self):
        self.ns['DEBUG'] = True
        output = io.StringIO()
        with redirect_stdout(output):
            self.call(self.response({'unexpected': MARKER}))
            self.call(self.response(status=404, text='not found'), self.response(error=ValueError(MARKER)))
        self.assertNotIn(MARKER, output.getvalue())

    def test_unavailable_reply_obeys_existing_output_policy(self):
        harness = test_twitch_outgoing.TwitchOutgoingTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        reply = self.call(self.response({'unexpected': MARKER}))
        client = harness.new_client()
        self.assertTrue(client.send_message(reply))
        sent = b''.join(client.sock.sent)
        self.assertIn(b'brain backend is cranky', sent)
        self.assertNotIn(MARKER.encode(), sent)
        harness.config['blocked_words'] = ['cranky']
        before = list(client.sock.sent)
        self.assertTrue(client.send_message(reply))
        self.assertEqual(len(client.sock.sent), len(before) + 1)
        self.assertNotIn(b'cranky', client.sock.sent[-1])
        self.assertNotIn(MARKER.encode(), client.sock.sent[-1])
