#!/usr/bin/env python3
"""
stack-doctor - auto-detect and fix recurring issues across a Sonarr/Radarr +
decypharr + Plex media stack.

Modular checks, each toggled and configured by environment variables:

  queue      *arr download queues       - clear stuck/dead/blocked items -> re-search
  decypharr  decypharr mount + API      - detect a hung FUSE mount -> run a restart hook
  plex       Plex Media Server          - detect unresponsive Plex (+ optional library scan)
  resources  host load / memory / swap  - report pressure, optional drop_caches relief
  janitor    usenet dead files          - quarantine library symlinks for permanently-dead
                                           releases (reversible) from a decypharr log file

Runs as a cron-style interval loop OR reacts to Sonarr/Radarr webhook events.
Pure Python standard library, no dependencies.
"""
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #

def _b(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

MODE        = os.environ.get("DOCTOR_MODE", "cron").strip().lower()   # cron | event
INTERVAL    = _i("DOCTOR_INTERVAL", 900)
PORT        = _i("DOCTOR_PORT", 8088)
LOG_LEVEL   = os.environ.get("DOCTOR_LOG_LEVEL", "INFO").upper()
LOG_FILE    = os.environ.get("DOCTOR_LOG_FILE", "")
TIMEOUT     = _i("DOCTOR_HTTP_TIMEOUT", 60)
DRY_RUN     = _b("DOCTOR_DRY_RUN", False)

# which checks are on
EN_QUEUE     = _b("ENABLE_QUEUE", True)
EN_DECYPHARR = _b("ENABLE_DECYPHARR", False)
EN_PLEX      = _b("ENABLE_PLEX", False)
EN_RESOURCES = _b("ENABLE_RESOURCES", False)
EN_JANITOR   = _b("ENABLE_JANITOR", False)
EN_PROVIDERS = _b("ENABLE_PROVIDERS", False)   # auto-test failed indexers/download clients (sonarr/radarr/prowlarr)
EN_BAZARR    = _b("ENABLE_BAZARR", False)      # Bazarr reachability

BAZARR_URL    = os.environ.get("BAZARR_URL", "")
BAZARR_APIKEY = os.environ.get("BAZARR_APIKEY", "")

# queue check
MIN_STRIKES   = _i("DOCTOR_MIN_STRIKES", 2)
MAX_ACTIONS   = _i("DOCTOR_MAX_ACTIONS", 20)
BLOCKLIST     = _b("DOCTOR_BLOCKLIST", True)
REMOVE_CLIENT = _b("DOCTOR_REMOVE_FROM_CLIENT", True)
STATE_FILE    = os.environ.get("DOCTOR_STATE_FILE", "/data/state.json")
DEFAULT_CONDITIONS = "downloadClientUnavailable,importBlocked,importFailed,importPending_warning,failedPending,stalled"
ENABLED_CONDITIONS = [c.strip() for c in os.environ.get("DOCTOR_CONDITIONS", DEFAULT_CONDITIONS).split(",") if c.strip()]

# resource thresholds (host load uses /proc/loadavg if mounted)
LOAD_MAX        = _f("DOCTOR_LOAD_MAX", 0)         # queue check pauses above this (0=off)
RES_LOAD_WARN   = _f("RES_LOAD_WARN", 40)
RES_SWAP_WARN   = _i("RES_SWAP_WARN_MB", 7000)
RES_MEM_MIN     = _i("RES_MEM_MIN_MB", 800)
RES_DROP_CACHES = _b("RES_DROP_CACHES", False)       # echo 1 > drop_caches on memory pressure (needs privilege)

# decypharr
DECY_URL          = os.environ.get("DECYPHARR_URL", "")             # e.g. http://192.168.50.202:8282
DECY_MOUNT_TEST   = os.environ.get("DECYPHARR_MOUNT_TEST", "")      # a dir on the FUSE mount to read-test
DECY_READ_TIMEOUT = _i("DECYPHARR_READ_TIMEOUT", 25)
DECY_RESTART_CMD  = os.environ.get("DECYPHARR_RESTART_CMD", "")     # shell cmd to recover a hung mount

# plex
PLEX_URL   = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_SCAN  = _b("PLEX_SCAN_ON_CHECK", False)

# janitor (give it decypharr's error log via a file OR a command, e.g. journalctl when on-host)
JAN_LIBS      = [p.strip() for p in os.environ.get("JANITOR_LIBRARY_PATHS", "").split(",") if p.strip()]
JAN_LOG       = os.environ.get("JANITOR_DECYPHARR_LOG", "")         # log file path
JAN_LOG_CMD   = os.environ.get("JANITOR_LOG_CMD", "")               # cmd printing the log, e.g. "journalctl -u decypharr -n 10000 --no-hostname"
JAN_QUAR      = os.environ.get("JANITOR_QUARANTINE_DIR", "/data/quarantine")
JAN_PATTERNS  = os.environ.get("JANITOR_DEAD_PATTERNS", "ARTICLE_NOT_FOUND,still missing").split(",")

TRIGGER_EVENTS = set(e.strip() for e in os.environ.get(
    "DOCTOR_TRIGGER_EVENTS", "Download,ManualInteractionRequired,DownloadFailed,Grab").split(",") if e.strip())

# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3))
    except Exception:
        pass
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
log = logging.getLogger("doctor")

# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def http_code(url, headers=None, t=10):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=t)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

def run_cmd(cmd):
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
        return (p.returncode, (p.stdout + p.stderr).strip()[:300])
    except Exception as e:
        return (1, "cmd error: " + str(e)[:120])

def run_output(cmd, t=120):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return p.stdout
    except Exception as e:
        log.warning("log cmd failed: %s", str(e)[:80])
        return ""

def host_load():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0

# =========================================================================== #
# CHECK: queue
# =========================================================================== #

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
                                          and any("stall" in m.lower() or "no files" in m.lower() for m in _msgs(r)),
}

