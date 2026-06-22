"""Scheduler: per-check intervals, bounded concurrency, and the full sweep."""
import time
import threading
import logging
from collections import namedtuple
from typing import Optional, Callable, Any
from .config import *
from .checks import *  # check_* functions referenced by CHECKS

# Descriptor for a scheduled check.
# Fields:
#   cid         – unique string id; used for logging and env-var lookup (<CID>_INTERVAL)
#   enabled     – bool from config (EN_* constant); False means the check never runs
#   fn          – the check_* function to call each cycle
#   speed       – "fast" or "slow"; selects FAST_INTERVAL / SLOW_INTERVAL when no override
#   default_iv  – optional int (seconds) that overrides speed without touching os.environ;
#                 still overrideable by a <CID>_INTERVAL env var
#
# Using a namedtuple makes field access self-documenting and turns wrong-width table
# edits into a TypeError at import time rather than a ValueError mid-sweep.
CheckEntry = namedtuple("CheckEntry", ["cid", "enabled", "fn", "speed", "default_iv", "needs_instances"])

CHECKS = [CheckEntry("queue",              EN_QUEUE,              check_queue,              "fast", None, True),
          CheckEntry("providers",          EN_PROVIDERS,          check_providers,          "fast", None, True),
          CheckEntry("decypharr",          EN_DECYPHARR,          check_decypharr,          "fast", None, False),
          CheckEntry("plex",               EN_PLEX,               check_plex,               "fast", None, False),
          CheckEntry("plexscan",           EN_PLEX_SCAN,          check_plex_scan,          "fast", None, False),
          CheckEntry("resources",          EN_RESOURCES,          check_resources,          "fast", None, False),
          CheckEntry("janitor",            EN_JANITOR,            check_janitor,            "slow", None, False),
          CheckEntry("repair",             EN_REPAIR,             check_repair,             "slow", None, True),
          CheckEntry("bazarr",             EN_BAZARR,             check_bazarr,             "fast", None, False),
          CheckEntry("seerr",              EN_SEERR,              check_seerr,              "fast", None, False),
          CheckEntry("missing_seasons",    EN_MISSING_SEASONS,    check_missing_seasons,    "slow", 900,   True),   # 15 min default
          CheckEntry("no_upgrade_profile", EN_NO_UPGRADE_PROFILE, check_no_upgrade_profile, "slow", None, True),
          CheckEntry("multipack",          MULTIPACK_ENABLED,     check_multipack,          "slow", None, True)]
_check_locks = {cid: threading.Lock() for cid, _, _, _, _, _ in CHECKS}
_scheduler_sem = threading.Semaphore(max(1, SCHEDULER_CONCURRENCY))
_lock = threading.Lock()
def sweep(only: Optional[Any] = None) -> None:
    if not _lock.acquire(blocking=False):
        log.debug("sweep already running"); return
    log.info("[sweep] starting initial sweep of %d enabled check(s)", sum(1 for _, e, _, _, _, _ in CHECKS if e))
    try:
        for cid, en, fn, _, _, _ in CHECKS:
            if not en:
                continue
            log.info("[sweep] running %s", cid)
            try:
                fn(only) if cid == "queue" else fn()
            except Exception as e:
                log.error("[%s] check error: %s", cid, e)
            log.info("[sweep] finished %s", cid)
    finally:
        _lock.release()
        log.info("[sweep] initial sweep complete")
def _run_scheduled_check(cid: str, fn: Callable[[], None]) -> None:
    """Run a single scheduled check with per-check locking and bounded concurrency."""
    lock = _check_locks.get(cid)
    if lock and not lock.acquire(blocking=False):
        log.debug("[%s] already running, skipping scheduled run", cid)
        return
    acquired = False
    try:
        if not _scheduler_sem.acquire(blocking=False):
            log.info("[%s] scheduler concurrency full, deferring", cid)
            return
        acquired = True
        log.info("[%s] running scheduled check", cid)
        fn()
    except Exception as e:
        log.error("[%s] scheduled check error: %s", cid, e)
    finally:
        if acquired:
            log.info("[%s] scheduled check finished", cid)
            _scheduler_sem.release()
        if lock:
            lock.release()
def scheduler_loop(stop: threading.Event) -> None:
    """Background loop that runs each enabled check on its own interval.
    An initial full sweep runs on startup, then checks are dispatched independently
    so fast checks (queue, providers, plex, ...) run every few minutes while slow
    checks (repair, janitor, missing_seasons, no_upgrade_profile) run every 30 min."""
    log.info("[scheduler] fast=%s, slow=%s, tick=%s, concurrency=%d",
             _human(FAST_INTERVAL), _human(SLOW_INTERVAL), _human(SCHEDULER_TICK), SCHEDULER_CONCURRENCY)
    sweep()
    now = time.time()
    last_run = {cid: now for cid, en, _, _, _, _ in CHECKS if en}
    while not stop.wait(SCHEDULER_TICK):
        now = time.time()
        for cid, en, fn, speed, default_iv, _ in CHECKS:
            if not en:
                continue
            interval = _check_interval(cid, speed, default_iv)
            if now - last_run.get(cid, 0) >= interval:
                elapsed = now - last_run.get(cid, 0)
                last_run[cid] = now
                log.info("[scheduler] dispatching %s (interval=%s, last=%.0fs ago)", cid, _human(interval), elapsed)
                threading.Thread(target=_run_scheduled_check, args=(cid, fn), daemon=True).start()
