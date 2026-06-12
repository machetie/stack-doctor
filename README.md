# stack-doctor

**A self-hosted health daemon that auto-detects and fixes the recurring problems in a
Sonarr/Radarr + decypharr + Plex media stack.**

If you run *arr apps against usenet (decypharr, SABnzbd, NZBGet) or torrents/debrid, you know
the failure modes: downloads that finish but never import, dead grabs stuck as
`downloadClientUnavailable`, incomplete files whose corrupt headers make ffprobe choke, a
hung decypharr FUSE mount that takes Plex down, memory/load pressure that OOMs your arrs.
You only notice when something's "missing" or the family complains.

stack-doctor runs a set of **modular checks** on an interval (or on Sonarr/Radarr webhooks),
detects these, and fixes the safe ones automatically. No third-party dependencies, one small
container, everything configured by env vars.

> Born out of a long night of hand-fixing exactly these problems on a usenet *arr stack.
> Now it's a daemon so you never have to do it by hand again.

## Checks (toggle each with `ENABLE_*`)

| check | detects | fixes |
|---|---|---|
| **queue** | stuck/dead/blocked *arr download-queue items | removes + blocklists -> *arr re-searches a different release |
| **providers** | failed indexers / download clients (sonarr/radarr/**prowlarr**) | runs the **Test** on them to re-validate + clear the failure |
| **decypharr** | hung FUSE mount (read-test) + API down | runs your restart hook (`DECYPHARR_RESTART_CMD`) |
| **plex** | Plex unresponsive | alerts (optional library refresh) |
| **resources** | host load / low memory / swap pressure | reports; optional `drop_caches` relief |
| **janitor** | permanently-dead usenet releases (from decypharr's log) | quarantines those library symlinks (reversible) |
| **bazarr** | Bazarr unreachable | alerts |

Safe by design: risky actions (restart, drop_caches) are **opt-in**, the queue fixer only
acts after an item is stuck for several consecutive checks, and everything supports
`DOCTOR_DRY_RUN=true`.

## Two ways to run (multi-level)

stack-doctor scales to the access it's given:

- **Container** (limited): the network/mount checks, `queue`, `plex`, the `decypharr` mount
  read-test, and `resources`. It can't restart a *host* decypharr service or read host
  journald, so leave `DECYPHARR_RESTART_CMD` empty (alert-only) or point it at
  `docker restart <decypharr>` / an SSH hook. See [`docker-compose.example.yml`](docker-compose.example.yml).
- **Host service** (full power): run it **on the same host as decypharr** (see
  [`stack-doctor.service.example`](stack-doctor.service.example)). Now it restarts decypharr
  natively (`DECYPHARR_RESTART_CMD=systemctl restart decypharr`), reads its journal for the
  janitor (`JANITOR_LOG_CMD=journalctl -u decypharr ...`), and touches the library directly,
  no container-to-host bridge needed. The *arr/Plex instances are still reached over the LAN.

Same `doctor.py`, same env vars; you just enable more checks where it has more power.

---

## What the queue check fixes

Each is a named **condition** you can enable/disable via `DOCTOR_CONDITIONS`:

| condition | what it catches |
|---|---|
| `downloadClientUnavailable` | dead grabs the download client rejected (orphans that never go away) |
| `importBlocked` | completed download the *arr refuses to import |
| `importFailed` | import attempted and failed |
| `importPending_warning` | completed but stuck pending with a warning, usually an **incomplete/corrupt file ffprobe can't parse** |
| `failedPending` | failed download awaiting handling |
| `stalled` | download flagged with a stall / "no files" warning |

The fix is always the same and safe: `DELETE` the queue item with `removeFromClient=true`
and (by default) `blocklist=true`. With `autoRedownloadFailed` on in your *arr (the default),
that triggers a fresh search for another release. It's **self-limiting**, once every bad
release for an item is blocklisted, there's nothing left to grab, so the churn stops.

---

## Quick start

```yaml
# docker-compose.yml
services:
  stack-doctor:
    image: ghcr.io/neoo-blue/stack-doctor:latest
    container_name: stack-doctor
    restart: unless-stopped
    environment:
      DOCTOR_MODE: cron
      DOCTOR_INTERVAL: "900"
      DOCTOR_MIN_STRIKES: "2"
      DOCTOR_BLOCKLIST: "true"
      INSTANCE_1_NAME: sonarr
      INSTANCE_1_TYPE: sonarr
      INSTANCE_1_URL: http://sonarr:8989
      INSTANCE_1_APIKEY: your_sonarr_key
      INSTANCE_2_NAME: radarr
      INSTANCE_2_TYPE: radarr
      INSTANCE_2_URL: http://radarr:7878
      INSTANCE_2_APIKEY: your_radarr_key
    volumes:
      - ./data:/data
```

```bash
docker compose up -d
docker compose logs -f stack-doctor
```

A full example with four instances and `.env` is in
[`docker-compose.example.yml`](docker-compose.example.yml). **Tip:** start with
`DOCTOR_DRY_RUN: "true"` to see what it *would* remove before letting it act.

---

## Configuration (all via env vars)

### Behaviour

| var | default | meaning |
|---|---|---|
| `DOCTOR_MODE` | `cron` | `cron` (interval sweeps) or `event` (Sonarr/Radarr webhook) |
| `DOCTOR_INTERVAL` | `900` | cron: seconds between sweeps |
| `DOCTOR_MIN_STRIKES` | `2` | item must be stuck this many consecutive checks before action (ignores transient blips like a download-client restart) |
| `DOCTOR_MAX_ACTIONS` | `20` | max removals per sweep (rate limit, keeps re-searches gentle) |
| `DOCTOR_BLOCKLIST` | `true` | blocklist removed grabs so a *different* release is fetched |
| `DOCTOR_REMOVE_FROM_CLIENT` | `true` | also remove from the download client |
| `DOCTOR_DRY_RUN` | `false` | `true` = log only, change nothing |
| `DOCTOR_CONDITIONS` | *all* | comma list of conditions to act on (see table above) |
| `DOCTOR_LOAD_MAX` | `0` | if > 0, skip a sweep when host 1-min load exceeds it (mount `/proc/loadavg:ro`) |
| `DOCTOR_HEALTH_REPORT` | `true` | log *arr `/health` warnings at debug level |
| `DOCTOR_STATE_FILE` | `/data/state.json` | where strike counts persist |
| `DOCTOR_PORT` | `8088` | webhook port (event mode) |
| `DOCTOR_TRIGGER_EVENTS` | `Download,ManualInteractionRequired,DownloadFailed,Grab` | webhook events that trigger a sweep |
| `DOCTOR_LOG_LEVEL` | `INFO` | `DEBUG` for verbose |

### Instances

Add as many as you want, numbered from 1:

| var | example |
|---|---|
| `INSTANCE_<n>_TYPE` | `sonarr` or `radarr` |
| `INSTANCE_<n>_URL` | `http://sonarr:8989` |
| `INSTANCE_<n>_APIKEY` | from *Settings → General* |
| `INSTANCE_<n>_NAME` | `sonarr4k` (optional label) |

---

## Cron vs Event mode

**Cron** (default): a daemon that sweeps every `DOCTOR_INTERVAL` seconds. Simple, reliable,
catches everything within ~`INTERVAL × MIN_STRIKES`.

**Event**: stack-doctor runs a tiny webhook server. Point each *arr at it
(*Settings → Connect → Webhook*, URL `http://stack-doctor:8088`, on **On Grab / On Import
Failure / On Manual Interaction Required**) and it sweeps the moment the *arr reports trouble.
A slow safety-net sweep still runs in the background in case a webhook is missed. In event
mode you'll usually set `DOCTOR_MIN_STRIKES: "1"` to act immediately, the event already
confirms the item is stuck.

---

## How the strike system works

To avoid over-reacting, an item is only removed once it's been seen stuck on
`MIN_STRIKES` **consecutive** checks. Counts persist in `/data/state.json`. This is what
stops it from blocklisting items that are merely *temporarily* unavailable (for example while
your download client restarts). Anything that recovers on its own is left alone.

---

## Extending

Conditions are just predicates in `doctor.py` (`CONDITIONS` dict). Adding a new
detect/fix rule is a couple of lines. PRs welcome.

## License

MIT
