"""Post-repair search verification."""
import re
import time
from datetime import datetime, timezone
from ...config import REPAIR_VERIFY_DEADLINE, REPAIR_HIERARCHICAL_FALLBACK, log
from ...clients import INSTANCES

def _repair_verify_key(arr_name, title, season_number=None):
    """Stable key for a pending repair search entry."""
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower())[:40]
    if season_number is not None:
        return "%s:%s:s%02d" % (arr_name, slug, season_number)
    return "%s:%s" % (arr_name, slug)


def _repair_verify_pending(state):
    """Check any in-flight repair searches from previous sweeps.
    State entry per pending item (keyed by '<arr_name>:<title_slug>[:sNN]'):
      {cmd_id, media_id, entity_ids, title, search_ts, arr_name, strategy, season_number, needs_fallback}
    Flow per item each sweep:
      1. If command_id present, poll /command/{id} — log when done/failed.
      2. Poll /history for a new 'grabbed' event after search_ts.
      3. On confirmed grab: log indexer + sourceTitle, remove from pending.
      4. On deadline exceeded without grab: log warning, remove from pending.
    """
    pv = state.setdefault("__repair_verify__", {})
    if not pv:
        log.debug("[repair:verify] no pending searches to verify")
        return
    log.debug("[repair:verify] checking %d pending search(es)", len(pv))
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
            strategy = v.get("strategy", "season")
            # Hierarchical fallback: series -> season -> episode (only for entries created by hierarchical search)
            if v.get("hierarchical") and REPAIR_HIERARCHICAL_FALLBACK and strategy in ("series", "season"):
                next_strategy = "season" if strategy == "series" else "episode"
                log.warning("[repair:verify:%s] no grab for '%s' (%s search) within deadline -> falling back to %s search",
                            arr.name, title, strategy, next_strategy)
                v["strategy"] = next_strategy
                v["needs_fallback"] = True
                v["deadline"] = time.time() + REPAIR_VERIFY_DEADLINE
                v["cmd_done"] = True  # old command is done; wait for new one
                v.pop("cmd_id", None)
                continue
            log.warning("[repair:verify:%s] no grab confirmed for '%s' within deadline — search may have stalled",
                        arr.name, title)
            expired.append(key)

    for key in expired:
        pv.pop(key, None)
def _repair_record_verify(state, arr, title, cmd_id, media_id, entity_ids,
                                    strategy="season", season_number=None, series_id=None,
                                    hierarchical=False):
    """Store a pending verification entry so the next sweep can check if the grab landed."""
    pv = state.setdefault("__repair_verify__", {})
    key = _repair_verify_key(arr.name, title, season_number)
    pv[key] = {
        "arr_name":       arr.name,
        "title":          title,
        "cmd_id":         cmd_id if isinstance(cmd_id, int) else None,
        "media_id":       media_id,
        "entity_ids":     entity_ids or [],
        "search_ts":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "deadline":       time.time() + REPAIR_VERIFY_DEADLINE,
        "strategy":       strategy,
        "season_number":  season_number,
        "series_id":      series_id if series_id is not None else media_id,
        "hierarchical":   hierarchical,
    }


def _repair_process_fallbacks(state):
    """Issue search commands for any pending repairs that have fallen back to a narrower strategy.
    Returns the number of fallback commands issued."""
    pv = state.get("__repair_verify__", {})
    if not pv:
        return 0
    arr_map = {a.name: a for a in INSTANCES}
    issued = 0
    for key, v in list(pv.items()):
        if not v.get("needs_fallback"):
            continue
        arr = arr_map.get(v.get("arr_name"))
        if not arr or arr.kind != "sonarr":
            v.pop("needs_fallback", None)
            continue
        strategy = v.get("strategy", "season")
        sid = v.get("series_id")
        sn = v.get("season_number")
        epids = v.get("entity_ids", [])
        if strategy == "series":
            cmd_id = arr.command("SeriesSearch", seriesId=sid)
        elif strategy == "season" and sn is not None:
            cmd_id = arr.command("SeasonSearch", seriesId=sid, seasonNumber=sn)
        elif strategy == "episode" and epids:
            cmd_id = arr.command("EpisodeSearch", episodeIds=epids)
        else:
            log.debug("[repair:verify:%s] cannot issue fallback for %s (strategy=%s, sn=%s, epids=%s)",
                      arr.name, key, strategy, sn, epids)
            v.pop("needs_fallback", None)
            continue
        if cmd_id:
            v["cmd_id"] = cmd_id if isinstance(cmd_id, int) else None
            v["needs_fallback"] = False
            v["cmd_done"] = False
            v["search_ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            v["deadline"] = time.time() + REPAIR_VERIFY_DEADLINE
            log.warning("[repair:verify:%s] fallback %s search issued for %s S%02d",
                        arr.name, strategy, v.get("title", key), sn or 0)
            issued += 1
        else:
            # command failed; leave needs_fallback set so next sweep retries
            log.warning("[repair:verify:%s] fallback %s search failed for %s, will retry",
                        arr.name, strategy, v.get("title", key))
    return issued
