"""Check: multipack — find cached multi-season torrent packs and push them to the download client.

Sonarr's automatic SeasonSearch only grabs single-season releases; it hard-rejects any torrent
whose title spans multiple seasons (e.g. "Show S01-S05").  When you search manually in the
Sonarr UI and pick such a pack, Sonarr bypasses its own rejection logic via /api/v3/release/push.
This check automates exactly that:

  1. Only consider series that missing_seasons has already SeasonSearch-ed at least once —
     meaning Sonarr tried per-season searches but the show is still incomplete.
  2. Call Sonarr's release-search API for Season 1 (same data the UI shows).
  3. Filter to multi-season packs: fullSeason=True AND title matches S\\d+-S\\d+.
  4. For each candidate, verify it is debrid-cached by checking whether its folder
     name exists under the zurg __all__ mount (DECY_MOUNT_TEST).
  5. Push the first cached pack via Sonarr's /api/v3/release/push, which bypasses
     quality/format rejection and delivers it straight to decypharr.

By scoping to already-searched series, the check avoids hammering Prowlarr with
release searches for shows that missing_seasons hasn't tried yet.
"""
import os
import re
import time
from ..config import *
from ..clients import *
from ..state import *

# Detects multi-season pack titles: S01-S05, S1-S3, S01-S02, etc.
_MULTI_SEASON_RE = re.compile(r'\bS(\d+)[.\-]S(\d+)\b', re.IGNORECASE)

def _zurg_all_dir():
    """Return the zurg __all__ directory path if accessible, else None."""
    mount = DECY_MOUNT_TEST  # e.g. /mnt/zurg/__all__
    if mount and os.path.isdir(mount):
        return mount
    return None

def _normalize(s):
    """Strip all non-alphanumeric chars for fuzzy folder-name matching."""
    return re.sub(r'[^a-z0-9]', '', s.lower())

def _is_cached(pack_title, zurg_dir):
    """Return True if a folder whose normalised name matches pack_title exists in zurg_dir."""
    norm = _normalize(pack_title)
    try:
        for entry in os.listdir(zurg_dir):
            if _normalize(entry) == norm:
                return True
    except OSError:
        pass
    return False

def _series_searched_by_missing_seasons(state, arr_name):
    """Return set of series IDs that missing_seasons has already SeasonSearch-ed at least once."""
    ms = state.get("__missing_seasons__", {})
    ids = set()
    prefix = arr_name + ":"
    for key in ms:
        if key.startswith(prefix):
            parts = key.split(":")
            if len(parts) == 3 and parts[2].isdigit() and not parts[2] == "series":
                try:
                    ids.add(int(parts[1]))
                except ValueError:
                    pass
    return ids

def _incomplete_series(arr):
    """Return dict of series_id -> title for monitored series with ≥1 incomplete season."""
    try:
        all_series = arr.series()
    except Exception as e:
        log.warning("[multipack:%s] failed to fetch series: %s", arr.name, str(e)[:60])
        return {}
    result = {}
    for ser in all_series:
        if not ser.get("monitored"):
            continue
        for season in (ser.get("seasons") or []):
            sn = season.get("seasonNumber", 0)
            if sn == 0 or not season.get("monitored"):
                continue
            st = season.get("statistics") or {}
            tc = st.get("totalEpisodeCount", 0)
            fc = st.get("episodeFileCount", 0)
            if tc > 0 and fc < tc:
                result[ser["id"]] = (ser.get("title") or "")[:60]
                break
    return result

def check_multipack():
    """For incomplete Sonarr series that missing_seasons has already tried, search for and push
    cached multi-season packs that Sonarr would normally reject."""
    if not MULTIPACK_ENABLED:
        return
    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    if not sonarr_instances:
        return

    zurg_dir = _zurg_all_dir()
    if not zurg_dir:
        log.warning("[multipack] DECY_MOUNT_TEST not accessible — cannot verify debrid cache")
        return

    with state_transaction() as state:
        mp = state.setdefault("__multipack__", {})
        now = time.time()
        acted = 0

        for arr in sonarr_instances:
            if acted >= MULTIPACK_MAX_ACTIONS:
                break

            # Only target series missing_seasons has already tried
            already_searched = _series_searched_by_missing_seasons(state, arr.name)
            if not already_searched:
                log.debug("[multipack:%s] no series searched by missing_seasons yet — skipping", arr.name)
                continue

            incomplete = _incomplete_series(arr)
            # Intersection: incomplete AND already tried by missing_seasons
            candidates = {sid: title for sid, title in incomplete.items() if sid in already_searched}
            log.debug("[multipack:%s] %d series targeted (incomplete + already SeasonSearched)",
                      arr.name, len(candidates))

            for sid, title in candidates.items():
                if acted >= MULTIPACK_MAX_ACTIONS:
                    break

                state_key = "%s:%d" % (arr.name, sid)
                if now - mp.get(state_key, 0) < MULTIPACK_RECHECK:
                    log.debug("[multipack:%s] cooldown: %s", arr.name, title)
                    continue

                log.debug("[multipack:%s] searching releases for: %s", arr.name, title)
                releases = arr.release_search(sid, season_number=1)
                if not releases:
                    mp[state_key] = now
                    continue

                # Filter to multi-season packs
                packs = [r for r in releases
                         if r.get("fullSeason") and _MULTI_SEASON_RE.search(r.get("title", ""))]
                log.debug("[multipack:%s] %s — %d release(s), %d multi-season pack(s)",
                          arr.name, title, len(releases), len(packs))

                pushed = False
                for pack in packs:
                    pack_title = pack.get("title", "")
                    if not _is_cached(pack_title, zurg_dir):
                        log.debug("[multipack:%s] not cached: %s", arr.name, pack_title[:70])
                        continue
                    if DRY_RUN:
                        log.info("[multipack:%s] DRY-RUN would push: %s -> %s",
                                 arr.name, title, pack_title[:70])
                        mp[state_key] = now
                        acted += 1
                        pushed = True
                        break
                    if arr.release_push(pack):
                        log.warning("[multipack:%s] pushed cached multi-season pack: %s -> %s",
                                    arr.name, title, pack_title[:70])
                        mp[state_key] = now
                        acted += 1
                        pushed = True
                        if MULTIPACK_ITEM_INTERVAL > 0:
                            time.sleep(MULTIPACK_ITEM_INTERVAL)
                        break

                if not pushed:
                    log.debug("[multipack:%s] no cached pack found: %s", arr.name, title)
                    mp[state_key] = now

        if acted:
            log.info("[multipack] pushed %d cached multi-season pack(s) this sweep", acted)
        else:
            log.debug("[multipack] no cached multi-season packs found this sweep")
