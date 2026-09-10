# stack-doctor — Improvements & Fixes Plan

Status snapshot (2026-08-24): full read of `doctor.py` (5,840 lines), the
`tests/` suite (48 tests, all green), git history, and the in-flight branch.
This plan captures verified gaps and enhancements, ranked by real-world risk.

**Ground rules for every item below**
- Test-first: add/extend a case under `tests/` matching the existing
  `unittest` + `patch.object(doctor, ...)` pattern before changing behavior.
- Preserve the safety posture: `DRY_RUN` default true, per-check action caps,
  mount-health gate on every destructive path.
- Keep it stdlib-only (no new dependencies) — a hard constraint of this project.
- Run `python3 -m unittest discover -s tests -p 'test_*.py'` before every commit.

---

## Already shipped (this branch)

- **fix(scrubber): stop tier-1 false positives from deleting good files**
  (`f261ffd`). Trust `ffprobe`/`ffmpeg` rc=0 by default, `SCRUBBER_STRICT_STDERR`
  opt-in, expanded benign-stderr allowlist, `_scrub_confirm_decode` 5s decode
  gate before any tier-1 BAD delete, plus `tests/test_scrubber.py`.

---

## P0 — correctness / data-safety (do first)

### P0-1. Atomic state-file writes  ⭐ highest value
**Problem.** All 10 state writers use the truncate-then-write pattern
`json.dump(s, open(F, "w"))`:

| # | Function | File |
|---|----------|------|
| 1 | `_save_state` | `STATE_FILE` (queue + churn offenders) |
| 2 | `_scrub_save_state` | `SCRUB_STATE` |
| 3 | (watchlists save) | `WL_STATE` |
| 4 | (holidays save) | `HOL_STATE` |
| 5 | `_backlog_save_state` | `BACKLOG_STATE` |
| 6 | `_repair_save_state` | `REPAIR_STATE` |
| 7 | `_missing_disk_save_state` | `MISSING_DISK_STATE` |
| 8 | `_riven_save_state` | `RIVEN_STATE` |
| 9 | `_scout_save` | `SCOUT_STATE` |
| 10 | `_config_write` / `_ui_save` | `CONFIG_FILE` |

If the container is killed mid-write (OOM, `docker restart`, SIGKILL) the file
is left truncated/half-written. `_load_state` and friends catch the exception
and return `{}` — silently wiping churn offender counts, scrubber strikes/results,
repair cooldowns, watchlist "already added" records, and backlog cooldowns.

**Consequence after an unlucky restart:** full library re-scan, watchlist
re-adds everything, repair cooldown reset → re-grab storm, churn brake forgets
offenders. This is especially relevant given the stack's restart-heavy history.

**Fix.** One helper, route all 10 writers through it:

```python
def _atomic_write_json(path, obj, indent=None):
    """Write JSON durably: temp file in the same dir, fsync, atomic rename.
    A crash mid-write leaves the previous good file intact (never truncated)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)   # atomic on POSIX
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise
```

**Tests (new `tests/test_state_io.py`).**
- `test_atomic_write_replaces_content` — normal path.
- `test_crash_mid_write_leaves_old_file_intact` — pre-seed a good file, patch
  `json.dump` to raise mid-call, assert the original bytes still parse.
- `test_creates_parent_dir`.
- Parametrize across all 10 save funcs to assert none call bare `open(...,"w")`.

**Effort:** ~20 lines + tests. **Behavior change:** none (durability only).

### P0-2. Serialize read-modify-write on shared state in event mode
**Problem.** `sweep()` uses `_lock.acquire(blocking=False)` so only one sweep
*body* runs at a time. But in event mode each webhook spawns
`threading.Thread(target=sweep)` and the main loop also calls `sweep()`. A second
sweep that can't get the lock just returns — fine — but the lock covers dispatch,
not the per-module `load → mutate → save` sequence. Two near-simultaneous
webhooks can still interleave.

**Fix.** Largely subsumed by P0-1 (atomic replace → last-writer-wins instead of
corruption). For full correctness, add a single module-level
`_state_lock = threading.Lock()` held around each `load()→save()` pair, or a
small `with _state_file_lock(path):` context. Keep it coarse; these writes are tiny.

**Tests.** Spawn N threads each doing load→increment→save; assert final count
== N (no lost updates) once locked.

---

## P1 — robustness (directly serves known pain)

### P1-1. HTTP retry/backoff on arr calls
**Problem.** `Arr._req` does a single `urlopen`. The changelog documents arr APIs
going slow under search load (the whole reason the `seerr` retry module exists).
A single transient 503/timeout on `queue()` returns `None` and the check skips
that instance for the entire sweep.

**Fix.** Small retry wrapper (2–3 tries, short exponential backoff, jitter) around
`_req` for idempotent GETs and the testall POSTs. Do **not** auto-retry DELETE
`/queue` or ManualImport (non-idempotent) — retry only reads and explicit test calls.

