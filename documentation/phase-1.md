# Phase 1: Metadata memory (report)

Status: **complete, waiting for review.** Branch `phase-1` (https://github.com/zynssam/Zachary/tree/phase-1),
not yet merged into `main`. CI is green on Linux and Windows (Python 3.11 and 3.12).

## 1. Goal and "done when"

From `prompt.md`: the scanner, watcher, catch-up, projects, recent-file ingestion, time parsing, keyword+recency search,
the CLI, the inotify limit check, and the privacy controls.

| Done-when check | Result |
|---|---|
| `zachary.cli search "fees yesterday"` finds a fee document opened yesterday | ✅ Test (`test_fees_yesterday_finds_doc_opened_yesterday_via_os_recent`) puts it first for English, Hinglish, Kannada and typo variants. On the 200k benchmark it ranks 2nd, behind an exact-name `Fees.pdf` that was also edited yesterday (see 5.2). |
| `zachary.cli projects` lists projects in order | ✅ On the real home folder: `Zachary`, `Room-Design`, ... by latest edit. Tests cover ordering and the nesting rule. |
| A new file appears in search within ~2 s | ✅ 200k tree: p50 **1.54 s**, p95 **1.62 s**. The test bound is 3 s on CI VMs (the Windows runner measured 2.50 s). |
| Search < 50 ms p95 on the synthetic tree | ✅ **p95 29.7–33.7 ms** (warm) over 1,200 searches on 200k files |

## 2. What was built

| Module | What it does |
|---|---|
| `core/db.py` migration #2 | `files_fts` with the porter stemmer (so "fees" matches "Fee"). It keeps combining marks inside words, so Devanagari and Kannada names aren't split. Adds `files_fts_vocab` (for typo correction) and a `state` table (journal cursors, config revision, indexer heartbeat). |
| `core/config.py` | `PathRules`: one root-aware exclude check shared by the indexer and search. Also: a config revision counter (the running indexer reloads within ~2 s), skipping hidden folders by default, and paths stored in their on-disk case. |
| `indexer/store.py` | All writes to the files tables. It decides insert, update, move (keeps the id) or delete; inserts in batches; keeps the FTS rows in sync; records accesses and edits; purges excluded paths. |
| `indexer/scanner.py` | Parallel `os.scandir` walk (one walker thread per root, all roots at once) feeding a single DB writer. The same code does the first scan and the Linux catch-up (diff against a DB snapshot). A rename is recognised by inode, with a size and mtime check. |
| `indexer/watcher.py` | watchdog events mark paths dirty; a path is processed after 1.5 s of quiet, in batches. Both ends of a move go in the same batch, so identity is kept. Live project-marker updates and edit logging. |
| `indexer/catchup.py` | Windows: replays the USN journal from the saved cursor. Linux, or a journal that can't be read: re-scan and diff. |
| `indexer/projects.py` | Marker detection (`.git`, `package.json`, `pyproject.toml`, ... `*.sln`, `*.csproj`), the nesting rule, `last_activity` = newest indexed edit inside the project, and incremental refresh. |
| `indexer/recent.py` + platform | Linux `recently-used.xbel` and Windows Recent Items (`.lnk` parsing) → `access_log` (`os_recent`). |
| `indexer/service.py`, `python -m zachary.indexer` | The daemon: low priority, catch-up, watcher, recent-file ingestion every 2 min, reacts to pause/resume/exclude/root changes, heartbeat, watch-limit fallback to periodic re-scans. |
| `brain/timeparse.py` | English, Hindi/Hinglish and Kannada time words, weekdays, "N days ago", "do din pehle", "past 5 days", typo tolerance. |
| `search/tokens.py`, `search/hybrid.py` | Keyword tiers (bm25) + recency, merged with RRF; time-range filter; kind hints; typo correction; "why it matched" text. |
| `cli.py` | `search`, `projects`, `recent`, `scan`, `status`, `roots`, `exclude`, `forget`, `pause`/`resume`, `set`. |

### Platform specifics (`core/platform/` only)
- **Linux:**
  - Reads `recently-used.xbel` (755 items on this machine).
  - Checks `fs.inotify.max_user_watches`. If the limit is too low it prints the exact `sysctl` commands and never asks for root.
  - Runs at nice +10 and the lowest best-effort IO class.
- **Windows:**
  - Parses Recent Items `.lnk` files with a pure-Python [MS-SHLLINK] parser (no pywin32).
  - Uses `PROCESS_MODE_BACKGROUND_BEGIN` for low priority.
  - Reads the USN journal through ctypes, handling V2 and V3 records.

## 3. Design decisions made (easy to change)

1. **Hidden folders are not indexed by default** (`set index_hidden_folders true` turns them on). `~/AppData` is excluded on Windows, as are `site-packages`, `~/snap` and `~/go/pkg`.
   - **Why:** on your real home folder, 120k of 129k entries were app data (VS Code extensions, Claude plugins, `site-packages`, browser caches). They polluted projects, recent and search.
   - **Effect:** 129,594 → 9,470 entries; first scan 23.5 s → 1.1 s; inotify watches 25,786 → 1,609.
   - Project markers like `.git` are still detected.
2. **"Forget" = add a path exclude.** The indexer purges the rows and all derived data; search hides them immediately, even before the purge.
3. **Windows USN catch-up uses the classic journal read only.**
   - The unprivileged variant returned records with empty names and missed our own changes (verified on CI).
   - The classic read needs volume read access (normally admin). Without it, catch-up falls back to the scan diff. That keeps "no admin by default" while staying fast when allowed.
4. **Kind hints:** "code", "document", "photo", "video", "song", "folder" boost that kind instead of being matched against names. With no hint, there's a small prior for documents and code over media.
5. **Missing roots** (an unplugged USB drive) keep their rows. Removed roots are purged.
6. **Symlinks and Windows junctions are not followed**, to avoid loops and duplicate trees. OneDrive placeholder files are kept.
7. **Windows paths are stored in their real on-disk case.** Case is only folded when matching against rules.
8. **Inode, file ID and device numbers are folded into signed 64-bit.** Windows `st_ino` can be 128-bit, and `st_dev` can be ≥ 2⁶³.

## 4. Testing

### 4.1 Results

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean |
| `mypy zachary tests` (strict), also `--platform win32` | no issues (45 files) |
| `pytest` on Linux (local + CI 3.11/3.12) | **149 passed, 4 skipped** (Windows-only tests) |
| `pytest` on Windows CI 3.11/3.12 | **150 passed, 3 skipped** (Linux-only tests) |

### 4.2 Test inventory (153 tests, 15 files; Phase 0 tests unchanged)

| File | # | Covers |
|---|---|---|
| `test_timeparse.py` | 39 | 29 phrases (English, Hinglish, Kannada, typos), false positives ("sundar", "sun.png", "kalman"), weekday edge case |
| `test_search.py` | 18 | "fees yesterday" via OS recent items (5 phrasings), stemming, the exact-before-prefix tier, as-you-type prefixes, folder words, typo correction, Devanagari, time-range fallback, time-only queries, recency, exclude hiding, kind hints, injection-like input, speed |
| `test_scanner.py` | 14 | first scan + excludes, no-op re-scan, new/modified/deleted, rename and folder move keep ids and update FTS/parent, recycled inode isn't a move, symlinks, subtree scan, purge, removed root, FTS rebuild, file→folder swap, hidden folders, 64-bit folding |
| `test_tokens.py` | 9 | camelCase, digits, separators, Indic scripts, accents |
| `test_watcher.py` | 8 | real watchdog: new file searchable < 3 s at the default debounce, create/modify/delete, rename + folder move identity, a new tree with a nested project, marker added, excludes ignored, burst batching, edit-log rate limit |
| `test_platform_parsers.py` | 7 | xbel (newest app time, non-file URLs), watch-limit message, `.lnk` ANSI/Unicode/ID list/garbage, USN V2/V3/unknown records |
| `test_os_live.py` | 6 | **Windows:** USN end-to-end catch-up (rename keeps id), USN read sees create + rename, Recent Items + roots, priority. **Linux:** priority, watch limit |
| `test_projects.py` | 6 | nesting rule, outer projects, `*.sln`, ordering by activity (`node_modules` ignored), `touch`, live marker add/remove |
| `test_catchup.py` | 4 | scan path, journal replay with a fake journal, journal unavailable → scan, missing root keeps rows |
| `test_cli.py` | 4 | scan/search/json/projects/recent, forget hides then purges, exclude/pause/status/roots, `set` |
| `test_service.py` | 2 | daemon: catch-up, live watch, forget from another process, pause/resume catch-up, heartbeat, low watch limit reported |
| Phase 0 files | 36 | unchanged (db, config, platform, roots) |

### 4.3 Bugs found and fixed during the phase
1. **Windows `OverflowError`:** `st_ino` (128-bit in Python 3.12) and `st_dev` (64-bit volume serial) don't fit in SQLite INTEGER. Both are now folded into signed 64-bit, and journal IDs are folded the same way.
2. **The Windows unprivileged USN read was useless:** it returned no names and missed records. It was replaced by the classic read with a scan fallback.
3. **The CLI's exclude filter checked folder names *above* the root**, so a root under `~/.cache` or `build/` returned nothing. Fixed with the shared, root-aware `PathRules`.
4. **"fees" ranked "feedback" above "Fee Receipt":** a bm25 short-name bias combined with the as-you-type prefix. Fixed by keyword tiers (whole words before prefixes).
5. **Devanagari queries returned nothing**, because the time-parser regex split words at vowel signs. Fixed with shared Indic word ranges.
6. **Deleted-row counts were wrong:** FK cascades removed children before the range DELETE reached them. Rows are now counted first.
7. **Three test races** (service reconfigure, async pause, burst timing) were fixed in the tests. They were not product bugs.

## 5. Timings

### 5.1 Synthetic tree
200,000 indexable files + 47,593 in `node_modules`/`.git`/`__pycache__`, 16,196 folders. It lives on the real disk (btrfs on LUKS) in `~/.cache/zachary-bench`. Run with `bench/make_tree.py` and `bench/bench_phase1.py`.

| Measurement | First version | Final | Budget |
|---|---|---|---|
| First metadata scan | 78.6 s | **27.2 s** (7,362 files/s), 178 MB peak RSS | "minutes" ✅ |
| Catch-up, nothing changed (Linux) | 12.8 s | **6.8 s** | |
| Catch-up after 1,000 offline changes | 19.8 s | **7.2 s**; renames kept identity **100/100** | |
| Project detection (854 projects) | 110 ms | **112 ms** | |
| Search, 40 queries × 30 runs | p95 150 ms | **p50 15–18 ms, p95 29.7–33.7 ms, p99 36–48 ms** | < 50 ms ✅ |
| Search, first query in a new process | — | 21–56 ms | |
| Projects list / recent top-20 | ~10 ms | p95 **8.7 / 7.8 ms** | |
| Watcher start (16k inotify watches) | 2.3 s | **1.9 s** | |
| New file → searchable | p95 2.46 s | **p50 1.54 s, p95 1.62 s** | ~2 s ✅ |
| DB size | 132 MB | 136 MB (626 B/row) | |

What changed between the first and final versions:
- **Scan:**
  - Batched `executemany` inserts.
  - No FTS delete on fresh rows.
  - Folder-path tokens cached.
  - Tuple snapshots instead of objects.
  - Suffix checks instead of `fnmatch`.
  - One walker thread per root instead of 8 (measured: 1 thread 5.8 s vs 4 threads 8.2 s on a warm cache, because of GIL contention).
- **Search:**
  - Keyword tiers, so the OR fallback only runs when nothing else matched.
  - Time-range queries match only inside the range, unranked. bm25 over all matches cost 84–129 ms, and seeking FTS per rowid cost 200+ ms.
  - "Why it matched" computed only for the shown results.
  - Plain-tuple cursors.

The synthetic vocabulary is deliberately harsh: 120 words reused across 200k names, so one word can match 22k files. Real data is much sparser. On this laptop's home folder, searches take ~2 ms.

### 5.2 Real home folder (this laptop)

| Measurement | Value |
|---|---|
| Default roots | `~` (Desktop, Documents and Downloads are inside it; no extra disks mounted) |
| First scan | **1.1 s**, 9,470 entries, 1,609 folders (before the hidden-folder default: 23.5 s, 129,594 entries) |
| Catch-up re-scan | 0.5 s |
| Recent-file accesses ingested | 567 |
| DB size | 7.5 MB |
| inotify | 1,609 watches needed; limit **46,173** |

About the ranking in the benchmark: with 5,000 random "opened" events, several "*fees*" files are opened yesterday, plus an exact-name `Fees.pdf` edited yesterday. The receipt ranks 2nd, behind `Fees.pdf`. Both are fair answers, and the Phase 2 confidence rule shows the top 3–5 when there's no clear winner.

## 6. Known limitations / not verified
- **Windows jump lists are not read yet**, only Recent Items. Jump lists are OLE compound files and need a separate parser.
- **Windows code only runs on CI runners (admin accounts).**
  - The non-admin fallback (no journal → scan diff) is covered by the fake-journal tests, not on a real non-admin account.
  - Recent Items, priority and roots run, but there's no real user history on CI.
- **Cold-cache scan speed was not measured.** Dropping caches needs root. On a cold cache, more than one walker thread per root may help; it's configurable (`workers=`).
- **First search in a new process is 21–56 ms** (cold connection). The Phase 2 quick bar should open and warm its connection at startup.
- **"kal" can't be told apart from a name** like "Kal Ho Na Ho": it parses as "yesterday". When nothing matches in that range, search falls back to normal keyword results.
- **The search benchmark had one 633 ms outlier in 1,200 queries** in one run (the next run's max was 72 ms); it looks like system noise.
- **Benchmark data stays on disk:** `~/.cache/zachary-bench` (~250k empty files + DBs). `rm -rf ~/.cache/zachary-bench` removes it.

## 7. Open questions for review
1. Keep **hidden folders excluded by default**? It's the biggest quality change. The alternative is indexing them and adding more app-specific excludes.
2. Is **Windows USN catch-up only when running with volume access (admin)** acceptable? Otherwise catch-up is the scan diff, ~7 s per 200k files.
3. Merge `phase-1` into `main` after review?
