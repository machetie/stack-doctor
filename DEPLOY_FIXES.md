# Deploy plan — ship the `fixes` branch to the live server

Goal: build a new stack-doctor image **on the live server**, replace the running
container managed by `/data/decypharr/compose.yml`, and monitor for regressions.
Behavior settings (incl. `DOCTOR_DRY_RUN`) are **kept as-is** — every change in
`fixes` is durability/robustness only, so no posture change is needed.

- **Delivery:** build on the live server from the repo (no registry).
- **Run method:** `docker compose` — service defined in `/data/decypharr/compose.yml`.
- **Rollback:** the previous image is retagged before we replace it, so rollback
  is one `compose up -d` away.

> Placeholders to fill once, then reuse below:
> - `SERVER` = your live server SSH target (e.g. `root@192.168.50.x`)
> - `COMPOSE=/data/decypharr/compose.yml`
> - `SVC` = the service name of stack-doctor **inside** that compose file
>   (find it in step 1; commonly `stack-doctor`)
> - `DATA` = the host path bind-mounted to `/data` for stack-doctor
>   (find it in step 1; the state/config live here)

---

## What's shipping (the `fixes` branch)

Six commits on top of the current `f261ffd`:

| Commit | Change | Risk |
|--------|--------|------|
| `68ffccf` | Atomic durable state writes (temp+fsync+rename) for all 10 state files | none (durability) |
| `35b1a09` | Serialize shared-state read-modify-write (re-entrant lock over sweep) | none (correctness) |
| `5d9bad5` | HTTP retry/backoff for idempotent arr reads + testall (never DELETE/ManualImport) | low |
| `fe67525` | Queue pagination past 1000 records, capped by `DOCTOR_QUEUE_MAX_FETCH` | low |
| `43b85d2` | Prometheus `/metrics` endpoint (stdlib, token-gated if UI token set) | none (additive) |
| `403ff85` | Mask secrets in cmd logs, startup cmd validation, per-sweep summary line | none |

**New env vars (all have safe defaults — nothing required):**

| Var | Default | Purpose |
|-----|---------|---------|
| `DOCTOR_HTTP_RETRIES` | `3` | attempts for idempotent arr reads/test calls |
| `DOCTOR_HTTP_RETRY_BASE` | `0.5` | first backoff delay (s), doubles + jitter |
| `DOCTOR_QUEUE_PAGE_SIZE` | `1000` | records per queue page |
| `DOCTOR_QUEUE_MAX_FETCH` | `5000` | hard cap on queue records fetched/sweep |
| `ENABLE_METRICS` | `true` | expose `/metrics` (same token gate as UI) |

Tests: 95 passing, stdlib-only, safety posture unchanged.

---

## Phase 0 — Pre-flight (local, before touching the server)

1. Confirm the branch is green and the tree is clean:

   ```bash
   cd /home/machetie/Documents/git/stack-doctor
   git status
   python3 -m unittest discover -s tests -p 'test_*.py'   # expect: OK, 95 tests
   git log --oneline -6
   ```

2. Get the code onto the server. Pick ONE:

   **2a. Via git (server can reach GitHub):** push the branch, pull on the server.

   ```bash
   git push origin fixes
   ```

   **2b. Via rsync (no registry / air-gapped):** copy the working tree.
   (only `doctor.py` + `Dockerfile` are needed to build)

   ```bash
   rsync -av --exclude .git --exclude tests/__pycache__ --exclude __pycache__ \
     /home/machetie/Documents/git/stack-doctor/ SERVER:/opt/stack-doctor-src/
   ```

---

## Phase 1 — Inspect the live setup (read-only, on the server)

SSH in and gather the exact service name, image, and data dir. **Change nothing yet.**

```bash
ssh SERVER
COMPOSE=/data/decypharr/compose.yml

# 1. Find the stack-doctor service block + its current image tag + /data bind mount
grep -n -A15 'stack-doctor' "$COMPOSE"

# 2. Confirm what's running now and note the current image ID/tag
docker compose -f "$COMPOSE" ps
docker inspect --format '{{.Config.Image}}' stack-doctor 2>/dev/null || true
docker images | grep -i stack-doctor

# 3. Confirm the host path bound to /data (state + config live here)
docker inspect --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' stack-doctor
```

Record: `SVC` (service name), current image ref (e.g. `ghcr.io/machetie/stack-doctor:latest`
or a local tag), and `DATA` (the host `/data` source path). If the current image is
`:latest` from GHCR, we'll build a **local** tag and point the compose file at it so a
future `docker compose pull` can't silently revert us.

---

## Phase 2 — Back up state + capture a rollback point (on the server)

The `/data` volume holds every state file (`state.json`, `scrubber.json`,
`repair.json`, `watchlists.json`, `config.json`, …). Back it up cold-ish and tag
the current image for instant rollback.

