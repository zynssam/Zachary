# Phase 0 — Scaffold: Report

Status: **complete, approved defaults applied.** Commits: `21dc476`, `82e246a`, `52db831` on `main`
(https://github.com/zynssam/Zachary, private).

## 1. Goal

From `prompt.md`:

- `pyproject.toml` with dev extras, the package layout from `CLAUDE.md`, ruff/mypy/pytest config.
- `core/db.py` with migration #1 (full schema), WAL mode, sqlite-vec loaded.
- `core/config.py`: index roots, default exclude rules, per-folder rules, settings stored in the DB.
- `core/platform/` stubs for Windows and Linux.

**Done when:** the DB is created with all tables, and tests run green on Linux (and Windows CI if available).
Both are met.

## 2. What was built

### 2.1 Project setup
- `pyproject.toml`: package `zachary` 0.0.1, Python >= 3.11 (developed on 3.12). Runtime dependency: `sqlite-vec` only.
  Dev extras: `pytest`, `ruff`, `mypy`.
- ruff (line length 100, rules E F W I B UP SIM RUF PTH C4), mypy `strict`, pytest with `tests/` as testpaths.
- Package layout from `CLAUDE.md` created (`core`, `indexer`, `brain`, `search`, `voice`, `actions`, `ui`, `telemetry`),
  typed (`py.typed`). Modules beyond Phase 0 are empty packages.
- `.github/workflows/ci.yml`: ubuntu + windows, Python 3.11 + 3.12; runs ruff check, ruff format --check, mypy, pytest.
- `bench/bench_phase0.py`: timing script (section 4).

### 2.2 `core/db.py`
- **Connection setup** (`connect`): sqlite-vec loaded, autocommit mode, `foreign_keys=ON`, `busy_timeout=5000`,
  WAL + `synchronous=NORMAL` (read-write), optional read-only connection for the assistant process.
- **Migrations:** numbered tuple `MIGRATIONS`; version stored in `PRAGMA user_version`. Each migration runs in one
  transaction and rolls back fully on error. Opening a DB from a newer build raises `SchemaTooNewError`.
- **Migration #1 — full schema:** `files`, `files_fts` (FTS5, prefix 2/3), `chunks`, `vec_chunks`, `vec_files`
  (sqlite-vec, int8[256], cosine), `relations` (kind restricted to content/name/location/coaccess/feedback),
  `projects`, `version_groups`, `topics`, `topic_members`, `access_log`, `sessions`, `session_files`, `feedback`,
  `action_plans`, `undo_log`, `settings`, `exclude_rules`, `folder_rules`.
- **Integrity:**
  - Foreign keys cascade, so deleting a file (or folder) removes its chunks, relations, access log, project and
    topic rows.
  - Triggers clean the virtual tables (FTS and both vector tables), which have no foreign keys.
  - `undo_log` references `action_plans` with `ON DELETE RESTRICT`, so undo history can't be lost.
  - CHECK constraints on enumerated columns (plan status, undo op, access source, folder rule, exclude kind).

Deviations from `CLAUDE.md`'s data model (all additive or renames):

| Change | Reason |
|---|---|
| `files.inode`, `files.dev` added | Renames/moves keep identity without re-embedding |
| `sessions.start_ts` / `end_ts` | `END` is an SQL keyword |
| Surrogate `id` on `access_log`, `feedback`, `undo_log` | Rows need stable identity; `access_log` has a UNIQUE (file, ts, source) |
| Fixed value lists for plan status, undo op, folder rule | Prevent invalid states |

Embedding size 256 matches Model2Vec; changing it later needs a migration.

### 2.3 `core/config.py`
- **Settings** stored as JSON in the `settings` table, with a typed `Settings` dataclass for defaults
  (`index_roots`, `indexing_paused`, `quickbar_hotkey`, `language_code`, `llm_enabled`, `embed_max_kb`).
  Unknown keys and wrong types are rejected.
- **Exclude rules** in `exclude_rules`: default directory names (`node_modules`, `.git`, `__pycache__`, `venv`,
  `build`, `dist`, `target`, caches...), default filename globs (`~$*`, `*.tmp`, `*.swp`, `*.part`,
  `*.crdownload`...), and OS/system paths from the platform. `seed_defaults()` is idempotent and never re-enables a
  rule the user disabled. User rules can be added, disabled, or removed; default rules can only be disabled.
- **`ExcludeMatcher`:** precompiled matcher with a cheap by-name check for scanner loops and a full-path check.
  Case-insensitive on Windows. Path matching respects folder boundaries (`/sys` does not match `/system-notes`).
- **Index roots:** from settings, else platform defaults. Nested roots are collapsed; roots that don't exist
  (unplugged drive) are kept and left for the scanner to skip.
- **Root-beats-ancestor-exclude:** a path exclude that is a strict ancestor of an index root is ignored, so a USB
  drive under `/run/media` still indexes even though `/run` is a system exclude. Excludes equal to or inside a root
  still apply.
- **Per-folder rules** (`never_touch`, `auto_tidy`, `name_only`), inherited by subfolders.
- Name-only extension list (media, archives, binaries) defined for later phases.

### 2.4 `core/platform/`
`Platform` protocol (data dir, default roots, system excludes, recent items, open, reveal, watch-limit check, low
priority) with `LinuxPlatform` and `WindowsPlatform`, selected by `get_platform()`.

**Default index roots** (decision recorded: "home, Documents, Desktop, Downloads, plus drives and mounts"):

| | Linux | Windows |
|---|---|---|
| User folders | `$HOME` + XDG Desktop/Documents/Downloads from `user-dirs.dirs` | `$HOME` + Known Folders Desktop/Documents/Downloads (follows OneDrive redirects) |
| Disks | Mounts of real disk filesystems (ext*, btrfs, xfs, vfat, exfat, ntfs...) except `/`, `/boot`, `/var`, snaps, and anything containing `$HOME` | Fixed and removable drives except the system drive |
| Not included | NFS/SMB/sshfs, tmpfs, squashfs | Network, CD-ROM, RAM drives |

Implemented for real: data directories, system excludes, root discovery. Stubs raising `NotImplementedError`
(naming their phase): recent items, open, reveal, Linux watch limit, low priority. Windows watch limit returns OK
(no inotify-style cap).

## 3. Testing

### 3.1 Results

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | Clean |
| `mypy zachary tests` (strict) | No issues in 19 files |
| `pytest` | **36 passed** in 0.22 s (Linux, Python 3.12) |
| CI: ubuntu 3.11 / 3.12 | passed |
| CI: windows 3.11 / 3.12 | passed (36 tests each) |

### 3.2 Test inventory (36 tests, 4 files)

`tests/test_db.py` (13)
- all tables created and schema version set; WAL, foreign keys and synchronous pragmas; sqlite-vec loaded
- migration idempotent; refuses newer schema; failed migration rolls back (table from the earlier statement is not left behind)
- int8 vector nearest-neighbour ordering; FTS5 prefix search
- deleting a folder cascades to files, FTS, vectors, chunks, chunk vectors, relations, access log, projects
- undo log cannot be orphaned by deleting its plan; CHECK constraints reject bad values
- read-only connection sees writes and refuses to write; `transaction()` rolls back on error

`tests/test_config.py` (9)
- settings defaults, round-trip, validation (unknown key, wrong type)
- default vs custom index roots; seeding idempotent and respects disabled rules
- default excludes (node_modules, .git, `__pycache__`, `~$` files, `.part`, `.crdownload`, system path, boundary cases)
- user excludes add/remove; case-insensitive matcher; folder-rule inheritance; DB path from `ZACHARY_DB`

`tests/test_platform.py` (5; one parametrized over both platforms)
- platform matches OS; XDG and LOCALAPPDATA data dirs; own data dir is in the excludes
- guard test: no OS-specific imports outside `core/platform/` (enforces the `CLAUDE.md` rule)

`tests/test_roots.py` (8)
- Linux mount parsing (disks kept; root, boot, tmpfs, snaps, NFS/SMB/sshfs, duplicates dropped; spaces in paths decoded)
- XDG user-dir parsing (`$HOME` expansion, "disabled" entries, unwanted keys)
- Windows drive selection (system, network, CD-ROM skipped)
- root collapsing; default roots on this machine; root beats ancestor exclude; excludes inside/equal to a root still apply

### 3.3 Bugs found by testing
1. **Windows CI, mypy:** the Linux branch of `get_platform()` looked unreachable on Windows. Fixed by relaxing
   `warn_unreachable` for the platform modules (commit `82e246a`).
2. **Windows CI, pytest:** the mount and XDG parsers used native path semantics, so POSIX test paths failed on
   Windows. They now use POSIX semantics on every OS (commit `52db831`). Real Linux behavior was unaffected.
3. Found while probing before writing code: a SQL trigger deleting from a `vec0` table works through FK cascades,
   which the cleanup design depends on.

### 3.4 Timings (`python bench/bench_phase0.py`, Linux, Python 3.12, 30 runs)

| Measurement | Median | p95 |
|---|---|---|
| Create new DB + migration #1 | 3.3 ms | 4.1 ms |
| Open existing DB + load sqlite-vec | 0.76 ms | 0.88 ms |
| Exclude check by name (scandir hot path) | 638 ns/entry | — |
| Exclude check by full path | 20 µs/path | — |

An earlier run on the same machine gave 7.8 ms / 1.9 ms / 1.3 µs / 26 µs; these timings vary between runs with disk
and CPU state. The by-name check puts exclude cost for a 200k-entry scan at roughly 0.1–0.3 s. The full-path check is
too slow for the scan loop, so the Phase 1 scanner must use the by-name check and use the full-path check only for
watcher events.

The `CLAUDE.md` budgets (search < 50 ms p95, related < 10 ms, quick bar < 150 ms) are not measurable yet; there is
no search code. The 200k-file synthetic tree is built in Phase 1.

## 4. Known limitations / not verified
- The Windows root discovery (Known Folders, drive listing) calls Windows APIs and has only run on the CI
  machine, never on a real desktop. Its drive-selection helper is unit-tested.
- Network filesystems are not indexed by default; the rest of the Windows system drive (outside `$HOME`) is not
  either.
- FTS5 and vector tables are created but nothing populates them until Phase 1 and Phase 3.
- `.claude/settings.json` (local permission rules) is not committed.

## 5. Handoff to Phase 1
- Scanner: use `ExcludeMatcher.excludes_name` inside `os.scandir` loops; call `Config.exclude_matcher()` once per scan.
- Store `inode`/`dev` on every file for rename detection.
- Wrap batches in `db.transaction()`; connections are autocommit.
- Implement the stubbed platform hooks: `recent_items`, `check_watch_limit`, `lower_process_priority`.
