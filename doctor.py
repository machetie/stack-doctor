#!/usr/bin/env python3
"""
stack-doctor - auto-detect and fix recurring issues across a Sonarr/Radarr +
decypharr + Plex media stack.

Modular checks, each toggled and configured by environment variables:

  queue      *arr download queues       - clear stuck/dead/blocked items -> re-search
  providers  *arr/prowlarr providers    - auto-Test failed indexers/download clients to clear them
  decypharr  decypharr mount + API      - detect a hung FUSE mount -> run a restart hook
  plex       Plex Media Server          - detect unresponsive Plex (+ optional library scan)
  plexscan   Plex library scans         - detect a scan wedged with no progress -> fix the hung
                                           mount, cancel the stuck scan, last-resort restart Plex
  resources  host load / memory / swap  - report pressure, optional drop_caches relief
  janitor    usenet dead files          - quarantine library symlinks for permanently-dead
                                           releases (reversible) from a decypharr log file
  repair     library integrity          - probe media files for unreadable/dead (decypharr link or
                                           usenet article gone) -> remove + re-search the owning *arr
  bazarr     Bazarr                     - reachability check
  seerr      Overseerr/Jellyseerr/Seerr - auto-retry FAILED requests (arr add timed out under load)
  warmer     Plex-driven precache       - read the head of likely-next media so playback starts
                                           instantly (next episode + On Deck); thread, not a sweep
  missing_seasons  Sonarr               - re-trigger searches for seasons with 0 episode files
  no_upgrade_profile  Sonarr            - auto-move ended+complete series to a no-upgrade profile

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

VERSION = "0.3"

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

# UI-saved overrides: merge a JSON overlay over the inherited env BEFORE config is read, so edits win.
CONFIG_FILE = os.environ.get("DOCTOR_CONFIG_FILE", "/data/config.json")

def _load_overrides():
    try:
        with open(CONFIG_FILE) as f:
            for k, v in json.load(f).items():
                if v is not None:
                    os.environ[str(k)] = str(v)
    except Exception:
        pass

_load_overrides()

MODE        = os.environ.get("DOCTOR_MODE", "cron").strip().lower()   # cron | event
INTERVAL    = _i("DOCTOR_INTERVAL", 900)
PORT        = _i("DOCTOR_PORT", 8088)                                 # webhook port (event mode)
UI_PORT     = _i("DOCTOR_UI_PORT", 12345)                            # web dashboard port
EN_UI       = _b("ENABLE_UI", False)
UI_TOKEN    = os.environ.get("DOCTOR_UI_TOKEN", "")                   # optional ?token= / X-Doctor-Token gate
LOG_LEVEL   = os.environ.get("DOCTOR_LOG_LEVEL", "INFO").upper()
LOG_FILE    = os.environ.get("DOCTOR_LOG_FILE", "")
TIMEOUT     = _i("DOCTOR_HTTP_TIMEOUT", 60)
DRY_RUN     = _b("DOCTOR_DRY_RUN", False)

# which checks are on
EN_QUEUE      = _b("ENABLE_QUEUE", True)
EN_DECYPHARR  = _b("ENABLE_DECYPHARR", False)
EN_PLEX       = _b("ENABLE_PLEX", False)
EN_RESOURCES  = _b("ENABLE_RESOURCES", False)
EN_JANITOR    = _b("ENABLE_JANITOR", False)
EN_PROVIDERS  = _b("ENABLE_PROVIDERS", False)
EN_BAZARR     = _b("ENABLE_BAZARR", False)
EN_SEERR      = _b("ENABLE_SEERR", False)       # Overseerr/Jellyseerr/Seerr: auto-retry FAILED requests
EN_PLEX_SCAN  = _b("ENABLE_PLEX_SCAN", False)   # detect + recover a wedged Plex library scan
EN_REPAIR     = _b("ENABLE_REPAIR", False)      # probe library for dead files -> remove + re-search

# missing_seasons: walk monitored Sonarr series, find seasons that have been monitored for at least
# MS_MIN_AGE_HOURS but have zero episode files, and trigger a SeasonSearch so Sonarr re-tries.
# Rate-limited: at most MS_MAX_ACTIONS searches per sweep. Skips seasons already searched recently
# (MS_RECHECK cooldown). Only acts on fully monitored seasons (all episodes monitored).
EN_MISSING_SEASONS    = _b("ENABLE_MISSING_SEASONS", False)
MS_MIN_AGE_HOURS      = _f("MISSING_SEASONS_MIN_AGE_HOURS", 1)   # ignore seasons added less than this long ago
MS_MAX_ACTIONS        = _i("MISSING_SEASONS_MAX_ACTIONS", 5)      # SeasonSearches per sweep
MS_RECHECK            = _dur(os.environ.get("MISSING_SEASONS_RECHECK", "24h"), 86400)  # cooldown between re-searching same season

# no_upgrade_profile: auto-move ended+complete Sonarr series to a no-upgrade quality profile
EN_NO_UPGRADE_PROFILE   = _b("ENABLE_NO_UPGRADE_PROFILE", False)
NO_UPGRADE_PROFILE_ID   = _i("NO_UPGRADE_PROFILE_ID", 0)   # target quality profile id in Sonarr
NO_UPGRADE_PROFILE_NAME = os.environ.get("NO_UPGRADE_PROFILE_NAME", "WEB-1080p (No Upgrade)")

BAZARR_URL    = os.environ.get("BAZARR_URL", "")
BAZARR_APIKEY = os.environ.get("BAZARR_APIKEY", "")

# seerr (Overseerr / Jellyseerr / Seerr) failed-request auto-retry
SEERR_URL       = os.environ.get("SEERR_URL", "")
SEERR_APIKEY    = os.environ.get("SEERR_APIKEY", "")
SEERR_MAX       = _i("SEERR_RETRY_MAX", 10)      # max requests retried per sweep
SEERR_MAX_TRIES = _i("SEERR_MAX_ATTEMPTS", 5)    # give up after this many auto-retries (0 = never)

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

# plexscan: a library scan that makes no progress for a while is wedged (almost always Plex's scanner
# blocking on a hung decypharr mount / unreadable file). Recover: fix the mount, cancel the scan, then
# (last resort) restart Plex. Reuses DECYPHARR_MOUNT_TEST / DECYPHARR_RESTART_CMD for the mount fix.
PLEX_SCAN_STUCK  = _dur(os.environ.get("PLEX_SCAN_STUCK_AFTER", "30m"), 1800)  # no-progress time before "stuck"
PLEX_SCAN_CANCEL = _b("PLEX_SCAN_CANCEL", True)                                # cancel the wedged scan via the activities API
PLEX_RESTART_CMD = os.environ.get("PLEX_RESTART_CMD", "")                      # last-resort hook if the scan stays wedged

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
WARM_LOAD_MAX     = _f("WARMER_LOAD_MAX", 0)            # skip warming if host 1-min load above this (protect Plex); 0=off
WARM_READ_TIMEOUT = _i("WARMER_READ_TIMEOUT", 60)       # abandon a single warm read after this long (hung mount guard)
WARM_CONCURRENCY  = _i("WARMER_CONCURRENCY", 2)         # simultaneous BACKGROUND (on-deck/recent) warm reads
WARM_OPEN_CONC    = _i("WARMER_OPEN_CONCURRENCY", 4)    # dedicated lane for the title you OPEN, so it starts instantly and never queues behind background warming
WARM_PARTS        = _i("WARMER_PARTS", 1)              # how many versions per title to warm (1 = highest-res only; 0 = all). Avoids warming a 1080p you'll never play next to the 4K
# low-cache mode: for small / RAM-backed caches. Skips On Deck (Continue Watching) warming entirely and
# only warms the NEXT episode as the current one nears its end, so almost nothing sits in cache early.
WARM_LOW_CACHE    = _b("WARMER_LOW_CACHE", False)
WARM_NEXT_REMAIN  = _i("WARMER_NEXT_REMAINING_MIN", 0)  # warm the next episode only when <= this many minutes remain (0 = as soon as playback is seen)
WARM_NEXT_NEAR_END = WARM_NEXT_REMAIN if WARM_NEXT_REMAIN > 0 else (10 if WARM_LOW_CACHE else 0)
WARM_SOURCES      = [s.strip().lower() for s in os.environ.get("WARMER_SOURCES", "ondeck,next").split(",") if s.strip()]
WARM_ONDECK       = _b("WARMER_ONDECK", True)          # quick on/off for Continue Watching (On Deck) warming
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
JAN_PATTERNS  = os.environ.get("JANITOR_DEAD_PATTERNS", "ARTICLE_NOT_FOUND,still missing,marked as bad").split(",")

# repair: walk the library, probe media files for unreadable/0-byte/dead-symlink (a dead debrid link or
# a usenet article gone). A file must fail REPAIR_MIN_STRIKES consecutive probes before it is acted on,
# so a transient mount hiccup never triggers a delete. When a SYSTEMIC failure is detected (the mount is
# hung -> many files failing at once) repair backs off entirely and leaves recovery to the decypharr/
# plexscan checks, so it can never mass-delete + mass-regrab during an outage. Gentle by design:
# load-guarded, per-file read timeout, capped probes + actions per sweep, slow rotation through the lib.
# REPAIR_DEBRID_MOUNT: optional path to the debrid mount root (e.g. /mnt/remote/realdebrid/__all__).
# If set, repair checks that the mount is non-empty before every sweep. An empty/missing mount means
# the debrid service is down or the mount is unmounted -> skip the sweep entirely to prevent mass-regrab.
MEDIA_EXTS              = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".m2ts", ".mpg", ".flv")
REPAIR_LIBS             = [p.strip() for p in os.environ.get("REPAIR_LIBRARY_PATHS",
                           os.environ.get("JANITOR_LIBRARY_PATHS", "")).split(",") if p.strip()]
REPAIR_MIN_STRIKES      = _i("REPAIR_MIN_STRIKES", 3)        # consecutive failed probes before a file is "dead"
REPAIR_MAX_SCAN         = _i("REPAIR_MAX_SCAN", 200)         # media files probed per sweep (rotates through the library)
REPAIR_MAX_ACTIONS      = _i("REPAIR_MAX_ACTIONS", 5)        # re-grabs per sweep (keep gentle on the providers)
REPAIR_READ_TIMEOUT     = _i("REPAIR_READ_TIMEOUT", 20)      # abandon a single file probe after this long (hung-mount guard)
REPAIR_RECHECK          = _dur(os.environ.get("REPAIR_RECHECK", "12h"), 43200)  # don't re-probe a known-good file more often than this
REPAIR_LOAD_MAX         = _f("REPAIR_LOAD_MAX", 0)           # skip the whole repair sweep above this host 1-min load (0=off)
REPAIR_ABORT_STREAK     = _i("REPAIR_ABORT_STREAK", 6)       # this many probe failures in a row -> assume hung mount, abort sweep
REPAIR_SYSTEMIC_PCT     = _f("REPAIR_SYSTEMIC_PCT", 25)      # if >= this %% of probed files fail, treat as systemic -> don't act
REPAIR_FFPROBE          = _b("REPAIR_FFPROBE", False)        # also ffprobe the stream (deeper corruption check; needs ffprobe)
REPAIR_DEBRID_MOUNT     = os.environ.get("REPAIR_DEBRID_MOUNT", "")  # debrid mount root; non-empty means "check it's live before sweep"
REPAIR_ITEM_INTERVAL    = _dur(os.environ.get("REPAIR_ITEM_INTERVAL", "0"), 0)  # seconds to wait between each re-grab (0=off)
REPAIR_SEASON_PACKS     = _b("REPAIR_SEASON_PACKS", False)   # flag sonarr seasons spread across multiple dirs (non-season-pack)
REPAIR_UNMONITORED      = _b("REPAIR_UNMONITORED", False)    # include unmonitored series/movies in the repair sweep
# MissingFromDisk mode: query *arr download history for items Sonarr/Radarr knows are missing from disk
# (reason=MissingFromDisk) and re-trigger a search. Complements the filesystem probe for usenet/direct
# downloads where no symlink exists to probe. Shares REPAIR_MAX_ACTIONS budget with the symlink sweep.
REPAIR_MISSING_FROM_DISK = _b("REPAIR_MISSING_FROM_DISK", False)  # enable history-based missing-file re-grab
REPAIR_MFD_RECHECK       = _dur(os.environ.get("REPAIR_MFD_RECHECK", "24h"), 86400)  # cooldown per item before re-searching
# Post-repair verification: after triggering a search, track the command ID and watch *arr history
# for a new 'grabbed' event to confirm the re-search actually produced a new grab. Results are logged
# so you can tell whether re-searches are landing. Verification state lives in __repair_verify__ in
# state.json and is checked at the start of each repair sweep (sweep-based, not real-time).
REPAIR_VERIFY            = _b("REPAIR_VERIFY", False)              # enable post-repair grab verification
REPAIR_VERIFY_DEADLINE   = _dur(os.environ.get("REPAIR_VERIFY_DEADLINE", "4h"), 14400)  # give up after this long

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
class _ColorFormatter(logging.Formatter):
    _GREY    = "\033[90m"
    _GREEN   = "\033[32m"
    _YELLOW  = "\033[33m"
    _RED     = "\033[31m"
    _BRED    = "\033[1;31m"
    _CYAN    = "\033[36m"
    _RESET   = "\033[0m"
    _LEVEL   = {
        "DEBUG":    "\033[36m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    def format(self, record):
        # Let the base class assemble the full message, including exc_info/exc_text/stack_info
        full  = super().format(record)
        ts    = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        lvl   = record.levelname
        lc    = self._LEVEL.get(lvl, "")
        # The base formatter produces "ts | LEVEL | name | msg[\ntraceback]"
        # We replace only the first line's header; any trailing traceback lines are kept as-is
        first_line, *rest = full.splitlines()
        header = (f"{self._GREY}{ts}{self._RESET} "
                  f"{lc}| {lvl:<7} |{self._RESET} "
                  f"{self._CYAN}{record.name}{self._RESET} | "
                  f"{record.getMessage()}")
        lines = [header] + rest
        return "\n".join(lines)

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_ColorFormatter())
handlers_colored = [_console]
if len(handlers) > 1:                         # file handler was added
    handlers_colored.append(handlers[-1])     # keep rotating file handler (no colour)
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    handlers=handlers_colored)
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

    # ---- repair helpers (map a dead library file -> *arr item, then remove + re-search) ----
    def _jget(self, path, t=30):
        try:
            return json.load(self._req("GET", path, t=t))
        except Exception as e:
            log.warning("[%s] GET %s failed: %s", self.name, path, str(e)[:70]); return None

    def movies(self):
        return self._jget("/movie") or []                       # radarr: each has movieFile.path

    def series(self):
        return self._jget("/series") or []                      # sonarr

    def episode_files(self, sid):
        return self._jget("/episodefile?seriesId=%d" % sid) or []

    def episodes(self, sid):
        return self._jget("/episode?seriesId=%d" % sid) or []

    def delete_file(self, file_id):
        """Delete a movieFile/episodeFile record (removes the dead library symlink so it can be re-grabbed)."""
        ep = "/moviefile/%d" % file_id if self.kind == "radarr" else "/episodefile/%d" % file_id
        try:
            self._req("DELETE", ep); return True
        except Exception as e:
            log.warning("[%s] delete file %s failed: %s", self.name, file_id, str(e)[:70]); return False

    def command(self, name, **kw):
        """POST /command and return the command ID (int) on success, or None on failure."""
        body = {"name": name}; body.update(kw)
        try:
            resp = json.load(self._req("POST", "/command", data=json.dumps(body).encode()))
            return resp.get("id") or True          # return id if present, else True for compat
        except Exception as e:
            log.warning("[%s] command %s failed: %s", self.name, name, str(e)[:70]); return None

    def command_status(self, command_id):
        """Poll GET /command/{id}. Returns the status string, or None on error."""
        try:
            resp = json.load(self._req("GET", "/command/%d" % command_id))
            return resp.get("status")
        except Exception:
            return None

    def history_grabbed(self, media_id, since_ts, entity_ids=None):
        """Return the most recent 'grabbed' history record for media_id posted after since_ts.
        For sonarr, optionally filter to specific episode IDs. Returns None if nothing found."""
        records = self.history(media_id, page_size=50)
        if isinstance(records, dict):
            records = records.get("records") or []
        for rec in records:
            if rec.get("eventType") != "grabbed":
                continue
            # history dates are ISO8601; string compare works for 'after' check
            if rec.get("date", "") <= since_ts:
                continue
            if entity_ids and self.kind == "sonarr":
                if rec.get("episodeId") not in entity_ids:
                    continue
            return rec
        return None

    def history(self, media_id, page_size=100):
        """Fetch download history for a specific series (sonarr) or movie (radarr).
        Returns a list of history records, each with eventType, sourceTitle, data dict, etc."""
        if self.kind == "sonarr":
            path = "/history/series?seriesId=%d&pageSize=%d&includeSeries=false&includeEpisode=true" % (media_id, page_size)
        elif self.kind == "radarr":
            path = "/history/movie?movieId=%d&pageSize=%d" % (media_id, page_size)
        else:
            return []
        return self._jget(path) or []

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

def _decy_restart(reason=""):
    """Run the decypharr restart hook to recover a hung mount, rate-limited to once / 5 min.
    Shared by the decypharr check and the plexscan check. Returns True if the hook ran."""
    tag = (" (%s)" % reason) if reason else ""
    if DRY_RUN or not DECY_RESTART_CMD:
        log.error("[decypharr] hung but no restart cmd set (or dry-run) -> alert only%s", tag); return False
    if time.time() - _decy_last_restart[0] < 300:
        log.warning("[decypharr] restarted <5m ago, holding off%s", tag); return False
    log.error("[decypharr] running restart hook%s: %s", tag, DECY_RESTART_CMD)
    rc = run_cmd(DECY_RESTART_CMD); _decy_last_restart[0] = time.time()
    log.error("[decypharr] restart hook rc=%s %s", rc[0] if rc else "?", rc[1] if rc else "")
    return True

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
    _decy_restart()

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
# CHECK: plexscan (a Plex library scan wedged with no progress -> recover)
# =========================================================================== #

_scan_seen = {}              # activity uuid -> {first, prog, prog_ts, title, acted_ts}
_plex_last_restart = [0.0]

def _is_scan_activity(a):
    t = (a.get("type") or "").lower()
    txt = ((a.get("title") or "") + " " + (a.get("subtitle") or "")).lower()
    if "scan" in txt:
        return True
    return t.startswith("library.update") or t.startswith("library.refresh")

def check_plex_scan():
    if not (PLEX_URL and PLEX_TOKEN):
        return
    plex = Plex(PLEX_URL, PLEX_TOKEN)
    acts = plex.activities()
    now = time.time(); cur = set(); stuck = []
    for a in acts:
        if not _is_scan_activity(a):
            continue
        uuid = a.get("uuid") or ""
        if not uuid:
            continue
        cur.add(uuid)
        try: prog = int(float(a.get("progress") or 0))
        except Exception: prog = 0
        title = (a.get("title") or a.get("subtitle") or "library scan")[:80]
        s = _scan_seen.setdefault(uuid, {"first": now, "prog": -1, "prog_ts": now, "title": title, "acted_ts": 0})
        if prog > s["prog"]:
            s["prog"] = prog; s["prog_ts"] = now           # progress advanced -> not stuck, reset the clock
        s["title"] = title
        if now - s["prog_ts"] >= PLEX_SCAN_STUCK:
            stuck.append((uuid, a, s))
    for u in list(_scan_seen):                              # forget scans that finished / disappeared
        if u not in cur:
            _scan_seen.pop(u, None)
    if not stuck:
        if cur:
            log.info("[plexscan] %d scan(s) running, progressing", len(cur))
        return
    for uuid, a, s in stuck:
        if now - s.get("acted_ts", 0) < PLEX_SCAN_STUCK:   # one recovery attempt per stuck-window; don't hammer
            continue
        s["acted_ts"] = now
        mins = int((now - s["prog_ts"]) / 60)
        log.error("[plexscan] STUCK scan '%s' (no progress for %dm, stalled at %d%%)", s["title"], mins, max(s["prog"], 0))
        if DRY_RUN:
            log.info("[plexscan] DRY-RUN: would fix mount + cancel scan"); continue
        # 1) root cause: a hung decypharr mount blocks the scanner on I/O
        if DECY_MOUNT_TEST and _read_test(DECY_MOUNT_TEST, DECY_READ_TIMEOUT) is False:
            log.error("[plexscan] decypharr mount is hung -> restarting it (the usual cause of a wedged scan)")
            _decy_restart("plex scan wedged on hung mount")
        # 2) cancel the wedged scan so Plex stops blocking on the bad item
        if PLEX_SCAN_CANCEL and (a.get("cancellable") in ("1", "true", None)):
            if plex.cancel_activity(uuid):
                log.warning("[plexscan] cancelled stuck scan '%s'", s["title"])
            else:
                log.warning("[plexscan] cancel failed for '%s'", s["title"])
        # 3) last resort: restart Plex if a scan stays wedged well past the threshold
        if PLEX_RESTART_CMD and now - s["first"] >= PLEX_SCAN_STUCK * 2 and now - _plex_last_restart[0] > 1800:
            log.error("[plexscan] scan still wedged -> restarting Plex: %s", PLEX_RESTART_CMD)
            rc = run_cmd(PLEX_RESTART_CMD); _plex_last_restart[0] = time.time()
            log.error("[plexscan] Plex restart rc=%s %s", rc[0] if rc else "?", rc[1] if rc else "")

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
    # Pattern 1: [webdav] Error streaming file: <path> error="<msg>"
    # Catches: ARTICLE_NOT_FOUND, still missing, marked as bad, etc.
    pat_stream = re.compile(r"Error streaming file: (.+?) error=\"([^\"]*)\"")
    for m in pat_stream.finditer(data):
        path, err = m.group(1), m.group(2)
        if any(p.strip() and p.strip() in err for p in JAN_PATTERNS):
            bad.add(path.strip().split("/")[0])
    # Pattern 2: [link] Giving up on entry ... filename=<name> reason=empty_link
    # Catches: empty_link / all re-insertion attempts exhausted (the only give-up lines that carry a filename)
    pat_filename = re.compile(r"(?:Giving up on entry|empty_link).*?\bfilename=(\S+)")
    for m in pat_filename.finditer(data):
        bad.add(m.group(1).split("/")[0])
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
# CHECK: seerr (Overseerr / Jellyseerr / Seerr) - auto-retry FAILED requests
#
# seerr hands an approved request to Radarr/Sonarr with a fixed ~10s API timeout
# and NO retry of its own. If the arr is briefly slow (heavy search load, host
# contention) the add times out, the request is marked FAILED, and the title
# silently never lands in the arr. We re-drive those FAILED requests each sweep
# so a transient blip self-heals; an attempt cap stops us looping on a request
# that fails for a real reason (dead tmdb id, removed title).
# =========================================================================== #

class Seerr:
    def __init__(self, url, apikey):
        self.base = url.rstrip("/") + "/api/v1"
        self.apikey = apikey

    def _req(self, method, path, data=None, t=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=t or TIMEOUT)

    def failed(self):
        """Requests currently in the FAILED state (seerr could not hand them to the arr)."""
        try:
            d = json.load(self._req("GET", "/request?take=100&skip=0&filter=failed&sort=added", t=15))
            return d.get("results", [])
        except Exception as e:
            log.warning("[seerr] failed-list fetch error: %s", str(e)[:80]); return None

    def retry(self, rid):
        self._req("POST", "/request/%d/retry" % int(rid), data=b"", t=30)

def check_seerr():
    if not SEERR_URL or not SEERR_APIKEY:
        return
    s = Seerr(SEERR_URL, SEERR_APIKEY)
    reqs = s.failed()
    if reqs is None:                                          # fetch errored -> seerr down/unreachable
        log.error("[seerr] %s unreachable", SEERR_URL); return
    if not reqs:
        log.info("[seerr] no failed requests"); return
    state = _load_state()
    tries = state.setdefault("__seerr__", {})
    log.warning("[seerr] %d failed request(s)", len(reqs))
    acted = 0
    for r in reqs:
        if acted >= SEERR_MAX:
            break
        rid = r.get("id")
        if rid is None:
            continue
        md = r.get("media") or {}
        label = "%s tmdb=%s req#%s" % (md.get("mediaType", "?"), md.get("tmdbId", "?"), rid)
        n = int(tries.get(str(rid), 0))
        if SEERR_MAX_TRIES and n >= SEERR_MAX_TRIES:          # keeps failing -> stop, leave it for a human
            log.error("[seerr] giving up on %s after %d retries (persistent failure)", label, n)
            continue
        if DRY_RUN:
            log.info("[seerr] DRY-RUN would retry %s", label); acted += 1; continue
        try:
            s.retry(rid)
            tries[str(rid)] = n + 1
            acted += 1
            log.info("[seerr] retried %s (attempt %d)", label, n + 1)
        except Exception as e:
            log.warning("[seerr] retry %s failed: %s", label, str(e)[:80])
    # a recovered request drops off the failed list; forget its counter so a future fresh fail starts clean
    live = set(str(r.get("id")) for r in reqs)
    for k in [k for k in tries if k not in live]:
        tries.pop(k, None)
    _save_state(state)
    if acted:
        log.info("[seerr] re-drove %d failed request(s)", acted)

# =========================================================================== #
# CHECK: repair (probe library for dead files -> remove + re-search the owning *arr)
# =========================================================================== #

def _iter_media(root):
    """Yield media file paths under root WITHOUT following symlinks, so a hung mount can never
    stall the directory walk itself (only the per-file probe touches the mount, and that is
    timeout-protected). Recurses into real dirs only."""
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except Exception:
        return
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                for x in _iter_media(e.path):
                    yield x
            elif e.name.lower().endswith(MEDIA_EXTS):
                yield e.path
        except Exception:
            continue

def _probe_file(fp, timeout):
    """True if fp is a live, non-empty file whose head reads within timeout. All filesystem ops run
    inside the worker thread so a hung FUSE path (stat/open/read) can't block the caller; a hang or
    any error returns False."""
    res = {"v": False}
    def _do():
        try:
            if os.path.islink(fp) and not os.path.exists(fp):    # dead symlink (debrid link gone)
                return
            if os.path.getsize(fp) <= 0:                         # 0-byte / placeholder
                return
            with open(fp, "rb", buffering=0) as fh:
                fh.read(131072)
            res["v"] = True
        except Exception:
            res["v"] = False
    th = threading.Thread(target=_do, daemon=True); th.start(); th.join(timeout)
    return False if th.is_alive() else res["v"]

def _ffprobe_ok(fp, timeout):
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", fp],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0 and "video" in (p.stdout or "")
    except Exception:
        return False

def _file_ok(fp):
    if not _probe_file(fp, REPAIR_READ_TIMEOUT):
        return False
    if REPAIR_FFPROBE and not _ffprobe_ok(fp, REPAIR_READ_TIMEOUT):
        return False
    return True

def _debrid_mount_ok():
    """Return True if the debrid mount looks live (path exists and has at least one child entry).
    An empty or missing mount means the debrid service is down or the FUSE mount dropped — we must
    not run repair in that state or we'd mass-delete + mass-regrab every file in the library."""
    p = REPAIR_DEBRID_MOUNT
    if not p:
        return True                                              # not configured -> no check, proceed
    try:
        children = os.listdir(p)
        if children:
            return True
        log.warning("[repair] debrid mount %s exists but is empty -> service down? skipping sweep", p)
        return False
    except Exception as e:
        log.warning("[repair] debrid mount %s not accessible (%s) -> skipping sweep", p, str(e)[:60])
        return False