```bash
# 1. Snapshot the data dir (adjust DATA to what Phase 1 reported)
DATA=/data/decypharr/stack-doctor     # <-- replace with the real bind source
sudo tar czf /root/stack-doctor-data-$(date +%F-%H%M).tgz -C "$DATA" .
ls -lh /root/stack-doctor-data-*.tgz

# 2. Tag the currently-running image as the rollback image
CUR=$(docker inspect --format '{{.Config.Image}}' stack-doctor)
docker tag "$CUR" stack-doctor:rollback
docker images | grep stack-doctor
```

If `$DATA` has an `.env` or secrets alongside the compose file, back that up too:

```bash
sudo cp /data/decypharr/compose.yml /root/compose.yml.bak-$(date +%F-%H%M)
[ -f /data/decypharr/.env ] && sudo cp /data/decypharr/.env /root/decypharr.env.bak-$(date +%F-%H%M)
```

---

## Phase 3 — Build the new image on the server

Build from the source you delivered in Phase 0, tag it clearly (date + short SHA).

```bash
# From git (2a):
cd /opt/stack-doctor-src 2>/dev/null || git clone https://github.com/machetie/stack-doctor.git /opt/stack-doctor-src && cd /opt/stack-doctor-src
git fetch origin && git checkout fixes && git pull --ff-only origin fixes

# OR from rsync (2b): just cd into it
# cd /opt/stack-doctor-src

SHA=$(git rev-parse --short HEAD 2>/dev/null || echo local)
TAG=stack-doctor:fixes-$(date +%F)-$SHA
docker build -t "$TAG" -t stack-doctor:fixes-latest .
echo "built $TAG"

# Smoke-test the image WITHOUT joining the stack. The app has no CLI flags and
# would try to start a real sweep, so import-check it instead (fast, no network):
docker run --rm --entrypoint python3 "$TAG" -c \
  "import doctor; print('doctor v'+doctor.VERSION+' imports OK')"
```

> `doctor.py` is the whole app; the build is seconds. `--platform` isn't needed —
> you're building for the host arch directly.

---

## Phase 4 — Point the compose service at the new image + apply new vars

Edit `/data/decypharr/compose.yml` for the stack-doctor service only.

1. Change its `image:` to the tag you just built:

   ```yaml
   services:
     stack-doctor:                      # = your SVC
       image: stack-doctor:fixes-latest # was ghcr.io/... or a prior local tag
   ```

2. (Optional) new tunables — **all default safely, add only if you want them explicit.**
   Recommended to add nothing on first deploy; the defaults are correct.

   > **`/metrics` reachability caveat:** the HTTP server only binds when the stack
   > is in **event mode** (`DOCTOR_MODE=event`, webhook port `DOCTOR_PORT`, default
   > 8088) **or** the dashboard is on (`ENABLE_UI=true`, `DOCTOR_UI_PORT`, default
   > 12345). `/metrics` is served on that same port — there is **no standalone
   > metrics port**. If this container runs plain `cron` mode with `ENABLE_UI`
   > unset, nothing listens and Phase-6 step 4 will not apply. To scrape metrics,
   > either keep `ENABLE_UI=true` (already the case if you use the dashboard) or
   > run in event mode, and make sure that port is published under `ports:`.

3. Validate the compose file parses before applying:

   ```bash
   docker compose -f "$COMPOSE" config >/dev/null && echo "compose OK"
   ```

---

## Phase 5 — Cut over (recreate only stack-doctor)

Recreate just the one service; leave the rest of the stack untouched.

```bash
COMPOSE=/data/decypharr/compose.yml
SVC=stack-doctor

docker compose -f "$COMPOSE" up -d --no-deps "$SVC"
docker compose -f "$COMPOSE" ps "$SVC"

# Confirm it picked up the new image
docker inspect --format '{{.Config.Image}}' "$SVC"
```

Immediately follow the logs for the first sweep:

```bash
docker compose -f "$COMPOSE" logs -f --since 2m "$SVC"
```

**Expect on a healthy start:**
- `stack-doctor vX.Y | mode=… | checks=[…] | instances=… | dry_run=<unchanged>`
- `safety posture: dry_run=… …`
- one new line per sweep: `sweep done: checks=N errors=0 dur=…s`
- no `[config] … unbalanced quotes` warnings (if any appear, a configured
  `*_CMD` has a typo — fix in compose, `up -d --no-deps` again)

---

## Phase 6 — Post-deploy verification (first ~15 min)

Run these on the server right after cutover.

1. **Container is up and not restart-looping:**

   ```bash
   docker compose -f "$COMPOSE" ps "$SVC"      # State=running, not Restarting
   docker inspect --format '{{.RestartCount}}' "$SVC"
   ```

2. **A sweep completed cleanly** (event mode: trigger one; cron: wait `DOCTOR_INTERVAL`):

   ```bash
   docker compose -f "$COMPOSE" logs --since 10m "$SVC" | grep -E "sweep done|check error|Traceback"
   ```
   Want: `sweep done: … errors=0`. Any `check error` / `Traceback` → see Rollback.

