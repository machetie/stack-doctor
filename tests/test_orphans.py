"""Tests for the orphans check (debrid torrents no library symlink references).

The safety guards are the highest-value tests: a down mount, an empty/broken
symlink scan, or a too-high orphan ratio MUST abort the sweep and delete nothing.
"""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import doctor


def _resp(payload, status=200):
    b = json.dumps(payload).encode()
    r = MagicMock()
    r.status = status
    r.read.return_value = b
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


class DebridClientTest(unittest.TestCase):
    def test_rd_paginates_and_builds_name_map(self):
        def page(items):
            return [{"id": "%d" % i, "filename": f} for i, f in enumerate(items)]
        full = page(["X"] * 1000)               # full page -> more follow
        short = [{"id": "B1", "filename": "X"}, {"id": "B2", "filename": "Z"}]  # short -> stop
        db = doctor.Debrid("realdebrid", "k")
        with patch.object(doctor.time, "sleep"), \
             patch.object(db, "_get", side_effect=[full, short]):
            m = db.list_map()
        self.assertEqual(len(m["X"]), 1001)     # 1000 from page1 + 1 from page2
        self.assertEqual(m["Z"], ["B2"])

    def test_ad_uses_magnets(self):
        db = doctor.Debrid("alldebrid", "k")
        db._get = MagicMock(return_value={"status": "success",
                                          "data": {"magnets": [{"id": 1, "filename": "F"},
                                                               {"id": 2, "filename": "F"}]}})
        m = db.list_map()
        self.assertEqual(m["F"], [1, 2])

    def test_rd_delete_returns_true_on_204(self):
        db = doctor.Debrid("realdebrid", "k")
        with patch.object(doctor.urllib.request, "urlopen", return_value=_resp({}, status=204)):
            self.assertTrue(db.delete("A1"))

    def test_ad_delete_returns_true_on_success(self):
        db = doctor.Debrid("alldebrid", "k")
        db._get = MagicMock(return_value={"status": "success"})
        self.assertTrue(db.delete(1))

    def test_delete_returns_false_on_error(self):
        db = doctor.Debrid("realdebrid", "k")
        with patch.object(doctor.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(db.delete("A1"))

    def test_list_map_returns_empty_on_error(self):
        db = doctor.Debrid("realdebrid", "k")
        db._get = MagicMock(side_effect=OSError("boom"))
        self.assertEqual(db.list_map(), {})


class UsedSetTest(unittest.TestCase):
    def test_file_level_used_detection(self):
        # symlink targets like /mnt/zurg/__all__/<folder>/<file> -> folder is used
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub"))
            os.symlink("/mnt/zurg/__all__/UsedFolder/ep01.mkv", os.path.join(d, "sub", "l1"))
            os.symlink("/mnt/altmount/sonarr/Other/thing.mkv", os.path.join(d, "sub", "l2"))
            with patch.object(doctor, "ORPH_LINK_DIRS", [d]):
                used, total = doctor._orphans_used_set()
        self.assertIn("UsedFolder", used)
        self.assertEqual(total, 2)

    def test_non_zurg_targets_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            os.symlink("/mnt/altmount/sonarr/X/f.mkv", os.path.join(d, "l1"))
            with patch.object(doctor, "ORPH_LINK_DIRS", [d]), \
                 patch.object(doctor, "ORPH_MOUNT", "/mnt/zurg"):
                used, total = doctor._orphans_used_set()
        self.assertEqual(used, set())
        self.assertEqual(total, 1)

    def test_configurable_mount_prefix(self):
        # a symlink under a non-default ORPH_MOUNT must still be counted as used
        with tempfile.TemporaryDirectory() as d:
            os.symlink("/custom/mount/__all__/Folder/f.mkv", os.path.join(d, "l1"))
            with patch.object(doctor, "ORPH_LINK_DIRS", [d]), \
                 patch.object(doctor, "ORPH_MOUNT", "/custom/mount"):
                used, total = doctor._orphans_used_set()
        self.assertIn("Folder", used)

    def test_relative_target_is_normalized_and_matched(self):
        # ORPH_MOUNT is the tempdir; a relative symlink target resolves into it
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub"))
            os.symlink("../__all__/Folder/f.mkv", os.path.join(d, "sub", "rel.mkv"))
            with patch.object(doctor, "ORPH_LINK_DIRS", [os.path.join(d, "sub")]), \
                 patch.object(doctor, "ORPH_MOUNT", d):
                used, total = doctor._orphans_used_set()
        self.assertIn("Folder", used)
        self.assertEqual(total, 1)


class GuardAbortTest(unittest.TestCase):
    def _run(self, **kw):
        # default a sane live-ish config, then override
        defaults = dict(DRY_RUN=False, ORPH_LOAD_MAX=0, ORPH_LINK_DIRS=["/lib"],
                        ORPH_VIEWS=["realdebrid"], ORPH_MIN_LINKS=0, ORPH_MAX_RATIO=0.99,
                        ORPH_MIN_AGE=0)
        defaults.update(kw)
        with tempfile.TemporaryDirectory() as d:
            with patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")):
                patches = [patch.object(doctor, k, v) for k, v in defaults.items()]
                patches += [patch.object(doctor, "_mount_ok_for", return_value=True),
                            patch.object(doctor, "host_load", return_value=0.0),
                            patch.object(doctor, "_orphans_debrids", return_value=[]),
                            patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000))]
                with patch.object(doctor, "os").__enter__() if False else __import__("contextlib").nullcontext():
                    pass
                for p in patches:
                    p.start()
                try:
                    doctor.check_orphans()
                finally:
                    for p in patches:
                        p.stop()

    def test_aborts_when_used_set_below_floor(self):
        with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
             patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
             patch.object(doctor, "ORPH_MIN_LINKS", 500), \
             patch.object(doctor, "ORPH_LOAD_MAX", 0), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "host_load", return_value=0.0), \
             patch.object(doctor, "_orphans_debrids", return_value=[MagicMock()]), \
             patch.object(doctor, "_orphans_used_set", return_value=(set(), 3)), \
             patch.object(doctor, "metric_inc") as mi:
            doctor.check_orphans()
        self.assertTrue(any(c.kwargs.get("reason") == "floor" for c in mi.call_args_list))

    def test_aborts_when_mount_down(self):
        with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
             patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
             patch.object(doctor, "ORPH_MIN_LINKS", 0), \
             patch.object(doctor, "ORPH_LOAD_MAX", 0), \
             patch.object(doctor, "_mount_ok_for", return_value=False), \
             patch.object(doctor, "host_load", return_value=0.0), \
             patch.object(doctor, "_orphans_debrids", return_value=[MagicMock()]), \
             patch.object(doctor, "metric_inc") as mi:
            doctor.check_orphans()
        self.assertTrue(any(c.kwargs.get("reason") == "mount_down" for c in mi.call_args_list))

    def test_dry_run_deletes_nothing(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {"Orphan": ["id1"]}
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid")
            os.makedirs(view)
            os.makedirs(os.path.join(view, "Orphan"))
            os.utime(os.path.join(view, "Orphan"), (0, 0))  # old mtime
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", True), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000)):
                doctor.check_orphans()
        client.delete.assert_not_called()

    def test_respects_max_deletes_cap(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {f"Orphan{i}": [f"id{i}"] for i in range(10)}
        client.delete.return_value = True
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            for i in range(10):
                p = os.path.join(view, f"Orphan{i}"); os.makedirs(p); os.utime(p, (0, 0))
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_MAX_DEL", 3), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000)), \
                 patch.object(doctor.time, "sleep"):
                doctor.check_orphans()
        self.assertEqual(client.delete.call_count, 3)

    def test_min_age_skips_recent(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {"Orphan": ["id1"]}
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            p = os.path.join(view, "Orphan"); os.makedirs(p)  # fresh mtime
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 720), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000)):
                doctor.check_orphans()
        client.delete.assert_not_called()

    def test_ratio_ceiling_skips_view(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {"Orphan1": ["id1"]}
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            for i in range(10):
                p = os.path.join(view, f"Orphan{i}"); os.makedirs(p); os.utime(p, (0, 0))
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 0.2), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000)), \
                 patch.object(doctor, "metric_inc") as mi:
                doctor.check_orphans()
        self.assertTrue(any(c.kwargs.get("reason") == "ratio" for c in mi.call_args_list))
        client.delete.assert_not_called()

    def test_bad_view_exempt_from_ratio_ceiling(self):
        # __bad__ is decypharr's small bad-marked list: high orphan ratio must NOT
        # trip the ratio guard (but the `used` + age checks still apply per folder).
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {"BadOrphan": ["id1"], "BadUsed": ["id2"]}
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "__bad__"); os.makedirs(view)
            for n in ("BadOrphan", "BadUsed"):
                p = os.path.join(view, n); os.makedirs(p); os.utime(p, (0, 0))
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 0.01), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", True), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=({"BadUsed"}, 1000)), \
                 patch.object(doctor.time, "sleep"):
                doctor.check_orphans()
        # BadOrphan (unused) deleted; BadUsed skipped (still referenced). Ratio 0.01 did not abort.
        client.delete.assert_called_once_with("id1")