def _radarr_resolve(movies, fp, include_unmonitored=False):
    for m in movies:
        if not include_unmonitored and not m.get("monitored", True):
            continue
        mf = m.get("movieFile") or {}
        if mf.get("path") == fp:
            return (m.get("id"), mf.get("id"), (m.get("title") or "")[:70])
    return None

def _sonarr_resolve(arr, series, fp, include_unmonitored=False):
    ser = next((s for s in series
                if (s.get("path") or "").rstrip("/") and
                   (fp == (s.get("path") or "").rstrip("/") or fp.startswith((s.get("path") or "").rstrip("/") + "/"))), None)
    if not ser:
        return None
    if not include_unmonitored and not ser.get("monitored", True):
        return None
    sid = ser.get("id")
    all_eps = arr.episodes(sid)
    efid = next((ef.get("id") for ef in arr.episode_files(sid) if ef.get("path") == fp), None)
    if not efid:
        return None
    matched_eps = [e for e in all_eps
                   if e.get("episodeFileId") == efid and (include_unmonitored or e.get("monitored", True))]
    epids = [e.get("id") for e in matched_eps]
    # season number — all matched episodes belong to the same file, so take the first
    season_number = matched_eps[0].get("seasonNumber") if matched_eps else None
    return (sid, efid, epids, season_number, (ser.get("title") or "")[:60])

