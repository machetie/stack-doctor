# stack-doctor — `orphans` check: build plan

Add a new check that finds **orphaned debrid torrents** (content in the
decypharr/zurg debrid mount that **no library symlink references at the file
level**) and deletes them via the **debrid provider APIs** (Real-Debrid,
AllDebrid), reclaiming account slots and stopping the WebDAV "marked as bad"
error spam.

This was validated by hand on the live stack (deleted 10 Punisher torrents →
folder dropped from the mount → WebDAV errors stopped). This plan turns that
one-off into a safe, gated, test-covered stack-doctor module.

**Ground rules (same as IMPROVEMENTS.md):**
- Test-first: add cases under `tests/` using the `unittest` + `patch.object(doctor, …)` pattern before changing behavior.
- Preserve the safety posture: `DRY_RUN` default true, per-check action caps, mount-health gate on every destructive path.
- **Stdlib-only** — no new dependencies (debrid APIs via `urllib.request`).
- Run `python3 -m unittest discover -s tests -p 'test_*.py'` before every commit.

---

## Why this belongs in stack-doctor

- It is the **inverse of the `janitor`**: janitor quarantines library symlinks
  for dead releases; `orphans` removes debrid torrents that no symlink uses.
- It reuses existing machinery: mount-health gate (`_mount_ok_for`), action caps,
  min-age gate (like the scrubber), per-module state (`_atomic_write_json`),
  metrics (`metric_inc`/`metric_set`), and the flat env-config surface.
- The manual investigation already produced the exact algorithm and the two
  provider API shapes; this is codifying proven behavior.

---

## The catastrophic failure mode (design the guards FIRST)

The orphan test is *"no symlink references this folder → delete it."* If the
**library mount is down/empty or the symlink scan fails**, the "used" set is
empty → **every debrid torrent looks orphaned → the whole account gets deleted.**

Every one of these guards is mandatory and must be unit-tested. The check must
**abort the whole sweep** (delete nothing) if any trip:

| Guard | Env (default) | Rule |
|---|---|---|
| Mount-health gate | reuses `MOUNT_HEALTH_GUARDS` | library dirs AND the debrid mount must be UP (`_mount_ok_for` not False) or abort |
| Min used-set floor | `ORPHANS_MIN_SYMLINKS` (500) | if the symlink scan finds fewer than N links, assume a broken scan → abort |
| Max orphan ratio | `ORPHANS_MAX_RATIO` (0.35) | if > this fraction of a provider's folders look orphaned, abort (scan/mount fault, not reality) |
| Per-sweep cap | `ORPHANS_MAX_DELETES` (25) | never delete more than N torrents per sweep; drains slowly over many sweeps |
| Min age | `ORPHANS_MIN_AGE_HOURS` (720 = 30d) | only delete folders whose mtime is older than this (skip in-flight/pending imports) |
| Dry-run | global `DOCTOR_DRY_RUN` (true) | default logs `WOULD delete`, changes nothing |
| Load gate | `ORPHANS_LOAD_MAX` (12) | skip when host load is high (like other checks) |

> The floor + ratio guards are the two that turn "mount blip" from a
> catastrophe into a skipped sweep. They are non-negotiable.

---

## Environment reality (from the live stack — bake into defaults/docs)

- Debrid mount root: `/mnt/zurg` with views `__all__`, `__bad__`, `realdebrid`,
  `alldebrid`, `alldebrid2`, `torrents`, `nzbs`. Per-provider folders live under
  `/mnt/zurg/<provider>/`.
- Library symlinks: `/mnt/iceberg` + `/mnt/altmount-links`, targets look like
  `/mnt/zurg/<view>/<folder>/<file>` (folder name = path component after the view).
- **Deletion cannot use `rm`** on the mount (I/O error — read-only for deletes).
  Must call the provider APIs.
- **Folder → torrent id is many-to-one** (duplicates: one episode had 50 RD
  torrents). Delete *all* ids mapped to an orphan folder.
- Debrid API keys live in decypharr's `config.json` (`debrids[].api_key`), or are
  supplied explicitly to stack-doctor (preferred — flat env surface).

### Provider APIs (stdlib `urllib`, Bearer auth)

**Real-Debrid**
- List: `GET https://api.real-debrid.com/rest/1.0/torrents?limit=1000&page=N`
  (paginate; `X-Total-Count` header gives the total). Item: `{id, filename, hash, status}`.
- Delete: `DELETE https://api.real-debrid.com/rest/1.0/torrents/delete/{id}` → **204**.

