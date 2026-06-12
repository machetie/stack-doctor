#!/usr/bin/env python3
"""
stack-doctor - auto-detect and fix recurring issues across a Sonarr/Radarr +
decypharr + Plex media stack.

Modular checks, each toggled and configured by environment variables:

  queue      *arr download queues       - clear stuck/dead/blocked items -> re-search
  providers  *arr/prowlarr providers    - auto-Test failed indexers/download clients to clear them
  decypharr  decypharr mount + API      - detect a hung FUSE mount -> run a restart hook
  plex       Plex Media Server          - detect unresponsive Plex (+ optional library scan)
  resources  host load / memory / swap  - report pressure, optional drop_caches relief
  janitor    usenet dead files          - quarantine library symlinks for permanently-dead
                                           releases (reversible) from a decypharr log file
  bazarr     Bazarr                     - reachability check
  warmer     Plex-driven precache       - read the head of likely-next media so playback starts
                                           instantly (next episode + On Deck); thread, not a sweep

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
import xml.etree.ElementTree as ET

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

def _dur(tok, default=0):
    """Parse a duration token: 30s / 10m / 2h / 1d, or a bare number of seconds."""
    t = str(tok).strip().lower()
    if not t:
        return default
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(float(t[:-1]) * mult[t[-1]]) if t[-1] in mult else int(float(t))
    except (ValueError, KeyError):
        return default

def _human(sec):
    sec = int(sec)
    for size, suf in ((86400, "d"), (3600, "h"), (60, "m")):
        if sec >= size and sec % size == 0:
            return "%d%s" % (sec // size, suf)
    return "%ds" % sec

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
# churn brake: a title that keeps grabbing dead releases (re-grabbed despite blocklist, or only
# dead releases exist) never imports and just burns cycles. After CHURN_LIMIT failed grabs of the
# SAME episode/movie, stop the loop. action: report (log only) | park (un-monitor) | backoff
# (un-monitor, then auto re-monitor on an escalating schedule for a fresh attempt).
CHURN_LIMIT    = _i("DOCTOR_CHURN_LIMIT", 0)              # 0 = brake off
CHURN_ACTION   = os.environ.get("DOCTOR_CHURN_ACTION", "report").strip().lower()
# backoff retry schedule: each park steps to the next delay; the last entry repeats forever.
# default "10m,1h,24h" = retry 10m after the 1st park, 1h after the 2nd, every 24h thereafter.
CHURN_BACKOFF  = [_dur(x) for x in os.environ.get("DOCTOR_CHURN_BACKOFF", "").split(",") if x.strip()]
if not CHURN_BACKOFF:
    _legacy = os.environ.get("DOCTOR_CHURN_COOLDOWN")    # back-compat with the old single fixed cooldown
    CHURN_BACKOFF = [_dur(_legacy)] if _legacy else [600, 3600, 86400]
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

# warmer (Plex-driven precache of the heads of likely-next media -> instant playback start)
EN_WARMER         = _b("ENABLE_WARMER", False)
WARM_HEAD_MB      = _i("WARMER_PRECACHE_MB", 64)        # how much of the file head to pull into cache
WARM_TAIL_MB      = _i("WARMER_TAIL_MB", 8)             # also pull the tail (mkv cues / Plex end-probe); 0=off
WARM_INTERVAL     = _i("WARMER_INTERVAL", 120)          # seconds between session polls (next-episode prefetch)
WARM_ONDECK_EVERY = _i("WARMER_ONDECK_EVERY", 600)      # seconds between on-deck / recent warms
WARM_NEXT_EPS     = _i("WARMER_NEXT_EPISODES", 1)       # warm this many upcoming episodes of an active show
WARM_RECENT_COUNT = _i("WARMER_RECENT_COUNT", 0)        # warm N most-recently-added per library (0=off)
WARM_MAX_CYCLE    = _i("WARMER_MAX_PER_CYCLE", 12)      # cap warms per cycle (rate-limit the usenet fetch)
WARM_COOLDOWN     = _i("WARMER_COOLDOWN", 3600)         # do not re-warm the same file within this many seconds
WARM_LOAD_MAX     = _f("WARMER_LOAD_MAX", 0)            # skip a cycle if host 1-min load above this (protect Plex); 0=off
WARM_READ_TIMEOUT = _i("WARMER_READ_TIMEOUT", 60)       # abandon a single warm read after this long (hung mount guard)
WARM_SOURCES      = [s.strip().lower() for s in os.environ.get("WARMER_SOURCES", "ondeck,next").split(",") if s.strip()]
WARM_PATH_MAP     = os.environ.get("WARMER_PATH_MAP", "")   # "plexPrefix:hostPrefix" if Plex's file path != this host's
# detail-page warming: tail Plex's server log and warm the exact title a viewer opens (the one true
# pre-play signal Plex emits). Give it a streaming command (tail -F, or `pct exec ... tail -F`) OR a file.
WARM_PLEXLOG_CMD  = os.environ.get("WARMER_PLEXLOG_CMD", "")
WARM_PLEXLOG_FILE = os.environ.get("WARMER_PLEXLOG_FILE", "")

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

    def set_monitored(self, ids, monitored):
        """Bulk toggle monitoring for episodes (sonarr) / movies (radarr). Used by the churn brake."""
        if self.kind == "sonarr":
            path, body = "/episode/monitor", {"episodeIds": list(ids), "monitored": monitored}
        elif self.kind == "radarr":
            path, body = "/movie/editor", {"movieIds": list(ids), "monitored": monitored}
        else:
            return False
        try:
            self._req("PUT", path, data=json.dumps(body).encode()); return True
        except Exception as e:
            log.warning("[churn:%s] monitor %s failed: %s", self.name, "on" if monitored else "off", str(e)[:70])
            return False

    def queue_target_id(self, rec):
        """Stable id of what a queue record is FOR (episode for sonarr, movie for radarr)."""
        return rec.get("episodeId") if self.kind == "sonarr" else rec.get("movieId") if self.kind == "radarr" else None

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

def _offenders(state):
    return state.setdefault("__offenders__", {})

def _churn_record(state, arr, rec, title):
    """Count a dead grab for this episode/movie; brake if it's over the limit.
    Returns True if it un-monitored the target (so the caller knows the blocklist-remove won't re-search)."""
    if CHURN_LIMIT <= 0:
        return False
    tid = arr.queue_target_id(rec)
    if not tid:
        return False
    off = _offenders(state).setdefault(arr.name, {})
    o = off.setdefault(str(tid), {"fails": 0, "until": 0, "level": 0, "title": title})
    o["fails"] += 1; o["title"] = title
    if o["fails"] < CHURN_LIMIT or o["until"] != 0:        # below limit, or already parked/reported
        return False
    if CHURN_ACTION == "report":
        log.warning("[churn:%s] REPEAT-OFFENDER (%d dead grabs, still retrying): %s", arr.name, o["fails"], title)
        o["until"] = -1
        return False
    if CHURN_ACTION in ("park", "backoff") and arr.set_monitored([int(tid)], False):
        o["fails"] = 0
        if CHURN_ACTION == "backoff":
            lvl = o.get("level", 0)
            delay = CHURN_BACKOFF[min(lvl, len(CHURN_BACKOFF) - 1)]
            o["until"] = time.time() + delay; o["level"] = lvl + 1
            log.warning("[churn:%s] REPEAT-OFFENDER parked (retry #%d in %s) -> un-monitored: %s",
                        arr.name, lvl + 1, _human(delay), title)
        else:  # park: no auto-retry
            o["until"] = -1
            log.warning("[churn:%s] REPEAT-OFFENDER parked (un-monitored, manual re-monitor): %s", arr.name, title)
        return True
    return False

def _churn_remonitor(state):
    """Re-monitor parked titles whose backoff delay has elapsed, giving them a fresh attempt."""
    if CHURN_LIMIT <= 0 or CHURN_ACTION != "backoff":
        return
    now = time.time(); off_all = state.get("__offenders__", {})
    for arr in INSTANCES:
        for tid, o in list(off_all.get(arr.name, {}).items()):
            until = o.get("until", 0)
            if isinstance(until, (int, float)) and until > 0 and now >= until:
                if arr.set_monitored([int(tid)], True):
                    log.info("[churn:%s] backoff #%d elapsed, re-monitoring for a fresh attempt: %s",
                             arr.name, o.get("level", 0), o.get("title", ""))
                    o["fails"] = 0; o["until"] = 0           # keep level so the next park escalates

def check_queue(only=None):
    if LOAD_MAX > 0 and host_load() > LOAD_MAX:
        log.info("[queue] host load > %.0f -> skipping", LOAD_MAX); return
    state = _load_state(); actions = 0
    _churn_remonitor(state)
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
                    parked = _churn_record(state, arr, r, title)   # un-monitor first so the remove can't re-search
                    try:
                        arr.remove(r["id"]); actions += 1; new.pop(iid, None)
                        log.info("[queue:%s] removed (%s, blocklist=%s)%s: %s", arr.name, reason, BLOCKLIST,
                                 " [parked, no re-search]" if parked else " -> re-search", title)
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
# WARMER: precache the head of likely-next media so playback starts instantly
#
# On a usenet/debrid FUSE mount the slow part of pressing Play is decypharr
# fetching the first segments from the provider. We ask Plex what a viewer is
# about to watch (the next episode of whatever is playing, plus everything in
# their On Deck / Continue Watching row) and read the first WARMER_PRECACHE_MB
# of each through the mount, which pulls those bytes into decypharr's on-disk
# cache. By the time Play is pressed, the head is already warm.
#
# Plex exposes no "user opened the detail page" event, so we approximate intent
# with the high-hit-rate signals it DOES expose (active sessions + On Deck).
# We do not force-delete warmed bytes: decypharr's cache is itself the speed
# win and it already evicts by age/LRU; instead we keep speculative cost low
# (small head, a per-cycle cap, a re-warm cooldown, and a host-load guard).
# =========================================================================== #

class Plex:
    def __init__(self, url, token):
        self.url = url.rstrip("/"); self.token = token

    def _get(self, path):
        sep = "&" if "?" in path else "?"
        with urllib.request.urlopen(self.url + path + sep + "X-Plex-Token=" + self.token, timeout=15) as r:
            return ET.fromstring(r.read())

    def sessions(self):
        try: return list(self._get("/status/sessions").iter("Video"))
        except Exception: return []

    def ondeck(self):
        try: return list(self._get("/library/onDeck").iter("Video"))
        except Exception: return []

    def leaves(self, show_rk):
        try: return list(self._get("/library/metadata/%s/allLeaves" % show_rk).iter("Video"))
        except Exception: return []

    def parts(self, rk):
        out = []
        try:
            for p in self._get("/library/metadata/%s" % rk).iter("Part"):
                if p.get("file"): out.append(p.get("file"))
        except Exception: pass
        return out

    def recent(self, n):
        out = []
        try:
            for d in self._get("/library/sections").iter("Directory"):
                if d.get("type") in ("movie", "show"):
                    ra = self._get("/library/sections/%s/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=%d" % (d.get("key"), n))
                    out += list(ra.iter("Video"))[:n]
        except Exception: pass
        return out

_warm_state = {}            # host_path -> last_warm_ts
_warm_last_ondeck = [0.0]

def _host_path(f):
    if WARM_PATH_MAP and ":" in WARM_PATH_MAP:
        a, b = WARM_PATH_MAP.split(":", 1)
        if f.startswith(a):
            return b + f[len(a):]
    return f

def _warm_file(path):
    p = _host_path(path)
    if time.time() - _warm_state.get(p, 0) < WARM_COOLDOWN:
        return False
    try:
        sz = os.path.getsize(p)
    except Exception as e:
        log.debug("[warmer] stat fail %s: %s", p, str(e)[:60]); return False
    head = min(WARM_HEAD_MB << 20, sz)
    tail = WARM_TAIL_MB > 0 and sz > head + (WARM_TAIL_MB << 20)
    res = {"got": 0, "err": None}
    def _do():
        try:
            with open(p, "rb", buffering=0) as fh:
                while res["got"] < head:
                    b = fh.read(min(4 << 20, head - res["got"]))
                    if not b: break
                    res["got"] += len(b)
                if tail:
                    fh.seek(sz - (WARM_TAIL_MB << 20))
                    while fh.read(4 << 20):
                        pass
        except Exception as e:
            res["err"] = str(e)[:60]
    t0 = time.time()
    th = threading.Thread(target=_do, daemon=True); th.start(); th.join(WARM_READ_TIMEOUT)
    if th.is_alive():
        log.warning("[warmer] read timed out (%ds, mount slow/hung?): %s", WARM_READ_TIMEOUT, os.path.basename(p))
        return False
    if res["err"]:
        log.warning("[warmer] read fail %s: %s", os.path.basename(p), res["err"]); return False
    _warm_state[p] = time.time()
    log.info("[warmer] warmed %dMB head%s in %.1fs: %s",
             res["got"] >> 20, "+%dMB tail" % WARM_TAIL_MB if tail else "",
             time.time() - t0, os.path.basename(p))
    return True

def _warm_targets(plex):
    """Ordered, de-duped list of (reason, plex_file_path) to warm this cycle."""
    targets, seen = [], set()
    def add(reason, path):
        if path and path not in seen:
            seen.add(path); targets.append((reason, path))
    if "next" in WARM_SOURCES:                              # next episode(s) of anything playing
        for v in plex.sessions():
            if v.get("type") != "episode" or not v.get("grandparentRatingKey"):
                continue
            eps = plex.leaves(v.get("grandparentRatingKey"))
            idx = next((i for i, e in enumerate(eps) if e.get("ratingKey") == v.get("ratingKey")), -1)
            if idx >= 0:
                for e in eps[idx + 1: idx + 1 + WARM_NEXT_EPS]:
                    for f in plex.parts(e.get("ratingKey")):
                        add("next-ep", f)
    if time.time() - _warm_last_ondeck[0] >= WARM_ONDECK_EVERY:
        _warm_last_ondeck[0] = time.time()
        if "ondeck" in WARM_SOURCES:                        # Continue Watching / Up Next
            for v in plex.ondeck():
                for f in plex.parts(v.get("ratingKey")):
                    add("ondeck", f)
        if "recent" in WARM_SOURCES and WARM_RECENT_COUNT > 0:
            for v in plex.recent(WARM_RECENT_COUNT):
                for f in plex.parts(v.get("ratingKey")):
                    add("recent", f)
    return targets

def warm_cycle():
    if WARM_LOAD_MAX > 0 and host_load() > WARM_LOAD_MAX:
        log.info("[warmer] host load > %.0f -> skip cycle", WARM_LOAD_MAX); return
    targets = _warm_targets(Plex(PLEX_URL, PLEX_TOKEN))
    done = 0
    for _, path in targets:
        if done >= WARM_MAX_CYCLE:
            break
        if _warm_file(path):
            done += 1
    if done:
        log.info("[warmer] cycle warmed %d (of %d candidate paths)", done, len(targets))

def warmer_loop(stop):
    log.info("[warmer] started: head=%dMB tail=%dMB sources=%s poll=%ds ondeck-every=%ds",
             WARM_HEAD_MB, WARM_TAIL_MB, ",".join(WARM_SOURCES) or "-", WARM_INTERVAL, WARM_ONDECK_EVERY)
    while not stop.is_set():
        try:
            warm_cycle()
        except Exception as e:
            log.error("[warmer] cycle error: %s", e)
        if stop.wait(WARM_INTERVAL):
            break

# the rich include*/async* query Plex logs only when a client OPENS a title's detail page
_PLEXLOG_RE = re.compile(r"/library/metadata/(\d+)\?[^\s]*includeExtras=1")

def _warm_opened(plex, rk):
    for f in plex.parts(rk):                                    # numeric ratingKey -> file path(s)
        if _warm_file(f):
            log.info("[warmer] you opened rk=%s -> warmed: %s", rk, os.path.basename(_host_path(f)))

def plexlog_loop(stop):
    """Tail Plex's server log; warm the exact title a viewer opens (true pre-play intent)."""
    cmd = WARM_PLEXLOG_CMD or ("tail -n0 -F %r" % WARM_PLEXLOG_FILE if WARM_PLEXLOG_FILE else "")
    if not cmd:
        return
    plex = Plex(PLEX_URL, PLEX_TOKEN)
    log.info("[warmer] detail-page warming enabled (tailing Plex log)")
    while not stop.is_set():
        proc = None
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
            for line in proc.stdout:
                if stop.is_set():
                    break
                m = _PLEXLOG_RE.search(line)
                if m:                                           # warm off-thread so the tailer stays responsive
                    threading.Thread(target=_warm_opened, args=(plex, m.group(1)), daemon=True).start()
        except Exception as e:
            log.warning("[warmer] plexlog tail error: %s", str(e)[:80])
        finally:
            if proc:
                try: proc.terminate()
                except Exception: pass
        if stop.wait(10):                                       # tail died/rotated -> reconnect
            break

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
    warmer_on = EN_WARMER and bool(PLEX_URL)
    if EN_WARMER and not PLEX_URL:
        log.warning("ENABLE_WARMER set but PLEX_URL is empty -> warmer disabled")
    if EN_QUEUE and not INSTANCES:
        log.error("queue check enabled but no instances. Set INSTANCE_1_URL / _APIKEY / _TYPE.")
        sys.exit(2)
    if not enabled and not warmer_on:
        log.error("nothing enabled. Set ENABLE_QUEUE / ENABLE_DECYPHARR / ENABLE_PLEX / ENABLE_RESOURCES / ENABLE_JANITOR / ENABLE_WARMER.")
        sys.exit(2)
    log.info("stack-doctor v0.2 | mode=%s | checks=[%s]%s | instances=%s | dry_run=%s",
             MODE, ",".join(enabled), " +warmer" if warmer_on else "",
             ", ".join(a.name for a in INSTANCES) or "-", DRY_RUN)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    if warmer_on:
        threading.Thread(target=warmer_loop, args=(stop,), daemon=True).start()
        if WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE:
            threading.Thread(target=plexlog_loop, args=(stop,), daemon=True).start()

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