def _sonarr_season_pack_check(arr, series):
    """Yield (series_title, season_number, arr) for any fully-available sonarr season whose episode
    files are spread across more than one parent directory — a sign that individual episode grabs
    replaced what should be a season pack. Only emits seasons where every episode is monitored."""
    for ser in series:
        if not ser.get("monitored", True):
            continue
        sid = ser.get("id")
        title = (ser.get("title") or "")[:60]
        try:
            seasons = {s["seasonNumber"]: s for s in (ser.get("seasons") or []) if s.get("seasonNumber", 0) > 0}
            efiles = arr.episode_files(sid)
            eps    = arr.episodes(sid)
        except Exception:
            continue
        # group episode files by season
        ef_by_season = {}
        for ef in efiles:
            sn = ef.get("seasonNumber")
            if sn:
                ef_by_season.setdefault(sn, []).append(ef)
        ep_by_season = {}
        for ep in eps:
            sn = ep.get("seasonNumber")
            if sn:
                ep_by_season.setdefault(sn, []).append(ep)
        for sn, efs in ef_by_season.items():
            season_meta = seasons.get(sn, {})
            stats = season_meta.get("statistics") or {}
            # only act when the season is fully downloaded
            if stats.get("episodeFileCount", 0) < stats.get("totalEpisodeCount", 1):
                continue
            parent_dirs = set(os.path.dirname(ef.get("path", "")) for ef in efs if ef.get("path"))
            if len(parent_dirs) > 1:
                yield title, sn, sid, arr

def _missing_from_disk_check(state, acted, budget):
    """Query *arr download history for items with reason=MissingFromDisk and re-trigger searches.
    This catches files that Sonarr/Radarr knows are gone but which have no on-disk symlink to probe
    (e.g. usenet direct downloads, or files cleaned up by an external tool). Shares the REPAIR_MAX_ACTIONS
    budget with the filesystem sweep so the two modes together never exceed the cap in one sweep."""
    mfd = state.setdefault("__repair_mfd__", {})
    now = time.time()
    for arr in INSTANCES:
        if arr.kind not in ("sonarr", "radarr") or budget <= 0:
            break
        try:
            all_media = arr.series() if arr.kind == "sonarr" else arr.movies()
        except Exception as e:
            log.warning("[repair:mfd:%s] failed to fetch media list: %s", arr.name, str(e)[:60]); continue
        for item in all_media:
            if budget <= 0:
                break
            if not item.get("monitored") and not REPAIR_UNMONITORED:
                continue
            mid = item.get("id")
            title = (item.get("title") or "")[:60]
            try:
                records = arr.history(mid)
            except Exception as e:
                log.warning("[repair:mfd:%s] history fetch failed for %s: %s", arr.name, title, str(e)[:60]); continue
            # sonarr returns a list directly; radarr wraps in {"records": [...]}
            if isinstance(records, dict):
                records = records.get("records") or []
            # find the most recent grabbed record that is now MissingFromDisk
            # group by season (sonarr) or movie so we only search once per parent
            searched = set()
            for rec in records:
                if rec.get("eventType") != "grabbed":
                    continue
                data = rec.get("data") or {}
                if data.get("reason") != "MissingFromDisk":
                    continue
                if arr.kind == "sonarr":
                    ep = rec.get("episode") or {}
                    season_number = ep.get("seasonNumber")
                    series_id = ep.get("seriesId") or mid
                    key = "%s:%d:s%s" % (arr.name, series_id, season_number)
                else:
                    key = "%s:%d" % (arr.name, mid)
                if key in searched:
                    continue
                if now - mfd.get(key, 0) < REPAIR_MFD_RECHECK:
                    continue                                  # searched recently, wait for cooldown
                if budget <= 0:
                    break
                if DRY_RUN:
                    log.info("[repair:mfd:%s] DRY-RUN would re-search MissingFromDisk: %s", arr.name, title)
                    mfd[key] = now; searched.add(key); acted += 1; budget -= 1; continue
                if arr.kind == "sonarr" and season_number is not None:
                    arr.command("SeasonSearch", seriesId=series_id, seasonNumber=season_number)
                    log.warning("[repair:mfd:%s] MissingFromDisk -> SeasonSearch: %s S%02d", arr.name, title, season_number)
                elif arr.kind == "radarr":
                    arr.command("MoviesSearch", movieIds=[mid])
                    log.warning("[repair:mfd:%s] MissingFromDisk -> MoviesSearch: %s", arr.name, title)
                else:
                    continue
                mfd[key] = now; searched.add(key); acted += 1; budget -= 1
                if REPAIR_ITEM_INTERVAL > 0:
                    time.sleep(REPAIR_ITEM_INTERVAL)
    return acted

