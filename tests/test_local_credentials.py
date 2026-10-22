"""Credential storage tests using dummy values and an in-memory WinCred API."""

import ctypes
import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "MedlarTV" / "core" / "local_credentials.py"
spec = importlib.util.spec_from_file_location("local_credentials_under_test", MODULE_PATH)
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)


class FakeWinCred:
    def __init__(self):
        self.records = {}
        self.writes = []
        self.frees = 0
        self.error = 0
        self.fail_read_key = None
        self.fail_write = False
        self._retained = []

    def CredWriteW(self, record_arg, flags):
        if self.fail_write:
            self.error = 5
            return 0
        record = ctypes.cast(record_arg, ctypes.POINTER(vault._CredentialW)).contents
        blob = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        self.records[record.TargetName] = blob
        self.writes.append((record.TargetName, record.Type, record.Persist, flags))
        self.error = 0
        return 1

    def CredReadW(self, target, credential_type, flags, output_arg):
        if target == self.fail_read_key:
            self.error = 5
            return 0
        blob = self.records.get(target)
        if blob is None:
            self.error = vault._ERROR_NOT_FOUND
            return 0
        assert credential_type == vault._CRED_TYPE_GENERIC
        assert flags == 0
        record = vault._CredentialW()
        record.Type = credential_type
        record.TargetName = target
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        record.CredentialBlobSize = len(blob)
        record.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        record_ptr = ctypes.pointer(record)
        output = ctypes.cast(output_arg, ctypes.POINTER(ctypes.POINTER(vault._CredentialW)))
        output[0] = record_ptr
        self._retained.append((record, buffer, record_ptr))
        self.error = 0
        return 1

    def CredFree(self, _pointer):
        self.frees += 1


class LocalCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWinCred()
        self.original_get_api = vault._get_api
        self.api_patch = mock.patch.object(vault, "_get_api", return_value=self.api)
        self.error_patch = mock.patch.object(vault.ctypes, "get_last_error", side_effect=lambda: self.api.error)
        self.api_patch.start()
        self.error_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.addCleanup(self.error_patch.stop)

    def test_allowlist_names_and_labels(self):
        self.assertEqual(set(vault.CREDENTIAL_FIELDS), {
            "MEDLARTV_TWITCH_TOKEN",
            "MEDLARTV_TWITCH_CLIENT_ID",
            "APP_TWITCH_CLIENT_ID",
            "APP_SECRET_ID",
            "DEVILMEDLAR_TWITCH_CLIENT_ID",
            "DEVILMEDLAR_TWITCH_TOKEN",
        })
        self.assertIn("bot", vault.CREDENTIAL_FIELDS["MEDLARTV_TWITCH_TOKEN"].lower())
        self.assertIn("bot", vault.CREDENTIAL_FIELDS["MEDLARTV_TWITCH_CLIENT_ID"].lower())
        self.assertIn("client ID", vault.CREDENTIAL_FIELDS["MEDLARTV_TWITCH_CLIENT_ID"])
        self.assertIn("secret", vault.CREDENTIAL_FIELDS["APP_SECRET_ID"].lower())
        self.assertIn("broadcaster", vault.CREDENTIAL_FIELDS["DEVILMEDLAR_TWITCH_TOKEN"].lower())

    def test_save_uses_generic_local_machine_credential_and_presence_masks_value(self):
        key = "MEDLARTV_TWITCH_TOKEN"
        self.assertFalse(vault.credential_present(key))
        vault.save_credential(key, "dummy-bot-token")
        self.assertEqual(self.api.writes, [
            ("MedlarTV/Connections/" + key, vault._CRED_TYPE_GENERIC,
             vault._CRED_PERSIST_LOCAL_MACHINE, 0)
        ])
        with mock.patch.object(vault.ctypes, "string_at", side_effect=AssertionError("decoded secret")):
            self.assertTrue(vault.credential_present(key))
        self.assertEqual(self.api.frees, 1)

    def test_saved_values_override_only_their_keys(self):
        vault.save_credential("MEDLARTV_TWITCH_TOKEN", "new-dummy-bot-token")
        vault.save_credential("MEDLARTV_TWITCH_CLIENT_ID", "new-dummy-bot-client-id")
        vault.save_credential("APP_TWITCH_CLIENT_ID", "new-dummy-app-id")
        env = {
            "MEDLARTV_TWITCH_TOKEN": "legacy-dummy-bot-token",
            "APP_SECRET_ID": "legacy-dummy-app-secret",
            "OTHER": "untouched",
        }
        applied = vault.apply_saved_credentials(env)
        self.assertEqual(applied, {"MEDLARTV_TWITCH_TOKEN", "MEDLARTV_TWITCH_CLIENT_ID",
                                   "APP_TWITCH_CLIENT_ID"})
        self.assertEqual(env, {
            "MEDLARTV_TWITCH_TOKEN": "new-dummy-bot-token",
            "MEDLARTV_TWITCH_CLIENT_ID": "new-dummy-bot-client-id",
            "APP_TWITCH_CLIENT_ID": "new-dummy-app-id",
            "APP_SECRET_ID": "legacy-dummy-app-secret",
            "OTHER": "untouched",
        })

    def test_missing_records_leave_environment_unchanged(self):
        env = {"APP_SECRET_ID": "legacy-dummy-app-secret", "OTHER": "untouched"}
        self.assertEqual(vault.apply_saved_credentials(env), set())
        self.assertEqual(env, {"APP_SECRET_ID": "legacy-dummy-app-secret", "OTHER": "untouched"})

    def test_invalid_keys_and_empty_values_never_reach_api(self):
        with self.assertRaises(ValueError):
            vault.save_credential("UNRELATED_TOKEN", "dummy")
        with self.assertRaises(ValueError):
            vault.credential_present("UNRELATED_TOKEN")
        with self.assertRaises(ValueError):
            vault.save_credential("APP_SECRET_ID", "  ")
        self.assertEqual(self.api.records, {})
        self.assertEqual(self.api.writes, [])

    def test_read_error_does_not_partially_overlay_environment(self):
        vault.save_credential("MEDLARTV_TWITCH_TOKEN", "new-dummy-bot-token")
        self.api.fail_read_key = "MedlarTV/Connections/APP_SECRET_ID"
        env = {"MEDLARTV_TWITCH_TOKEN": "legacy-dummy-bot-token"}
        with self.assertRaises(OSError):
            vault.apply_saved_credentials(env)
        self.assertEqual(env, {"MEDLARTV_TWITCH_TOKEN": "legacy-dummy-bot-token"})

    def test_save_error_does_not_disclose_value(self):
        self.api.fail_write = True
        secret = "dummy-secret-that-must-not-appear"
        with self.assertRaises(OSError) as caught:
            vault.save_credential("APP_SECRET_ID", secret)
        self.assertNotIn(secret, str(caught.exception))

    def test_unsupported_platform_fails_before_native_api_load(self):
        with mock.patch.object(os, "name", "posix"), mock.patch.object(vault.ctypes, "WinDLL") as native:
            with self.assertRaises(OSError):
                self.original_get_api()
            native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
