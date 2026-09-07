"""Regression tests for the prefetch-retry SeasonSearch escalation state machine.

Pins the decision logic of _placeholder_prefetch_retry_check: drop-on-success, purge-unaired,
wait-on-active-grab, fail-aware escalation (audit #44), and max-retries give-up.
"""
import datetime
import unittest
from unittest.mock import MagicMock, patch

import doctor


def _mk_state(items):
    return list(items)


def _item(ep_id=100, sid=380, season=1, ep=3, retries=0, ts=None, last_retry=None):
    return {"episode_id": ep_id, "series_id": sid, "season": season,
            "episode": ep, "retries": retries, "ts": ts or 0, "last_retry": last_retry}


class PrefetchRetryTest(unittest.TestCase):
    def setUp(self):
        self.arr = MagicMock()
        # default episode: aired, no file
        self.arr.get_json.side_effect = self._dispatch

    def _dispatch(self, path):
        path = str(path)
        if path.startswith("/episode/"):
            return self.ep
        if path.startswith("/queue"):
            return {"records": self.queue}
        if path.startswith("/history"):
            return {"records": self.hist}
        return None

    def _run(self, state, ep=None, queue=None, hist=None, retries=0, last_retry=0):
        self.ep = ep if ep is not None else {"hasFile": False, "airDateUtc": "2020-01-05T00:00:00Z"}
        self.queue = queue or []
        self.hist = hist or []
        self.retries = retries
        self.last_retry = last_retry
        with patch.object(doctor, "_sonarr_instance", return_value=self.arr), \
             patch.object(doctor, "_placeholder_prefetch_retry_load", return_value=state), \
             patch.object(doctor, "_placeholder_prefetch_retry_save") as self.save, \
             patch.object(doctor, "_reng") as self.reng:
            self.reng.is_unaired.return_value = False
            doctor._placeholder_prefetch_retry_check()
        return self.arr.command.call_args_list

    def test_seasonsearch_fires_when_fileless_and_idle(self):
        calls = self._run(_mk_state([_item(retries=0, last_retry=0)]))
        self.assertTrue(calls, "SeasonSearch should fire")
        name = calls[0][0][0].get("name")
        self.assertEqual(name, "SeasonSearch")

    def test_drops_on_success(self):
        ep = {"hasFile": True, "airDateUtc": "2020-01-05T00:00:00Z"}
        calls = self._run(_mk_state([_item(retries=0, last_retry=0)]), ep=ep)
        self.assertEqual(calls, [], "hasFile=True should drop the item, no search")
        # and the item should be removed from state (saved without it)
        saved = self.save.call_args[0][0]
        self.assertEqual(saved, [])

    def test_purges_unaired(self):
        self.ep = {"hasFile": False, "airDateUtc": "2026-12-01T00:00:00Z"}
        self.queue = []
        self.hist = []
        with patch.object(doctor, "_sonarr_instance", return_value=self.arr), \
             patch.object(doctor, "_placeholder_prefetch_retry_load", return_value=_mk_state([_item()])), \
             patch.object(doctor, "_placeholder_prefetch_retry_save") as save, \
             patch.object(doctor, "_reng") as reng:
            reng.is_unaired.return_value = True
            doctor._placeholder_prefetch_retry_check()
        self.assertEqual(save.call_args[0][0], [], "unaired should be purged")

    def test_waits_when_active_grab_and_no_failures(self):
        # past threshold, but an active grab with no failures -> wait (no escalation)
        state = _mk_state([_item(retries=0, last_retry=0)])
        queue = [{"status": "downloading"}]
        hist = []
        self.ep = {"hasFile": False, "airDateUtc": "2020-01-05T00:00:00Z"}
        self.queue = queue
        self.hist = hist
        with patch.object(doctor, "_sonarr_instance", return_value=self.arr), \
             patch.object(doctor, "_placeholder_prefetch_retry_load", return_value=state), \
             patch.object(doctor, "_placeholder_prefetch_retry_save"), \
             patch.object(doctor, "_reng") as reng:
            reng.is_unaired.return_value = False
            doctor._placeholder_prefetch_retry_check()
        self.assertEqual(self.arr.command.call_count, 0, "active grab + no fails -> wait")

    def test_escalates_despite_active_grab_when_failures(self):
        # §44: active grab BUT >= FAIL_ESCALATE downloadFailed events -> escalate anyway
        state = _mk_state([_item(retries=0, last_retry=0)])
        queue = [{"status": "downloading"}]
        hist = [{"eventType": "downloadFailed"}, {"eventType": "downloadFailed"}]
        self.ep = {"hasFile": False, "airDateUtc": "2020-01-05T00:00:00Z"}
        self.queue = queue
        self.hist = hist
        with patch.object(doctor, "_sonarr_instance", return_value=self.arr), \
             patch.object(doctor, "_placeholder_prefetch_retry_load", return_value=state), \
             patch.object(doctor, "_placeholder_prefetch_retry_save"), \
             patch.object(doctor, "_reng") as reng:
            reng.is_unaired.return_value = False
            doctor._placeholder_prefetch_retry_check()
        self.assertEqual(self.arr.command.call_count, 1, ">=2 fails should escalate despite active grab")

    def test_gives_up_at_max_retries(self):
        state = _mk_state([_item(retries=doctor.PLACEHOLDER_PREFETCH_MAX_RETRIES, last_retry=0)])
        self.ep = {"hasFile": False, "airDateUtc": "2020-01-05T00:00:00Z"}
        self.queue = []
        self.hist = []
        with patch.object(doctor, "_sonarr_instance", return_value=self.arr), \
             patch.object(doctor, "_placeholder_prefetch_retry_load", return_value=state), \
             patch.object(doctor, "_placeholder_prefetch_retry_save"), \
             patch.object(doctor, "_reng") as reng:
            reng.is_unaired.return_value = False
            doctor._placeholder_prefetch_retry_check()
        self.assertEqual(self.arr.command.call_count, 0, "max retries -> give up")


if __name__ == "__main__":
    unittest.main()
