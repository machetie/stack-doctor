#!/usr/bin/env python3
"""
Park / unpark operations for the placeholder project.
Run from the host (or any context with /mnt and Sonarr/Tautulli access).
"""
import os, sys, json, shutil, urllib.request, urllib.parse, datetime, time, re

# Import rules engine from same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rules_engine as reng

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SONARR_URL      = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_APIKEY   = os.environ.get("SONARR_APIKEY", "")
def _resolve_dummy_asset():
    # honor the real env var (PLACEHOLDER_DUMMY_ASSET) first, then legacy DUMMY_ASSET,
    # then known good locations. Return the first that exists so a park never fails
    # halfway (removing the real file but not writing a dummy).
    cands = [
        os.environ.get("PLACEHOLDER_DUMMY_ASSET"),
        os.environ.get("DUMMY_ASSET"),
        "/data/assets/dummy.mp4",
        "/data/stack-doctor-data/assets/dummy.mp4",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    # last resort: return the primary env value (write_dummy will guard on existence)
    return os.environ.get("PLACEHOLDER_DUMMY_ASSET") or os.environ.get("DUMMY_ASSET") or "/data/assets/dummy.mp4"

DUMMY_ASSET     = _resolve_dummy_asset()
QUARANTINE_ROOT = os.environ.get("QUARANTINE_ROOT", "/data/scrub-quarantine")
PLACEHOLDER_REGISTRY = os.environ.get("PLACEHOLDER_REGISTRY", "/data/stack-doctor-data/placeholder_registry.json")
PLEX_URL        = os.environ.get("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN      = os.environ.get("PLEX_TOKEN", "")

PLACEHOLDER_EXT = os.environ.get("PLACEHOLDER_EXT", ".mp4")  # extension for dummy file

# --------------------------------------------------------------------------- #
# Sonarr helpers
# --------------------------------------------------------------------------- #
def sonarr_req(method, path, data=None):
    url = SONARR_URL.rstrip("/") + "/api/v3" + path
    headers = {"X-Api-Key": SONARR_APIKEY, "Content-Type": "application/json"}
    if data is not None:
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
        return json.loads(body.decode()) if body else None

def sonarr_delete_episode_file(file_id):
    return sonarr_req("DELETE", "/episodeFile/%d" % file_id)

def sonarr_unmonitor_episodes(episode_ids):
    if not episode_ids:
        return True
    try:
        sonarr_req("PUT", "/episode/monitor", {"episodeIds": list(episode_ids), "monitored": False})
        return True
    except Exception as e:
        print("WARN: unmonitor failed: %s" % e)
        return False

def sonarr_rescan_series(series_id):
    return sonarr_req("POST", "/command", {"name": "RescanSeries", "seriesId": series_id})

def sonarr_search_episodes(episode_ids):
    if not episode_ids:
        return True
    return sonarr_req("POST", "/command", {"name": "EpisodeSearch", "episodeIds": list(episode_ids)})

def sonarr_monitor_episodes(episode_ids):
    if not episode_ids:
        return True
    return sonarr_req("PUT", "/episode/monitor", {"episodeIds": list(episode_ids), "monitored": True})

# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #
def _sanitize(p):
    return p.strip("/").replace("//", "/")

def _placeholder_registry_path():
    return PLACEHOLDER_REGISTRY

def _load_registry():
    try:
        with open(_placeholder_registry_path()) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_registry(reg):
    os.makedirs(os.path.dirname(_placeholder_registry_path()), exist_ok=True)
    with open(_placeholder_registry_path(), "w") as f:
        json.dump(reg, f, indent=2)

def _reg_key(series_id, season, episode):
    return "%d:S%02dE%02d" % (series_id, season, episode)

def quarantine_path(orig_path, qroot=None):
    """Return a quarantine path that preserves the relative structure under qroot."""
    qroot = qroot or QUARANTINE_ROOT
    # Use a timestamped sweep dir
    sweep = datetime.datetime.now().strftime("park_%Y%m%d_%H%M%S")
    rel = _sanitize(orig_path)
    return os.path.join(qroot, sweep, rel)

def move_to_quarantine(orig_path, qpath):
    """Move orig_path (symlink or file) into quarantine, creating parent dirs."""
    os.makedirs(os.path.dirname(qpath), exist_ok=True)
    if os.path.lexists(qpath):
        # collision: append a counter
        base, ext = os.path.splitext(qpath)
        i = 1
        while os.path.lexists(qpath):
            qpath = "%s_%d%s" % (base, i, ext)
            i += 1
    shutil.move(orig_path, qpath)
    return qpath

def write_dummy(orig_path, dummy_asset=None):
    """Write a copy of the dummy asset at orig_path, keeping the SAME filename
    (including extension) so Sonarr import overwrites it on re-grab.
    FAIL-SAFE: the dummy asset MUST exist and be copyable to a temp file BEFORE we
    remove/replace the original, so a bad asset path can never leave the episode with
    no file at all."""
    dummy_asset = dummy_asset or DUMMY_ASSET
    if not os.path.isfile(dummy_asset):
        raise FileNotFoundError("dummy asset missing: %s" % dummy_asset)
    target = orig_path
    tmp = target + ".dummytmp"
    # copy to temp first; if this fails, the original is untouched
    shutil.copy2(dummy_asset, tmp)
    if os.path.lexists(target):
        os.remove(target)
    os.replace(tmp, target)  # atomic within same dir
    return target

# --------------------------------------------------------------------------- #
# Park
# --------------------------------------------------------------------------- #
def park_series_phase1(series_id, dry_run=True, qroot=None, scope_paths=None):
    """Phase 1 of parking: quarantine real symlinks, delete Sonarr episodeFile records,
    and unmonitor episodes. Returns a list of deferred items ready for dummy writing.
    This two-phase split lets us wait for AltMount's health check to notice the symlink
    removal before we write the dummy, so AltMount can delete the source NZB."""
    qroot = qroot or QUARANTINE_ROOT
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    series = reng.fetch_series(series_id)
    episodes = reng.fetch_episodes(series_id)
    files = reng.fetch_episode_files(series_id)
    history = reng.fetch_tautulli_history(series.get("title", ""))
    file_by_id = {f["id"]: f for f in files if f.get("id")}

    res = reng.compute_keep_real(series, episodes, files, history, now_utc)
    keep = res["keep"]

    deferred = []
    to_unmonitor = []

    for ep in episodes:
        if ep.get("seasonNumber") is None or ep.get("episodeNumber") is None:
            continue
        key = reng.ep_key(ep["seasonNumber"], ep["episodeNumber"])
        if not ep.get("hasFile"):
            continue
        if key in keep:
            continue
        if reng.is_unaired(ep, now_utc):
            continue

        ef = file_by_id.get(ep.get("episodeFileId", 0))
        if not ef:
            print("WARN: episode S%02dE%02d has hasFile but no episodeFile record" % key)
            continue
        orig_path = ef.get("path")
        if not orig_path or not os.path.lexists(orig_path):
            print("WARN: S%02dE%02d path does not exist: %s" % (key + (orig_path,)))
            continue

        if scope_paths and not any(orig_path.startswith(p) for p in scope_paths):
            print("SKIP: S%02dE%02d path %s outside scope" % (key + (orig_path,)))
            continue

        if dry_run:
            print("DRY-RUN park S%02dE%02d: %s (%.1f MB)" % (key + (orig_path, ef.get("size", 0) / (1024**2))))
            continue

        try:
            qpath = quarantine_path(orig_path, qroot)
            final_qpath = move_to_quarantine(orig_path, qpath)
            sonarr_delete_episode_file(ef["id"])
            to_unmonitor.append(ep["id"])
            deferred.append({
                "series_id": series_id, "series_title": series.get("title"),
                "season": key[0], "episode": key[1], "episode_id": ep.get("id"),
                "episode_file_id": ef.get("id"), "orig_path": orig_path,
                "quarantine_path": final_qpath, "size": ef.get("size", 0),
            })
            print("PHASE1 S%02dE%02d quarantined -> %s" % (key + (final_qpath,)))
        except Exception as e:
            print("ERROR parking S%02dE%02d: %s" % (key + (e,)))

    if to_unmonitor:
        sonarr_unmonitor_episodes(to_unmonitor)

    print("Phase1 deferred %d episodes (dry_run=%s)" % (len(deferred), dry_run))
    return deferred


def write_park_dummies(deferred, qroot=None, dummy_asset=None):
    """Phase 2 of parking: write dummy files, update registry, save manifest.
    Returns the manifest list."""
    qroot = qroot or QUARANTINE_ROOT
    dummy_asset = dummy_asset or DUMMY_ASSET
    if not deferred:
        return []
    registry = _load_registry()
    manifest = []
    for item in deferred:
        orig_path = item["orig_path"]
        final_qpath = item["quarantine_path"]
        key = (item["season"], item["episode"])
        try:
            dummy_path = write_dummy(orig_path, dummy_asset)
            manifest.append({
                "series_id": item["series_id"], "series_title": item.get("series_title"),
                "season": item["season"], "episode": item["episode"],
                "episode_id": item["episode_id"], "episode_file_id": item.get("episode_file_id"),
                "orig_path": orig_path, "quarantine_path": final_qpath, "dummy_path": dummy_path,
                "size": item.get("size", 0), "ts": int(time.time()),
            })
            registry[_reg_key(item["series_id"], item["season"], item["episode"])] = {
                "series_id": item["series_id"], "season": item["season"], "episode": item["episode"],
                "episode_id": item["episode_id"], "dummy_path": dummy_path,
                "quarantine_path": final_qpath, "orig_path": orig_path,
            }
            print("PHASE2 S%02dE%02d dummy -> %s" % (key + (dummy_path,)))
        except Exception as e:
            print("ERROR writing dummy S%02dE%02d: %s" % (key + (e,)))

    _save_registry(registry)
    sweep_dir = datetime.datetime.now().strftime("park_%Y%m%d_%H%M%S")
    mpath = os.path.join(qroot, sweep_dir, "manifest.json")
    os.makedirs(os.path.dirname(mpath), exist_ok=True)
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)
    print("Wrote manifest: %s" % mpath)
    print("Phase2 wrote %d dummies" % len(manifest))
    return manifest


def park_series(series_id, dry_run=True, qroot=None, dummy_asset=None, scope_paths=None):
    """Park episodes of a Sonarr series according to the rules engine.

    scope_paths: optional list of root paths to restrict to (e.g. ["/mnt/iceberg/anime_shows"]).
                 Episodes whose path does not start with one of these are skipped.
    Returns manifest list (each item is a dict describing what was done).
    """
    deferred = park_series_phase1(series_id, dry_run=dry_run, qroot=qroot, scope_paths=scope_paths)
    if dry_run:
        manifest = [{**d, "dry_run": True} for d in deferred]
        print("Parked 0 episodes (dry_run=%s)" % dry_run)
        return manifest
    manifest = write_park_dummies(deferred, qroot=qroot, dummy_asset=dummy_asset)
    print("Parked %d episodes (dry_run=%s)" % (len(manifest), dry_run))
    return manifest

# --------------------------------------------------------------------------- #
# Unpark / restore
# --------------------------------------------------------------------------- #
def unpark_series(manifest_path, delete_dummies=True, rescan=True):
    """Restore a series from a park manifest.
    Moves symlinks back from quarantine, removes dummies, re-monitors episodes,
    and optionally triggers a Sonarr rescan.
    """
    with open(manifest_path) as f:
        manifest = json.load(f)
    if not manifest:
        print("Empty manifest")
        return

    series_id = manifest[0]["series_id"]
    restored = 0
    to_monitor = []
    for item in manifest:
        orig = item["orig_path"]
        q = item.get("quarantine_path")
        dummy = item.get("dummy_path")
        if not q or not os.path.lexists(q):
            print("WARN: quarantine missing for S%02dE%02d, skipping" % (item["season"], item["episode"]))
            continue
        # Remove dummy if present
        if delete_dummies and dummy and os.path.lexists(dummy):
            os.remove(dummy)
        # Ensure parent dir exists and move symlink back
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        if os.path.lexists(orig):
            os.remove(orig)
        shutil.move(q, orig)
        to_monitor.append(item["episode_id"])
        restored += 1
        print("RESTORED S%02dE%02d -> %s" % (item["season"], item["episode"], orig))

    if to_monitor:
        sonarr_monitor_episodes(to_monitor)
    if rescan:
        print("Triggering Sonarr rescan for series %d" % series_id)
        sonarr_rescan_series(series_id)
    print("Restored %d episodes" % restored)

# --------------------------------------------------------------------------- #
# Prefetch on playback
# --------------------------------------------------------------------------- #
def find_manifests(series_id=None, qroot=None):
    """Return list of manifest paths under qroot, optionally filtered by series_id."""
    qroot = qroot or QUARANTINE_ROOT
    out = []
    if not os.path.isdir(qroot):
        return out
    for root, dirs, files in os.walk(qroot):
        for fn in files:
            if fn == "manifest.json":
                p = os.path.join(root, fn)
                try:
                    with open(p) as f:
                        m = json.load(f)
                    if series_id is None or (m and m[0].get("series_id") == series_id):
                        out.append(p)
                except Exception:
                    continue
    return out

def _dummy_path_from_manifests(series_id, season, episode, qroot=None):
    """Find the dummy path recorded for a parked episode, searching newest manifest first."""
    for mp in sorted(find_manifests(series_id, qroot), key=os.path.getmtime, reverse=True):
        try:
            with open(mp) as f:
                manifest = json.load(f)
            for item in manifest:
                if item.get("season") == season and item.get("episode") == episode:
                    return item.get("dummy_path")
        except Exception:
            continue
    return None

def prefetch_position(series_id, season, episode, ahead=3, include_next_season_premiere=True,
                      wait=True, timeout=600, poll=5, cleanup=True, refresh_plex=True):
    """Ensure the played episode and the next N episodes in the season are real.
    Optionally waits for Sonarr import, removes stale dummy files, and refreshes Plex."""
    episodes = reng.fetch_episodes(series_id)
    by_key = {(e["seasonNumber"], e["episodeNumber"]): e for e in episodes if e.get("seasonNumber") is not None and e.get("episodeNumber") is not None}

    targets = []
    target_meta = {}  # episode_id -> (season, episode, dummy_path)
    for off in range(0, ahead + 1):
        key = (season, episode + off)
        if key not in by_key:
            break
        ep = by_key[key]
        if reng.is_unaired(ep, datetime.datetime.now(datetime.timezone.utc)):
            break
        if not ep.get("hasFile"):
            targets.append(ep["id"])
            target_meta[ep["id"]] = (key[0], key[1])

    if include_next_season_premiere:
        next_season = season + 1
        key = (next_season, 1)
        if key in by_key:
            ep = by_key[key]
            if not reng.is_unaired(ep, datetime.datetime.now(datetime.timezone.utc)) and not ep.get("hasFile"):
                targets.append(ep["id"])
                target_meta[ep["id"]] = (key[0], key[1])

    if not targets:
        print("PREFETCH: all requested episodes already have files")
        return []

    sonarr_monitor_episodes(targets)
    sonarr_search_episodes(targets)
    print("PREFETCH: monitored+searched %d episode(s): %s" % (len(targets), ", ".join("S%02dE%02d" % target_meta[tid] for tid in targets)))

    if not wait:
        return targets

    # Poll Sonarr until each target has a file or timeout
    pending = set(targets)
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for tid in list(pending):
            ep = reng.sonarr_get("/episode/%d" % tid)
            if ep and ep.get("hasFile"):
                s, e = target_meta[tid]
                if cleanup:
                    dummy = _dummy_path_from_manifests(series_id, s, e)
                    if dummy and os.path.lexists(dummy):
                        os.remove(dummy)
                        print("PREFETCH: removed stale dummy %s" % dummy)
                pending.discard(tid)
                print("PREFETCH: S%02dE%02d now has real file" % (s, e))
        if pending:
            time.sleep(poll)

    if pending:
        print("PREFETCH: timed out waiting for %s" % ", ".join("S%02dE%02d" % target_meta[tid] for tid in pending))

    if refresh_plex:
        try:
            urllib.request.urlopen("%s/library/sections/3/refresh?X-Plex-Token=%s" % (PLEX_URL, PLEX_TOKEN), timeout=10)
        except Exception:
            pass
    return [tid for tid in targets if tid not in pending]

# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["dry-run", "park", "unpark", "prefetch"])
    ap.add_argument("series_id", type=int)
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--episode", type=int, default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--scope", default="", help="comma-separated root paths to restrict park to")
    args = ap.parse_args()

    scope = [p.strip() for p in args.scope.split(",") if p.strip()] or None

    if args.action == "dry-run":
        reng.dry_run_series(args.series_id)
    elif args.action == "park":
        park_series(args.series_id, dry_run=False, scope_paths=scope)
    elif args.action == "unpark":
        if not args.manifest:
            # find newest manifest
            candidates = []
            for root, dirs, files in os.walk(QUARANTINE_ROOT):
                for fn in files:
                    if fn == "manifest.json":
                        candidates.append(os.path.join(root, fn))
            if not candidates:
                print("No manifest found in %s" % QUARANTINE_ROOT); sys.exit(1)
            args.manifest = max(candidates, key=os.path.getmtime)
            print("Using newest manifest: %s" % args.manifest)
        unpark_series(args.manifest)
    elif args.action == "prefetch":
        if args.season is None or args.episode is None:
            print("--season and --episode required for prefetch")
            sys.exit(1)
        prefetch_position(args.series_id, args.season, args.episode)