def _repair_verify_pending(state):
    """Check any in-flight repair searches from previous sweeps.
    State entry per pending item (keyed by '<arr_name>:<title_slug>'):
      {cmd_id, media_id, entity_ids, kind, title, search_ts, arr_name}
    Flow per item each sweep:
      1. If command_id present, poll /command/{id} — log when done/failed.
      2. Poll /history for a new 'grabbed' event after search_ts.
      3. On confirmed grab: log indexer + sourceTitle, remove from pending.
      4. On deadline exceeded without grab: log warning, remove from pending.
    """
    pv = state.setdefault("__repair_verify__", {})
    if not pv:
        return
    now = time.time()
    arr_map = {a.name: a for a in INSTANCES}
    expired = []
    for key, v in list(pv.items()):
        arr = arr_map.get(v.get("arr_name"))
        if not arr:
            expired.append(key); continue
        title    = v.get("title", key)
        search_ts = v.get("search_ts", "")
        deadline  = v.get("deadline", 0)
        cmd_id    = v.get("cmd_id")
        media_id  = v.get("media_id")
        entity_ids = v.get("entity_ids") or []

        # step 1: poll command status if we haven't confirmed it finished yet
        if cmd_id and not v.get("cmd_done"):
            status = arr.command_status(cmd_id)
            if status in ("completed", "failed", "aborted"):
                log.info("[repair:verify:%s] search command %s: %s", arr.name, cmd_id, status)
                v["cmd_done"] = True
            elif status is None:
                v["cmd_done"] = True          # endpoint gone, assume finished

        # step 2: check history for a new grab
        if media_id:
            rec = arr.history_grabbed(media_id, search_ts, entity_ids if arr.kind == "sonarr" else None)
            if rec:
                src     = rec.get("sourceTitle") or "?"
                indexer = (rec.get("data") or {}).get("indexer") or "?"
                log.warning("[repair:verify:%s] GRABBED '%s' via %s: %s", arr.name, title, indexer, src)
                expired.append(key); continue

        # step 3: deadline check
        if now > deadline:
            log.warning("[repair:verify:%s] no grab confirmed for '%s' within deadline — search may have stalled",
                        arr.name, title)
            expired.append(key)

    for key in expired:
        pv.pop(key, None)

def _repair_record_verify(state, arr, title, cmd_id, media_id, entity_ids):
    """Store a pending verification entry so the next sweep can check if the grab landed."""
    import datetime
    pv = state.setdefault("__repair_verify__", {})
    # key is stable across sweeps; title slug + arr name
    key = "%s:%s" % (arr.name, re.sub(r"[^a-z0-9]+", "_", title.lower())[:40])
    pv[key] = {
        "arr_name":  arr.name,
        "title":     title,
        "cmd_id":    cmd_id if isinstance(cmd_id, int) else None,
        "media_id":  media_id,
        "entity_ids": entity_ids or [],
        "search_ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "deadline":  time.time() + REPAIR_VERIFY_DEADLINE,
    }

def _repair_one(fp, caches, state=None):
    """Map a dead file to its *arr item, delete the (dead) file record, and trigger a fresh search.
    The blocklist/churn handling on the queue side then keeps it from re-grabbing the same dead release."""
    for arr in INSTANCES:
        if arr.kind == "radarr":
            hit = _radarr_resolve(caches.setdefault(arr.name, arr.movies()), fp, REPAIR_UNMONITORED)
            if hit:
                mid, mfid, title = hit
                if DRY_RUN:
                    log.info("[repair:%s] DRY-RUN would remove + re-search: %s", arr.name, title); return True
                if mfid:
                    arr.delete_file(mfid)
                cmd_id = arr.command("MoviesSearch", movieIds=[mid])
                log.warning("[repair:%s] dead file -> removed record + re-searching: %s", arr.name, title)
                if REPAIR_VERIFY and state is not None:
                    _repair_record_verify(state, arr, title, cmd_id, mid, [mid])
                return True
        elif arr.kind == "sonarr":
            hit = _sonarr_resolve(arr, caches.setdefault(arr.name, arr.series()), fp, REPAIR_UNMONITORED)
            if hit:
                sid, efid, epids, season_number, title = hit
                if DRY_RUN:
                    log.info("[repair:%s] DRY-RUN would remove + re-search: %s", arr.name, title); return True
                if efid:
                    arr.delete_file(efid)
                # prefer SeasonSearch (finds a season pack if available) then fall back to EpisodeSearch
                if season_number:
                    cmd_id = arr.command("SeasonSearch", seriesId=sid, seasonNumber=season_number)
                elif epids:
                    cmd_id = arr.command("EpisodeSearch", episodeIds=epids)
                else:
                    cmd_id = arr.command("SeriesSearch", seriesId=sid)
                log.warning("[repair:%s] dead file -> removed record + re-searching: %s", arr.name, title)
                if REPAIR_VERIFY and state is not None:
                    _repair_record_verify(state, arr, title, cmd_id, sid, epids)
                return True
    log.info("[repair] dead file not matched to any *arr (left in place): %s", os.path.basename(fp))
    return False

def check_repair():
    if not (REPAIR_LIBS and INSTANCES):
        log.debug("[repair] need REPAIR_LIBRARY_PATHS (or JANITOR_LIBRARY_PATHS) + a sonarr/radarr instance"); return
    if REPAIR_LOAD_MAX > 0 and host_load() > REPAIR_LOAD_MAX:
        log.info("[repair] host load > %.0f -> skip sweep", REPAIR_LOAD_MAX); return
    if not _debrid_mount_ok():
        return
    state = _load_state(); rs = state.setdefault("__repair__", {})
    # verify pending searches from previous sweeps before starting a new one
    if REPAIR_VERIFY:
        _repair_verify_pending(state)
        _save_state(state)
    now = time.time(); checked = 0; failed = 0; streak = 0; broken = []; aborted = False
    for libp in REPAIR_LIBS:
        if aborted:
            break
        for fp in _iter_media(libp):
            meta = rs.get(fp)
            if meta and meta.get("strikes", 0) == 0 and now - meta.get("last", 0) < REPAIR_RECHECK:
                continue                                        # recently confirmed good -> skip (rotate slowly)
            if checked >= REPAIR_MAX_SCAN:
                aborted = True; break
            checked += 1
            if _file_ok(fp):
                rs[fp] = {"strikes": 0, "last": now}; streak = 0
                continue
            failed += 1; streak += 1
            m = rs.get(fp) or {"strikes": 0}
            m["strikes"] = m.get("strikes", 0) + 1; m["last"] = now; rs[fp] = m
            if m["strikes"] >= REPAIR_MIN_STRIKES:
                broken.append(fp)
            if streak >= REPAIR_ABORT_STREAK:                   # many failures in a row -> mount likely hung, bail
                log.warning("[repair] %d failed probes in a row -> hung mount? aborting sweep, deferring to decypharr/plexscan", streak)
                aborted = True; broken = []; break
    # systemic guard: a big fraction failing means the mount is sick, not individual dead files -> don't act
    if broken and checked >= 8 and (failed * 100.0 / checked) >= REPAIR_SYSTEMIC_PCT:
        log.warning("[repair] %d/%d probes failed (>= %.0f%%) -> systemic (hung mount?), NOT re-grabbing this sweep",
                    failed, checked, REPAIR_SYSTEMIC_PCT)
        broken = []
    acted = 0
    if broken:
        caches = {}
        for fp in broken:
            if acted >= REPAIR_MAX_ACTIONS:
                break
            if _repair_one(fp, caches, state):
                rs.pop(fp, None); acted += 1
                if REPAIR_ITEM_INTERVAL > 0:
                    time.sleep(REPAIR_ITEM_INTERVAL)
    if checked:                                                 # prune state for files that no longer exist (lstat, no mount touch)
        for p in list(rs):
            if not os.path.lexists(p):
                rs.pop(p, None)
    _save_state(state)
    if checked or broken:
        log.info("[repair] probed %d (%d failed), %d dead (>= %d strikes), %d re-grabbed",
                 checked, failed, len(broken), REPAIR_MIN_STRIKES, acted)
    # season-pack check: find sonarr seasons that are fully downloaded but spread across multiple
    # parent dirs (individual episode grabs, not a season pack). Trigger a season search to upgrade.
    if REPAIR_SEASON_PACKS and acted < REPAIR_MAX_ACTIONS:
        sp_budget = REPAIR_MAX_ACTIONS - acted
        for arr in INSTANCES:
            if arr.kind != "sonarr" or sp_budget <= 0:
                break
            try:
                series = arr.series()
            except Exception:
                continue
            for title, sn, sid, a in _sonarr_season_pack_check(arr, series):
                if sp_budget <= 0:
                    break
                if DRY_RUN:
                    log.info("[repair:season_pack] DRY-RUN would search season pack: %s S%02d", title, sn); sp_budget -= 1; continue
                if a.command("SeasonSearch", seriesId=sid, seasonNumber=sn):
                    log.warning("[repair:season_pack] non-season-pack detected -> searching season pack: %s S%02d", title, sn)
                    sp_budget -= 1
                    if REPAIR_ITEM_INTERVAL > 0:
                        time.sleep(REPAIR_ITEM_INTERVAL)
    # MissingFromDisk check: query *arr history for items Sonarr/Radarr knows are gone from disk.
    # Runs after the filesystem sweep so both modes share the REPAIR_MAX_ACTIONS budget.
    if REPAIR_MISSING_FROM_DISK and acted < REPAIR_MAX_ACTIONS:
        acted = _missing_from_disk_check(state, acted, REPAIR_MAX_ACTIONS - acted)
        _save_state(state)

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
        """File paths for this item, highest-resolution version first (so we can warm just the top one)."""
        out = []
        try:
            for m in self._get("/library/metadata/%s" % rk).iter("Media"):
                try: res = int(m.get("height") or 0) * 1000000 + int(m.get("bitrate") or 0)
                except Exception: res = 0
                for p in m.iter("Part"):
                    if p.get("file"):
                        out.append((res, p.get("file")))
            out.sort(key=lambda x: x[0], reverse=True)
        except Exception:
            return []
        return [f for _, f in out]

    def recent(self, n):
        out = []
        try:
            for d in self._get("/library/sections").iter("Directory"):
                if d.get("type") in ("movie", "show"):
                    ra = self._get("/library/sections/%s/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=%d" % (d.get("key"), n))
                    out += list(ra.iter("Video"))[:n]
        except Exception: pass
        return out

    def activities(self):
        """Running background activities (library scans, analysis...). Used by the plexscan check."""
        try: return list(self._get("/activities").iter("Activity"))
        except Exception: return []

    def cancel_activity(self, uuid):
        try:
            req = urllib.request.Request(self.url + "/activities/" + uuid + "?X-Plex-Token=" + self.token, method="DELETE")
            urllib.request.urlopen(req, timeout=10); return True
        except Exception:
            return False

