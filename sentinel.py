#!/usr/bin/env python3
"""
arr-sentinel - auto-detect and fix recurring Sonarr/Radarr stack issues.

It watches your *arr download queues and clears the things that silently pile up
and stall a usenet/torrent media stack:

  * downloadClientUnavailable  - dead grabs the client rejected (orphans)
  * importBlocked / importFailed - completed downloads that won't import
  * importPending + warning     - incomplete/corrupt files ffprobe can't parse
  * failedPending / stalled     - failed or no-progress downloads

When an item has been stuck for MIN_STRIKES consecutive checks it is removed
(optionally blocklisted) so the *arr re-searches a different release.

Everything is configured by environment variables (see README). Runs as a
cron-style interval loop OR reacts to Sonarr/Radarr webhook events.

Pure Python standard library, no dependencies.
"""
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def _b(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

MODE          = os.environ.get("SENTINEL_MODE", "cron").strip().lower()       # cron | event
INTERVAL      = _i("SENTINEL_INTERVAL", 900)                                  # seconds between cron sweeps
MIN_STRIKES   = _i("SENTINEL_MIN_STRIKES", 2)                                 # consecutive checks before acting
MAX_ACTIONS   = _i("SENTINEL_MAX_ACTIONS", 20)                                # rate limit per sweep
DRY_RUN       = _b("SENTINEL_DRY_RUN", False)
BLOCKLIST     = _b("SENTINEL_BLOCKLIST", True)
REMOVE_CLIENT = _b("SENTINEL_REMOVE_FROM_CLIENT", True)
LOAD_MAX      = float(os.environ.get("SENTINEL_LOAD_MAX", "0") or 0)          # pause if 1-min load > this (0 = off)
STATE_FILE    = os.environ.get("SENTINEL_STATE_FILE", "/data/state.json")
PORT          = _i("SENTINEL_PORT", 8088)
HEALTH_REPORT = _b("SENTINEL_HEALTH_REPORT", True)
LOG_LEVEL     = os.environ.get("SENTINEL_LOG_LEVEL", "INFO").upper()
TIMEOUT       = _i("SENTINEL_HTTP_TIMEOUT", 60)

DEFAULT_CONDITIONS = "downloadClientUnavailable,importBlocked,importFailed,importPending_warning,failedPending,stalled"
ENABLED_CONDITIONS = [c.strip() for c in os.environ.get("SENTINEL_CONDITIONS", DEFAULT_CONDITIONS).split(",") if c.strip()]

# Sonarr/Radarr webhook eventTypes that trigger a sweep in event mode
TRIGGER_EVENTS = set(e.strip() for e in os.environ.get(
    "SENTINEL_TRIGGER_EVENTS", "Download,ManualInteractionRequired,DownloadFailed,Grab").split(",") if e.strip())

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("arr-sentinel")

# --------------------------------------------------------------------------- #
# stuck-condition predicates
# --------------------------------------------------------------------------- #

def _msgs(rec):
    out = []
    for sm in (rec.get("statusMessages") or []):
        out += [m for m in (sm.get("messages") or [])]
    if rec.get("errorMessage"):
        out.append(rec["errorMessage"])
    return out

CONDITIONS = {
    "downloadClientUnavailable": lambda r: r.get("status") == "downloadClientUnavailable",
    "importBlocked":            lambda r: r.get("trackedDownloadState") == "importBlocked",
    "importFailed":             lambda r: r.get("trackedDownloadState") == "importFailed",
    "importPending_warning":    lambda r: r.get("trackedDownloadState") == "importPending"
                                          and r.get("trackedDownloadStatus") in ("warning", "error"),
    "failedPending":            lambda r: r.get("trackedDownloadState") == "failedPending",
    "stalled":                  lambda r: r.get("trackedDownloadStatus") == "warning"
                                          and any("stall" in m.lower() or "no files" in m.lower()
                                                  for m in _msgs(r)),
}

def stuck_reason(rec):
    for name in ENABLED_CONDITIONS:
        pred = CONDITIONS.get(name)
        if pred and pred(rec):
            return name
    return None

# --------------------------------------------------------------------------- #
# *arr client
# --------------------------------------------------------------------------- #

class Arr:
    def __init__(self, name, kind, url, apikey):
        self.name = name
        self.kind = kind  # "sonarr" | "radarr"
        self.base = url.rstrip("/") + "/api/v3"
        self.apikey = apikey
        self.unknown = "includeUnknownSeriesItems=true" if kind == "sonarr" else "includeUnknownMovieItems=true"

    def _req(self, method, path, t=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=t or TIMEOUT)

    def queue(self):
        try:
            data = json.load(self._req("GET", "/queue?page=1&pageSize=1000&" + self.unknown))
            return data.get("records", [])
        except Exception as e:
            log.warning("[%s] queue fetch failed: %s", self.name, e)
            return None

    def health(self):
        try:
            return json.load(self._req("GET", "/health"))
        except Exception:
            return []

    def remove(self, item_id):
        q = "removeFromClient=%s&blocklist=%s" % (str(REMOVE_CLIENT).lower(), str(BLOCKLIST).lower())
        self._req("DELETE", "/queue/%d?%s" % (item_id, q))

# --------------------------------------------------------------------------- #
# instances from env (INSTANCE_<N>_URL / _APIKEY / _TYPE / _NAME)
# --------------------------------------------------------------------------- #

def load_instances():
    out = []
    for n in range(1, 51):
        url = os.environ.get("INSTANCE_%d_URL" % n)
        if not url:
            continue
        apikey = os.environ.get("INSTANCE_%d_APIKEY" % n, "")
        kind = os.environ.get("INSTANCE_%d_TYPE" % n, "").strip().lower()
        if kind not in ("sonarr", "radarr"):
            # infer from url/name if not given
            kind = "radarr" if "radarr" in url.lower() else "sonarr"
        name = os.environ.get("INSTANCE_%d_NAME" % n, "%s-%d" % (kind, n))
        if not apikey:
            log.warning("INSTANCE_%d has no APIKEY, skipping", n)
            continue
        out.append(Arr(name, kind, url, apikey))
    return out

# --------------------------------------------------------------------------- #
# strike state (persisted)
# --------------------------------------------------------------------------- #

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.debug("state save failed: %s", e)

# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #

def host_load():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0

_sweep_lock = threading.Lock()

def sweep(instances, only=None):
    if not _sweep_lock.acquire(blocking=False):
        log.debug("sweep already running, skipping")
        return
    try:
        if LOAD_MAX > 0:
            l = host_load()
            if l > LOAD_MAX:
                log.info("host load %.0f > %.0f -> skipping sweep (protecting the host)", l, LOAD_MAX)
                return
        state = load_state()
        actions = 0
        for arr in instances:
            if only and arr.name.lower() != only.lower():
                continue
            recs = arr.queue()
            if recs is None:
                continue
            strikes = state.get(arr.name, {})
            new_strikes = {}
            stuck_now = 0
            for r in recs:
                reason = stuck_reason(r)
                if not reason:
                    continue
                stuck_now += 1
                iid = str(r.get("id"))
                cnt = strikes.get(iid, 0) + 1
                new_strikes[iid] = cnt
                if cnt >= MIN_STRIKES and actions < MAX_ACTIONS:
                    title = (r.get("title") or "")[:70]
                    if DRY_RUN:
                        log.info("[%s] WOULD remove (%s, strike %d): %s", arr.name, reason, cnt, title)
                    else:
                        try:
                            arr.remove(r["id"])
                            log.info("[%s] removed (%s, blocklist=%s) -> re-search: %s",
                                     arr.name, reason, BLOCKLIST, title)
                            actions += 1
                            new_strikes.pop(iid, None)  # gone now
                        except Exception as e:
                            log.warning("[%s] remove failed for %s: %s", arr.name, title, e)
            state[arr.name] = new_strikes
            if stuck_now:
                log.debug("[%s] %d stuck item(s) tracked", arr.name, stuck_now)
            if HEALTH_REPORT:
                for h in arr.health():
                    if h.get("type") in ("error", "warning"):
                        log.debug("[%s] health %s: %s", arr.name, h.get("type"), (h.get("message") or "")[:90])
        save_state(state)
        if actions:
            log.info("sweep done: %d remediation(s) this pass", actions)
    finally:
        _sweep_lock.release()

# --------------------------------------------------------------------------- #
# event mode (webhook receiver)
# --------------------------------------------------------------------------- #

def make_handler(instances):
    class Handler(BaseHTTPRequestHandler):
        def _ok(self, code=200, body=b"ok"):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/health", "/healthz", "/"):
                self._ok(body=b"arr-sentinel ok")
            else:
                self._ok(404, b"not found")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            event = payload.get("eventType") or payload.get("EventType") or "?"
            inst = payload.get("instanceName") or payload.get("InstanceName")
            self._ok()  # ack fast
            if event == "Test":
                log.info("received webhook Test from %s", inst or "?")
                return
            if TRIGGER_EVENTS and event not in TRIGGER_EVENTS:
                log.debug("ignoring event %s", event)
                return
            log.info("event '%s' from %s -> sweep", event, inst or "all")
            threading.Thread(target=sweep, args=(instances,), kwargs={"only": inst}, daemon=True).start()

        def log_message(self, *a):
            pass
    return Handler

# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    instances = load_instances()
    if not instances:
        log.error("no instances configured. Set INSTANCE_1_URL / INSTANCE_1_APIKEY / INSTANCE_1_TYPE (see README).")
        sys.exit(2)
    log.info("arr-sentinel starting | mode=%s | instances=%s | conditions=%s | min_strikes=%d | dry_run=%s | blocklist=%s",
             MODE, ", ".join("%s(%s)" % (a.name, a.kind) for a in instances),
             ",".join(ENABLED_CONDITIONS), MIN_STRIKES, DRY_RUN, BLOCKLIST)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    if MODE == "event":
        # run an initial sweep, then serve webhooks; a slow cron fallback still runs
        sweep(instances)
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(instances))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("listening for Sonarr/Radarr webhooks on :%d (events: %s)", PORT, ", ".join(sorted(TRIGGER_EVENTS)))
        fallback = max(INTERVAL, 1800)
        while not stop.wait(fallback):
            sweep(instances)  # safety net in case a webhook is missed
        srv.shutdown()
    else:
        sweep(instances)
        while not stop.wait(INTERVAL):
            sweep(instances)
    log.info("arr-sentinel stopped")

if __name__ == "__main__":
    main()