def stuck_reason(rec):
    for name in ENABLED_CONDITIONS:
        pred = CONDITIONS.get(name)
        if pred and pred(rec):
            return name
    return None

class Arr:
    def __init__(self, name, kind, url, apikey):
        self.name, self.kind = name, kind                       # sonarr | radarr | prowlarr
        self.base = url.rstrip("/") + ("/api/v1" if kind == "prowlarr" else "/api/v3")
        self.apikey = apikey
        self.unknown = "includeUnknownSeriesItems=true" if kind == "sonarr" else "includeUnknownMovieItems=true"

    def _req(self, method, path, data=None, t=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=t or TIMEOUT)

    def queue(self):
        if self.kind == "prowlarr":
            return []                                            # prowlarr has no download queue
        try:
            return json.load(self._req("GET", "/queue?page=1&pageSize=1000&" + self.unknown)).get("records", [])
        except Exception as e:
            log.warning("[%s] queue fetch failed: %s", self.name, e); return None

    def health(self):
        try:
            return json.load(self._req("GET", "/health"))
        except Exception:
            return []

    def remove(self, item_id):
        q = "removeFromClient=%s&blocklist=%s" % (str(REMOVE_CLIENT).lower(), str(BLOCKLIST).lower())
        self._req("DELETE", "/queue/%d?%s" % (item_id, q))

    def post(self, path, t=150):
        """POST with empty body (used for /indexer/testall, /downloadclient/testall). Returns parsed JSON or []."""
        try:
            body = self._req("POST", path, data=b"", t=t).read()
            return json.loads(body) if body else []
        except urllib.error.HTTPError as e:
            try: return json.loads(e.read())
            except Exception: return []
        except Exception as ex:
            log.debug("[%s] POST %s err %s", self.name, path, str(ex)[:50]); return []

def load_instances():
    out = []
    for n in range(1, 51):
        url = os.environ.get("INSTANCE_%d_URL" % n)
        if not url:
            continue
        key = os.environ.get("INSTANCE_%d_APIKEY" % n, "")
        kind = os.environ.get("INSTANCE_%d_TYPE" % n, "").strip().lower()
        if kind not in ("sonarr", "radarr", "prowlarr"):
            kind = ("radarr" if "radarr" in url.lower() else
                    "prowlarr" if "prowlarr" in url.lower() else "sonarr")
        name = os.environ.get("INSTANCE_%d_NAME" % n, "%s-%d" % (kind, n))
        if not key:
            log.warning("INSTANCE_%d has no APIKEY, skipping", n); continue
        out.append(Arr(name, kind, url, key))
    return out

INSTANCES = []

def _load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}