class CorrectnessTest(unittest.TestCase):
    def test_folder_with_used_file_is_not_orphan(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {"Used": ["id1"], "Orphan": ["id2"]}
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            for n in ("Used", "Orphan"):
                p = os.path.join(view, n); os.makedirs(p); os.utime(p, (0, 0))
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=({"Used"}, 1000)), \
                 patch.object(doctor.time, "sleep"):
                doctor.check_orphans()
        # only the truly-orphaned folder's id was deleted
        client.delete.assert_called_once_with("id2")

    def test_unmatched_folder_skipped_not_deleted(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {}  # no name match
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            p = os.path.join(view, "Orphan"); os.makedirs(p); os.utime(p, (0, 0))
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000)):
                doctor.check_orphans()
        client.delete.assert_not_called()

    def test_duplicate_ids_all_deleted_and_recorded(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {"Orphan": ["id1", "id2", "id3"]}
        client.delete.return_value = True
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            p = os.path.join(view, "Orphan"); os.makedirs(p); os.utime(p, (0, 0))
            state_path = os.path.join(d, "state.json")
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_MAX_DEL", 50), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", state_path), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000)), \
                 patch.object(doctor.time, "sleep"):
                doctor.check_orphans()
            self.assertEqual(client.delete.call_count, 3)
            state = json.load(open(state_path))
            self.assertEqual(len(state["deleted"]), 3)
            self.assertEqual({r["id"] for r in state["deleted"]}, {"id1", "id2", "id3"})


