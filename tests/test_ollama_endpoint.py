"""Offline unittest checks for the shared Ollama endpoint resolver and probe."""

import importlib.util
import io
import logging
import traceback
from types import SimpleNamespace
from http.client import HTTPException
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


MODULE_PATH = Path(__file__).resolve().parents[1] / "MedlarTV" / "core" / "ollama_endpoint.py"
spec = importlib.util.spec_from_file_location("ollama_endpoint_under_test", MODULE_PATH)
ollama_endpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ollama_endpoint)


class _FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        self.read_limits.append(limit)
        return self.body


class OllamaEndpointTests(unittest.TestCase):
    def assert_safe_configuration_error(self, call, values, marker):
        with patch.object(ollama_endpoint.urllib_request, 'urlopen',
                          side_effect=AssertionError('configuration must fail before HTTP')) as http:
            try:
                call(values)
            except ValueError as error:
                rendered = ''.join(traceback.format_exception(error))
                output = io.StringIO()
                logger = logging.getLogger('test_ollama_endpoint.synthetic_error')
                handler = logging.StreamHandler(output)
                logger.addHandler(handler)
                try:
                    logger.error('Endpoint unavailable: %s', error, exc_info=True)
                finally:
                    logger.removeHandler(handler)
                for exposed in (str(error), repr(error), rendered, output.getvalue()):
                    self.assertIn('Ollama', exposed)
                    self.assertNotIn(marker, exposed)
                    self.assertNotIn(next(iter(values.values())), exposed)
            else:
                self.fail('Invalid configuration was accepted')
            http.assert_not_called()

    def test_synthetic_credential_urls_are_never_formatted_into_errors(self):
        marker = 'SYNTHETIC_ENDPOINT_SECRET'
        endpoints = (
            f'http://user:{marker}@localhost:{marker}',
            f'http://user:{marker}@localhost:99999',
            f'http://user:{marker}@[::1',
            f'http://user:{marker}@localhost:11434',
            f'http://localhost:11434?token={marker}',
            f'http://localhost:11434#{marker}',
            f'http://bad^{marker}:11434',
            f'http://bad {marker}:11434',
        )
        for endpoint in endpoints:
            for key in ('OLLAMA_URL', 'OLLAMA_HOST'):
                for call in (ollama_endpoint.ollama_base_url,
                             ollama_endpoint.ollama_probe_target,
                             ollama_endpoint.ollama_api_ready):
                    with self.subTest(case=endpoints.index(endpoint), key=key, call=call.__name__):
                        self.assert_safe_configuration_error(call, {key: endpoint}, marker)

    def test_parser_exception_context_is_suppressed(self):
        marker = 'SYNTHETIC_ENDPOINT_SECRET'
        with patch.object(ollama_endpoint, 'urlsplit', side_effect=ValueError(marker)):
            self.assert_safe_configuration_error(ollama_endpoint.ollama_base_url,
                                                 {'OLLAMA_URL': 'localhost:11434'}, marker)

    def test_ipv6_validation_error_is_sanitized(self):
        marker = 'SYNTHETIC_ENDPOINT_SECRET'
        parsed = SimpleNamespace(scheme='http', hostname=f'bad:{marker}', port=11434,
                                 username=None, password=None, query='', fragment='',
                                 netloc='synthetic-host:11434', path='')
        with patch.object(ollama_endpoint, 'urlsplit', return_value=parsed), \
             patch.object(ollama_endpoint.ipaddress, 'IPv6Address', side_effect=ValueError(marker)):
            self.assert_safe_configuration_error(ollama_endpoint.ollama_base_url,
                                                 {'OLLAMA_URL': 'localhost:11434'}, marker)

    def test_sanitization_does_not_relax_validation(self):
        invalid = ('ftp://localhost:11434', 'http://', 'localhost:',
                   'localhost:0', 'localhost:-1', 'localhost:65536',
                   'http://.localhost', 'http://-localhost', 'http://[::1')
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    ollama_endpoint.ollama_base_url({'OLLAMA_URL': endpoint})

    def test_client_url_and_probe_use_same_endpoint(self):
        cases = (
            ({}, "http://127.0.0.1:11434", ("127.0.0.1", 11434)),
            ({"OLLAMA_HOST": "127.0.0.1:11434"},
             "http://127.0.0.1:11434", ("127.0.0.1", 11434)),
            ({"OLLAMA_URL": "localhost:11434/ollama/"},
             "http://localhost:11434/ollama", ("localhost", 11434)),
            ({"OLLAMA_URL": "example.com"}, "http://example.com", ("example.com", 80)),
            ({"OLLAMA_URL": " HTTPS://Example.COM/ollama/// ", "OLLAMA_HOST": "127.0.0.1:9999"},
             "https://example.com/ollama", ("example.com", 443)),
            ({"OLLAMA_URL": " ", "OLLAMA_HOST": "http://localhost"},
             "http://localhost", ("localhost", 80)),
            ({"OLLAMA_HOST": "0.0.0.0:11434/"},
             "http://127.0.0.1:11434", ("127.0.0.1", 11434)),
            ({"OLLAMA_URL": "http://[::]:11434/"},
             "http://127.0.0.1:11434", ("127.0.0.1", 11434)),
            ({"OLLAMA_URL": "http://[::1]:11434"},
             "http://[::1]:11434", ("::1", 11434)),
            ({"OLLAMA_URL": "https://example.com:8443/ollama/"},
             "https://example.com:8443/ollama", ("example.com", 8443)),
        )
        for values, base, target in cases:
            with self.subTest(values=values):
                self.assertEqual(ollama_endpoint.ollama_base_url(values), base)
                self.assertEqual(ollama_endpoint.ollama_probe_target(values), target)

    def test_malformed_endpoints_raise_clear_error(self):
        values = (
            "ftp://localhost:11434", "http://", "localhost:abc",
            "localhost:0", "localhost:99999", "http://user:pass@localhost:11434",
            "http://localhost:11434?token=dummy", "http://localhost:11434#fragment",
            "http://bad host:11434", "http://[::1", "http://bad^host:11434",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Ollama"):
                    ollama_endpoint.ollama_base_url({"OLLAMA_URL": value})
                with self.assertRaisesRegex(ValueError, "Ollama"):
                    ollama_endpoint.ollama_probe_target({"OLLAMA_HOST": value})

    def test_api_ready_requires_ollama_model_list_and_bounded_body(self):
        cases = (
            (b'{"models": []}', 200, True),
            (b'{"models": [{"name": "dummy"}]}', 200, True),
            (b'{"other": []}', 200, False),
            (b'{"models": {}}', 200, False),
            (b'[]', 200, False),
            (b'not JSON', 200, False),
            (b'{"models": []}', 503, False),
            (b'X' * (1024 * 1024 + 1), 200, False),
        )
        for body, status, expected in cases:
            with self.subTest(status=status, body_prefix=body[:30]):
                response = _FakeResponse(body, status)
                calls = []

                def fake_urlopen(probe, *, timeout):
                    calls.append((probe.full_url, probe.get_method(),
                                  probe.get_header("Accept"), timeout))
                    return response

                with patch.object(ollama_endpoint.urllib_request, "urlopen", fake_urlopen):
                    ready = ollama_endpoint.ollama_api_ready(
                        {"OLLAMA_HOST": "localhost:11434/"}, timeout=0.1,
                    )

                self.assertIs(ready, expected)
                self.assertEqual(calls, [
                    ("http://localhost:11434/api/tags", "GET", "application/json", 0.1),
                ])
                self.assertEqual(response.read_limits, [] if status != 200 else [1024 * 1024 + 1])

    def test_api_ready_uses_configured_base_path(self):
        with patch.object(ollama_endpoint.urllib_request, "urlopen",
                          return_value=_FakeResponse(b'{"models": []}')) as urlopen:
            self.assertTrue(ollama_endpoint.ollama_api_ready(
                {"OLLAMA_URL": "localhost:11434/ollama/"},
            ))
        self.assertEqual(urlopen.call_args.args[0].full_url,
                         "http://localhost:11434/ollama/api/tags")

    def test_api_ready_returns_false_on_connection_or_http_failure(self):
        http_error = HTTPError(
            "http://localhost:11434/api/tags", 503, "dummy unavailable", {}, None,
        )
        self.addCleanup(http_error.close)
        errors = (
            OSError("dummy offline"),
            TimeoutError("dummy timeout"),
            HTTPException("dummy malformed response"),
            http_error,
        )
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                 patch.object(ollama_endpoint.urllib_request, "urlopen", side_effect=error):
                self.assertFalse(ollama_endpoint.ollama_api_ready(
                    {"OLLAMA_URL": "http://localhost:11434"},
                ))
        self.assertTrue(http_error.closed)

    def test_api_ready_does_not_hide_invalid_configuration(self):
        with patch.object(ollama_endpoint.urllib_request, "urlopen",
                          side_effect=AssertionError("network probe should not run")) as urlopen:
            with self.assertRaisesRegex(ValueError, "Ollama"):
                ollama_endpoint.ollama_api_ready({"OLLAMA_URL": "http://localhost:bad"})
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