_warm_state = {}            # host_path -> last_warm_ts
_warm_lock = threading.Lock()
_warm_sem = threading.Semaphore(max(1, WARM_CONCURRENCY))        # background warming lane
_warm_sem_open = threading.Semaphore(max(1, WARM_OPEN_CONC))     # detail-page (you opened it) lane - separate so opens never wait
_warm_last_ondeck = [0.0]
_warm_count = [0]           # total warms since start (for the UI)
_warm_recent = []           # recent warms for the UI: [{"ts","title","why"}]

def _warm_record(title, why):
    _warm_count[0] += 1
    _warm_recent.append({"ts": time.time(), "title": title, "why": why})
    if len(_warm_recent) > 80:
        del _warm_recent[:len(_warm_recent) - 80]

def _limit_parts(files):
    return files if WARM_PARTS <= 0 else files[:WARM_PARTS]

def _host_path(f):
    if WARM_PATH_MAP and ":" in WARM_PATH_MAP:
        a, b = WARM_PATH_MAP.split(":", 1)
        if f.startswith(a):
            return b + f[len(a):]
    return f

def _warm_file(path, reason="cycle"):
    p = _host_path(path)
    # a title you actively opened tolerates more load (2x) than speculative background warming, but
    # both still yield before meltdown; concurrency stays capped either way so a burst can't flood.
    guard = (WARM_LOAD_MAX * 2) if reason == "detail-page" else WARM_LOAD_MAX
    if guard > 0 and host_load() > guard:
        return False
    with _warm_lock:                                    # atomic claim: one warm per file per cooldown
        if time.time() - _warm_state.get(p, 0) < WARM_COOLDOWN:
            return False
        _warm_state[p] = time.time()
    try:
        sz = os.path.getsize(p)
    except Exception as e:
        _warm_state.pop(p, None)                         # release so it can be retried
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
    sem = _warm_sem_open if reason == "detail-page" else _warm_sem   # opens get their own lane (instant)
    with sem:                                           # cap concurrent usenet pulls so warming never floods decypharr
        th = threading.Thread(target=_do, daemon=True); th.start(); th.join(WARM_READ_TIMEOUT)
    if th.is_alive():
        _warm_state.pop(p, None)
        log.warning("[warmer] read timed out (%ds, mount slow/hung?): %s", WARM_READ_TIMEOUT, os.path.basename(p))
        return False
    if res["err"]:
        _warm_state.pop(p, None)
        log.warning("[warmer] read fail %s: %s", os.path.basename(p), res["err"]); return False
    _warm_record(os.path.basename(p), reason)
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
    sessions = plex.sessions()
    if "next" in WARM_SOURCES:                              # next episode(s) of anything playing
        for v in sessions:
            if v.get("type") != "episode" or not v.get("grandparentRatingKey"):
                continue
            if WARM_NEXT_NEAR_END > 0:                       # only warm the next ep once the current one nears the end
                try:
                    remain_min = (int(v.get("duration", 0)) - int(v.get("viewOffset", 0))) / 60000.0
                except Exception:
                    remain_min = 0
                if remain_min > WARM_NEXT_NEAR_END:
                    continue
            eps = plex.leaves(v.get("grandparentRatingKey"))
            idx = next((i for i, e in enumerate(eps) if e.get("ratingKey") == v.get("ratingKey")), -1)
            if idx >= 0:
                for e in eps[idx + 1: idx + 1 + WARM_NEXT_EPS]:
                    for f in _limit_parts(plex.parts(e.get("ratingKey"))):
                        add("next-ep", f)
    # Plex-first: speculative On Deck / recent warming pauses while ANYONE is watching (never competes
    # with a live stream), and is skipped entirely in low-cache mode (keep almost nothing pre-warmed).
    if not WARM_LOW_CACHE and not sessions and time.time() - _warm_last_ondeck[0] >= WARM_ONDECK_EVERY:
        _warm_last_ondeck[0] = time.time()
        if WARM_ONDECK and "ondeck" in WARM_SOURCES:        # Continue Watching / Up Next (WARMER_ONDECK is the on/off)
            for v in plex.ondeck():
                for f in _limit_parts(plex.parts(v.get("ratingKey"))):
                    add("ondeck", f)
        if "recent" in WARM_SOURCES and WARM_RECENT_COUNT > 0:
            for v in plex.recent(WARM_RECENT_COUNT):
                for f in _limit_parts(plex.parts(v.get("ratingKey"))):
                    add("recent", f)
    return targets

def warm_cycle():
    if WARM_LOAD_MAX > 0 and host_load() > WARM_LOAD_MAX:
        log.info("[warmer] host load > %.0f -> skip cycle", WARM_LOAD_MAX); return
    targets = _warm_targets(Plex(PLEX_URL, PLEX_TOKEN))
    done = 0
    for reason, path in targets:
        if done >= WARM_MAX_CYCLE:
            break
        if _warm_file(path, reason):
            done += 1
    if done:
        log.info("[warmer] cycle warmed %d (of %d candidate paths)", done, len(targets))

def warmer_loop(stop):
    mode = (" | LOW-CACHE: no On Deck, next ep @<=%dmin left" % WARM_NEXT_NEAR_END) if WARM_LOW_CACHE \
        else ((" | next ep @<=%dmin left" % WARM_NEXT_NEAR_END) if WARM_NEXT_NEAR_END else "")
    log.info("[warmer] started: head=%dMB tail=%dMB sources=%s poll=%ds ondeck-every=%ds%s",
             WARM_HEAD_MB, WARM_TAIL_MB, ",".join(WARM_SOURCES) or "-", WARM_INTERVAL, WARM_ONDECK_EVERY, mode)
    while not stop.is_set():
        try:
            warm_cycle()
        except Exception as e:
            log.error("[warmer] cycle error: %s", e)
        if stop.wait(WARM_INTERVAL):
            break

# opening a title's detail page fetches its extras (/extras, every client incl. Infuse) and, on the
# native Plex app, a rich includeExtras=1 metadata request. Match either -> works for Plex + Infuse.
_PLEXLOG_RE = re.compile(r"/library/metadata/(\d+)(?:/extras|\?[^\s]*includeExtras=1)")

_playing = {"ts": 0.0, "rks": set()}

def _playing_rks(plex):
    """ratingKeys with an active Plex session, cached ~10s (Plex sends the same metadata query while
    you browse a title AND while you play it, so this tells the two apart)."""
    if time.time() - _playing["ts"] > 10:
        try: _playing["rks"] = set(v.get("ratingKey") for v in plex.sessions())
        except Exception: pass
        _playing["ts"] = time.time()
    return _playing["rks"]

def _warm_opened(plex, rk):
    if rk in _playing_rks(plex):                                # already playing (so already cached) -> not a new open
        return
    for f in _limit_parts(plex.parts(rk)):                      # warm just the top version(s) you'd actually play
        if _warm_file(f, "detail-page"):
            log.info("[warmer] you opened rk=%s -> warmed: %s", rk, os.path.basename(_host_path(f)))

def plexlog_loop(stop):
    """Tail Plex's server log; warm the exact title a viewer opens (true pre-play intent)."""
    cmd = WARM_PLEXLOG_CMD or ("tail -n0 -F %r" % WARM_PLEXLOG_FILE if WARM_PLEXLOG_FILE else "")
    if not cmd:
        return
    plex = Plex(PLEX_URL, PLEX_TOKEN)
    seen = {}                                                   # ratingKey -> last-handled ts
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
                if not m:
                    continue
                rk = m.group(1); now = time.time()
                if now - seen.get(rk, 0) < 300:                 # a detail page is polled repeatedly while open -> react once per item / 5 min
                    continue
                seen[rk] = now                                  # warm off-thread so the tailer stays responsive
                threading.Thread(target=_warm_opened, args=(plex, rk), daemon=True).start()
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

# =========================================================================== #
# missing_seasons - find monitored Sonarr seasons with no files and re-trigger
# =========================================================================== #

def check_missing_seasons():
    """Walk every monitored Sonarr series. For each season that is fully monitored, has been
    around long enough (MS_MIN_AGE_HOURS), and has zero episode files, trigger a SeasonSearch.
    State tracks the last time each (instance, series_id, season) was searched so we don't
    hammer the same season every sweep."""
    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    if not sonarr_instances:
        log.debug("[missing_seasons] no sonarr instances configured"); return
    state = _load_state(); ms = state.setdefault("__missing_seasons__", {})
    now = time.time(); acted = 0; skipped = 0
    min_age_secs = MS_MIN_AGE_HOURS * 3600
    for arr in sonarr_instances:
        try:
            all_series = arr.series()
        except Exception as e:
            log.warning("[missing_seasons:%s] failed to fetch series: %s", arr.name, str(e)[:60]); continue
        for ser in all_series:
            if not ser.get("monitored"):
                continue
            sid   = ser.get("id")
            title = (ser.get("title") or "")[:60]
            # use the series added date as a proxy for how long it's been monitored
            added_str = ser.get("added") or ""
            try:
                import email.utils
                added_ts = email.utils.parsedate_to_datetime(added_str).timestamp() if added_str else 0
            except Exception:
                added_ts = 0
            if added_ts and (now - added_ts) < min_age_secs:
                continue                                  # too new, give Sonarr time to grab it first
            for season in (ser.get("seasons") or []):
                sn = season.get("seasonNumber", 0)
                if sn == 0:
                    continue                              # skip specials
                if not season.get("monitored"):
                    continue
                stats = season.get("statistics") or {}
                if stats.get("episodeFileCount", 0) > 0:
                    continue                              # has files, all good
                if stats.get("totalEpisodeCount", 0) == 0:
                    continue                              # no episodes exist yet in Sonarr
                key = "%s:%d:%d" % (arr.name, sid, sn)
                if now - ms.get(key, 0) < MS_RECHECK:
                    skipped += 1; continue               # searched recently, wait for cooldown
                if acted >= MS_MAX_ACTIONS:
                    break
                if DRY_RUN:
                    log.info("[missing_seasons:%s] DRY-RUN would search: %s S%02d", arr.name, title, sn)
                    ms[key] = now; acted += 1; continue
                if arr.command("SeasonSearch", seriesId=sid, seasonNumber=sn):
                    log.warning("[missing_seasons:%s] 0 files in monitored season -> SeasonSearch: %s S%02d",
                                arr.name, title, sn)
                    ms[key] = now; acted += 1
            if acted >= MS_MAX_ACTIONS:
                break
    _save_state(state)
    log.info("[missing_seasons] searched %d season(s), skipped %d (cooldown)", acted, skipped)


