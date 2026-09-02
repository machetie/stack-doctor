"""Regression tests for warmer pre-warm scrubber gate (phase B crash prevention)."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import doctor


class WarmVerifyAndActTest(unittest.TestCase):
    def test_verify_disabled_returns_ok(self):
        with patch.object(doctor, "WARM_VERIFY", False):
            ok, why = doctor._warm_verify_and_act("/mnt/lib/movie.mkv", "detail-page")
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_no_scrub_paths_returns_ok(self):
        with patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "SCRUB_PATHS", []):
            ok, why = doctor._warm_verify_and_act("/mnt/lib/movie.mkv", "detail-page")
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_path_outside_scrub_paths_returns_ok(self):
        with patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/library"]), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True):
            ok, why = doctor._warm_verify_and_act("/mnt/other/movie.mkv", "detail-page")
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_similar_prefix_does_not_match(self):
        """A path under /mnt/library2 must not match SCRUB_PATHS /mnt/library."""
        with patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/library"]), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True), \
             patch.object(doctor, "_scrub_t1_header") as t1:
            ok, why = doctor._warm_verify_and_act("/mnt/library2/movie.mkv", "detail-page")
        self.assertTrue(ok)
        self.assertIsNone(why)
        t1.assert_not_called()

    def test_missing_bins_returns_ok_and_does_not_act(self):
        with patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/library"]), \
             patch.object(doctor, "_scrub_bins_ok", return_value=False), \
             patch.object(doctor, "_scrub_act_on_bad") as act:
            ok, why = doctor._warm_verify_and_act("/mnt/library/movie.mkv", "detail-page")
        self.assertTrue(ok)
        self.assertIsNone(why)
        act.assert_not_called()

    def test_good_file_returns_ok(self):
        with patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/library"]), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True), \
             patch.object(doctor, "_realpath_with_timeout", return_value=("/mnt/library/movie.mkv", False)), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "_scrub_t1_header", return_value=(True, "")), \
             patch.object(doctor, "_scrub_act_on_bad") as act:
            ok, why = doctor._warm_verify_and_act("/mnt/library/movie.mkv", "detail-page")
        self.assertTrue(ok)
        self.assertIsNone(why)
        act.assert_not_called()

    def test_bad_tier1_calls_action_and_returns_false(self):
        act = MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as quar, \
             patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/library"]), \
             patch.object(doctor, "SCRUB_QUAR", quar), \
             patch.object(doctor, "WARM_VERIFY_TIER", 1), \
             patch.object(doctor, "SCRUB_CONFIRM_DEL", False), \
             patch.object(doctor, "DRY_RUN", False), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True), \
             patch.object(doctor, "_realpath_with_timeout", return_value=("/real/movie.mkv", False)), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "_scrub_t1_header", return_value=(False, "ffprobe rc=1")), \
             patch.object(doctor, "_scrub_act_on_bad", act) as act_mock, \
             patch.object(doctor, "_atomic_write_json"):
            ok, why = doctor._warm_verify_and_act("/mnt/library/movie.mkv", "detail-page")
        self.assertFalse(ok)
        self.assertIn("ffprobe rc=1", why)
        act_mock.assert_called_once()
        args = act_mock.call_args[0]
        self.assertEqual(args[0], "/real/movie.mkv")   # real path
        self.assertEqual(args[1], "/mnt/library/movie.mkv")  # host/library path
        self.assertIn("warmer-verify", args[2])

    def test_bad_tier1_unconfirmed_gets_downgraded(self):
        act = MagicMock(return_value=True)
        with patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/library"]), \
             patch.object(doctor, "WARM_VERIFY_TIER", 1), \
             patch.object(doctor, "SCRUB_CONFIRM_DEL", True), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True), \
             patch.object(doctor, "_realpath_with_timeout", return_value=("/real/movie.mkv", False)), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "_scrub_t1_header", return_value=(False, "cosmetic")), \
             patch.object(doctor, "_scrub_confirm_decode", return_value=(True, "")), \
             patch.object(doctor, "_scrub_act_on_bad", act) as act_mock:
            ok, why = doctor._warm_verify_and_act("/mnt/library/movie.mkv", "detail-page")
        self.assertTrue(ok)
        self.assertIsNone(why)
        act_mock.assert_not_called()

    def test_path_map_is_respected_for_scrub_path_check(self):
        with patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "WARM_PATH_MAP", "/plex:/mnt/library"), \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/library"]), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True), \
             patch.object(doctor, "_realpath_with_timeout", return_value=("/mnt/library/movie.mkv", False)), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "_scrub_t1_header", return_value=(True, "")), \
             patch.object(doctor, "_scrub_act_on_bad") as act:
            ok, why = doctor._warm_verify_and_act("/plex/movie.mkv", "detail-page")
        self.assertTrue(ok)
        act.assert_not_called()


class WarmFileVerifyGateTest(unittest.TestCase):
    def test_warm_file_skips_warm_when_verify_bad(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "WARM_LOAD_MAX", 0), \
             patch.object(doctor, "WARM_COOLDOWN", 0), \
             patch.object(doctor, "_warm_verify_and_act", return_value=(False, "dead file")) as verify, \
             patch.object(doctor, "_warm_record") as rec:
            path = os.path.join(tmp, "movie.mkv")
            with open(path, "wb") as f:
                f.write(b"data")
            result = doctor._warm_file(path, "detail-page")
        self.assertFalse(result)
        verify.assert_called_once_with(path, "detail-page")
        rec.assert_not_called()

    def test_warm_file_proceeds_when_verify_ok(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(doctor, "WARM_VERIFY", True), \
             patch.object(doctor, "WARM_HEAD_MB", 1), \
             patch.object(doctor, "WARM_TAIL_MB", 0), \
             patch.object(doctor, "WARM_LOAD_MAX", 0), \
             patch.object(doctor, "WARM_COOLDOWN", 0), \
             patch.object(doctor, "WARM_READ_TIMEOUT", 10), \
             patch.object(doctor, "_warm_verify_and_act", return_value=(True, None)), \
             patch.object(doctor, "_warm_record") as rec:
            path = os.path.join(tmp, "movie.mkv")
            with open(path, "wb") as f:
                f.write(b"x" * (2 << 20))
            result = doctor._warm_file(path, "detail-page")
        self.assertTrue(result)
        rec.assert_called_once()


if __name__ == "__main__":
    unittest.main()
