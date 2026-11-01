# Phase 2: Quick bar and results UI (report)

Status: **complete, waiting for review.** Branch `phase-2` (not merged). CI green on Linux and Windows,
Python 3.11 + 3.12.

## 1. Goal and "done when"

| Done-when check | Result |
|---|---|
| Quick bar opens and shows results in < 150 ms | ✅ hotkey → results painted: **p95 23.3 ms on the real Wayland display**, p95 7.6–10.3 ms offscreen (200k-file index) |
| Follow-ups work | ✅ "open the second one", "show its folder", "the older one/newer one", "doosra wala", "iska folder kholo": tested in the handler and end to end through the real Qt widgets |
| The UI never blocks | ✅ All DB and search work runs on a worker thread. Measured with a 5 ms event-loop heartbeat while typing: longest stall 26–40 ms on 200k files, one 65 ms stall in one real-display run (p99 ≤ 14.5 ms). A test injects a 250 ms query and asserts no stall > 60 ms. |
| **Real global hotkey on your GNOME desktop** | ⚠️ **Not verified yet.** It needs you to approve GNOME's shortcut dialog (see 6). |

## 2. What was built

| Module | What it does |
|---|---|
| `brain/intents.py` | Local rule + fuzzy parser: `open`, `find`, `show_projects`, `recent`, and follow-ups `select` (nth), `reveal`, `older`/`newer`. Accepts English, Hinglish and Kannada. Typos are corrected only in command words (English targets, 4-letter words only by transposition or one missing letter, length ±1), so "shop", "report" and "mere" are left alone. `confidence` < 0.7 marks a guess, for the Phase 6 LLM fallback. ~0.2 ms per phrase. |
| `brain/context.py` | Last result list, the focused item, ordinals, older/newer by time, 15-minute expiry. |
| `brain/handler.py` | Qt-free core: text → intent → search/projects/recent → `Reply` (one short line to show/say, the items, the action). **Confidence rule:** `open` acts only if the top score beats the runner-up by ≥ 12%; otherwise it shows the top 5 and asks "Which one?". `preview` (as-you-type) never acts. |
| `actions/safe_actions.py` | Whitelist `open, reveal, search, show, suggest`. Opens or reveals only items whose id **and** path match the index and that still exist. Logs `access_log` (`zach`). No shell, nothing destructive. |
| `ui/engine.py` | Worker on its own `QThread` with its own SQLite connection. Request ids; as-you-type previews are coalesced (stale ones skipped). |
| `ui/quickbar.py`, `ui/results.py` | Frameless always-on-top bar; result list with kind badges, name, age, folder and why-matched. Built once, then shown/hidden; the last list appears at once and refreshes in the background. Click or Enter opens; context menu has Open / Show in folder / Copy path; projects view opens the folder. Keys: ↑/↓, Enter, Ctrl+Enter (show in folder), Ctrl+1–9, Esc. Hides when focus leaves, or 0.7 s after acting. |
| `ui/tray.py` | Tray icon (Open / Pause indexing / Quit) wherever the desktop has a tray. |
| `assistant.py` (`python -m zachary.assistant`) | Single instance via a local socket: a second launch, or `--toggle`, toggles the running bar (acknowledged protocol). Starts the indexer as a separate background process if its heartbeat is stale. The indexer now takes a single-instance lock. |
| `core/hotkeys.py` + `core/platform/` | Hotkey spec parsing (`alt+space`, `ctrl+shift+z`, `win+space`, `f9`), with a backend per OS (below). |
| `telemetry/latency.py` | Local-only latency log (quick-bar opens now; voice stages in Phase 4). |

### Global hotkey backends (all in `core/platform/`)

