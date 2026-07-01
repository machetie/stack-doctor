"""Check: force_import (importarr-style).

Sonarr/Radarr sometimes refuse to auto-import a release that matches the series/movie
by ID but whose title does not match any known alias. This is common with obfuscated
release names, bad metadata, or anime where TVDB/TMDb aliases are missing.

This check scans the queue for that exact error, fetches the manual-import candidates
for the series/movie, and submits a ManualImport command. If the import fails (or no
candidate is found) we optionally fall back to the standard remove+re-search flow.
"""
import time
from ..config import (
    DRY_RUN, FI_FALLBACK, FI_IMPORT_MODE, FI_MAX_ACTIONS, FI_MIN_STRIKES,
    FI_RECHECK, BLOCKLIST, REMOVE_CLIENT, log,
)
from ..clients import INSTANCES
from ..state import _churn_record, state_transaction

# The exact message text varies slightly between Sonarr and Radarr, but all of
# these result in a release sitting in the queue that Sonarr/Radarr won't auto-import
# but that a ManualImport command can usually push through successfully.
_MATCH_PHRASES = (
    # obfuscated/misnamed release — matched by TVDB/TMDb ID but title doesn't match
    "matched to series by id",
    "matched to movie by id",
    "automatic import is not possible",
    "found matching series via grab history",
    "found matching movie via grab history",
    # sample-detection failure — common on FUSE/rclone mounts where reads are slow
    "unable to determine if file is a sample",
    "unable to determine if", # covers slight wording variations
)


def _is_matched_by_id(rec):
    """Return True if this queue record is the target failure type."""
    msgs = []
    for sm in (rec.get("statusMessages") or []):
        msgs += [m for m in (sm.get("messages") or [])]
    if rec.get("errorMessage"):
        msgs.append(rec["errorMessage"])
    joined = " ".join(msgs).lower()
    return any(p in joined for p in _MATCH_PHRASES)


def _target_id(arr, rec):
    """Episode id (sonarr) or movie id (radarr) this queue record is for."""
    if arr.kind == "sonarr":
        return rec.get("episodeId")
    if arr.kind == "radarr":
        return rec.get("movieId")
    return None


def _media_id(arr, rec):
    """Series id (sonarr) or movie id (radarr) for the manualimport lookup."""
    if arr.kind == "sonarr":
        return rec.get("seriesId")
    if arr.kind == "radarr":
        return rec.get("movieId")
    return None


def _release_folder(rec):
    """Best guess at the download folder the files are sitting in."""
    # outputPath is the top-level download folder in the *arr queue record.
    return rec.get("outputPath") or rec.get("downloadPath") or ""


def _pick_candidates(arr, rec, candidates):
    """Return the candidate(s) that belong to this queue record.

    For Sonarr we match by episodeId(s). For Radarr we prefer the candidate whose
    path matches the queue item's outputPath; otherwise we take all returned files
    for the movie (Radarr manualimport already filters by movieId).
    """
    target = _target_id(arr, rec)
    folder = _release_folder(rec)
    out = []
    for cand in candidates:
        if arr.kind == "sonarr":
            ep_ids = cand.get("episodeIds") or []
            if target and ep_ids:
                if target in ep_ids:
                    out.append(cand)
            elif folder and folder in (cand.get("path") or cand.get("folderName", "")):
                out.append(cand)
        elif arr.kind == "radarr":
            cand_path = cand.get("path") or cand.get("relativePath") or ""
            if folder and cand_path:
                if folder in cand_path:
                    out.append(cand)
            else:
                out.append(cand)
    return out


def _dedupe_candidates(candidates):
    """Remove duplicate paths (manualimport can return the same file twice)."""
    seen = set()
    out = []
    for c in candidates:
        p = c.get("path") or c.get("relativePath")
        if p in seen:
            continue
        seen.add(p)
        out.append(c)
    return out


