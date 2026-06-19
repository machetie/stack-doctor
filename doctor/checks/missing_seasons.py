"""Check: missing_seasons."""
import os
import sys
import json
import re
import time
import signal
import subprocess
import threading
import logging
import logging.handlers
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import email.utils
from datetime import datetime, timezone
from ..config import *
from ..clients import *
from ..state import *

def _season_still_airing(episodes, season_number):
    """Return True if *season_number* has at least one episode whose air date is in the future.
    This prevents triggering a SeasonSearch for a season that is still actively airing
    (only some episodes have been released so far)."""
    now = datetime.now(timezone.utc)
    for ep in episodes:
        if ep.get("seasonNumber") != season_number:
            continue
        air = ep.get("airDateUtc") or ""
        if not air:
            continue
        try:
            dt = datetime.fromisoformat(air.replace("Z", "+00:00"))
            if dt > now:
                return True
        except (ValueError, TypeError):
            pass
    return False

def _series_added_ts(ser):
    """Parse Sonarr's 'added' timestamp into a Unix epoch, or 0 if unknown."""
    added_str = ser.get("added") or ""
    try:
        return email.utils.parsedate_to_datetime(added_str).timestamp() if added_str else 0
    except Exception:
        return 0

def _priority_key(c):
    """Sort key for missing-season candidates.

    'added'  -> oldest series first (smallest timestamp), then largest seasons.
    'episodes' -> largest seasons first, then oldest series.
    'mixed'  -> oldest series first, then largest seasons (the default).
    Unknown added dates are pushed to the end."""
    added = c.get("added_ts", 0) or float("inf")
    total = c.get("total_episodes", 0)
    if MS_SORT_BY == "episodes":
        return (-total, added)
    # mixed and added both prioritize age, then size
    return (added, -total)

def _gather_candidates(ms, now, min_age_secs, recheck, backfill):
    """Walk every Sonarr instance and collect all seasons that are monitored, fully aired,
    have zero episode files, and are old enough to be considered for a search.

    Returns (candidates, skipped, airing)."""
    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    candidates = []
    skipped = 0
    airing = 0
    for arr in sonarr_instances:
        try:
            all_series = arr.series()
        except Exception as e:
            log.warning("[missing_seasons:%s] failed to fetch series: %s", arr.name, str(e)[:60])
            continue
        for ser in all_series:
            if not ser.get("monitored"):
                continue
            sid = ser.get("id")
            title = (ser.get("title") or "")[:60]
            added_ts = _series_added_ts(ser)
            if added_ts and (now - added_ts) < min_age_secs:
                continue  # too new, give Sonarr time to grab it first
            ep_cache = None
            for season in (ser.get("seasons") or []):
                sn = season.get("seasonNumber", 0)
                if sn == 0:
                    continue
                if not season.get("monitored"):
                    continue
                stats = season.get("statistics") or {}
                if stats.get("episodeFileCount", 0) > 0:
                    continue
                if stats.get("totalEpisodeCount", 0) == 0:
                    continue
                key = "%s:%d:%d" % (arr.name, sid, sn)
                if not backfill and (now - ms.get(key, 0) < recheck):
                    skipped += 1
                    continue
                if ep_cache is None:
                    try:
                        ep_cache = arr.episodes(sid)
                    except Exception:
                        ep_cache = []
                if _season_still_airing(ep_cache, sn):
                    airing += 1
                    log.debug("[missing_seasons:%s] skipping still-airing season: %s S%02d", arr.name, title, sn)
                    continue
                candidates.append({
                    "arr": arr,
                    "title": title,
                    "sid": sid,
                    "sn": sn,
                    "key": key,
                    "added_ts": added_ts,
                    "total_episodes": stats.get("totalEpisodeCount", 0),
                })
    return candidates, skipped, airing

def _process_candidates(ms, candidates, now, backfill):
    """Issue SeasonSearch commands for up to MS_MAX_ACTIONS candidates (or all in backfill mode).

    Returns the number of searches issued."""
    max_actions = 0 if backfill else MS_MAX_ACTIONS
    acted = 0
    batch_size = MS_BACKFILL_BATCH if backfill else 0
    for c in candidates:
        if max_actions and acted >= max_actions:
            break
        if DRY_RUN:
            log.info("[missing_seasons:%s] DRY-RUN would search: %s S%02d",
                     c["arr"].name, c["title"], c["sn"])
            ms[c["key"]] = now
            acted += 1
            continue
        if c["arr"].command("SeasonSearch", seriesId=c["sid"], seasonNumber=c["sn"]):
            log.warning("[missing_seasons:%s] 0 files in monitored season -> SeasonSearch: %s S%02d",
                        c["arr"].name, c["title"], c["sn"])
            ms[c["key"]] = now
            acted += 1
        if backfill and batch_size > 0 and acted > 0 and acted % batch_size == 0:
            time.sleep(MS_BACKFILL_DELAY)
    return acted

def _run_missing_seasons(backfill=False):
    """Core implementation shared between the scheduled check and the one-shot backfill."""
    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    if not sonarr_instances:
        log.warning("[missing_seasons] no Sonarr instances configured")
        return
    with state_transaction() as state:
        ms = state.setdefault("__missing_seasons__", {})
        now = time.time()
        recheck = 0 if backfill else MS_RECHECK
        candidates, skipped, airing = _gather_candidates(ms, now, MS_MIN_AGE_HOURS * 3600, recheck, backfill)
        candidates.sort(key=_priority_key)
        acted = _process_candidates(ms, candidates, now, backfill)
        label = "missing_seasons:backfill" if backfill else "missing_seasons"
        log.info("[%s] searched %d season(s), skipped %d (cooldown), %d (still airing)",
                 label, acted, skipped, airing)

def check_missing_seasons():
    """Scheduled check: capped by MS_MAX_ACTIONS and respects MS_RECHECK cooldown."""
    _run_missing_seasons(backfill=False)

def backfill_missing_seasons():
    """One-shot backfill: ignore cap and recheck, search every eligible missing season once.

    Useful for clearing a large backlog. Normal scheduling resumes afterwards (if this was
    invoked via `python -m doctor --backfill-missing-seasons`)."""
    _run_missing_seasons(backfill=True)