```python
def _req_retry(self, method, path, data=None, t=None, tries=3):
    delay = 0.5
    for i in range(tries):
        try:
            return self._req(method, path, data=data, t=t)
        except Exception:
            if i == tries - 1 or method not in ("GET",):
                raise
            time.sleep(delay); delay *= 2
```

**Tests.** Patch `_req` to fail twice then succeed; assert `queue()` returns data.
Assert DELETE is never retried.

### P1-2. Queue pagination fallback
**Problem.** `check_queue` fetches `pageSize=1000` with no follow-up page. A mass
grab (>1000 records — has happened) makes records beyond 1000 invisible to the
doctor — a blind spot in the exact scenario the tool exists for.

**Fix.** Loop pages until `records` is empty or `totalRecords` is reached; cap
total fetched with a `DOCTOR_QUEUE_MAX_FETCH` (default e.g. 5000) to bound work.

**Tests.** Mock a 2-page response; assert both pages merged; assert the cap halts.

### P1-3. Shell-command hardening / surfacing
**Problem.** `run_cmd`/`run_output` use `shell=True` with operator-set command
strings (`DECYPHARR_RESTART_CMD`, `ALT_RESTART_CMD`, `ALT_PROP_FIX_CMD`,
`METACLEAN_FAILED_CMD`, `JANITOR_LOG_CMD`, `pct exec` probes). Trust boundary is
the operator, so not an injection vuln — but these run as **root** in a container
with `/var/run/docker.sock` and `/mnt` rshared mounted. A malformed value fails
opaquely; a typo in a restart command runs as root against the docker socket.

**Fix (low-risk subset).**
- Log the exact command (masked for secrets) at DEBUG before running.
- Add a startup validation pass that logs a clear warning for empty/obviously
  malformed configured commands.
- Optional: a `DOCTOR_ALLOW_SHELL=true` gate (default true for back-compat) so a
  hardened deployment can require the safer split-args form.

**Tests.** `test_run_cmd_masks_secrets_in_log`, `test_empty_cmd_returns_none`.

---

## P2 — observability & maintainability

### P2-1. Prometheus `/metrics` endpoint
**Why.** The doctor already runs an HTTP server and computes rich counts
(scrubber ok/suspect/bad, queue actions, churn offenders parked, repair re-grabs,
backlog searches). Today they only hit the log. A `/metrics` text-format endpoint
turns reactive RCA sessions into trend detection, and fits the existing
`exportarr`/`scraparr` Prometheus setup on this stack.

**Fix.** A module-level counter dict updated by each check; render Prometheus text
in `_build_server` under `/metrics` (no auth or same token gate as UI). Stdlib only.

**Suggested series.**
- `stackdoctor_sweep_total`, `stackdoctor_sweep_errors_total{check=}`
- `stackdoctor_scrubber_files_total{result=ok|suspect|bad}`
- `stackdoctor_queue_actions_total{action=,instance=}`
- `stackdoctor_churn_offenders`, `stackdoctor_repair_regrabs_total`
- `stackdoctor_mount_up{mount=}` (1/0 from the mount-guard cache)

**Tests.** Hit `/metrics` with a seeded counter dict; assert format + values.

### P2-2. Per-sweep structured summary line
**Why.** Each check logs its own summary; there's no single "sweep N did X" line
to grep/alert on. Add one INFO line per sweep aggregating counts + duration.

### P2-3. (Deferred) module split
Not urgent — the single file works and is tested. If the file keeps growing,
split into a package (`doctor/queue.py`, `doctor/scrubber.py`, shared `doctor/arr.py`,
`doctor/state.py`) preserving the flat env-config surface. Only worth doing if it
starts blocking test isolation or review.

---

## Explicitly NOT doing (and why)

- **Adding Cleanuparr / Decluttarr / Checkrr** — redundant with, and would race,
  `check_queue` (strike system, per-condition actions, churn brake) and the
  scrubber/repair/missing-from-disk trio. A second actor on the same queue +
  blocklist is a regression, not an improvement.
- **Config env-vs-`config.json` "footgun"** — already solved and tested
  (`ConfigDivergenceTest`, commits `1b52ed2` / `1925d14`): env is authoritative,
  `config.json` is fallback, empty strings ignored, ignored keys warned. Only
  enhancement worth considering is surfacing the "ignored key" warning in the UI
  itself rather than stderr — low priority.

---

## Suggested execution order

1. **P0-1** atomic writes (isolated, highest safety ROI, easy to verify).
2. **P0-2** state locking (small, builds on P0-1).
3. **P1-1** HTTP retry (directly fixes "arr slow under load").
4. **P1-2** queue pagination (closes the mass-grab blind spot).
5. **P2-1** `/metrics` (turns firefighting into trend detection).
6. **P1-3** shell hardening, **P2-2** sweep summary — opportunistic.
7. **P2-3** module split — only if it becomes a blocker.

Each lands as its own conventional commit with tests, pushed to `origin/fixes`.