def _check_force_import(only=None):
    with state_transaction() as state:
        fi_state = state.setdefault("force_import", {})
        now = time.time()
        actions = 0

        for arr in INSTANCES:
            if only and arr.name.lower() != only.lower():
                continue
            if arr.kind not in ("sonarr", "radarr"):
                continue

            recs = arr.queue()
            if recs is None:
                continue

            hits = 0
            for rec in recs:
                if not _is_matched_by_id(rec):
                    continue
                hits += 1

                iid = str(rec.get("id"))
                media_id = _media_id(arr, rec)
                target = _target_id(arr, rec)
                title = (rec.get("title") or rec.get("sourceTitle") or "unknown")[:70]

                if not media_id:
                    log.debug("[force_import:%s] no series/movie id for %s", arr.name, title)
                    continue

                # recheck cooldown
                key = "%s:%s:%s" % (arr.name, media_id, target or iid)
                last = fi_state.get(key, 0)
                if now - last < FI_RECHECK:
                    log.debug("[force_import:%s] %s still in recheck cooldown", arr.name, title)
                    continue

                strikes_key = "%s:strikes:%s" % (arr.name, key)
                strikes = fi_state.get(strikes_key, 0) + 1
                fi_state[strikes_key] = strikes
                if strikes < FI_MIN_STRIKES:
                    log.info("[force_import:%s] matched-by-ID strike %d/%d: %s",
                             arr.name, strikes, FI_MIN_STRIKES, title)
                    continue

                # fetch manualimport candidates
                candidates = arr.manualimport(
                    series_id=media_id if arr.kind == "sonarr" else None,
                    movie_id=media_id if arr.kind == "radarr" else None,
                )
                if not candidates:
                    log.info("[force_import:%s] no manualimport candidates for %s", arr.name, title)
                    if FI_FALLBACK:
                        _fallback(state, arr, rec, title, fi_state, key)
                    continue

                picked = _pick_candidates(arr, rec, candidates)
                picked = _dedupe_candidates(picked)
                if not picked:
                    log.info("[force_import:%s] no matching candidate for %s", arr.name, title)
                    if FI_FALLBACK:
                        _fallback(state, arr, rec, title, fi_state, key)
                    continue

                if actions >= FI_MAX_ACTIONS:
                    log.info("[force_import:%s] max actions reached (%d), skipping %s",
                             arr.name, FI_MAX_ACTIONS, title)
                    continue

                if DRY_RUN:
                    log.info("[force_import:%s] WOULD manual import %d file(s): %s",
                             arr.name, len(picked), title)
                    actions += 1
                    fi_state[key] = now
                    fi_state.pop(strikes_key, None)
                    continue

                # Pass the manualimport candidate back to the command endpoint almost
                # untouched.  Remove only the UI-only fields that are known to be rejected.
                files = []
                for c in picked:
                    f = dict(c)
                    for k in ("id", "rejections", "customFormatScore", "isCustomFormatScoreCalculated"):
                        f.pop(k, None)
                    files.append(f)

                cmd_id = arr.manualimport_command(files, import_mode=FI_IMPORT_MODE)
                if cmd_id:
                    actions += 1
                    fi_state[key] = now
                    fi_state.pop(strikes_key, None)
                    log.info("[force_import:%s] manual import command %s (%d file(s)): %s",
                             arr.name, cmd_id, len(files), title)
                else:
                    log.warning("[force_import:%s] manual import command failed: %s", arr.name, title)
                    if FI_FALLBACK:
                        _fallback(state, arr, rec, title, fi_state, key)

            if hits:
                log.info("[force_import:%s] %d matched-by-ID item(s), %d acted",
                         arr.name, hits, actions)
            else:
                log.debug("[force_import:%s] no matched-by-ID items", arr.name)


def _fallback(state, arr, rec, title, fi_state, key):
    """Remove the queue item and trigger a re-search as a last resort."""
    if DRY_RUN:
        log.info("[force_import:%s] WOULD fallback remove (blocklist=%s): %s",
                 arr.name, BLOCKLIST, title)
        fi_state[key] = time.time()
        return
    parked = _churn_record(state, arr, rec, title)
    try:
        q = "removeFromClient=%s&blocklist=%s" % (str(REMOVE_CLIENT).lower(), str(BLOCKLIST).lower())
        arr._req("DELETE", "/queue/%d?%s" % (rec["id"], q))
        fi_state[key] = time.time()
        log.info("[force_import:%s] fallback remove (blocklist=%s)%s: %s",
                 arr.name, BLOCKLIST,
                 " [parked, no re-search]" if parked else " -> re-search", title)
    except Exception as e:
        log.warning("[force_import:%s] fallback remove failed: %s", arr.name, e)


def check_force_import(only=None):
    """Entry point used by the scheduler."""
    try:
        _check_force_import(only)
    except Exception as e:
        log.error("[force_import] check error: %s", e)
