# Phase 7: Cleaning and organizing (report)

Status: **built and tested; waiting for review.** Branch `phase-7`.

## 1. "Done when"

| Check | Result |
|---|---|
| No file is ever changed without approval | ✅ Making a plan only writes an `action_plans` row; tests snapshot the whole tree before and after. The executor refuses any plan that isn't `approved`, including `proposed` and `rejected` ones, and runs only the ticked items. Saying "yes" never approves a plan; approval comes only from the review window's Approve button or `cli plan approve` (which asks y/N). At run time each item is checked again: it still exists, is unchanged since the plan (size + mtime), is still not protected, and a move never overwrites. |
| Undo fully restores every operation (tested) | ✅ After execute and undo, the whole tree is byte-for-byte identical in tests, for trash (files and folders), moves and created folders. The real OS trash is tested too: freedesktop trash on Linux (home trash and a separate drive, `/tmp/.Trash-1000`), and the Recycle Bin (`$I`/`$R`) on Windows CI. If something can't be undone (the trash was emptied, or a new file sits at the old place), undo says so, never overwrites, and restores the rest. |
| Duplicates detected correctly | ✅ Size, then a quick hash (first and last 64 KB), then a full blake2b hash. Tested: a same-size file whose first and last 64 KB are identical but whose middle differs is not a duplicate. Symlinks and hard links are not duplicates. Code and binaries are ignored. |

## 2. What was built

| Module | What it does |
|---|---|
| `actions/planner.py` | Builds plans and never executes them. **Clean** looks for duplicates (keeping the copy outside Downloads, not named "(1)" or "copy", used most, and oldest), old installers in Downloads (> 30 days), temp and junk files (`*.tmp`, `~$*`, `.~lock.*#`, `*.crdownload`, … older than 1 day, found by listing indexed folders because the index excludes them), empty folders (checked on disk), and large untouched files (≥ 500 MB, 180 days, **not preselected**). **Organize** moves a topic's (or a search's) documents into the folder most of them already share, else `Documents/<topic>`, with " (2)" added on name clashes. |
| `actions/executor.py` | The only code that moves or removes files, and only for an approved plan. Removal always goes through the platform `trash()` (send2trash). Each operation is written to `undo_log` immediately (autocommit), so undo still works if Zachary is killed halfway through. |
| `actions/undo.py` | Replays the `undo_log` backwards: move files back, restore from trash, remove the folders that were created. |
| `core/platform/trash.py` | Moves to the OS trash, then finds the item again so undo can restore it: Linux (freedesktop spec, home and per-drive trash) and Windows (Recycle Bin `$I` v1/v2 parsing). Added `trash()`/`restore()` to the Platform interface. |
| Intents | `clean` ("clean up my downloads", "find duplicates", "free up space", "remove old installers", "delete empty folders", "downloads saaf karo", typos), `organize` ("organize my DSA stuff", "organise…", "sort out my fees files"), `undo` ("undo", "undo the last cleanup"). Non-destructive phrases stay searches: "open the cleanup report", "delete my resume", "restore my files". |
| UI | `ui/plan_review.py` review window: grouped, tickable items with size and reason, an editable "Move to" column, Approve ("Move 12 to the trash (340 MB)") / Reject / Close, then the result and a one-click **Undo**. It opens from the bar on a plan reply. Execution runs in the engine thread, not the UI thread. Styles are in `ui/style.py`. |
| CLI | `clean [what]`, `organize TEXT`, `plan [show|approve|reject] ID [--only/--skip]`, `undo [ID]`, `rules [add|remove PATH RULE]`. |

## 3. Decisions (please review)

1. **Never proposed:**
   - anything under a `never_touch` rule, inside a code project, or hidden;
   - **installed software**: any folder with ≥ 10 libraries or binaries. This was found on your home folder, where the first version proposed 575 bundled `.jar` "duplicates" and 66 empty `conf/`, `data/` and `logs/` folders inside `~/Documents/hackathon/infaagent` (an installed program);
   - the index roots and Documents / Downloads / … themselves.

   This over-protects on purpose: all of `~/Documents/hackathon` and `~/Documents/Projects` are skipped.
2. **Duplicates are only your content** (documents, images, video, audio, archives), ≥ 16 KB.
3. **Thresholds:** installers 30 days old, temp files 1 day, large files 500 MB and 180 days untouched. These are constants in `planner.py`; they could become settings if you want.
4. **Large files are shown but never preselected.**
5. **Undo needs a "yes"** (it changes files too). The review window's Undo button is one click.
6. **The hash is blake2b**, which the project already used, not xxhash: no new dependency, and it's fast enough (see timings). The new dependency is `send2trash`.
7. `auto_tidy` and `name_only` rules can be set (`cli rules`), but nothing uses them yet; currently only `never_touch` affects plans.

## 4. Timings

| What | Measured |
|---|---|
| Clean-up plan on **your home folder** | **1.1 s**, 63 MB: 19 duplicates (18.8 MB), 3 old installers (514 MB), 20 lock files, 8 empty folders |
| Clean-up plan, **200k-file tree** + 500 duplicate pairs (100 KB each) | **3.8 s**, 250 MB peak. Duplicates 2.0–2.8 s (reading 100 MB to hash it), junk listing 1.2 s (12k folders), guard 0.3–0.5 s, empty folders 0.04 s |
| Organize 500 files | 0.34–0.65 s |
| Fixed along the way | Junk scan: 11 `fnmatch` calls per file → one compiled regex (whole plan 7.5–9.6 s → 3.8 s). The protection check was a scan over 854 project folders per path; it's now a set lookup per folder level. |

These run only when you ask (a clean/organize command), not on the search hot path. The bar shows "Thinking…" while a plan is being built.

## 5. Testing

- `tests/test_clean.py` (19 tests):
  - detection and keeper choice;
  - categories, scope, and large files not preselected;
  - nothing changes without approval;
  - only selected items run;
  - an item changed or protected at run time is skipped;
  - editing only before approval;
  - execute + undo restores the whole tree, and organize moves + undo do the same;
  - organize picks the shared folder and never overwrites;
  - undo reports what it can't restore;
  - the real OS trash and restore;
  - installed software, symlinks/hard links, and code/binaries are never duplicates;
  - through the handler, including that "yes" never approves;
  - **rules match regardless of case**. Windows CI caught this: rules are stored lower-cased there, so `never_touch` didn't match, and the original in Documents was proposed for the trash instead of the protected copy. Every path comparison in the guard is now case-insensitive on Windows.
- `test_ui.py`: the review window (ticks, button text, approve/reject/undo signals), plus **an untick that survives the round trip through the engine**. This test caught a real bug: a Qt `dict` signal silently dropped the selection (int keys), which would have run the plan's original ticks instead of yours. The signal now passes plain objects. It also caught Approve and Reject staying disabled for a second plan.
- `test_intents.py`: 15 new cases.
- ruff, ruff format, mypy strict (+ win32) and the full suite pass locally and on CI (Linux + Windows, Python 3.11 + 3.12), including the real Recycle Bin on Windows.

## 6. Open questions

1. Is protecting whole folders that contain installed software (e.g. all of `~/Documents/hackathon`) right, or should only the software subfolder be protected? Doing that reliably is harder.
2. Should `~/Downloads` get `auto_tidy` by default? What should it mean: preselect everything there, or let Zachary propose a weekly clean-up?
3. Are the thresholds right: installers 30 days, large files 500 MB and 180 days?
