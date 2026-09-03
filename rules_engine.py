#!/usr/bin/env python3
"""
Rules engine for placeholder/park decisions.
Given a Sonarr series id, computes the KEEP-REAL vs PARK sets per MASTER-HANDOFF sec 4.
Run standalone for dry-runs; later import into doctor.py.
"""
import os, sys, json, urllib.request, urllib.parse, datetime, time

# --------------------------------------------------------------------------- #
# Config (env-overridable)
# --------------------------------------------------------------------------- #
SONARR_URL      = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_APIKEY   = os.environ.get("SONARR_APIKEY", "")
TAUTULLI_URL    = os.environ.get("TAUTULLI_URL", "http://localhost:8181")
TAUTULLI_APIKEY = os.environ.get("TAUTULLI_APIKEY", "")

KNOBS = {
    "PREFETCH_AHEAD": int(os.environ.get("PREFETCH_AHEAD", "3")),
    "PREFETCH_NEXT_SEASON": os.environ.get("PREFETCH_NEXT_SEASON", "true").lower() == "true",
    "ENTRY_EPS": int(os.environ.get("ENTRY_EPS", "2")),
    "KEEP_SEASON_PREMIERES": os.environ.get("KEEP_SEASON_PREMIERES", "true").lower() == "true",
    "AIRING_KEEP_DAYS": int(os.environ.get("AIRING_KEEP_DAYS", "30")),
    "KEEP_LATEST_EPS": int(os.environ.get("KEEP_LATEST_EPS", "3")),
    "RESUME_KEEP_DAYS": int(os.environ.get("RESUME_KEEP_DAYS", "120")),
}

# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def sonarr_get(path):
    url = SONARR_URL.rstrip("/") + "/api/v3" + path
    req = urllib.request.Request(url, headers={"X-Api-Key": SONARR_APIKEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def tautulli_get(cmd, **params):
    q = {"apikey": TAUTULLI_APIKEY, "cmd": cmd}
    q.update(params)
    url = TAUTULLI_URL.rstrip("/") + "/api/v2?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.loads(r.read().decode())
    if data.get("response", {}).get("result") != "success":
        raise RuntimeError("Tautulli error: %s" % data)
    return data["response"]["data"]

# --------------------------------------------------------------------------- #
# Data fetching
# --------------------------------------------------------------------------- #
def fetch_series(series_id):
    return sonarr_get("/series/%d" % series_id)

def fetch_episodes(series_id):
    return sonarr_get("/episode?seriesId=%d" % series_id)

def fetch_episode_files(series_id):
    return sonarr_get("/episodefile?seriesId=%d" % series_id)

def fetch_tautulli_history(series_title, max_rows=5000):
    """Return list of history records for episodes whose grandparent_title matches series_title."""
    out = []
    start = 0
    length = min(1000, max_rows)
    search = series_title
    while len(out) < max_rows:
        data = tautulli_get(
            "get_history",
            media_type="episode",
            search=search,
            start=start,
            length=length,
        )
        rows = data.get("data", [])
        if not rows:
            break
        for row in rows:
            if row.get("grandparent_title") == series_title:
                out.append(row)
        if len(rows) < length:
            break
        start += length
    return out

# --------------------------------------------------------------------------- #
# Rule logic
# --------------------------------------------------------------------------- #
def parse_air_date(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def ep_key(season, episode):
    return (season, episode)

def is_unaired(ep, now_utc):
    air = parse_air_date(ep.get("airDateUtc"))
    return air is None or air > now_utc

def is_fresh(ep, now_utc, days):
    air = parse_air_date(ep.get("airDateUtc"))
    return air is not None and (now_utc - air).total_seconds() < days * 86400

def compute_keep_real(series, episodes, files, history, now_utc=None):
    """Return KEEP-REAL set per MASTER-HANDOFF sec 4 (revised 2026-09-03).
    Even ACTIVE multi-season shows park old seasons; only per-viewer windows,
    premieres, entry, fresh+unaired, and newest are kept real."""
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)

    resume_days = KNOBS["RESUME_KEEP_DAYS"]

    # Build episode index
    ep_index = {}
    for ep in episodes:
        if ep.get("seasonNumber") is None or ep.get("episodeNumber") is None:
            continue
        key = ep_key(ep["seasonNumber"], ep["episodeNumber"])
        ep_index[key] = ep

    # Per-user watch data: {user: {"max": (season, episode), "last_ts": int}}
    user_watch = {}
    for h in history:
        user = h.get("user")
        if not user:
            continue
        s = h.get("parent_media_index")  # season
        e = h.get("media_index")         # episode
        ts = h.get("date") or h.get("stopped") or 0
        if s is None or e is None:
            continue
        key = (int(s), int(e))
        rec = user_watch.setdefault(user, {"max": key, "last_ts": ts})
        if key > rec["max"]:
            rec["max"] = key
        if ts > rec["last_ts"]:
            rec["last_ts"] = ts

    active = any(
        rec["last_ts"] and (now_utc.timestamp() - rec["last_ts"]) <= resume_days * 86400
        for rec in user_watch.values()
    )
    abandoned = not bool(user_watch)

    keep = set()
    reasons = {}

    # (D) unaired - keep all (never park)
    for ep in episodes:
        key = ep_key(ep["seasonNumber"], ep["episodeNumber"])
        if is_unaired(ep, now_utc):
            keep.add(key)
            reasons[key] = "unaired"

    # (E) fresh aired
    for ep in episodes:
        key = ep_key(ep["seasonNumber"], ep["episodeNumber"])
        if key not in keep and is_fresh(ep, now_utc, KNOBS["AIRING_KEEP_DAYS"]):
            keep.add(key)
            reasons[key] = "fresh"

    # (A) entry set S01E01..E0{ENTRY_EPS}
    for i in range(1, KNOBS["ENTRY_EPS"] + 1):
        key = (1, i)
        if key in ep_index and key not in keep:
            keep.add(key)
            reasons[key] = "entry"

    # (C) season premieres
    if KNOBS["KEEP_SEASON_PREMIERES"]:
        seen_seasons = set(ep["seasonNumber"] for ep in episodes if ep.get("seasonNumber") is not None)
        for s in seen_seasons:
            key = (s, 1)
            if key in ep_index and key not in keep:
                keep.add(key)
                reasons[key] = "premiere"

    # (B) per-user resume windows (only users active within RESUME_KEEP_DAYS)
    for user, rec in user_watch.items():
        if (now_utc.timestamp() - rec["last_ts"]) > resume_days * 86400:
            continue
        s, e = rec["max"]
        for off in range(0, KNOBS["PREFETCH_AHEAD"] + 1):
            key = (s, e + off)
            if key in ep_index and key not in keep:
                keep.add(key)
                reasons[key] = "resume:%s" % user

    # (F) newest KEEP_LATEST_EPS aired eps if series continuing/upcoming
    status = (series.get("status") or "").lower()
    if status in ("continuing", "upcoming"):
        aired = [(ep_key(ep["seasonNumber"], ep["episodeNumber"]), ep) for ep in episodes
                 if ep.get("seasonNumber", 0) > 0 and not is_unaired(ep, now_utc)]
        aired.sort(key=lambda x: parse_air_date(x[1].get("airDateUtc")) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)
        for key, ep in aired[:KNOBS["KEEP_LATEST_EPS"]]:
            if key not in keep:
                keep.add(key)
                reasons[key] = "newest"

    return {"keep": keep, "reasons": reasons, "active": active, "stale": not active,
            "abandoned": abandoned, "user_watch": user_watch}

# --------------------------------------------------------------------------- #
# Output / dry-run
# --------------------------------------------------------------------------- #
def dry_run_series(series_id, now_utc=None):
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)

    series = fetch_series(series_id)
    episodes = fetch_episodes(series_id)
    files = fetch_episode_files(series_id)
    history = fetch_tautulli_history(series.get("title", ""))

    file_by_id = {f["id"]: f for f in files if f.get("id")}

    res = compute_keep_real(series, episodes, files, history, now_utc)
    keep = res["keep"]

    # Build park list: hasFile True, not in keep, not unaired
    park = []
    kept = []
    for ep in episodes:
        if ep.get("seasonNumber") is None or ep.get("episodeNumber") is None:
            continue
        key = ep_key(ep["seasonNumber"], ep["episodeNumber"])
        has_file = ep.get("hasFile", False)
        if not has_file:
            continue
        if key in keep:
            kept.append((key, ep, res["reasons"].get(key, "?")))
        elif not is_unaired(ep, now_utc):
            park.append((key, ep))

    total_bytes = sum(file_by_id.get(ep.get("episodeFileId", 0), {}).get("size", 0) for _, ep, _ in kept) + \
                  sum(file_by_id.get(ep.get("episodeFileId", 0), {}).get("size", 0) for _, ep in park)
    park_bytes = sum(file_by_id.get(ep.get("episodeFileId", 0), {}).get("size", 0) for _, ep in park)
    total_gb = total_bytes / (1024**3)
    park_gb = park_bytes / (1024**3)

    print("=" * 70)
    print("DRY-RUN: %s (Sonarr id=%d)" % (series.get("title"), series_id))
    print("  status=%s  nextAiring=%s" % (series.get("status"), series.get("nextAiring") or "none"))
    print("  active=%s  stale=%s  abandoned=%s" % (res["active"], res["stale"], res["abandoned"]))
    print("  users watched: %s" % ", ".join("%s@%s" % (u, "S%02dE%02d" % rec["max"]) for u, rec in res["user_watch"].items()))
    print("  total files: %d | keep: %d | park: %d | keep %.1f GB | park %.1f GB" % (
        len(kept) + len(park), len(kept), len(park), total_gb - park_gb, park_gb))
    print("=" * 70)
    print("KEEP-REAL (%d):" % len(kept))
    for (s, e), ep, reason in sorted(kept):
        f = file_by_id.get(ep.get("episodeFileId", 0), {})
        print("  S%02dE%02d  %-14s  %8.1f MB  %s" % (s, e, reason, f.get("size", 0) / (1024**2), ep.get("title", "")))
    print("PARK (%d):" % len(park))
    for (s, e), ep in sorted(park):
        f = file_by_id.get(ep.get("episodeFileId", 0), {})
        print("  S%02dE%02d  %8.1f MB  %s" % (s, e, f.get("size", 0) / (1024**2), ep.get("title", "")))

    return {"series": series, "episodes": episodes, "files": files, "history": history, "result": res,
            "kept": kept, "park": park, "park_gb": park_gb}

# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    series_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1053
    dry_run_series(series_id)