# =========================================================================== #
# CHECK: seerr (Overseerr / Jellyseerr / Seerr) - auto-retry FAILED requests
#
# seerr hands an approved request to Radarr/Sonarr with a fixed ~10s API timeout
# and NO retry of its own. If the arr is briefly slow (heavy search load, host
# contention) the add times out, the request is marked FAILED, and the title
# silently never lands in the arr. We re-drive those FAILED requests each sweep
# so a transient blip self-heals; an attempt cap stops us looping on a request
# that fails for a real reason (dead tmdb id, removed title).
# =========================================================================== #

class Seerr:
    def __init__(self, url, apikey):
        self.base = url.rstrip("/") + "/api/v1"
        self.apikey = apikey

    def _req(self, method, path, data=None, t=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=t or TIMEOUT)

    def failed(self):
        """Requests currently in the FAILED state (seerr could not hand them to the arr)."""
        try:
            d = json.load(self._req("GET", "/request?take=100&skip=0&filter=failed&sort=added", t=15))
            return d.get("results", [])
        except Exception as e:
            log.warning("[seerr] failed-list fetch error: %s", str(e)[:80]); return None

    def retry(self, rid):
        self._req("POST", "/request/%d/retry" % int(rid), data=b"", t=30)

def check_seerr():
    if not SEERR_URL or not SEERR_APIKEY:
        return
    s = Seerr(SEERR_URL, SEERR_APIKEY)
    reqs = s.failed()
    if reqs is None:
        log.error("[seerr] %s unreachable", SEERR_URL); return
    if not reqs:
        log.info("[seerr] no failed requests"); return
    state = _load_state()
    tries = state.setdefault("__seerr__", {})
    log.warning("[seerr] %d failed request(s)", len(reqs))
    acted = 0
    for r in reqs:
        if acted >= SEERR_MAX:
            break
        rid = r.get("id")
        if rid is None:
            continue
        md = r.get("media") or {}
        label = "%s tmdb=%s req#%s" % (md.get("mediaType", "?"), md.get("tmdbId", "?"), rid)
        n = int(tries.get(str(rid), 0))
        if SEERR_MAX_TRIES and n >= SEERR_MAX_TRIES:
            log.error("[seerr] giving up on %s after %d retries (persistent failure)", label, n)
            continue
        if DRY_RUN:
            log.info("[seerr] DRY-RUN would retry %s", label); acted += 1; continue
        try:
            s.retry(rid)
            tries[str(rid)] = n + 1
            acted += 1
            log.info("[seerr] retried %s (attempt %d)", label, n + 1)
        except Exception as e:
            log.warning("[seerr] retry %s failed: %s", label, str(e)[:80])
    live = set(str(r.get("id")) for r in reqs)
    for k in [k for k in tries if k not in live]:
        tries.pop(k, None)
    _save_state(state)
    if acted:
        log.info("[seerr] re-drove %d failed request(s)", acted)


def check_no_upgrade_profile():
    """Find ended Sonarr series that are 100% complete and move them to the no-upgrade profile."""
    if not EN_NO_UPGRADE_PROFILE:
        return

    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    if not sonarr_instances:
        log.warning("[no_upgrade_profile] no Sonarr instances configured")
        return

    for arr in sonarr_instances:
        # Resolve target profile id per-instance — each Sonarr may have different profile IDs
        target_id = NO_UPGRADE_PROFILE_ID
        try:
            if not target_id:
                profiles = json.load(arr._req("GET", "/qualityprofile"))
                match = next((p for p in profiles if p["name"] == NO_UPGRADE_PROFILE_NAME), None)
                if not match:
                    log.warning("[no_upgrade_profile:%s] profile %r not found — skipping", arr.name, NO_UPGRADE_PROFILE_NAME)
                    continue
                target_id = match["id"]
                log.info("[no_upgrade_profile:%s] resolved profile %r -> id %d", arr.name, NO_UPGRADE_PROFILE_NAME, target_id)

            # Fetch all series
            all_series = json.load(arr._req("GET", "/series"))
        except Exception as e:
            log.warning("[no_upgrade_profile:%s] fetch failed: %s", arr.name, e)
            continue

        to_move = []
        for s in all_series:
            if s.get("status") != "ended":
                continue
            if s.get("qualityProfileId") == target_id:
                continue
            stats = s.get("statistics", {})
            ep_count  = stats.get("episodeCount", 0)
            pct       = stats.get("percentOfEpisodes", 0)
            if ep_count > 0 and pct >= 100:
                to_move.append(s)

        if not to_move:
            log.debug("[no_upgrade_profile:%s] no newly completed ended shows found", arr.name)
            continue

        log.info("[no_upgrade_profile:%s] moving %d completed ended show(s) to profile %d (%s)",
                 arr.name, len(to_move), target_id, NO_UPGRADE_PROFILE_NAME)
        moved, failed = 0, 0
        for s in to_move:
            try:
                s["qualityProfileId"] = target_id
                arr._req("PUT", "/series/%d" % s["id"], data=json.dumps(s).encode())
                log.info("[no_upgrade_profile:%s] -> %s", arr.name, s["title"])
                moved += 1
            except Exception as e:
                log.warning("[no_upgrade_profile:%s] failed to update %s: %s", arr.name, s["title"], e)
                failed += 1

        log.info("[no_upgrade_profile:%s] done — moved:%d failed:%d", arr.name, moved, failed)


def _plex_sections():
    """Return list of (key, title) for all Plex library sections. Raises on error."""
    import xml.etree.ElementTree as ET
    plex_url   = os.environ.get("PLEX_URL", "").rstrip("/")
    plex_token = os.environ.get("PLEX_TOKEN", "")
    if not plex_url or not plex_token:
        raise ValueError("PLEX_URL or PLEX_TOKEN not set")
    with urllib.request.urlopen(
            urllib.request.Request("%s/library/sections?X-Plex-Token=%s" % (plex_url, plex_token)),
            timeout=10) as r:
        root = ET.fromstring(r.read())
    sections = [(d.get("key"), d.get("title", d.get("key")))
                for d in root.findall("Directory") if d.get("key")]
    if not sections:
        raise ValueError("no library sections found")
    return plex_url, plex_token, sections


def _plex_rescan():
    """Trigger a Plex library scan (refresh) for all sections. Returns (ok, message)."""
    try:
        plex_url, plex_token, sections = _plex_sections()
    except Exception as e:
        return False, str(e)
    ok, failed = [], []
    for key, title in sections:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "%s/library/sections/%s/refresh?X-Plex-Token=%s" % (plex_url, key, plex_token),
                    method="GET"),
                timeout=10)
            ok.append(title)
        except Exception as e:
            log.warning("[plex] rescan section %s (%s) failed: %s", key, title, e)
            failed.append(title)
    msg = "rescanned %d section(s): %s" % (len(ok), ", ".join(ok))
    if failed:
        msg += " | failed: %s" % ", ".join(failed)
    log.info("[plex] %s", msg)
    return len(failed) == 0, msg


def _plex_empty_trash():
    """Empty trash in all Plex library sections. Returns (ok, message)."""
    try:
        plex_url, plex_token, sections = _plex_sections()
    except Exception as e:
        return False, str(e)
    ok, failed = [], []
    for key, title in sections:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "%s/library/sections/%s/emptyTrash?X-Plex-Token=%s" % (plex_url, key, plex_token),
                    method="PUT"),
                timeout=10)
            ok.append(title)
        except Exception as e:
            log.warning("[plex] empty trash section %s (%s) failed: %s", key, title, e)
            failed.append(title)
    msg = "emptied trash for %d section(s): %s" % (len(ok), ", ".join(ok))
    if failed:
        msg += " | failed: %s" % ", ".join(failed)
    log.info("[plex] %s", msg)
    return len(failed) == 0, msg


CHECKS = [("queue", EN_QUEUE, check_queue), ("providers", EN_PROVIDERS, check_providers),
          ("decypharr", EN_DECYPHARR, check_decypharr), ("plex", EN_PLEX, check_plex),
          ("plexscan", EN_PLEX_SCAN, check_plex_scan),
          ("resources", EN_RESOURCES, check_resources), ("janitor", EN_JANITOR, check_janitor),
          ("repair", EN_REPAIR, check_repair), ("bazarr", EN_BAZARR, check_bazarr),
          ("seerr", EN_SEERR, check_seerr),
          ("missing_seasons", EN_MISSING_SEASONS, check_missing_seasons),
          ("no_upgrade_profile", EN_NO_UPGRADE_PROFILE, check_no_upgrade_profile)]

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

# =========================================================================== #
# web dashboard (optional, no dependencies): status + per-service health +
# warmer stats + editable tuning config + live logs. Secrets stay masked.
# =========================================================================== #

_SECRET_HINT = ("APIKEY", "API_KEY", "TOKEN", "PASSWORD", "PASS", "SECRET")