3. **State writes are landing atomically** (no `.tmp-*.json` left behind, files fresh):

   ```bash
   ls -la "$DATA"                       # state.json etc. should have recent mtimes
   ls -la "$DATA"/.tmp-*.json 2>/dev/null && echo "WARN leftover temp files" || echo "no temp leftovers (good)"
   python3 - <<'PY'
   import json, glob, sys
   for f in glob.glob("REPLACE_DATA/*.json"):
       try: json.load(open(f)); print("ok", f)
       except Exception as e: print("BAD", f, e); sys.exit(1)
   PY
   ```
   (replace `REPLACE_DATA` with `$DATA`)

4. **`/metrics` responds** — *only if `ENABLE_UI=true` or `DOCTOR_MODE=event`*
   (see the Phase-4 caveat). Use the port that's actually published: `12345` for
   the UI, or `DOCTOR_PORT` (8088) in event mode.

   ```bash
   docker compose -f "$COMPOSE" logs --since 5m "$SVC" | grep "http on :"   # shows the bound port
   curl -fsS http://localhost:12345/metrics | head -20
   # if DOCTOR_UI_TOKEN is set:
   # curl -fsS "http://localhost:12345/metrics?token=YOURTOKEN" | head -20
   ```
   Want Prometheus text: `stackdoctor_sweep_total …`, `stackdoctor_mount_up{…}`, etc.
   (If no `http on :` line appears, the server isn't bound — that's expected in
   plain cron mode without the UI; skip this step.)

5. **Queue pagination sanity** (only if an arr has a big queue right now):

   ```bash
   docker compose -f "$COMPOSE" logs --since 10m "$SVC" | grep -i "queue fetch capped" || echo "no cap hit (fine)"
   ```

6. **Dashboard loads** (if `ENABLE_UI`): open `http://<host>:12345` — Status/Logs tabs render.

---

## Phase 7 — Extended monitoring (first 24–48 h)

The high-value fixes only prove themselves across restarts and load spikes. Watch for:

- **Restart durability (P0-1/P0-2):** if the container OOMs or you `docker restart`
  it, state must survive. Optionally force one restart during a quiet window and
  confirm no full re-scan / watchlist re-add storm:

  ```bash
  docker compose -f "$COMPOSE" restart "$SVC"
  docker compose -f "$COMPOSE" logs --since 3m "$SVC" | grep -iE "re-scan|re-add|baseline|offender" 
  ```
  Want: churn offenders / cooldowns remembered, no mass re-add.

- **HTTP retry (P1-1):** under arr search load, transient failures should now
  retry instead of skipping a sweep:

  ```bash
  docker compose -f "$COMPOSE" logs --since 24h "$SVC" | grep -iE "queue fetch failed" | tail
  ```
  These should be rarer than before. If you set `DOCTOR_LOG_LEVEL=DEBUG` you'll
  also see `[cmd] run:` masked-command lines confirming Phase-6 masking works.

- **Metrics trend:** if scraping, chart `stackdoctor_sweep_errors_total`,
  `stackdoctor_scrubber_files_total{result="bad"}`, `stackdoctor_mount_up`.
  A rising `mount_up == 0` or `sweep_errors` is your early-warning signal.

- **Log error scan (run daily):**

  ```bash
  docker compose -f "$COMPOSE" logs --since 24h "$SVC" \
    | grep -iE "Traceback|check error|state save failed|unbalanced" | tail -50
  ```

Success criteria to close out the deploy:
- ✅ 24 h with `errors=0` on the sweep-summary line (or only known-transient)
- ✅ no `Traceback` / `state save failed`
- ✅ at least one container restart survived with state intact
- ✅ `/metrics` scraping (if used) shows sane series

---

## Rollback (any red flag in Phase 5–7)

Fast path — repoint compose at the saved rollback image:

```bash
COMPOSE=/data/decypharr/compose.yml
SVC=stack-doctor

# option A: edit image: back to the previous ref, then:
docker compose -f "$COMPOSE" up -d --no-deps "$SVC"

# option B: one-off override without editing the file:
#   temporarily set image to stack-doctor:rollback in the compose file, up -d.
```

Restore state only if a state file was actually corrupted (rare — atomic writes
make this nearly impossible now):

```bash
docker compose -f "$COMPOSE" stop "$SVC"
sudo tar xzf /root/stack-doctor-data-<timestamp>.tgz -C "$DATA"
docker compose -f "$COMPOSE" up -d --no-deps "$SVC"
```

Then capture logs for a bug report:

```bash
docker compose -f "$COMPOSE" logs --since 30m "$SVC" > /root/stack-doctor-fail-$(date +%F-%H%M).log
```

---

## After a successful 24–48 h soak

- Merge `fixes` → `main` so CI publishes the image and it's the new baseline:

  ```bash
  # locally
  git checkout main && git merge --no-ff fixes && git push origin main
  ```
  Then, if you prefer GHCR going forward, switch the compose `image:` back to
  `ghcr.io/machetie/stack-doctor:latest` and `docker compose pull && up -d --no-deps`.

- Clean up local build tags on the server once you're confident:

  ```bash
  docker image prune -f
  # keep stack-doctor:rollback until the next successful deploy
  ```
