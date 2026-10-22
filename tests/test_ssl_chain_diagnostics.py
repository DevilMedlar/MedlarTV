"""Classify synthetic TLS errors without messages, getters or network access."""
import io
import logging
import ssl
import unittest

from test_startup_stage_diagnostics import ROOT, SECRET, definitions, stream_fixture


class SSLChainTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        logger = logging.Logger("isolated-ssl-chain", level=logging.DEBUG)
        logger.addHandler(logging.StreamHandler(self.output))
        self.ns = definitions(ROOT / "MedlarTV/core/stream_management.py",
                              ("_safe_exception_class", "_ssl_exception_details", "_log_verification_stage"),
                              {"log": logger})

    def tearDown(self):
        self.assertNotIn(SECRET, self.output.getvalue())

    def emit(self, error):
        self.ns["_log_verification_stage"]("broadcaster_validation", "failed", exception=error)
        return self.output.getvalue()

    def certificate(self):
        error = ssl.SSLCertVerificationError(1, SECRET)
        error.reason, error.library, error.verify_code = "CERTIFICATE_VERIFY_FAILED", "SSL", 20
        error.verify_message = SECRET
        return error

    def test_native_certificate_codes_and_symbols_without_message(self):
        output = self.emit(self.certificate())
        for field in ("reason=CERTIFICATE_VERIFY_FAILED", "library=SSL", "verify_code=20", "error_code=1"):
            self.assertIn(field, output)

    def test_requests_urllib3_shape_reaches_exception_argument_without_formatting_it(self):
        retry_type = type("MaxRetryError", (Exception,), {})
        retry = retry_type(SECRET)
        retry.reason = ssl.SSLError(self.certificate(), SECRET)
        outer = ssl.SSLError(retry, SECRET)
        self.assertIn("verify_code=20", self.emit(outer))

    def test_cause_context_and_original_error_links_are_supported(self):
        for link in ("__cause__", "__context__", "original_error"):
            with self.subTest(link=link):
                outer = ssl.SSLError(SECRET)
                setattr(outer, link, self.certificate())
                self.assertIn("verify_code=20", self.emit(outer))

    def test_self_and_mutual_cycles_terminate(self):
        outer, inner = ssl.SSLError(SECRET), self.certificate()
        outer.__cause__, inner.__context__, inner.reason = inner, outer, outer
        output = self.emit(outer)
        self.assertIn("verify_code=20", output)
        self.assertLess(len(output), 2048)
        outer.__cause__ = outer
        self.assertIn("verify_code=unknown", self.ns["_ssl_exception_details"](outer))

    def test_eight_node_bound_leaves_deeper_details_unknown(self):
        outer = ssl.SSLError(SECRET)
        current = outer
        for _ in range(8):
            child = RuntimeError(SECRET)
            current.__cause__, current = child, child
        current.__cause__ = self.certificate()
        output = self.emit(outer)
        self.assertIn("verify_code=unknown", output)
        self.assertNotIn("SSLCertVerificationError", output)

    def test_properties_and_custom_getattribute_are_never_invoked(self):
        class HostileError(Exception):
            def __getattribute__(self, name):
                raise AssertionError("Getter must not run")
            @property
            def reason(self): raise AssertionError("Property must not run")
            def __str__(self): raise AssertionError("Message must not be formatted")
            def __repr__(self): raise AssertionError("Error must not be represented")
        outer = HostileError(SECRET)
        BaseException.__cause__.__set__(outer, self.certificate())
        output = self.emit(outer)
        self.assertIn("exception=UnknownError", output)
        self.assertIn("verify_code=20", output)

    def test_unknown_attributes_and_string_subclasses_are_not_coerced(self):
        class HostileString(str):
            def __hash__(self): raise AssertionError("String subclass hash must not run")
            def __str__(self): raise AssertionError("String conversion must not run")
        class HostileObject:
            def __str__(self): raise AssertionError("Object conversion must not run")
        error = ssl.SSLError(SECRET)
        error.reason = HostileString("CERTIFICATE_VERIFY_FAILED")
        error.library = HostileString("SSL")
        error.verify_code = HostileObject()
        error.verify_message, error.url, error.headers, error.body = SECRET, SECRET, SECRET, SECRET
        output = self.emit(error)
        for field in ("reason=unknown", "library=unknown", "verify_code=unknown"):
            self.assertIn(field, output)

    def test_unknown_child_objects_and_metaclass_names_do_not_run_getters(self):
        class HostileMeta(type):
            @property
            def __name__(cls): raise AssertionError("Metaclass getter must not run")
        class HostileError(Exception, metaclass=HostileMeta): pass
        class HostileChild:
            def __getattribute__(self, name): raise AssertionError("Child getter must not run")
        outer = HostileError(SECRET)
        outer.__cause__ = self.certificate()
        outer.reason = HostileChild()
        self.assertIn("verify_code=20", self.emit(outer))

    def test_unallowlisted_strings_and_invalid_numeric_values_stay_unknown(self):
        for code in (True, -1, 65536, SECRET):
            with self.subTest(code=code):
                error = ssl.SSLError(SECRET)
                error.reason, error.library, error.verify_code, error.errno = SECRET, SECRET, code, code
                output = self.ns["_ssl_exception_details"](error)
                self.assertIn("verify_code=unknown", output)
                self.assertIn("error_code=unknown", output)
                self.assertNotIn(SECRET, output)

    def test_secret_class_name_and_message_do_not_leak_through_stage_or_legacy_logs(self):
        unknown_type = type(SECRET, (Exception,), {})
        ns, output, _, _, _ = stream_fixture(debug=True)
        ns["requests"].get.side_effect = unknown_type(SECRET)
        from contextlib import redirect_stdout
        with redirect_stdout(output):
            self.assertIs(ns["verify_twitch_tokens"](), False)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertIn("exception=UnknownError", output.getvalue())

    def test_non_tls_error_does_not_invent_tls_reason(self):
        output = self.emit(ValueError(SECRET))
        self.assertNotIn("[TwitchTLS]", output)


if __name__ == "__main__": unittest.main()