class DebridKeyResolutionTest(unittest.TestCase):
    def test_explicit_env_keys_win(self):
        with patch.object(doctor, "ORPH_RD_KEY", "rdkey"), \
             patch.object(doctor, "ORPH_AD_KEYS", ["ad1", "ad2"]), \
             patch.object(doctor, "ORPH_DECY_CFG", "/nonexistent"):
            ds = doctor._orphans_debrids()
        self.assertEqual([d.provider for d in ds], ["realdebrid", "alldebrid", "alldebrid2"])
        self.assertEqual(ds[0].key, "rdkey")

    def test_reads_decypharr_config_fallback(self):
        cfg = {"debrids": [
            {"provider": "realdebrid", "api_key": "rd"},
            {"provider": "alldebrid", "api_key": "ad1"},
            {"provider": "alldebrid", "api_key": "ad2"},
        ]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f); path = f.name
        try:
            with patch.object(doctor, "ORPH_RD_KEY", ""), \
                 patch.object(doctor, "ORPH_AD_KEYS", []), \
                 patch.object(doctor, "ORPH_DECY_CFG", path):
                ds = doctor._orphans_debrids()
            self.assertEqual([d.provider for d in ds], ["realdebrid", "alldebrid", "alldebrid2"])
        finally:
            os.unlink(path)

    def test_no_keys_yields_empty(self):
        with patch.object(doctor, "ORPH_RD_KEY", ""), \
             patch.object(doctor, "ORPH_AD_KEYS", []), \
             patch.object(doctor, "ORPH_DECY_CFG", "/nonexistent"):
            self.assertEqual(doctor._orphans_debrids(), [])


