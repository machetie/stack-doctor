"""Regression tests for the scrubber tier-1 false-positive fix.

The scrubber must trust ffprobe's rc=0 by default and only treat cosmetic stderr
warnings as corruption when explicitly opted in. Before any destructive action on
a tier-1 BAD, a quick decode confirm must downgrade the file to OK if it actually plays.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import doctor


class ScrubBenignStderrTest(unittest.TestCase):
    def test_each_known_benign_string_is_benign(self):
        for marker in doctor._SCRUB_BENIGN_STDERR:
            with self.subTest(marker=marker):
                self.assertTrue(
                    doctor._scrub_benign_only("[mov,mp4 @ 0x1] %s\n" % marker),
                    "%r should be classified as benign" % marker,
                )

    def test_mixed_benign_only_stderr_is_benign(self):
        err = (
            "[mov,mp4 @ 0x1] Referenced QT chapter track not found\n"
            "[h264 @ 0x2] sps_id 1 out of range\n"
            "[h264 @ 0x2] pps_id out of range\n"
        )
        self.assertTrue(doctor._scrub_benign_only(err))

    def test_unknown_line_makes_stderr_not_benign(self):
        err = (
            "[mov,mp4 @ 0x1] Referenced QT chapter track not found\n"
            "[h264 @ 0x2] real corruption here\n"
        )
        self.assertFalse(doctor._scrub_benign_only(err))


class ScrubT1HeaderTest(unittest.TestCase):
    def test_rc0_qt_chapter_warning_is_ok_by_default(self):
        err = "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] Referenced QT chapter track not found\n"
        with patch.object(doctor, "_scrub_run", return_value=(0, err)):
            ok, why = doctor._scrub_t1_header("/tmp/file.mp4")
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_rc0_sps_id_warning_is_ok_by_default(self):
        err = "[h264 @ 0x1] sps_id 1 out of range\n"
        with patch.object(doctor, "_scrub_run", return_value=(0, err)):
            ok, why = doctor._scrub_t1_header("/tmp/file.mkv")
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_rc0_unknown_stderr_is_bad_when_strict(self):
        err = "[h264 @ 0x1] something actually wrong\n"
        with patch.object(doctor, "_scrub_run", return_value=(0, err)), \
             patch.object(doctor, "SCRUB_STRICT_STDERR", True):
            ok, why = doctor._scrub_t1_header("/tmp/file.mkv")
        self.assertFalse(ok)
        self.assertIn("rc=0", why)

    def test_rc0_unknown_stderr_is_ok_when_not_strict(self):
        err = "[h264 @ 0x1] something actually wrong\n"
        with patch.object(doctor, "_scrub_run", return_value=(0, err)), \
             patch.object(doctor, "SCRUB_STRICT_STDERR", False):
            ok, why = doctor._scrub_t1_header("/tmp/file.mkv")
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_nonzero_rc_is_bad(self):
        err = "some parse error\n"
        with patch.object(doctor, "_scrub_run", return_value=(1, err)):
            ok, why = doctor._scrub_t1_header("/tmp/file.mkv")
        self.assertFalse(ok)
        self.assertIn("rc=1", why)


class ScrubT3FullTest(unittest.TestCase):
    def test_rc0_benign_stderr_is_ok_by_default(self):
        err = "[h264 @ 0x1] co located POCs unavailable\n"
        with patch.object(doctor, "_scrub_run", return_value=(0, err)):
            ok, why = doctor._scrub_t3_full("/tmp/file.mkv")
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_rc0_unknown_stderr_is_bad_when_strict(self):
        err = "[h264 @ 0x1] real decode error\n"
        with patch.object(doctor, "_scrub_run", return_value=(0, err)), \
             patch.object(doctor, "SCRUB_STRICT_STDERR", True):
            ok, why = doctor._scrub_t3_full("/tmp/file.mkv")
        self.assertFalse(ok)
        self.assertIn("rc=0", why)


class ScrubConfirmBeforeDeleteTest(unittest.TestCase):
    def _run(self, confirm_ok, strict_stderr=False):
        st = MagicMock()
        st.st_mtime = 0.0
        st.st_size = 12345
        files = [("/mnt/zurg/real.mkv", "/mnt/lib/link.mkv")]
        act = MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as quar, \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/lib"]), \
             patch.object(doctor, "SCRUB_QUAR", quar), \
             patch.object(doctor, "SCRUB_LOAD_MAX", 0), \
             patch.object(doctor, "SCRUB_STRIKES", 1), \
             patch.object(doctor, "SCRUB_MIN_AGE", 0), \
             patch.object(doctor, "SCRUB_MAX_DELETES", 1), \
             patch.object(doctor, "SCRUB_MAX_FILES", 10), \
             patch.object(doctor, "SCRUB_CONFIRM_DEL", True), \
             patch.object(doctor, "SCRUB_STRICT_STDERR", strict_stderr), \
             patch.object(doctor, "DRY_RUN", False), \
             patch.object(doctor, "_scrub_walk", return_value=iter(files)), \
             patch.object(doctor, "_stat_with_timeout", return_value=st), \
             patch.object(doctor, "_scrub_load_state", return_value={}), \
             patch.object(doctor, "_scrub_save_state", return_value=None), \
             patch.object(doctor, "_scrub_t1_header", return_value=(False, "ffprobe rc=0 cosmetic")), \
             patch.object(doctor, "_scrub_confirm_decode", return_value=(confirm_ok, "")), \
             patch.object(doctor, "_scrub_act_on_bad", act) as act_mock, \
             patch.object(doctor, "_mount_ok_for", return_value=True):
            doctor.check_scrubber()
        return act_mock

    def test_unconfirmed_tier1_bad_is_downgraded(self):
        act = self._run(confirm_ok=True)
        act.assert_not_called()

    def test_confirmed_tier1_bad_still_actions(self):
        act = self._run(confirm_ok=False)
        act.assert_called_once()


class ScrubMissingBinaryTest(unittest.TestCase):
    def test_scrub_run_missing_binary_returns_127_and_flags(self):
        doctor._SCRUB_BINS_MISSING[0] = False
        with patch.object(doctor.subprocess, "run", side_effect=FileNotFoundError("ffprobe")):
            rc, err = doctor._scrub_run(["ffprobe", "x"], 10)
        self.assertEqual(rc, 127)
        self.assertTrue(doctor._SCRUB_BINS_MISSING[0])
        self.assertIn("binary not found", err)

    def test_missing_bins_disables_scrubber_without_walking(self):
        with patch.object(doctor, "SCRUB_PATHS", ["/mnt/lib"]), \
             patch.object(doctor, "_scrub_bins_ok", return_value=False), \
             patch.object(doctor, "_scrub_walk") as walk, \
             patch.object(doctor, "log") as lg:
            doctor.check_scrubber()
        walk.assert_not_called()

    def test_mid_sweep_binary_missing_aborts_without_strike(self):
        st = MagicMock()
        st.st_mtime = 0.0
        st.st_size = 12345
        files = [("/mnt/zurg/real.mkv", "/mnt/lib/link.mkv")]

        def t1(path):
            doctor._SCRUB_BINS_MISSING[0] = True
            return (False, "binary not found: ffprobe")

        act = MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as quar, \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/lib"]), \
             patch.object(doctor, "SCRUB_QUAR", quar), \
             patch.object(doctor, "SCRUB_LOAD_MAX", 0), \
             patch.object(doctor, "SCRUB_STRIKES", 1), \
             patch.object(doctor, "SCRUB_MIN_AGE", 0), \
             patch.object(doctor, "SCRUB_MAX_DELETES", 1), \
             patch.object(doctor, "SCRUB_MAX_FILES", 10), \
             patch.object(doctor, "SCRUB_CONFIRM_DEL", False), \
             patch.object(doctor, "DRY_RUN", False), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True), \
             patch.object(doctor, "_scrub_walk", return_value=iter(files)), \
             patch.object(doctor, "_stat_with_timeout", return_value=st), \
             patch.object(doctor, "_scrub_load_state", return_value={}), \
             patch.object(doctor, "_scrub_save_state", return_value=None), \
             patch.object(doctor, "_scrub_t1_header", side_effect=t1), \
             patch.object(doctor, "_scrub_act_on_bad", act) as act_mock, \
             patch.object(doctor, "_mount_ok_for", return_value=True):
            doctor.check_scrubber()
        act_mock.assert_not_called()

    def test_bins_ok_true_when_both_present(self):
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/ffprobe"):
            self.assertTrue(doctor._scrub_bins_ok())

    def test_bins_ok_false_when_ffmpeg_missing(self):
        with patch.object(doctor.shutil, "which", side_effect=lambda b: "/x" if b == doctor.SCRUB_FFPROBE else None):
            self.assertFalse(doctor._scrub_bins_ok())


class ScrubStatePruneTest(unittest.TestCase):
    def test_stale_files_state_entries_are_pruned(self):
        st = MagicMock()
        st.st_mtime = 0.0
        st.st_size = 12345
        files = [("/mnt/zurg/real.mkv", "/mnt/lib/link.mkv")]
        state = {"files": {"/old/path.mkv": {"ts": 0, "status": "ok"},
                           "/fresh/path.mkv": {"ts": 9_999_999_999, "status": "ok"}}}
        saved = {}

        def save(s):
            saved.update(s)

        with tempfile.TemporaryDirectory() as quar, \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/lib"]), \
             patch.object(doctor, "SCRUB_QUAR", quar), \
             patch.object(doctor, "SCRUB_LOAD_MAX", 0), \
             patch.object(doctor, "SCRUB_STRIKES", 1), \
             patch.object(doctor, "SCRUB_MIN_AGE", 0), \
             patch.object(doctor, "SCRUB_MAX_DELETES", 1), \
             patch.object(doctor, "SCRUB_MAX_FILES", 10), \
             patch.object(doctor, "SCRUB_PRUNE_DAYS", 90), \
             patch.object(doctor, "SCRUB_CONFIRM_DEL", False), \
             patch.object(doctor, "DRY_RUN", False), \
             patch.object(doctor, "_scrub_bins_ok", return_value=True), \
             patch.object(doctor, "_scrub_walk", return_value=iter(files)), \
             patch.object(doctor, "_stat_with_timeout", return_value=st), \
             patch.object(doctor, "_scrub_load_state", return_value=state), \
             patch.object(doctor, "_scrub_save_state", side_effect=save), \
             patch.object(doctor, "_scrub_t1_header", return_value=(True, "")), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor.time, "time", return_value=2_000_000_000.0), \
             patch.object(doctor.time, "sleep"):
            doctor.check_scrubber()
        self.assertNotIn("/old/path.mkv", saved["files"])
        self.assertIn("/fresh/path.mkv", saved["files"])


if __name__ == "__main__":
    unittest.main()