**AllDebrid** (v4 is **discontinued** — use **v4.1**)
- List: `GET https://api.alldebrid.com/v4.1/magnet/status?agent=stackdoctor`
  → `{status:"success", data:{magnets:[{id, filename, hash, status}]}}`.
- Delete: `GET https://api.alldebrid.com/v4.1/magnet/delete?agent=stackdoctor&id={id}`
  → `{status:"success"}`.

Rate limit ~250/min per account → space deletes (~0.3s) and honor `ORPHANS_MAX_DELETES`.

---

## Config surface (new env vars)

```
ENABLE_ORPHANS=false                     # master toggle (off by default)
ORPHANS_DEBRID_MOUNT=/mnt/zurg           # provider views live here
ORPHANS_PROVIDER_VIEWS=realdebrid,alldebrid,alldebrid2
ORPHANS_LINK_DIRS=/mnt/iceberg,/mnt/altmount-links   # where library symlinks live
ORPHANS_MIN_AGE_HOURS=720                # 30d; only delete older orphans
ORPHANS_MAX_DELETES=25                   # per-sweep cap (torrents, not folders)
ORPHANS_MIN_SYMLINKS=500                 # abort if fewer links found (broken scan)
ORPHANS_MAX_RATIO=0.35                   # abort if >35% of a provider looks orphaned
ORPHANS_LOAD_MAX=12
ORPHANS_INCLUDE_BAD=true                 # prioritize the __bad__ view (decypharr-flagged)
ORPHANS_STATE_FILE=/data/orphans.json

# credentials — explicit (preferred) OR auto-read from decypharr config.json
ORPHANS_REALDEBRID_APIKEY=
ORPHANS_ALLDEBRID_APIKEYS=              # comma-separated (supports alldebrid + alldebrid2)
ORPHANS_DECYPHARR_CONFIG=/data/decypharr/config.json   # fallback: read debrids[].api_key
```

Secrets (`*_APIKEY*`) must be masked in logs (`_is_secret` already covers `APIKEY`).

---

## Algorithm (matches the validated one-off)

1. **Gate**: load check; if `DRY_RUN` off, verify mount-health for link dirs +
   debrid mount; check load. Abort on any failure.
2. **Build the used-set (file-level)**: `find <link dirs> -type l -printf '%l\n'`,
   keep targets under the debrid mount, extract the folder-name path component.
   A folder is "used" if ≥1 symlink points at any file inside it.
   - **Floor guard**: if total links < `ORPHANS_MIN_SYMLINKS` → abort.
3. **Per provider view**: list `/mnt/zurg/<provider>/` folders; `orphans =
   folders − used`. **Ratio guard**: if `len(orphans)/len(folders) > MAX_RATIO`
   → abort that provider (log loudly).
4. **Age filter**: drop orphans whose mtime is newer than `MIN_AGE_HOURS`.
5. **Map to torrent ids**: fetch the provider's `filename → [ids]` map (cached
   per sweep); for each orphan folder collect all ids (handles duplicates).
   Unmatched folders (no id) are logged and skipped (never blindly deleted).
6. **Delete (capped, gated)**: up to `ORPHANS_MAX_DELETES` torrent ids total this
   sweep. `DRY_RUN` → log `WOULD delete <folder> (<n> ids)`. Else call the
   provider delete API per id; record each deleted id + folder to state (audit).
7. **Metrics + summary**: emit counters and one summary line.

Optional priority: process the `__bad__` view first (decypharr already flagged
these; highest value, lowest risk).

---

## State & auditability (`/data/orphans.json`)

- `deleted`: append-only list of `{ts, provider, id, folder}` — the audit trail
  of everything removed (re-grabbable via the arrs; the record is what matters).
- `cooldown`: folder → ts, so a name that reappears (re-grab) isn't re-hammered.
- `unmatched`: orphan folders with no debrid id, surfaced for manual review.

All writes go through `_atomic_write_json` (already in the codebase).

---

## Metrics (Prometheus, via the P2-1 registry)

- `stackdoctor_orphans_found{provider}` (gauge)
- `stackdoctor_orphans_deleted_total{provider}` (counter)
- `stackdoctor_orphans_skipped_total{reason=age|cap|unmatched|dry_run}`
- `stackdoctor_orphans_aborted_total{reason=mount|floor|ratio|load}`

---

## Code shape (mirrors existing checks)