UI_SCHEMA = [
    ("Mode", [("DOCTOR_MODE", "cron|event"), ("DOCTOR_INTERVAL", "900"),
              ("DOCTOR_DRY_RUN", "false"), ("DOCTOR_LOG_LEVEL", "INFO")]),
    ("Checks (on/off)", [("ENABLE_QUEUE", ""), ("ENABLE_PROVIDERS", ""), ("ENABLE_DECYPHARR", ""),
              ("ENABLE_PLEX", ""), ("ENABLE_PLEX_SCAN", ""), ("ENABLE_RESOURCES", ""),
              ("ENABLE_JANITOR", ""), ("ENABLE_REPAIR", ""), ("ENABLE_BAZARR", ""),
              ("ENABLE_SEERR", ""), ("ENABLE_WARMER", ""),
              ("ENABLE_MISSING_SEASONS", ""), ("ENABLE_NO_UPGRADE_PROFILE", "")]),
    ("Plex scan recovery", [("PLEX_SCAN_STUCK_AFTER", "30m"), ("PLEX_SCAN_CANCEL", "true|false")]),
    ("Repair (dead-file re-grab)", [("REPAIR_LIBRARY_PATHS", "/mnt/library/movies,/mnt/library/tv"),
              ("REPAIR_MIN_STRIKES", "3"), ("REPAIR_MAX_SCAN", "200"), ("REPAIR_MAX_ACTIONS", "5"),
              ("REPAIR_READ_TIMEOUT", "20"), ("REPAIR_RECHECK", "12h"), ("REPAIR_LOAD_MAX", "0"),
              ("REPAIR_FFPROBE", "false"), ("REPAIR_DEBRID_MOUNT", ""),
              ("REPAIR_ITEM_INTERVAL", "0"), ("REPAIR_SEASON_PACKS", "false"),
              ("REPAIR_UNMONITORED", "false"),
              ("REPAIR_MISSING_FROM_DISK", "false"), ("REPAIR_MFD_RECHECK", "24h"),
              ("REPAIR_VERIFY", "false"), ("REPAIR_VERIFY_DEADLINE", "4h")]),
    ("Missing Seasons", [("MISSING_SEASONS_MIN_AGE_HOURS", "1"), ("MISSING_SEASONS_MAX_ACTIONS", "5"),
              ("MISSING_SEASONS_RECHECK", "24h")]),
    ("No-Upgrade Profile", [("NO_UPGRADE_PROFILE_NAME", "WEB-1080p (No Upgrade)"),
              ("NO_UPGRADE_PROFILE_ID", "0")]),
    ("Seerr (failed-request retry)", [("SEERR_URL", "http://overseerr:5055"), ("SEERR_APIKEY", ""),
              ("SEERR_RETRY_MAX", "10"), ("SEERR_MAX_ATTEMPTS", "5")]),

    ("Queue / churn brake", [("DOCTOR_MIN_STRIKES", "2"), ("DOCTOR_MAX_ACTIONS", "20"), ("DOCTOR_BLOCKLIST", "true"),
              ("DOCTOR_CHURN_LIMIT", "0"), ("DOCTOR_CHURN_ACTION", "report|park|backoff"), ("DOCTOR_CHURN_BACKOFF", "10m,1h,24h")]),
    ("Warmer", [("WARMER_PRECACHE_MB", "64"), ("WARMER_TAIL_MB", "8"), ("WARMER_SOURCES", "ondeck,next"),
              ("WARMER_ONDECK", "true|false"), ("WARMER_MAX_PER_CYCLE", "40"), ("WARMER_NEXT_EPISODES", "1"),
              ("WARMER_COOLDOWN", "3600"), ("WARMER_LOAD_MAX", "0")]),
    ("Resources", [("RES_LOAD_WARN", "40"), ("RES_SWAP_WARN_MB", "7000"), ("RES_MEM_MIN_MB", "800")]),
]
UI_KEYS = set(k for _, items in UI_SCHEMA for k, _ in items)

def _is_secret(k):
    ku = k.upper()
    return any(h in ku for h in _SECRET_HINT)

def _ui_health():
    """Quick reachability of every monitored service, probed in parallel (short timeouts)."""
    def arr_probe(a):
        def f():
            st = json.load(a._req("GET", "/system/status", t=5))
            warns = [h for h in a.health() if h.get("type") in ("warning", "error")]
            return True, ("v%s" % st.get("version", "?")) + (", %d health warn" % len(warns) if warns else "")
        return f
    jobs = [(a.name, a.kind, arr_probe(a)) for a in INSTANCES]
    if DECY_URL:
        jobs.append(("decypharr", "mount", lambda: (http_code(DECY_URL, t=5) == 200, DECY_URL)))
    if PLEX_URL:
        jobs.append(("plex", "plex", lambda: (
            http_code(PLEX_URL.rstrip("/") + "/identity" + ("?X-Plex-Token=" + PLEX_TOKEN if PLEX_TOKEN else ""), t=5) == 200, "")))
    if BAZARR_URL:
        jobs.append(("bazarr", "bazarr", lambda: (http_code(BAZARR_URL.rstrip("/") + "/api/system/status",
            headers={"X-API-KEY": BAZARR_APIKEY} if BAZARR_APIKEY else None, t=5) == 200, "")))
    if SEERR_URL:
        jobs.append(("seerr", "seerr", lambda: (http_code(SEERR_URL.rstrip("/") + "/api/v1/status",
            headers={"X-Api-Key": SEERR_APIKEY} if SEERR_APIKEY else None, t=5) == 200, "")))
    out = [None] * len(jobs)
    def run(i, name, kind, fn):
        try:
            up, detail = fn()
        except Exception as e:
            up, detail = False, str(e)[:46]
        out[i] = {"name": name, "kind": kind, "up": up, "detail": detail}
    ths = [threading.Thread(target=run, args=(i, n, k, fn), daemon=True) for i, (n, k, fn) in enumerate(jobs)]
    for t in ths: t.start()
    for t in ths: t.join(7)
    return [r for r in out if r]

def _ui_status():
    checks = [{"name": n, "on": bool(e)} for n, e, _ in CHECKS]
    checks.append({"name": "warmer", "on": _b("ENABLE_WARMER", False) and bool(PLEX_URL)})
    checks.append({"name": "detail-page warm", "on": bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE)})
    return {"version": VERSION, "mode": MODE, "dry_run": DRY_RUN, "load": round(host_load(), 2), "checks": checks}

def _ui_warmer():
    rec = [{"title": r["title"], "why": r["why"], "ago": int(time.time() - r["ts"])} for r in reversed(_warm_recent)]
    return {"enabled": _b("ENABLE_WARMER", False) and bool(PLEX_URL),
            "detail_page": bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE),
            "total": _warm_count[0], "recent": rec[:40]}

def _ui_config():
    groups = []
    for g, items in UI_SCHEMA:
        rows = [{"key": k, "val": ("" if _is_secret(k) else os.environ.get(k, "")), "ph": ph, "secret": _is_secret(k)}
                for k, ph in items]
        groups.append({"group": g, "rows": rows})
    return {"groups": groups, "file": CONFIG_FILE}

def _ui_save(body):
    try:
        incoming = json.loads(body or b"{}")
    except Exception:
        return False, "bad json"
    try:
        ov = json.load(open(CONFIG_FILE))
    except Exception:
        ov = {}
    n = 0
    for k, v in incoming.items():
        if k in UI_KEYS and not _is_secret(k):
            ov[k] = v; os.environ[str(k)] = str(v); n += 1
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
        json.dump(ov, open(CONFIG_FILE, "w"), indent=1)
    except Exception as e:
        return False, str(e)[:80]
    return True, "saved %d (restart to apply)" % n

def _ui_logs(n):
    if not LOG_FILE:
        return "(set DOCTOR_LOG_FILE to view logs here)"
    try:
        return "".join(open(LOG_FILE, errors="ignore").readlines()[-n:])
    except Exception as e:
        return "log read error: " + str(e)[:80]

