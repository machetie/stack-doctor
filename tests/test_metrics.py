"""Tests for the Prometheus /metrics registry and exposition rendering."""
import unittest
from unittest.mock import MagicMock, patch

import doctor


class MetricsRegistryTest(unittest.TestCase):
    def setUp(self):
        # isolate each test from the shared module-level registry
        self._c = dict(doctor._metrics_counters)
        self._g = dict(doctor._metrics_gauges)
        doctor._metrics_counters.clear()
        doctor._metrics_gauges.clear()

    def tearDown(self):
        doctor._metrics_counters.clear()
        doctor._metrics_counters.update(self._c)
        doctor._metrics_gauges.clear()
        doctor._metrics_gauges.update(self._g)

    def test_counter_accumulates(self):
        doctor.metric_inc("stackdoctor_sweep_total")
        doctor.metric_inc("stackdoctor_sweep_total")
        doctor.metric_inc("stackdoctor_sweep_total", 3)
        out = doctor._metrics_render()
        self.assertIn("stackdoctor_sweep_total 5", out)

    def test_counter_labels_are_distinct_series(self):
        doctor.metric_inc("stackdoctor_queue_actions_total", action="clear", instance="sonarr-1")
        doctor.metric_inc("stackdoctor_queue_actions_total", action="research", instance="sonarr-1")
        doctor.metric_inc("stackdoctor_queue_actions_total", action="clear", instance="sonarr-1")
        out = doctor._metrics_render()
        self.assertIn('stackdoctor_queue_actions_total{action="clear",instance="sonarr-1"} 2', out)
        self.assertIn('stackdoctor_queue_actions_total{action="research",instance="sonarr-1"} 1', out)

    def test_gauge_is_set_not_accumulated(self):
        doctor.metric_set("stackdoctor_mount_up", 1, mount="/mnt/zurg")
        doctor.metric_set("stackdoctor_mount_up", 0, mount="/mnt/zurg")
        out = doctor._metrics_render()
        self.assertIn('stackdoctor_mount_up{mount="/mnt/zurg"} 0', out)
        self.assertNotIn('stackdoctor_mount_up{mount="/mnt/zurg"} 1', out)

    def test_type_lines_emitted_once_per_metric(self):
        doctor.metric_inc("stackdoctor_scrubber_files_total", 1, result="ok")
        doctor.metric_inc("stackdoctor_scrubber_files_total", 1, result="bad")
        out = doctor._metrics_render()
        self.assertEqual(out.count("# TYPE stackdoctor_scrubber_files_total counter"), 1)

    def test_integers_render_without_trailing_dot_zero(self):
        doctor.metric_inc("stackdoctor_sweep_total", 2)
        out = doctor._metrics_render()
        self.assertIn("stackdoctor_sweep_total 2", out)
        self.assertNotIn("2.0", out)

    def test_label_values_are_escaped(self):
        doctor.metric_set("stackdoctor_mount_up", 1, mount='a"b')
        out = doctor._metrics_render()
        self.assertIn('mount="a\\"b"', out)

    def test_empty_registry_renders_empty(self):
        self.assertEqual(doctor._metrics_render(), "")


class MetricsEndpointTest(unittest.TestCase):
    """Exercise the do_GET routing logic for /metrics without a live socket."""

    def _handler(self):
        # build a handler class without running __init__ (which needs a socket)
        srv = doctor._build_server(0)
        H = type(srv.RequestHandlerClass.__name__, (srv.RequestHandlerClass,), {})
        srv.server_close()
        inst = H.__new__(H)
        inst.sent = []

        def _send(code, ctype, body):
            inst.sent.append((code, ctype, body))
        inst._send = _send
        return inst

    def test_metrics_served_when_enabled(self):
        h = self._handler()
        h.path = "/metrics"
        h.headers = {}
        with patch.object(doctor, "EN_METRICS", True), \
             patch.object(doctor, "UI_TOKEN", ""):
            h.do_GET()
        code, ctype, _ = h.sent[-1]
        self.assertEqual(code, 200)
        self.assertIn("text/plain", ctype)

    def test_metrics_404_when_disabled(self):
        h = self._handler()
        h.path = "/metrics"
        h.headers = {}
        with patch.object(doctor, "EN_METRICS", False):
            h.do_GET()
        code, _, _ = h.sent[-1]
        self.assertEqual(code, 404)

    def test_metrics_requires_token_when_set(self):
        h = self._handler()
        h.path = "/metrics"
        h.headers = {}
        with patch.object(doctor, "EN_METRICS", True), \
             patch.object(doctor, "UI_TOKEN", "secret"):
            h.do_GET()
        code, _, _ = h.sent[-1]
        self.assertEqual(code, 401)

    def test_metrics_served_with_valid_token(self):
        h = self._handler()
        h.path = "/metrics?token=secret"
        h.headers = {}
        with patch.object(doctor, "EN_METRICS", True), \
             patch.object(doctor, "UI_TOKEN", "secret"):
            h.do_GET()
        code, _, _ = h.sent[-1]
        self.assertEqual(code, 200)


class PostBodyCapTest(unittest.TestCase):
    """A huge Content-Length must be rejected with 413 before reading the body."""

    def _handler(self):
        srv = doctor._build_server(0)
        H = type(srv.RequestHandlerClass.__name__, (srv.RequestHandlerClass,), {})
        srv.server_close()
        inst = H.__new__(H)
        inst.sent = []
        inst._send = lambda code, ctype, body: inst.sent.append((code, ctype, body))
        return inst

    def test_oversized_content_length_returns_413(self):
        h = self._handler()
        h.path = "/api/config"
        h.headers = {"Content-Length": str(doctor.MAX_POST + 1)}
        h.rfile = MagicMock()
        h.do_POST()
        self.assertEqual(h.sent[0][0], 413)
        h.rfile.read.assert_not_called()

    def test_small_body_is_read(self):
        h = self._handler()
        h.path = "/api/scout/clear"
        h.headers = {"Content-Length": "2"}
        h.rfile = MagicMock()
        h.rfile.read.return_value = b"{}"
        with patch.object(doctor, "EN_UI", False):
            h.do_POST()
        h.rfile.read.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