```python
# config block near the other ENABLE_* / *_STATE flags
EN_ORPHANS = _b("ENABLE_ORPHANS", False)
ORPH_MOUNT = os.environ.get("ORPHANS_DEBRID_MOUNT", "/mnt/zurg")
ORPH_VIEWS = [v.strip() for v in os.environ.get("ORPHANS_PROVIDER_VIEWS","realdebrid,alldebrid,alldebrid2").split(",") if v.strip()]
ORPH_LINK_DIRS = [p.strip() for p in os.environ.get("ORPHANS_LINK_DIRS","/mnt/iceberg,/mnt/altmount-links").split(",") if p.strip()]
ORPH_MIN_AGE   = _i("ORPHANS_MIN_AGE_HOURS", 720)
ORPH_MAX_DEL   = _i("ORPHANS_MAX_DELETES", 25)
ORPH_MIN_LINKS = _i("ORPHANS_MIN_SYMLINKS", 500)
ORPH_MAX_RATIO = _f("ORPHANS_MAX_RATIO", 0.35)
ORPH_LOAD_MAX  = _i("ORPHANS_LOAD_MAX", 12)
ORPH_STATE     = os.environ.get("ORPHANS_STATE_FILE", "/data/orphans.json")

class Debrid:                      # thin RD/AD client (urllib, Bearer)
    def list_map(self): ...        # filename -> [ids]  (RD paginates; AD one call)
    def delete(self, tid): ...     # RD: DELETE /torrents/delete/{id}; AD: v4.1/magnet/delete

def _orphans_used_set(): ...       # find -printf '%l' -> folder-name set (+ count)
def _orphans_for_view(view, used): ...   # folders - used, ratio-guarded, age-filtered
def check_orphans(): ...           # gate -> build -> per-view -> cap-delete -> metrics -> summary

# register in CHECKS
("orphans", EN_ORPHANS, check_orphans),
```

Keep `check_orphans()` read-only unless `not DRY_RUN` **and** all guards pass.

---

## Tests (new `tests/test_orphans.py`)

Model on `tests/test_scrubber.py` / `test_mount_guard.py` (patch.object, temp dirs).

**Safety guards (highest priority):**
- `test_aborts_when_used_set_below_floor` — few symlinks → 0 deletes.
- `test_aborts_when_orphan_ratio_too_high` — >MAX_RATIO → 0 deletes.
- `test_aborts_when_mount_down` — `_mount_ok_for` False → 0 deletes.
- `test_dry_run_deletes_nothing` — DRY_RUN true → only `WOULD` logs, `Debrid.delete` never called.
- `test_respects_max_deletes_cap` — many orphans, only N deleted.
- `test_min_age_skips_recent` — new mtime folder not deleted.

**Correctness:**
- `test_file_level_used_detection` — a folder with one referenced file is NOT an orphan.
- `test_maps_folder_to_all_duplicate_ids` — 8-id folder → 8 delete calls.
- `test_unmatched_folder_is_skipped_not_deleted`.
- `test_deleted_ids_recorded_in_state` (via patched `_atomic_write_json`).

**Providers (mock urllib):**
- `test_rd_paginates_and_deletes` — patch `urlopen`; 2 pages merged; DELETE→204.
- `test_ad_uses_v41_and_deletes`.
- `test_keys_read_from_decypharr_config_when_env_absent`.

---

## Rollout

1. **Land the module + tests** on a branch; `DRY_RUN` true, `ENABLE_ORPHANS`
   false by default — zero behavior change until opted in.
2. **Enable in dry-run on the live stack** (`ENABLE_ORPHANS=true`, keep
   `DOCTOR_DRY_RUN=true`) for a few sweeps; confirm the `WOULD delete` list
   matches the reviewed TSVs (`/root/zurg-orphans/orphans-*.tsv`) and that the
   guards don't trip in normal operation.
3. **Flip to live with a tiny cap** (`ORPHANS_MAX_DELETES=25`, `MIN_AGE=720h`)
   so it drains the 2,762-torrent backlog slowly over many sweeps, watched via
   `/metrics` and the sweep summary.
4. **Tune** cap/age once trusted; keep the floor/ratio guards permanently.

---

## Suggested commit sequence (each with tests, green suite)

1. `feat(orphans): debrid client (RD paginate/delete, AD v4.1) + key loading`
2. `feat(orphans): file-level orphan detection + safety guards (floor/ratio/mount/age)`
3. `feat(orphans): capped deletion, state/audit, metrics, sweep summary`
4. `feat(orphans): register check + docs (env surface, DEPLOY notes)`
