"""Tests for durable, atomic state-file writes (_atomic_write_json).

A crash mid-write must never truncate the previous good file: readers see either
the old full contents or the new full contents, never a half-written one. All 10
state writers must route through the atomic helper (no bare open(..., "w")).
"""
import inspect
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import doctor


class AtomicWriteJsonTest(unittest.TestCase):
    def test_atomic_write_replaces_content(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            doctor._atomic_write_json(path, {"a": 1})
            self.assertEqual(json.load(open(path)), {"a": 1})
            doctor._atomic_write_json(path, {"b": 2})
            self.assertEqual(json.load(open(path)), {"b": 2})

    def test_crash_mid_write_leaves_old_file_intact(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            # seed a good file
            doctor._atomic_write_json(path, {"good": True})
            original = open(path, "rb").read()

            # patch json.dump to raise mid-call (simulate SIGKILL / OOM)
            with patch.object(doctor.json, "dump", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    doctor._atomic_write_json(path, {"bad": True})

            # the original file is untouched and still parses
            self.assertEqual(open(path, "rb").read(), original)
            self.assertEqual(json.load(open(path)), {"good": True})

    def test_crash_leaves_no_temp_files_behind(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with patch.object(doctor.json, "dump", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    doctor._atomic_write_json(path, {"x": 1})
            leftovers = [f for f in os.listdir(d) if f.startswith(".tmp-")]
            self.assertEqual(leftovers, [])

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "deeper", "state.json")
            doctor._atomic_write_json(path, {"ok": 1})
            self.assertTrue(os.path.exists(path))
            self.assertEqual(json.load(open(path)), {"ok": 1})

    def test_indent_is_passed_through(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            doctor._atomic_write_json(path, {"a": 1, "b": 2}, indent=1)
            text = open(path).read()
            self.assertIn("\n", text)  # indent produces multiline output


class StateWritersUseAtomicHelperTest(unittest.TestCase):
    """No state writer may use the truncate-then-write pattern directly."""

    SAVE_FUNCS = [
        "_save_state",
        "_scrub_save_state",
        "_wl_save_state",
        "_hol_save_state",
        "_backlog_save_state",
        "_repair_save_state",
        "_missing_disk_save_state",
        "_riven_save_state",
        "_scout_save",
        "_config_write",
        "_ui_save",
    ]

    def test_no_save_func_uses_truncate_then_write(self):
        for name in self.SAVE_FUNCS:
            with self.subTest(func=name):
                src = inspect.getsource(getattr(doctor, name))
                self.assertNotIn(
                    'json.dump(', src,
                    "%s should not call json.dump directly (truncate-then-write "
                    "risk); route through _atomic_write_json" % name,
                )

    def test_each_save_func_routes_through_atomic_helper(self):
        for name in self.SAVE_FUNCS:
            with self.subTest(func=name):
                src = inspect.getsource(getattr(doctor, name))
                self.assertIn(
                    "_atomic_write_json", src,
                    "%s should call _atomic_write_json" % name,
                )


class SaveStateDurabilityIntegrationTest(unittest.TestCase):
    def test_save_state_survives_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with patch.object(doctor, "STATE_FILE", path):
                doctor._save_state({"queue": [1, 2, 3]})
                self.assertEqual(json.load(open(path)), {"queue": [1, 2, 3]})
                original = open(path, "rb").read()
                # _save_state swallows exceptions; assert file stays intact
                with patch.object(doctor.json, "dump", side_effect=OSError("boom")):
                    doctor._save_state({"queue": []})
                self.assertEqual(open(path, "rb").read(), original)


class StateUpdateSerializationTest(unittest.TestCase):
    """_state_update must serialize concurrent load->mutate->save so no update
    is lost when many threads race (the event-mode webhook scenario)."""

    def test_no_lost_updates_under_concurrency(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            doctor._atomic_write_json(path, {"count": 0})

            def load():
                return json.load(open(path))

            def save(s):
                doctor._atomic_write_json(path, s)

            def mutate(s):
                # widen the race window so an unlocked impl would lose updates
                cur = s.get("count", 0)
                time.sleep(0.001)
                s["count"] = cur + 1

            N = 25

            def worker():
                doctor._state_update(load, mutate, save)

            threads = [threading.Thread(target=worker) for _ in range(N)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(json.load(open(path)), {"count": N})

    def test_state_update_returns_mutate_result(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            doctor._atomic_write_json(path, {"n": 5})
            out = doctor._state_update(
                lambda: json.load(open(path)),
                lambda s: s.get("n", 0) * 2,
                lambda s: doctor._atomic_write_json(path, s),
            )
            self.assertEqual(out, 10)

    def test_state_lock_is_reentrant(self):
        # _state_update nested inside a held _state_lock must not deadlock
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            doctor._atomic_write_json(path, {"n": 0})
            with doctor._state_lock:
                doctor._state_update(
                    lambda: json.load(open(path)),
                    lambda s: s.__setitem__("n", 1),
                    lambda s: doctor._atomic_write_json(path, s),
                )
            self.assertEqual(json.load(open(path)), {"n": 1})


if __name__ == "__main__":
    unittest.main()