| Session | How | Verified |
|---|---|---|
| **Wayland (your GNOME 50)** | XDG **GlobalShortcuts portal** over D-Bus (jeepney): `Registry.Register` → `CreateSession` → `BindShortcuts` → `Activated`. It uses the activation token so the bar can take focus. A user `.desktop` file (`~/.local/share/applications/ai.zachary.Zachary.desktop`) is installed on start, because GNOME resolves the app id from it. | `CreateSession` works against your portal (v2). The approval dialog and key press need you. |
| X11 | `XGrabKey` via ctypes on its own thread (with CapsLock/NumLock variants; X errors caught, so "already taken" is reported instead of crashing) | ✅ CI under Xvfb, with real key presses from `xdotool` |
| Windows | `RegisterHotKey` on its own thread with its own message loop | ✅ CI (posted `WM_HOTKEY`) |
| Any desktop | Fallback: bind `python -m zachary.assistant --toggle` in the desktop's keyboard settings. Shown automatically if the hotkey fails. | ✅ test (`test_second_launch_toggles_first`) |

New dependencies: `PySide6-Essentials` (Qt), `jeepney` (Linux only, pure-Python D-Bus), and dev-only `pytest-qt`.

## 3. Design decisions (easy to change)
1. **Enter does what the text says.** "open the second one" runs the follow-up and "open fee receipt" applies the confidence rule. If you moved with ↑/↓, Enter opens the row you picked. A single click opens.
2. **As-you-type previews show what Enter would do** ("Enter to open Fee Receipt Sem 5.pdf") but never act.
3. **The quick bar hides itself** 0.7 s after opening something, and whenever it loses focus.
4. **"the older/newer one"** walks the shown list by time (newest of mtime and last access), starting from the focused item.
5. **Default hotkey is `super+space`** (decided in review, instead of the spec's Alt+Space, which is GNOME's window-menu key). On GNOME, Super+Space is also bound to "switch input source"; with a single keyboard layout that binding does nothing, but it should be freed so GNOME doesn't take the key (see `run-and-test.md` §3.3). On Windows, Win+Space is reserved by the OS input switcher; if registration fails the bar says so and suggests another key.
6. **No tray on stock GNOME** (it needs the AppIndicator extension, which you don't have enabled). The hotkey and the CLI (`pause`/`resume`) cover it.

## 4. Testing

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean |
| `mypy zachary tests` (strict) and `--platform win32` | no issues (64 files) |
| Linux local + CI 3.11/3.12 (CI runs pytest under `xvfb-run`) | **271 passed, 6 skipped** (Windows-only) |
| Windows CI 3.11/3.12 | **272 passed, 5 skipped** (Linux/X11-only) |

New tests (130; 277 total):

| File | # | Covers |
|---|---|---|
| `test_intents.py` | 76 | Every intent in English, Hinglish/Kannada and with typos; follow-up ordinals (`#3`, `2nd`, `doosra`, `eradane`, `last`), reveal, older/newer, "open it", false-command guards ("shop receipts", "report", "sem 5 notes", "mere notes", "the second report"), the full sentence from the spec |
| `test_hotkeys.py` | 19 | Spec parsing/formatting, invalid specs, Windows VK/modifier codes, X11 masks/keysyms, `.desktop` quoting; **live:** Windows `WM_HOTKEY`, X11 key press via `xdotool`, X11 "already taken" |
| `test_handler.py` | 13 | Clear winner opens and logs; the spec sentence opens the fee document; ambiguous → "Which one?" → "open the second one" → "show its folder"; older/newer; preview never acts; projects/recent views; open a project by name; clicks; no context; out-of-range ordinal; nothing found; the clear-winner rule; safe actions refuse non-indexed, mismatched or missing paths and non-whitelisted actions |
| `test_ui.py` | 8 | Real widgets offscreen: open → results < 150 ms (p95 of 20); type + Enter opens; the full follow-up flow; ↑/↓ Enter, Ctrl+3, Ctrl+Enter, Esc; click opens a project folder; **no event-loop stall > 60 ms during a deliberately slow 250 ms query**; preview coalescing; stale replies ignored |
| `test_context.py` | 3 | Ordinals and focus, older/newer walk, expiry |
| `test_assistant.py` | 3 | Acknowledged socket protocol, indexer auto-start only when stale, **second launch toggles the first** (two real processes) |

### Bugs found by testing and fixed
1. **Windows: toggle messages from a second launch could be lost** (the named-pipe client disconnected before the server read). Now the server answers `ok` and the client waits for it.
2. **Ties in keyword ranking were broken by row id**, which depends on the order the OS lists a folder (NTFS is alphabetical). On Windows the *oldest* `report_v*.docx` ranked first. Equal bm25 scores now share a rank, so recency decides.
3. **Over-eager typo correction** found while building the parser: `mere→tere`, `opened→open`, `shop→show`, `report→repo`, and the wake word "zachary" eating the project name in "open the zachary project". Fixed, with regression tests.
4. **Typing latency:** dense as-you-type prefixes (`re*`, `proj*`, matching up to 78k of 200k files) were bm25-sorted in full (86–108 ms). Prefixes estimated (via the FTS vocabulary, ~1 ms) to match > 20k files now take the most recent matches unranked. Keystroke p95 went from 185 ms to 72–93 ms.

## 5. Timings

`bench/bench_phase2.py` runs the real `Engine` + `QuickBar`; nothing is opened. The open timer starts when the hotkey is received and stops at the first paint of the results list.

| Measurement | Offscreen, 200k files | **Wayland display**, 200k files | **Wayland display**, your home (9.5k) |
|---|---|---|---|
| Hotkey → results painted | p50 5.9–6.2 ms, p95 7.6–8.4 ms | p50 14.7 ms, **p95 23.3 ms** | p50 12.4 ms, **p95 23.3 ms** |
| Keystroke → results (incl. 25 ms debounce) | p50 ~48 ms, p95 72–93 ms | p50 44.7 ms, p95 79.3 ms | p50 29.3 ms, p95 45.0 ms |
| Longest event-loop stall while typing | 26–31 ms | 40.4 ms | 64.7 ms (once; p99 14.5 ms) |
| Cold start → first results | 700 ms | 374 ms | 420 ms |
| Handler preview (parse + search + items) | p95 26 ms | p95 26 ms | p95 12.3 ms |
| Intent parse | ~0.2 ms | | ~0.3 ms |

Budget: quick bar open → first results < 150 ms ✅ (worst measured p95: 23.3 ms).

## 6. Known limitations / not verified
- **The GNOME hotkey hasn't been tried end to end.** On first start GNOME shows a dialog to approve the shortcut; that, and pressing the key, need you.
- **Super+Space is GNOME's "switch input source" shortcut** (`switch-input-source = ['<Super>space', 'XF86Keyboard']`). It does nothing with one layout (yours is `us` only), but GNOME may still claim the key; free it with the command in `run-and-test.md` §3.3.
- **Window placement on Wayland:** the compositor decides where the bar appears (usually centred). Exact positioning works only on X11/Windows.
- **Focus on Wayland** relies on the portal's activation token. If GNOME doesn't pass one, the bar can open without keyboard focus (you'd need to click it once). This needs checking on your desktop.
- **The tray icon is not shown on stock GNOME** (no AppIndicator extension).
- **One 65 ms UI stall** in one real-display run (probably the window resizing when the result count changes). Not seen offscreen; worth watching.
- **Windows UI** has only been exercised offscreen on CI, not on a real Windows desktop.
- **`--toggle` startup time** (~0.4 s of Python/Qt imports) only matters for the fallback binding, not for the in-process hotkey.

## 7. Questions for review
1. ~~Hotkey~~: **decided: Super+Space** (default changed, `test_default_hotkey_is_super_space`).
2. Please try it: `.venv/bin/python -m zachary.assistant -v`, approve the shortcut in GNOME's dialog, press it, and try "fees yesterday", "my projects", then "open the second one". Tell me whether the bar gets keyboard focus.
3. Merge `phase-2` into `main` after review?