def _save_state(s):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        json.dump(s, open(STATE_FILE, "w"))
    except Exception:
        pass

def check_queue(only=None):
    if LOAD_MAX > 0 and host_load() > LOAD_MAX:
        log.info("[queue] host load > %.0f -> skipping", LOAD_MAX); return
    state = _load_state(); actions = 0
    for arr in INSTANCES:
        if only and arr.name.lower() != only.lower():
            continue
        recs = arr.queue()
        if recs is None:
            continue
        strikes = state.get(arr.name, {}); new = {}; stuck = 0
        for r in recs:
            reason = stuck_reason(r)
            if not reason:
                continue
            stuck += 1; iid = str(r.get("id")); cnt = strikes.get(iid, 0) + 1; new[iid] = cnt
            if cnt >= MIN_STRIKES and actions < MAX_ACTIONS:
                title = (r.get("title") or "")[:70]
                if DRY_RUN:
                    log.info("[queue:%s] WOULD remove (%s strike %d): %s", arr.name, reason, cnt, title)
                else:
                    try:
                        arr.remove(r["id"]); actions += 1; new.pop(iid, None)
                        log.info("[queue:%s] removed (%s, blocklist=%s) -> re-search: %s", arr.name, reason, BLOCKLIST, title)
                    except Exception as e:
                        log.warning("[queue:%s] remove failed: %s", arr.name, e)
        state[arr.name] = new
        if stuck:
            log.info("[queue:%s] %d stuck tracked, %d acted", arr.name, stuck, actions)
        for h in arr.health():
            if h.get("type") in ("error", "warning"):
                log.debug("[queue:%s] health %s: %s", arr.name, h.get("type"), (h.get("message") or "")[:90])
    _save_state(state)

# =========================================================================== #
# CHECK: decypharr (mount hang -> restart hook)
# =========================================================================== #

def _read_test(path, timeout):
    """Return True if a file under path read its first bytes within timeout, else False (hung/failed)."""
    result = {"ok": False}
    target = {"f": None}
    try:
        for root, _, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".ts")):
                    target["f"] = os.path.join(root, fn); break
            if target["f"]:
                break
    except Exception:
        return None  # cannot even list -> unknown
    if not target["f"]:
        return None
    def _do():
        try:
            with open(target["f"], "rb") as fh:
                fh.read(65536)
            result["ok"] = True
        except Exception:
            result["ok"] = False
    th = threading.Thread(target=_do, daemon=True); th.start(); th.join(timeout)
    if th.is_alive():
        return False  # hung
    return result["ok"]

_decy_last_restart = [0.0]

def check_decypharr():
    if DECY_URL:
        c = http_code(DECY_URL, t=10)
        log.info("[decypharr] api %s -> %s", DECY_URL, c if c else "DOWN")
    if not DECY_MOUNT_TEST:
        return
    ok = _read_test(DECY_MOUNT_TEST, DECY_READ_TIMEOUT)
    if ok is None:
        log.warning("[decypharr] mount %s: no test file found / unlistable", DECY_MOUNT_TEST); return
    if ok:
        log.info("[decypharr] mount %s read OK", DECY_MOUNT_TEST); return
    log.error("[decypharr] mount %s READ HUNG (FUSE stall)", DECY_MOUNT_TEST)
    if DRY_RUN or not DECY_RESTART_CMD:
        log.error("[decypharr] no restart cmd set (or dry-run) -> alert only"); return
    if time.time() - _decy_last_restart[0] < 300:
        log.warning("[decypharr] restarted <5m ago, holding off"); return
    log.error("[decypharr] running restart hook: %s", DECY_RESTART_CMD)
    rc = run_cmd(DECY_RESTART_CMD); _decy_last_restart[0] = time.time()
    log.error("[decypharr] restart hook rc=%s %s", rc[0] if rc else "?", rc[1] if rc else "")

# =========================================================================== #
# CHECK: plex
# =========================================================================== #

