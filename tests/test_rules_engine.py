"""Regression tests for the placeholder KEEP-REAL rules engine.

Pins compute_keep_real (rules A-F + unaired) so park/prefetch changes cannot silently
change what gets kept real vs parked. Baseline fixtures use OLD air dates (not fresh)
unless a test is explicitly exercising the "fresh" rule.
"""
import datetime
import unittest

import rules_engine as re


def ep(season, episode, air=None, has_file=False, monitored=False):
    return {
        "seasonNumber": season, "episodeNumber": episode,
        "airDateUtc": air, "hasFile": has_file, "monitored": monitored,
    }


NOW = datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc)
OLD = "2020-01-05T00:00:00Z"          # aired long ago -> not fresh, not unaired
FRESH = "2026-09-05T00:00:00Z"        # 2 days ago -> fresh
UNAIRED = "2026-12-01T00:00:00Z"      # future -> unaired


def iso(day, month=9, year=2026):
    return "%04d-%02d-%02dT00:00:00Z" % (year, month, day)


class ParseAirDateTest(unittest.TestCase):
    def test_valid_z_suffix(self):
        d = re.parse_air_date("2026-09-07T01:02:03Z")
        self.assertEqual((d.year, d.month, d.day), (2026, 9, 7))

    def test_none_and_invalid(self):
        self.assertIsNone(re.parse_air_date(None))
        self.assertIsNone(re.parse_air_date(""))
        self.assertIsNone(re.parse_air_date("not-a-date"))


class UnairedTest(unittest.TestCase):
    def test_future_is_unaired(self):
        self.assertTrue(re.is_unaired(ep(1, 1, air=iso(10)), NOW))

    def test_past_is_aired(self):
        self.assertFalse(re.is_unaired(ep(1, 1, air=OLD), NOW))

    def test_no_airdate_is_unaired(self):
        self.assertTrue(re.is_unaired(ep(1, 1, air=None), NOW))


class FreshTest(unittest.TestCase):
    def test_within_window(self):
        self.assertTrue(re.is_fresh(ep(1, 1, air=FRESH), NOW, 30))

    def test_outside_window(self):
        self.assertFalse(re.is_fresh(ep(1, 1, air=OLD), NOW, 30))


class KeepRealTest(unittest.TestCase):
    def _series(self, status="ended"):
        return {"status": status}

    def _eps(self, season, count, air=OLD):
        return [ep(season, i, air=air) for i in range(1, count + 1)]

    def test_rule_A_entry_episodes_kept(self):
        eps = self._eps(1, 8)                          # all old/aired
        res = re.compute_keep_real(self._series(), eps, [], [], NOW)
        self.assertIn((1, 1), res["keep"])
        self.assertIn((1, 2), res["keep"])             # ENTRY_EPS default = 2
        self.assertNotIn((1, 3), res["keep"])
        self.assertEqual(res["reasons"][(1, 1)], "entry")

    def test_rule_C_premieres_kept(self):
        eps = self._eps(1, 4) + self._eps(2, 4)
        res = re.compute_keep_real(self._series(), eps, [], [], NOW)
        self.assertIn((2, 1), res["keep"])
        self.assertEqual(res["reasons"][(2, 1)], "premiere")
        self.assertNotIn((2, 2), res["keep"])

    def test_rule_D_unaired_kept(self):
        eps = [ep(1, 1, air=OLD), ep(1, 2, air=UNAIRED)]
        res = re.compute_keep_real(self._series(), eps, [], [], NOW)
        self.assertIn((1, 2), res["keep"])
        self.assertEqual(res["reasons"][(1, 2)], "unaired")

    def test_rule_E_fresh_aired_kept(self):
        eps = [ep(1, 1, air=OLD), ep(1, 2, air=FRESH)]
        res = re.compute_keep_real(self._series(), eps, [], [], NOW)
        self.assertIn((1, 2), res["keep"])
        self.assertEqual(res["reasons"][(1, 2)], "fresh")

    def test_rule_F_newest_not_applied_to_ended(self):
        eps = self._eps(1, 10)                         # all old, ended
        res = re.compute_keep_real(self._series("ended"), eps, [], [], NOW)
        self.assertNotIn((1, 10), res["keep"])

    def test_rule_F_newest_kept_for_continuing(self):
        # air dates ascending -> E10 is newest; newest 3 = E08,E09,E10
        eps = [ep(1, i, air=iso(i, month=1)) for i in range(1, 11)]
        res = re.compute_keep_real(self._series("continuing"), eps, [], [], NOW)
        for i in (8, 9, 10):
            self.assertIn((1, i), res["keep"], "newest ep %d should be kept" % i)
        self.assertEqual(res["reasons"][(1, 10)], "newest")

    def test_rule_B_resume_window(self):
        eps = self._eps(1, 10)
        ts = NOW.timestamp()
        history = [{"user": "alice", "parent_media_index": 1, "media_index": 5, "date": ts}]
        res = re.compute_keep_real(self._series(), eps, [], history, NOW)
        self.assertTrue(res["active"])
        self.assertFalse(res["stale"])
        self.assertFalse(res["abandoned"])
        self.assertIn((1, 5), res["keep"])
        self.assertIn((1, 8), res["keep"])             # PREFETCH_AHEAD=3
        self.assertNotIn((1, 9), res["keep"])
        self.assertTrue(res["reasons"][(1, 6)].startswith("resume:"))

    def test_stale_user_ignored(self):
        eps = self._eps(1, 10)
        old = NOW.timestamp() - (200 * 86400)          # > RESUME_KEEP_DAYS (120)
        history = [{"user": "alice", "parent_media_index": 1, "media_index": 5, "date": old}]
        res = re.compute_keep_real(self._series(), eps, [], history, NOW)
        self.assertFalse(res["active"])
        self.assertTrue(res["stale"])
        self.assertNotIn((1, 5), res["keep"])

    def test_abandoned_with_no_history(self):
        res = re.compute_keep_real(self._series(), self._eps(1, 5), [], [], NOW)
        self.assertTrue(res["abandoned"])
        self.assertFalse(res["active"])


if __name__ == "__main__":
    unittest.main()
