"""Tests for bounded retry/backoff on transient arr HTTP failures.

Idempotent reads (GET) and explicit test POSTs may retry; non-idempotent calls
(DELETE /queue, ManualImport) must never retry -- a retry there could
double-delete or double-grab.
"""
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import doctor


def _resp(payload):
    return io.BytesIO(json.dumps(payload).encode())


class ArrRetryTest(unittest.TestCase):
    def setUp(self):
        self.arr = doctor.Arr("radarr-1", "radarr", "http://x", "key")

    def test_queue_retries_transient_failure_then_succeeds(self):
        calls = []

        def flaky(method, path, data=None, t=None):
            calls.append(method)
            if len(calls) < 3:
                raise OSError("transient 503")
            return _resp({"records": [{"id": 1}]})

        with patch.object(self.arr, "_req", side_effect=flaky), \
             patch.object(doctor.time, "sleep"), \
             patch.object(doctor, "HTTP_RETRIES", 3), \
             patch.object(doctor, "HTTP_RETRY_BASE", 0.01):
            recs = self.arr.queue()

        self.assertEqual(recs, [{"id": 1}])
        self.assertEqual(len(calls), 3)

    def test_queue_gives_up_after_max_tries(self):
        with patch.object(self.arr, "_req", side_effect=OSError("down")), \
             patch.object(doctor.time, "sleep"), \
             patch.object(doctor, "HTTP_RETRIES", 3), \
             patch.object(doctor, "HTTP_RETRY_BASE", 0.01):
            recs = self.arr.queue()
        self.assertIsNone(recs)

    def test_get_json_retries(self):
        calls = []

        def flaky(method, path, data=None, t=None):
            calls.append(method)
            if len(calls) < 2:
                raise OSError("transient")
            return _resp({"ok": True})

        with patch.object(self.arr, "_req", side_effect=flaky), \
             patch.object(doctor.time, "sleep"), \
             patch.object(doctor, "HTTP_RETRIES", 3), \
             patch.object(doctor, "HTTP_RETRY_BASE", 0.01):
            out = self.arr.get_json("/movie")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_delete_is_never_retried(self):
        calls = []

        def always_fail(method, path, data=None, t=None):
            calls.append(method)
            raise OSError("boom")

        with patch.object(self.arr, "_req", side_effect=always_fail), \
             patch.object(doctor.time, "sleep") as slept, \
             patch.object(doctor, "HTTP_RETRIES", 5), \
             patch.object(doctor, "HTTP_RETRY_BASE", 0.01):
            with self.assertRaises(OSError):
                self.arr.remove(42)
        self.assertEqual(len(calls), 1)      # DELETE tried exactly once
        slept.assert_not_called()

    def test_command_manualimport_is_never_retried(self):
        calls = []

        def always_fail(method, path, data=None, t=None):
            calls.append(method)
            raise OSError("boom")

        with patch.object(self.arr, "_req", side_effect=always_fail), \
             patch.object(doctor.time, "sleep") as slept, \
             patch.object(doctor, "HTTP_RETRIES", 5), \
             patch.object(doctor, "HTTP_RETRY_BASE", 0.01):
            # command() swallows the error and returns None; the point is it
            # calls _req exactly once (POST /command via bare _req, no retry).
            out = self.arr.command({"name": "ManualImport", "files": []})
        self.assertIsNone(out)
        self.assertEqual(len(calls), 1)
        slept.assert_not_called()

    def test_testall_post_retries_when_opted_in(self):
        calls = []

        def flaky(method, path, data=None, t=None):
            calls.append(method)
            if len(calls) < 2:
                raise OSError("transient")
            return io.BytesIO(b"[]")

        with patch.object(self.arr, "_req", side_effect=flaky), \
             patch.object(doctor.time, "sleep"), \
             patch.object(doctor, "HTTP_RETRIES", 3), \
             patch.object(doctor, "HTTP_RETRY_BASE", 0.01):
            out = self.arr.post("/indexer/testall")
        self.assertEqual(out, [])
        self.assertEqual(len(calls), 2)      # POST retried once


if __name__ == "__main__":
    unittest.main()