def check_plex():
    if not PLEX_URL:
        return
    sep = "&" if "?" in PLEX_URL else "?"
    url = PLEX_URL.rstrip("/") + "/identity"
    c = http_code(url + (sep + "X-Plex-Token=" + PLEX_TOKEN if PLEX_TOKEN else ""), t=10)
    if c == 200:
        log.info("[plex] %s -> 200 OK", PLEX_URL)
    else:
        log.error("[plex] %s -> %s (unresponsive)", PLEX_URL, c if c else "DOWN")
    if PLEX_SCAN and PLEX_TOKEN and c == 200:
        try:
            urllib.request.urlopen(PLEX_URL.rstrip("/") + "/library/sections/all/refresh?X-Plex-Token=" + PLEX_TOKEN, timeout=10)
            log.info("[plex] triggered library refresh")
        except Exception as e:
            log.debug("[plex] refresh failed: %s", e)

# =========================================================================== #
# CHECK: resources
# =========================================================================== #

def _meminfo():
    d = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            d[k.strip()] = int(v.split()[0]) // 1024  # MB
    except Exception:
        pass
    return d

def check_resources():
    l1 = host_load()
    mi = _meminfo()
    avail = mi.get("MemAvailable", -1)
    swap_used = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
    msg = "[resources] load=%.1f memAvail=%sMB swapUsed=%sMB" % (l1, avail, swap_used)
    crit = (l1 >= RES_LOAD_WARN) or (0 <= avail < RES_MEM_MIN) or (swap_used >= RES_SWAP_WARN)
    (log.warning if crit else log.info)(msg + (" <-- PRESSURE" if crit else ""))
    if crit and RES_DROP_CACHES and not DRY_RUN:
        rc = run_cmd("sync; echo 1 > /proc/sys/vm/drop_caches")
        log.warning("[resources] dropped page cache rc=%s", rc[0] if rc else "?")

# =========================================================================== #
# CHECK: janitor (usenet dead-file quarantine, from a decypharr log file)
# =========================================================================== #

def check_janitor():
    has_log = JAN_LOG_CMD or (JAN_LOG and os.path.exists(JAN_LOG))
    if not (JAN_LIBS and has_log):
        log.debug("[janitor] need JANITOR_LIBRARY_PATHS + (JANITOR_LOG_CMD or a readable JANITOR_DECYPHARR_LOG)")
        return
    bad = set()
    try:
        if JAN_LOG_CMD:
            data = run_output(JAN_LOG_CMD)                       # e.g. journalctl when running on-host
        else:
            data = open(JAN_LOG, errors="ignore").read()[-2_000_000:]
    except Exception as e:
        log.warning("[janitor] cannot read log: %s", e); return
    pat = re.compile(r"Error streaming file: (.+?) error=\"([^\"]*)\"")
    for m in pat.finditer(data):
        path, err = m.group(1), m.group(2)
        if any(p.strip() and p.strip() in err for p in JAN_PATTERNS):
            bad.add(path.strip().split("/")[0])
    if not bad:
        log.debug("[janitor] no dead releases in log tail"); return
    moved = 0
    qroot = os.path.join(JAN_QUAR, time.strftime("%Y%m%d-%H%M%S"))
    manifest = []
    for libp in JAN_LIBS:
        for root, _, files in os.walk(libp):
            for fn in files:
                fp = os.path.join(root, fn)
                if not os.path.islink(fp):
                    continue
                try:
                    tgt = os.readlink(fp)
                except Exception:
                    continue
                mm = re.search(r"/__all__/([^/]+)(?:/|$)", tgt)
                if mm and mm.group(1) in bad:
                    if DRY_RUN:
                        log.info("[janitor] WOULD quarantine: %s", fp); continue
                    try:
                        dst = os.path.join(qroot, os.path.relpath(fp, "/"))
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.symlink(tgt, dst); os.unlink(fp)
                        manifest.append({"orig": fp, "target": tgt}); moved += 1
                    except Exception as e:
                        log.warning("[janitor] move failed %s: %s", fp, e)
    if manifest:
        try:
            os.makedirs(qroot, exist_ok=True); json.dump(manifest, open(qroot + "/manifest.json", "w"), indent=1)
        except Exception:
            pass
    if moved:
        log.info("[janitor] quarantined %d dead-file symlink(s) across %d release(s) -> %s", moved, len(bad), qroot)

# =========================================================================== #
# CHECK: providers (radarr/sonarr/prowlarr indexers + download clients that errored -> Test)
# =========================================================================== #

_PROVIDER_KEYWORDS = ("indexer", "download client", "applications unavailable", "applications are unavailable")

