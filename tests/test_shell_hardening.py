"""Tests for shell-command hardening: secret masking in logs, empty-cmd
handling, and startup validation of configured commands."""
import unittest
from unittest.mock import MagicMock, patch

import doctor


class MaskCmdTest(unittest.TestCase):
    def test_plain_command_unchanged(self):
        self.assertEqual(doctor._mask_cmd("systemctl restart altmount"),
                         "systemctl restart altmount")

    def test_masks_apikey_assignment(self):
        self.assertEqual(doctor._mask_cmd("APIKEY=sekret run.sh"), "APIKEY=*** run.sh")

    def test_masks_header_token(self):
        out = doctor._mask_cmd('curl -H "X-Api-Key: abc123secret" http://x')
        self.assertNotIn("abc123secret", out)
        self.assertIn("***", out)

    def test_masks_flag_token(self):
        out = doctor._mask_cmd("tool --token=deadbeef --url http://x")
        self.assertNotIn("deadbeef", out)
        self.assertIn("--url http://x", out)

    def test_masks_password(self):
        out = doctor._mask_cmd("mysql --password=hunter2 -e 'select 1'")
        self.assertNotIn("hunter2", out)

    def test_empty_is_passthrough(self):
        self.assertEqual(doctor._mask_cmd(""), "")
        self.assertIsNone(doctor._mask_cmd(None))


class RunCmdTest(unittest.TestCase):
    def test_run_cmd_masks_secrets_in_debug_log(self):
        with patch.object(doctor.log, "debug") as dbg, \
             patch.object(doctor.subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")):
            doctor.run_cmd("tool --token=deadbeef")
        logged = " ".join(str(c.args) for c in dbg.call_args_list)
        self.assertIn("***", logged)
        self.assertNotIn("deadbeef", logged)

    def test_run_cmd_empty_returns_none(self):
        with patch.object(doctor.subprocess, "run") as sr:
            self.assertIsNone(doctor.run_cmd(""))
        sr.assert_not_called()

    def test_run_output_empty_returns_empty_without_subprocess(self):
        with patch.object(doctor.subprocess, "run") as sr:
            self.assertEqual(doctor.run_output(""), "")
        sr.assert_not_called()


class ValidateShellCommandsTest(unittest.TestCase):
    def test_warns_on_unbalanced_quotes(self):
        with patch.object(doctor, "DECY_RESTART_CMD", 'echo "oops'), \
             patch.object(doctor, "ALT_RESTART_CMD", ""), \
             patch.object(doctor, "ALT_PROP_FIX_CMD", ""), \
             patch.object(doctor, "JAN_LOG_CMD", ""), \
             patch.object(doctor, "META_FAILED_CMD", ""), \
             patch.object(doctor, "META_STORM_CMD", ""), \
             patch.object(doctor, "WARM_PLEXLOG_CMD", ""), \
             patch.object(doctor.log, "warning") as warn:
            doctor._validate_shell_commands()
        self.assertTrue(any("unbalanced" in str(c.args) for c in warn.call_args_list))

    def test_no_warning_for_balanced_or_empty(self):
        with patch.object(doctor, "DECY_RESTART_CMD", 'systemctl restart x'), \
             patch.object(doctor, "ALT_RESTART_CMD", ""), \
             patch.object(doctor, "ALT_PROP_FIX_CMD", ""), \
             patch.object(doctor, "JAN_LOG_CMD", ""), \
             patch.object(doctor, "META_FAILED_CMD", ""), \
             patch.object(doctor, "META_STORM_CMD", ""), \
             patch.object(doctor, "WARM_PLEXLOG_CMD", ""), \
             patch.object(doctor.log, "warning") as warn:
            doctor._validate_shell_commands()
        self.assertFalse(any("unbalanced" in str(c.args) for c in warn.call_args_list))


if __name__ == "__main__":
    unittest.main()
