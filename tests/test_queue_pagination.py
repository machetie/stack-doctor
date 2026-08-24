"""Tests for queue pagination: a mass grab (>1 page) must not go invisible.

check_queue relied on a single pageSize=1000 fetch, so records beyond the first
page were a blind spot in the exact scenario the tool exists for. queue() now
loops pages until short/total/cap.
"""
import io
import json
import unittest
from unittest.mock import patch

import doctor


def _page(records, total):
    return io.BytesIO(json.dumps({"records": records, "totalRecords": total}).encode())


class QueuePaginationTest(unittest.TestCase):
    def setUp(self):
        self.arr = doctor.Arr("sonarr-1", "sonarr", "http://x", "key")

    def test_merges_two_pages(self):
        p1 = [{"id": i} for i in range(3)]
        p2 = [{"id": i} for i in range(3, 5)]
        pages = [_page(p1, 5), _page(p2, 5)]

        with patch.object(self.arr, "_req_retry", side_effect=lambda *a, **k: pages.pop(0)), \
             patch.object(doctor, "QUEUE_PAGE_SIZE", 3), \
             patch.object(doctor, "QUEUE_MAX_FETCH", 5000):
            recs = self.arr.queue()

        self.assertEqual(len(recs), 5)
        self.assertEqual([r["id"] for r in recs], [0, 1, 2, 3, 4])

    def test_single_short_page_stops(self):
        p1 = [{"id": 0}, {"id": 1}]
        calls = []

        def one_page(*a, **k):
            calls.append(1)
            return _page(p1, 2)

        with patch.object(self.arr, "_req_retry", side_effect=one_page), \
             patch.object(doctor, "QUEUE_PAGE_SIZE", 1000), \
             patch.object(doctor, "QUEUE_MAX_FETCH", 5000):
            recs = self.arr.queue()

        self.assertEqual(len(recs), 2)
        self.assertEqual(len(calls), 1)          # no needless second page

    def test_cap_halts_fetching(self):
        # every page is "full" and total is huge -> cap must stop the loop
        def full_page(*a, **k):
            return _page([{"id": 0}, {"id": 1}], total=1000)

        with patch.object(self.arr, "_req_retry", side_effect=full_page), \
             patch.object(doctor, "QUEUE_PAGE_SIZE", 2), \
             patch.object(doctor, "QUEUE_MAX_FETCH", 4):
            recs = self.arr.queue()

        # 2 pages of 2 == 4 records, then cap stops it
        self.assertEqual(len(recs), 4)

    def test_stops_at_total_records(self):
        p1 = [{"id": i} for i in range(2)]
        p2 = [{"id": i} for i in range(2, 4)]
        pages = [_page(p1, 4), _page(p2, 4)]
        calls = []

        def paged(*a, **k):
            calls.append(1)
            return pages.pop(0)

        with patch.object(self.arr, "_req_retry", side_effect=paged), \
             patch.object(doctor, "QUEUE_PAGE_SIZE", 2), \
             patch.object(doctor, "QUEUE_MAX_FETCH", 5000):
            recs = self.arr.queue()

        self.assertEqual(len(recs), 4)
        self.assertEqual(len(calls), 2)          # stopped at totalRecords, no 3rd page

    def test_prowlarr_returns_empty_without_fetch(self):
        pw = doctor.Arr("prowlarr-1", "prowlarr", "http://x", "key")
        with patch.object(pw, "_req_retry", side_effect=AssertionError("should not fetch")):
            self.assertEqual(pw.queue(), [])


if __name__ == "__main__":
    unittest.main()