def check_providers():
    for arr in INSTANCES:
        if arr.kind not in ("sonarr", "radarr", "prowlarr"):
            continue
        issues = [h for h in arr.health()
                  if h.get("type") in ("warning", "error")
                  and any(k in (h.get("message") or "").lower() for k in _PROVIDER_KEYWORDS)]
        if not issues:
            continue
        log.warning("[providers:%s] %d provider issue(s): %s", arr.name, len(issues),
                    " | ".join((h.get("message") or "")[:60] for h in issues[:2]))
        if DRY_RUN:
            continue
        # re-test everything; a passing test clears the failure status and re-enables recovered ones
        for ep, label in (("/indexer/testall", "indexers"), ("/downloadclient/testall", "download-clients")):
            res = arr.post(ep)
            if isinstance(res, list) and res:
                ok = sum(1 for r in res if r.get("isValid"))
                still = [r.get("id") for r in res if not r.get("isValid")]
                log.info("[providers:%s] tested %s: %d ok, %d still failing %s",
                         arr.name, label, ok, len(still), still or "")

# =========================================================================== #
# CHECK: bazarr (reachability)
# =========================================================================== #

def check_bazarr():
    if not BAZARR_URL:
        return
    c = http_code(BAZARR_URL.rstrip("/") + "/api/system/status",
                  headers={"X-API-KEY": BAZARR_APIKEY} if BAZARR_APIKEY else None, t=10)
    (log.info if c == 200 else log.error)("[bazarr] %s -> %s", BAZARR_URL, c if c else "DOWN")

# =========================================================================== #
# sweep / loop
# =========================================================================== #

CHECKS = [("queue", EN_QUEUE, check_queue), ("providers", EN_PROVIDERS, check_providers),
          ("decypharr", EN_DECYPHARR, check_decypharr), ("plex", EN_PLEX, check_plex),
          ("resources", EN_RESOURCES, check_resources), ("janitor", EN_JANITOR, check_janitor),
          ("bazarr", EN_BAZARR, check_bazarr)]

_lock = threading.Lock()

def sweep(only=None):
    if not _lock.acquire(blocking=False):
        log.debug("sweep already running"); return
    try:
        for cid, en, fn in CHECKS:
            if not en:
                continue
            try:
                fn(only) if cid == "queue" else fn()
            except Exception as e:
                log.error("[%s] check error: %s", cid, e)
    finally:
        _lock.release()

def main():
    global INSTANCES
    INSTANCES = load_instances()
    enabled = [c for c, e, _ in CHECKS if e]
    if EN_QUEUE and not INSTANCES:
        log.error("queue check enabled but no instances. Set INSTANCE_1_URL / _APIKEY / _TYPE.")
        sys.exit(2)
    if not enabled:
        log.error("no checks enabled. Set ENABLE_QUEUE / ENABLE_DECYPHARR / ENABLE_PLEX / ENABLE_RESOURCES / ENABLE_JANITOR.")
        sys.exit(2)
    log.info("stack-doctor v0.2 | mode=%s | checks=[%s] | instances=%s | dry_run=%s",
             MODE, ",".join(enabled), ", ".join(a.name for a in INSTANCES) or "-", DRY_RUN)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    if MODE == "event":
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class H(BaseHTTPRequestHandler):
            def _ok(self, code=200, body=b"ok"):
                self.send_response(code); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                self._ok(body=b"stack-doctor ok") if self.path in ("/", "/health", "/healthz") else self._ok(404, b"nf")
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    p = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    p = {}
                ev = p.get("eventType") or p.get("EventType") or "?"; inst = p.get("instanceName") or p.get("InstanceName")
                self._ok()
                if ev == "Test":
                    log.info("webhook Test from %s", inst or "?"); return
                if TRIGGER_EVENTS and ev not in TRIGGER_EVENTS:
                    return
                log.info("event '%s' from %s -> sweep", ev, inst or "all")
                threading.Thread(target=sweep, kwargs={"only": inst}, daemon=True).start()
            def log_message(self, *a):
                pass
        sweep()
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("listening for webhooks on :%d", PORT)
        fb = max(INTERVAL, 1800)
        while not stop.wait(fb):
            sweep()
        srv.shutdown()
    else:
        sweep()
        while not stop.wait(INTERVAL):
            sweep()
    log.info("stack-doctor stopped")

if __name__ == "__main__":
    main()
