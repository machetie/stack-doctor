"""Dead symlink detection and repair actions."""
import os
import re
from datetime import datetime, timezone
from ...config import (
    DRY_RUN, REPAIR_LIBS, REPAIR_UNMONITORED, REPAIR_VERIFY,
    REPAIR_HIERARCHICAL_SEARCH, REPAIR_SEASON_ENDED_THRESHOLD, log,
)
from .common import _dead_symlink
from .verify import _repair_record_verify, _repair_verify_key

def _release_rel(target):
    """Return the path of a symlink target relative to the /__all__ or /complete root.

    Example: /mnt/zurg/__all__/RELEASE/file.mkv -> RELEASE/file.mkv
    """
    mm = re.search(r"/(?:__all__|complete)/(.+)$", target)
    if not mm:
        return None
    return mm.group(1).lstrip("/")

def _is_janitor_dead(fp, janitor_dead):
    """Return True if the symlink target is recorded as dead by the janitor.

    janitor_dead is a dict keyed by relative path (RELEASE/file.mkv) or by filename.
    """
    if not janitor_dead or not os.path.islink(fp):
        return False
    try:
        target = os.readlink(fp)
    except Exception:
        return False
    rel = _release_rel(target)
    if rel is None:
        return False
    if rel in janitor_dead:
        return True
    return os.path.basename(rel) in janitor_dead

def _parse_janitor_dead_path(orig_path, series):
    """Parse a quarantined library path into (series, season_number, episode_numbers).

    Expected path layout: /.../Series Name (Year) {imdb-xxx}/Season 01/Series Name - S01E02.mkv
    """
    for ser in series:
        sp = ser.get("path")
        if not sp or not orig_path.startswith(sp + "/"):
            continue
        rel = orig_path[len(sp) + 1:]
        parts = rel.split("/", 1)
        if len(parts) != 2:
            continue
        season_folder, filename = parts
        sm = re.match(r"Season\s+(\d+)", season_folder, re.I)
        if not sm:
            continue
        sn = int(sm.group(1))
        eps = _parse_episodes_from_filename(filename, sn)
        if eps:
            return ser, sn, eps
    return None


def _parse_episodes_from_filename(filename, season_number):
    """Extract episode numbers for a given season from a filename.

    Handles S01E05, S01E01-E02, S01E01E02, etc.
    """
    episodes = []
    for m in re.finditer(r"[Ss](\d+)[Ee](\d+)", filename):
        s, e = m.group(1), m.group(2)
        if int(s) != season_number:
            continue
        start = int(e)
        episodes.append(start)
        # Look for a trailing continuation: S01E01-E02 or S01E01E02
        rest = filename[m.end():]
        extra_m = re.match(r"(?:[-Ee][Ee]?)(\d+)", rest)
        if extra_m:
            end = int(extra_m.group(1))
            if end > start:
                episodes.extend(range(start + 1, end + 1))
            elif end != start:
                episodes.append(end)
    return episodes


def _normalize_title(title):
    """Normalize a title for fuzzy matching by removing punctuation and lowercasing."""
    return re.sub(r"\s+", " ", title.replace(".", "").replace("'", "").replace("-", " ")).strip().lower()


def _guess_series_from_release(release_name, series):
    """Guess a Sonarr series from a release name like Mr.Robot.S01-S04.1080p...."""
    # Strip season/episode ranges and everything after the first Sxx or year marker.
    cleaned = re.sub(r"[Ss]\d+([-Ee]\d+)?.*$", "", release_name)
    cleaned = re.sub(r"\s+\d{4}\s+.*$", "", cleaned)
    cleaned = cleaned.replace(".", " ").replace("_", " ").strip()
    cleaned_norm = _normalize_title(cleaned)
    best = None
    for ser in series:
        title = _normalize_title(ser.get("title", ""))
        sort_title = _normalize_title(ser.get("sortTitle", ""))
        if title == cleaned_norm or sort_title == cleaned_norm:
            return ser
        if cleaned_norm.startswith(title + " ") or cleaned_norm.startswith(sort_title + " "):
            best = ser
    return best