class OrphansTocTouTest(unittest.TestCase):
    def test_toctou_refresh_skips_newly_referenced_folder(self):
        # first scan: folder orphaned; refresh (forced) shows it referenced -> skip delete
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {"Orphan": ["id1"]}
        used_calls = [set(), {"Orphan"}]

        def used_fn():
            return (used_calls.pop(0), 1000)

        clock = [0.0]

        def fake_time():
            clock[0] += 1.0
            return clock[0]

        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            p = os.path.join(view, "Orphan"); os.makedirs(p); os.utime(p, (0, 0))
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "ORPH_RESCAN_SECONDS", 0), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", side_effect=used_fn), \
                 patch.object(doctor.time, "time", side_effect=fake_time), \
                 patch.object(doctor.time, "sleep"):
                doctor.check_orphans()
        client.delete.assert_not_called()


class OrphansPruneTest(unittest.TestCase):
    def test_old_cooldown_and_unmatched_are_pruned(self):
        client = MagicMock()
        client.provider = "realdebrid"
        client.list_map.return_value = {}  # -> candidate goes to unmatched
        state = {"cooldown": {"old": 0, "fresh": 9_999_999_999},
                 "unmatched": {"old_u": 0, "fresh_u": 9_999_999_999}}
        captured = {}

        def save(s):
            captured.update(s)

        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "realdebrid"); os.makedirs(view)
            p = os.path.join(view, "Orphan"); os.makedirs(p); os.utime(p, (0, 0))
            with patch.object(doctor, "ORPH_LINK_DIRS", ["/lib"]), \
                 patch.object(doctor, "ORPH_VIEWS", ["realdebrid"]), \
                 patch.object(doctor, "ORPH_MOUNT", d), \
                 patch.object(doctor, "ORPH_MIN_LINKS", 0), \
                 patch.object(doctor, "ORPH_MIN_AGE", 0), \
                 patch.object(doctor, "ORPH_MAX_RATIO", 1.0), \
                 patch.object(doctor, "ORPH_LOAD_MAX", 0), \
                 patch.object(doctor, "ORPH_INC_BAD", False), \
                 patch.object(doctor, "ORPH_STATE", os.path.join(d, "state.json")), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_mount_ok_for", return_value=True), \
                 patch.object(doctor, "host_load", return_value=0.0), \
                 patch.object(doctor, "_orphans_debrids", return_value=[client]), \
                 patch.object(doctor, "_orphans_used_set", return_value=(set(), 1000)), \
                 patch.object(doctor, "_orphans_load_state", return_value=state), \
                 patch.object(doctor, "_orphans_save_state", side_effect=save), \
                 patch.object(doctor.time, "time", return_value=2_000_000_000.0), \
                 patch.object(doctor.time, "sleep"):
                doctor.check_orphans()
        self.assertNotIn("old", captured["cooldown"])
        self.assertIn("fresh", captured["cooldown"])
        self.assertNotIn("old_u", captured["unmatched"])
        self.assertIn("fresh_u", captured["unmatched"])


if __name__ == "__main__":
    unittest.main()
