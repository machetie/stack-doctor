"""Tests for the per-sweep structured summary INFO line and error counting."""
import unittest
from unittest.mock import patch

import doctor


class SweepSummaryTest(unittest.TestCase):
    def _run_with_checks(self, checks):
        with patch.object(doctor, "CHECKS", checks), \
             patch.object(doctor.log, "info") as info, \
             patch.object(doctor, "metric_inc"):
            doctor.sweep()
        return info

    def test_summary_line_emitted(self):
        checks = [("queue", True, lambda only=None: None),
                  ("plex", True, lambda: None)]
        info = self._run_with_checks(checks)
        summaries = [c for c in info.call_args_list if "sweep done" in str(c.args)]
        self.assertEqual(len(summaries), 1)
        line = summaries[0].args[0] % summaries[0].args[1:]
        self.assertIn("checks=2", line)
        self.assertIn("errors=0", line)

    def test_summary_counts_errors_and_names_failing_check(self):
        def boom():
            raise RuntimeError("nope")

        checks = [("queue", True, lambda only=None: None),
                  ("plex", True, boom)]
        with patch.object(doctor, "CHECKS", checks), \
             patch.object(doctor.log, "info") as info, \
             patch.object(doctor.log, "error"), \
             patch.object(doctor, "metric_inc"):
            doctor.sweep()
        summaries = [c for c in info.call_args_list if "sweep done" in str(c.args)]
        self.assertEqual(len(summaries), 1)
        line = summaries[0].args[0] % summaries[0].args[1:]
        self.assertIn("checks=2", line)
        self.assertIn("errors=1", line)
        self.assertIn("plex", line)

    def test_disabled_checks_not_counted(self):
        checks = [("queue", True, lambda only=None: None),
                  ("plex", False, lambda: None)]
        info = self._run_with_checks(checks)
        summaries = [c for c in info.call_args_list if "sweep done" in str(c.args)]
        line = summaries[0].args[0] % summaries[0].args[1:]
        self.assertIn("checks=1", line)


if __name__ == "__main__":
    unittest.main()