def _radarr_dead_files(movies, state=None, processed=None):
    """Yield (movie_id, title, movie_file_id) for monitored movies whose on-disk symlink is dead.
    Skips unmonitored movies unless REPAIR_UNMONITORED.
    Also considers files that the janitor has recorded as dead in the persistent state.
    """
    janitor_dead = (state or {}).get("__janitor_dead_files__", {})
    for m in movies:
        if not m.get("monitored", True) and not REPAIR_UNMONITORED:
            continue
        mid = m.get("id")
        mf = m.get("movieFile") or {}
        fp = mf.get("path")
        if not mid or not fp:
            continue
        if REPAIR_LIBS and not any(fp.startswith(p) for p in REPAIR_LIBS):
            continue
        if _dead_symlink(fp) or _is_janitor_dead(fp, janitor_dead):
            if processed is not None:
                for key, info in list(janitor_dead.items()):
                    if info.get("orig") == fp:
                        processed.append(key)
            yield mid, (m.get("title") or "")[:70], mf.get("id")

def _sonarr_dead_files(arr, series, state=None, processed=None):
    """Yield (series_id, title, season_number, [episode_file_ids], series, [episode_ids])
    per season that has dead symlinks or files flagged dead by the janitor.

    Skips unmonitored series unless REPAIR_UNMONITORED.
    episode_ids is populated when the janitor has already removed the file and we know the
    specific missing episode(s) from the quarantined library path.
    """
    janitor_dead = (state or {}).get("__janitor_dead_files__", {})
    for ser in series:
        if not ser.get("monitored", True) and not REPAIR_UNMONITORED:
            continue
        sid = ser.get("id")
        if not sid:
            continue
        title = (ser.get("title") or "")[:70]
        try:
            efiles = arr.episode_files(sid)
            eps = arr.episodes(sid)
        except Exception:
            continue
        # episodeFile objects may not include seasonNumber, so cross-reference with episodes
        efid_to_season = {}
        efid_to_epid = {}
        for ep in eps:
            if ep.get("episodeFileId"):
                efid_to_season[ep["episodeFileId"]] = ep.get("seasonNumber")
                efid_to_epid[ep["episodeFileId"]] = ep.get("id")
        dead_by_season = {}
        for ef in efiles:
            fp = ef.get("path")
            if not fp:
                continue
            if REPAIR_LIBS and not any(fp.startswith(p) for p in REPAIR_LIBS):
                continue
            if not _dead_symlink(fp) and not _is_janitor_dead(fp, janitor_dead):
                continue
            efid = ef.get("id")
            if not efid:
                continue
            sn = ef.get("seasonNumber") if ef.get("seasonNumber") is not None else efid_to_season.get(efid)
            if sn is None:
                continue
            entry = dead_by_season.setdefault(sn, {"efids": [], "epids": set(), "series": ser})
            entry["efids"].append(efid)
            if efid_to_epid.get(efid):
                entry["epids"].add(efid_to_epid[efid])
            # Mark the janitor entry as processed if it matches this file.
            if processed is not None:
                for key, info in list(janitor_dead.items()):
                    if info.get("orig") == fp:
                        processed.append(key)
        # Handle files the janitor already removed (no episodeFile record left).
        if janitor_dead:
            for key, info in list(janitor_dead.items()):
                orig = info.get("orig")
                parsed = None
                if orig:
                    parsed = _parse_janitor_dead_path(orig, series)
                if not parsed:
                    # Fallback: guess the series from the release name and parse SxxEyy from filename.
                    if "/" in key:
                        release_name, filename = key.rsplit("/", 1)
                    else:
                        release_name, filename = "", key
                    jser = _guess_series_from_release(release_name, series)
                    if jser:
                        jsn = None
                        for m in re.finditer(r"[Ss](\d+)[Ee](\d+)", filename):
                            jsn = int(m.group(1))
                            break
                        if jsn is not None:
                            jepisodes = _parse_episodes_from_filename(filename, jsn)
                            parsed = (jser, jsn, jepisodes)
                if not parsed:
                    continue
                jser, jsn, jepisodes = parsed
                jsid = jser.get("id")
                if jsid != sid:
                    continue
                try:
                    jeps = arr.episodes(jsid)
                except Exception:
                    continue
                jepids = [e.get("id") for e in jeps
                          if e.get("seasonNumber") == jsn
                          and e.get("episodeNumber") in jepisodes
                          and e.get("id")]
                if not jepids:
                    continue
                entry = dead_by_season.setdefault(jsn, {"efids": [], "epids": set(), "series": jser})
                entry["epids"].update(jepids)
                if processed is not None and key not in processed:
                    processed.append(key)
        for sn, data in dead_by_season.items():
            yield sid, title, sn, data["efids"], data["series"], sorted(data["epids"])