UI_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>stack-doctor</title><style>
:root{--bg:#0d1117;--card:#161b22;--bd:#21262d;--fg:#c9d1d9;--mut:#8b949e;--ok:#3fb950;--off:#6e7681;--bad:#f85149;--ac:#2f81f7;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:13px 18px;border-bottom:1px solid var(--bd);display:flex;gap:12px;align-items:baseline}
h1{font-size:15px;margin:0;letter-spacing:.02em}.mut{color:var(--mut);font-size:12px}
nav{display:flex;gap:6px;padding:10px 18px 0}
nav button{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:6px 6px 0 0;padding:7px 14px;cursor:pointer;font-size:13px}
nav button.active{color:#fff;background:var(--bg);border-color:var(--ac)}
main{padding:14px 18px 40px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:14px;margin:0 0 14px}
h3{margin:0 0 10px;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.07em}
.badge{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;font-weight:600}
.b-on{background:rgba(63,185,80,.16);color:var(--ok)}.b-off{background:rgba(110,118,129,.16);color:var(--off)}.b-bad{background:rgba(248,81,73,.16);color:var(--bad)}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--bd)}.row:last-child{border:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.chip{display:flex;justify-content:space-between;align-items:center;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:7px 10px}
.big{font-size:26px;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:5px 6px;border-bottom:1px solid var(--bd)}td.why{color:var(--mut)}td.ago{color:var(--mut);text-align:right;white-space:nowrap}
label{display:block;color:var(--mut);font-size:11px;margin:9px 0 3px}
input{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:5px;padding:6px 8px;font:13px ui-monospace,monospace}
input:disabled{color:var(--mut)}
.cfg{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:6px 12px}
button.act{background:var(--ac);color:#fff;border:0;border-radius:6px;padding:9px 16px;cursor:pointer;font-size:13px;margin-right:8px}
button.warn{background:var(--warn);color:#1a1a1a}
pre{background:#010409;border:1px solid var(--bd);border-radius:8px;padding:12px;margin:0;max-height:66vh;overflow:auto;white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,monospace}
#toast{position:fixed;right:16px;bottom:16px;background:var(--card);border:1px solid var(--ac);padding:10px 14px;border-radius:8px;opacity:0;transition:.3s;pointer-events:none}
</style></head><body>
<header><h1>stack-doctor</h1><span class=mut id=sub>loading</span></header>
<nav><button data-t=dash class=active>Dashboard</button><button data-t=config>Config</button><button data-t=logs>Logs</button></nav>
<main>
<div id=dash>
 <div class=card><h3>Checks</h3><div class=grid id=checks></div></div>
 <div class=card><h3>Monitored services</h3><div id=health></div></div>
 <div class=card id=plex-card style=display:none><h3>Plex</h3><div id=plex-btns style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
  <button class=act onclick=plexRescan(this)>&#x21bb; Rescan Libraries</button>
  <button class=act onclick=plexEmptyTrash(this)>&#x1f5d1; Empty Trash</button>
 </div><div id=plex-msg style="margin-top:8px;font-size:12px;color:var(--mut)"></div></div>
 <div class=card><h3>Warmer</h3><div id=warm></div></div>
</div>
<div id=config style=display:none></div>
<div id=logs style=display:none></div>
</main><div id=toast></div>
<script>
var tok=new URLSearchParams(location.search).get('token')||'';
function q(p){return p+(p.indexOf('?')>-1?'&':'?')+(tok?'token='+encodeURIComponent(tok):'')}
function E(i){return document.getElementById(i)}
function esc(s){return (s==null?'':''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}
function toast(m){var e=E('toast');e.textContent=m;e.style.opacity=1;setTimeout(function(){e.style.opacity=0},2600)}
function ago(s){if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';return Math.floor(s/3600)+'h ago'}
var timer;
function show(t){var b=document.querySelectorAll('nav button');for(var i=0;i<b.length;i++)b[i].classList.toggle('active',b[i].dataset.t===t);
 E('dash').style.display=t==='dash'?'':'none';E('config').style.display=t==='config'?'':'none';E('logs').style.display=t==='logs'?'':'none';
 clearInterval(timer);
 if(t==='dash'){loadDash();timer=setInterval(loadDash,5000)}
 if(t==='config')loadConfig();
 if(t==='logs'){loadLogs();timer=setInterval(loadLogs,4000)}}
var nb=document.querySelectorAll('nav button');for(var i=0;i<nb.length;i++)nb[i].onclick=(function(t){return function(){show(t)}})(nb[i].dataset.t);
function loadDash(){
 fetch(q('/api/status')).then(function(r){return r.json()}).then(function(s){
  E('sub').textContent='v'+s.version+' / mode '+s.mode+' / load '+s.load+(s.dry_run?' / DRY-RUN':'');
  var h='';for(var i=0;i<s.checks.length;i++){var c=s.checks[i];h+='<div class=chip><span>'+esc(c.name)+'</span><span class="badge '+(c.on?'b-on':'b-off')+'">'+(c.on?'on':'off')+'</span></div>'}
  E('checks').innerHTML=h});
 fetch(q('/api/health')).then(function(r){return r.json()}).then(function(a){
  var h='';for(var i=0;i<a.length;i++){var s=a[i];h+='<div class=row><span>'+esc(s.name)+' <span class=mut>'+esc(s.kind)+'</span></span><span><span class=mut style="margin-right:8px">'+esc(s.detail)+'</span><span class="badge '+(s.up?'b-on':'b-bad')+'">'+(s.up?'up':'down')+'</span></span></div>'}
  E('health').innerHTML=h||'<span class=mut>none</span>'});
 fetch(q('/api/warmer')).then(function(r){return r.json()}).then(function(w){
  var h='<div class=row><span class=mut>total warmed since start</span><span class=big>'+w.total+'</span></div>';
  h+='<div class=row><span class=mut>detail-page (warm what you open)</span><span class="badge '+(w.detail_page?'b-on':'b-off')+'">'+(w.detail_page?'on':'off')+'</span></div>';
  h+='<table style="margin-top:8px">';
  if(!w.recent.length)h+='<tr><td class=mut>nothing warmed yet</td></tr>';
  for(var i=0;i<w.recent.length;i++){var r=w.recent[i];h+='<tr><td>'+esc(r.title)+'</td><td class=why>'+esc(r.why)+'</td><td class=ago>'+ago(r.ago)+'</td></tr>'}
  h+='</table>';E('warm').innerHTML=h});
 fetch(q('/api/health')).then(function(r){return r.json()}).then(function(a){
  var plexUp=a.some(function(s){return s.name==='plex'&&s.up});
  E('plex-card').style.display=plexUp?'':'none';});
}
function plexRescan(btn){
 btn.disabled=true;btn.textContent='Rescanning...';
 fetch(q('/api/plex/rescan'),{method:'POST'}).then(function(r){return r.json()}).then(function(r){
  toast(r.msg||'done');E('plex-msg').textContent=r.msg||'';btn.disabled=false;btn.textContent='\u21bb Rescan Libraries';
 }).catch(function(e){toast('error: '+e);btn.disabled=false;btn.textContent='\u21bb Rescan Libraries';})}
function plexEmptyTrash(btn){
 btn.disabled=true;btn.textContent='Emptying...';
 fetch(q('/api/plex/emptytrash'),{method:'POST'}).then(function(r){return r.json()}).then(function(r){
  toast(r.msg||'done');E('plex-msg').textContent=r.msg||'';btn.disabled=false;btn.textContent='\ud83d\uddd1 Empty Trash';
 }).catch(function(e){toast('error: '+e);btn.disabled=false;btn.textContent='\ud83d\uddd1 Empty Trash';})}
function loadConfig(){fetch(q('/api/config')).then(function(r){return r.json()}).then(function(c){
  var h='';for(var g=0;g<c.groups.length;g++){var grp=c.groups[g];h+='<div class=card><h3>'+esc(grp.group)+'</h3><div class=cfg>';
   for(var i=0;i<grp.rows.length;i++){var r=grp.rows[i];h+='<div><label>'+esc(r.key)+'</label>';
    if(r.secret)h+='<input value="set in unit (hidden)" disabled>';
    else h+='<input id="cf_'+esc(r.key)+'" value="'+esc(r.val)+'" placeholder="'+esc(r.ph)+'">';h+='</div>'}
   h+='</div></div>'}
  h+='<div class=card><button class=act onclick=saveCfg()>Save</button><button class="act warn" onclick=restart()>Save and Restart</button> <span class=mut>changes apply after a restart</span></div>';
  E('config').innerHTML=h})}
function gather(){var o={},els=document.querySelectorAll('[id^=cf_]');for(var i=0;i<els.length;i++)o[els[i].id.slice(3)]=els[i].value;return o}
function saveCfg(){fetch(q('/api/config'),{method:'POST',body:JSON.stringify(gather())}).then(function(r){return r.json()}).then(function(r){toast(r.msg||'saved')})}
function restart(){fetch(q('/api/config'),{method:'POST',body:JSON.stringify(gather())}).then(function(){return fetch(q('/api/restart'),{method:'POST'})}).then(function(){toast('restarting')}).then(function(){setTimeout(function(){show('dash')},4500)})}
function loadLogs(){fetch(q('/api/logs?n=400')).then(function(r){return r.text()}).then(function(t){
  var d=E('logs');if(!d.dataset.i){d.innerHTML='<pre id=lp></pre>';d.dataset.i=1}
  var lp=E('lp'),bot=lp.scrollTop+lp.clientHeight>=lp.scrollHeight-40;lp.textContent=t;if(bot)lp.scrollTop=lp.scrollHeight})}
show('dash');
</script></body></html>"""

def _build_server(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    class H(BaseHTTPRequestHandler):
        def _send(self, code, ctype, body):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            try: self.wfile.write(body)
            except Exception: pass
        def _authed(self):
            if not UI_TOKEN:
                return True
            q = parse_qs(urlparse(self.path).query)
            return self.headers.get("X-Doctor-Token") == UI_TOKEN or q.get("token", [""])[0] == UI_TOKEN
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/health", "/healthz"):
                return self._send(200, "text/plain", "ok")
            if not EN_UI:
                return self._send(404, "text/plain", "nf")
            if not self._authed():
                return self._send(401, "text/plain", "unauthorized")
            if path in ("/", "/ui", "/index.html"):
                return self._send(200, "text/html; charset=utf-8", UI_HTML)
            if path == "/api/status":  return self._send(200, "application/json", json.dumps(_ui_status()))
            if path == "/api/health":  return self._send(200, "application/json", json.dumps(_ui_health()))
            if path == "/api/warmer":      return self._send(200, "application/json", json.dumps(_ui_warmer()))

            if path == "/api/config":           return self._send(200, "application/json", json.dumps(_ui_config()))
            if path == "/api/logs":
                try: n = min(int(parse_qs(urlparse(self.path).query).get("n", ["300"])[0]), 3000)
                except Exception: n = 300
                return self._send(200, "text/plain; charset=utf-8", _ui_logs(n))
            return self._send(404, "text/plain", "nf")
        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            if path in ("/api/config", "/api/restart",
                        "/api/plex/rescan", "/api/plex/emptytrash"):
                if not EN_UI or not self._authed():
                    return self._send(401, "text/plain", "unauthorized")
                if path == "/api/config":
                    ok, msg = _ui_save(body)
                    return self._send(200 if ok else 400, "application/json", json.dumps({"ok": ok, "msg": msg}))
                if path == "/api/plex/rescan":
                    threading.Thread(target=_plex_rescan, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "Plex rescan started"}))
                if path == "/api/plex/emptytrash":
                    threading.Thread(target=_plex_empty_trash, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "Plex empty trash started"}))
                self._send(200, "application/json", json.dumps({"ok": True, "msg": "restarting"}))
                log.info("[ui] restart requested"); threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
                return
            if MODE == "event":                                  # arr webhook
                try: p = json.loads(body or b"{}")
                except Exception: p = {}
                ev = p.get("eventType") or p.get("EventType") or "?"; inst = p.get("instanceName") or p.get("InstanceName")
                self._send(200, "text/plain", "ok")
                if ev == "Test":
                    log.info("webhook Test from %s", inst or "?"); return
                if TRIGGER_EVENTS and ev not in TRIGGER_EVENTS:
                    return
                log.info("event '%s' from %s -> sweep", ev, inst or "all")
                threading.Thread(target=sweep, kwargs={"only": inst}, daemon=True).start(); return
            self._send(404, "text/plain", "nf")
        def log_message(self, *a):
            pass
    return ThreadingHTTPServer(("0.0.0.0", port), H)

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
    if not enabled and not warmer_on and not EN_UI:
        log.error("nothing enabled. Set ENABLE_QUEUE / ENABLE_DECYPHARR / ENABLE_PLEX / ENABLE_PLEX_SCAN / "
                  "ENABLE_RESOURCES / ENABLE_JANITOR / ENABLE_REPAIR / ENABLE_WARMER / ENABLE_UI.")
        sys.exit(2)
    log.info("stack-doctor v%s | mode=%s | checks=[%s]%s%s | instances=%s | dry_run=%s", VERSION,
             MODE, ",".join(enabled), " +warmer" if warmer_on else "", " +ui" if EN_UI else "",
             ", ".join(a.name for a in INSTANCES) or "-", DRY_RUN)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    if warmer_on:
        threading.Thread(target=warmer_loop, args=(stop,), daemon=True).start()
        if WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE:
            threading.Thread(target=plexlog_loop, args=(stop,), daemon=True).start()

    # http server(s): arr webhooks (event mode) and/or the web dashboard (ENABLE_UI)
    servers, wanted = [], {}
    if MODE == "event":
        wanted[PORT] = "webhooks"
    if EN_UI:
        wanted[UI_PORT] = (wanted.get(UI_PORT, "") + "+dashboard").lstrip("+")
    for pnum, what in wanted.items():
        try:
            s = _build_server(pnum)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            servers.append(s); log.info("http on :%d (%s)", pnum, what)
        except Exception as e:
            log.error("http bind :%d failed: %s", pnum, e)

    sweep()
    interval = max(INTERVAL, 1800) if MODE == "event" else INTERVAL
    while not stop.wait(interval):
        sweep()
    for s in servers:
        try: s.shutdown()
        except Exception: pass
    log.info("stack-doctor stopped")

if __name__ == "__main__":
    main()