def _repair_radarr_movie(arr, mid, title, mfid, state=None):
    """Delete a dead movie file record, toggle the movie monitor off+on, and re-search."""
    if DRY_RUN:
        log.info("[repair:%s] DRY-RUN would delete dead file + re-search movie: %s", arr.name, title)
        return True
    if mfid:
        arr.delete_file(mfid)
    # toggle monitor off+on to force the arr to refresh the title's availability state
    try:
        arr.set_monitored([mid], False)
        arr.set_monitored([mid], True)
    except Exception as e:
        log.warning("[repair:%s] monitor toggle failed for movie %s: %s", arr.name, title, str(e)[:70])
    cmd_id = arr.command("MoviesSearch", movieIds=[mid])
    log.warning("[repair:%s] dead symlink -> deleted file + re-searching movie: %s", arr.name, title)
    if REPAIR_VERIFY and state is not None:
        _repair_record_verify(state, arr, title, cmd_id, mid, [mid], hierarchical=False)
    return True

def _sonarr_search_strategy(series, season_number):
    """Choose the broadest Sonarr search for a dead season based on airing status.

    Returns (command_name, command_kwargs, strategy_tag).
    - Ended show -> SeriesSearch (multi-season / complete-series packs).
    - Ended season (and show still continuing) -> SeasonSearch (season packs).
    - Ongoing season -> EpisodeSearch (specific episode(s)).
    """
    if not REPAIR_HIERARCHICAL_SEARCH:
        return "SeasonSearch", {"seriesId": series["id"], "seasonNumber": season_number}, "season"

    # Show ended -> try for a complete/multi-season pack first
    if series.get("ended") or series.get("status") == "ended":
        return "SeriesSearch", {"seriesId": series["id"]}, "series"

    # Season ended -> season pack
    seasons = series.get("seasons", [])
    season = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
    if season:
        prev_air = (season.get("statistics") or {}).get("previousAiring")
        if prev_air:
            try:
                last_air = datetime.fromisoformat(prev_air.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - last_air).total_seconds()
                if age >= REPAIR_SEASON_ENDED_THRESHOLD:
                    return "SeasonSearch", {"seriesId": series["id"], "seasonNumber": season_number}, "season"
            except Exception:
                pass

    # Ongoing season -> search only the affected episodes
    return "EpisodeSearch", {}, "episode"


def _repair_sonarr_season(arr, sid, title, season_number, efids, state=None, series=None, epids=None):
    """Delete all dead episode file records for a season, toggle the season's episodes off+on, and
    trigger the appropriate search command (Series/Season/Episode) based on airing status.

    epids may be the specific episode IDs that are missing; if not provided, all episodes in the
    season are used for EpisodeSearch.
    """
    if DRY_RUN:
        log.info("[repair:%s] DRY-RUN would delete %d dead file(s) + re-search season: %s S%02d",
                 arr.name, len(efids), title, season_number)
        return True
    for efid in efids:
        arr.delete_file(efid)
    # toggle every episode in this season off then on to force a fresh availability state
    all_epids = []
    if epids is None:
        try:
            eps = arr.episodes(sid)
            epids = [e.get("id") for e in eps if e.get("seasonNumber") == season_number and e.get("id")]
            all_epids = epids
        except Exception as e:
            log.warning("[repair:%s] monitor toggle failed for %s S%02d: %s", arr.name, title, season_number, str(e)[:70])
    else:
        all_epids = epids
    if all_epids:
        try:
            arr.set_monitored(all_epids, False)
            arr.set_monitored(all_epids, True)
        except Exception as e:
            log.warning("[repair:%s] monitor toggle failed for %s S%02d: %s", arr.name, title, season_number, str(e)[:70])
    cmd_name, cmd_kwargs, strategy = _sonarr_search_strategy(series or {"id": sid}, season_number)
    if cmd_name == "EpisodeSearch":
        cmd_kwargs = {"episodeIds": epids or all_epids}
    elif cmd_name == "SeasonSearch":
        cmd_kwargs = {"seriesId": sid, "seasonNumber": season_number}
    elif cmd_name == "SeriesSearch":
        cmd_kwargs = {"seriesId": sid}
    cmd_id = arr.command(cmd_name, **cmd_kwargs)
    log.warning("[repair:%s] dead symlinks -> deleted %d file(s) + re-searching %s: %s S%02d (strategy=%s)",
                arr.name, len(efids), cmd_name, title, season_number, strategy)
    if REPAIR_VERIFY and state is not None:
        _repair_record_verify(state, arr, title, cmd_id, sid, epids or all_epids,
                              strategy=strategy, season_number=season_number, series_id=sid,
                              hierarchical=REPAIR_HIERARCHICAL_SEARCH)
    return True
