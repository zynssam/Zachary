# Running and testing Zachary

How to set up, run every part (indexer, CLI, quick bar), run all automated
checks and benchmarks, and test everything by hand. Covers Phases 0–4.

All commands run from the repo root: `~/Documents/PROJECTS/Zachary`.
Windows equivalents are shown where they differ.

---

## 1. One-time setup

```bash
python3.12 -m venv .venv                      # Python 3.11+ works; 3.12 recommended
.venv/bin/pip install -e ".[dev]"
```
Windows (PowerShell): `py -3.12 -m venv .venv` then `.venv\Scripts\pip install -e ".[dev]"`.

Optional: activate the venv so you can type `python` instead of `.venv/bin/python`:
```bash
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
```
The rest of this file assumes the venv is active.

**Linux system packages** (most desktops already have them):
- `xdg-open` (opening files), a file manager that supports `org.freedesktop.FileManager1` (Nautilus, Dolphin, Nemo) for "show in folder";
- for the X11 hotkey tests only: `xvfb` and `xdotool` (`sudo dnf install xorg-x11-server-Xvfb xdotool` / `sudo apt install xvfb xdotool`).

---

## 2. Where data lives

| What | Default location | Override |
|---|---|---|
| Database | Linux `~/.local/share/zachary/zachary.db`, Windows `%LOCALAPPDATA%\Zachary\zachary.db` | `--db PATH` on every command, or env `ZACHARY_DB=PATH` |
| Desktop entry (Wayland hotkey) | `~/.local/share/applications/ai.zachary.Zachary.desktop` | written on first start of the assistant |
| Gnani key | `.env` in the repo root: `GNANI_API_KEY=...` (copy `.env.example`; git-ignored) | env `GNANI_API_KEY` |
| Gnani usage ledger / voice latency log | `<data dir>/gnani_usage.jsonl`, `<data dir>/latency.jsonl` | |
| Whisper model (offline STT + wake word) | `<data dir>/models/whisper` | |
| Benchmark tree + DBs | `~/.cache/zachary-bench/` | created by the bench scripts |

**Safe sandbox:** to try things without touching your real index, point everything at a test DB:
```bash
export ZACHARY_DB=/tmp/zach-test.db          # Windows: $env:ZACHARY_DB="$env:TEMP\zach-test.db"
```
Unset it (`unset ZACHARY_DB`) to go back to the real one.

---

## 3. Run the parts

### 3.1 Indexer (background daemon)
```bash
python -m zachary.indexer -v          # runs until Ctrl+C; catch-up, then live watching
python -m zachary.indexer --once -v   # catch up + ingest recent files, then exit
```
- Only one indexer runs per database (a lock file next to the DB); a second start exits quietly.
- The quick bar starts it automatically if it isn't running.
- Stop a background one: `pkill -f zachary.indexer` (Windows: Task Manager → python → zachary.indexer).

### 3.2 CLI
```bash
python -m zachary.cli scan                    # index now in this process (first run: full scan)
python -m zachary.cli status                  # counts, roots, paused?, indexer running?, watch limit
python -m zachary.cli search fees yesterday   # search (time words, Hinglish, typos all OK)
python -m zachary.cli search --json fee receipt
python -m zachary.cli projects                # projects, most recently active first
python -m zachary.cli recent                  # recently opened / edited
python -m zachary.cli recent last week
python -m zachary.cli roots                   # list index roots (defaults: home + Desktop/Documents/Downloads + disks)
python -m zachary.cli roots add ~/Work        # add / remove / reset
python -m zachary.cli exclude                 # list exclude rules
python -m zachary.cli exclude add ~/Private   # exclude a folder (or a glob like "*.log")
python -m zachary.cli exclude remove build    # disable a default rule
python -m zachary.cli forget ~/Private/diary.txt   # hide now; indexer purges all derived data
python -m zachary.cli pause                   # pause indexing (resume with: resume)
python -m zachary.cli embed                   # index file *contents* now (downloads the model once)
python -m zachary.cli search notes on data structures   # finds by meaning once embedded
python -m zachary.cli related ~/Documents/notes.pdf     # files related to a file
python -m zachary.cli set                     # show settings
python -m zachary.cli set index_hidden_folders true   # also index hidden folders
python -m zachary.cli set quickbar_hotkey ctrl+alt+space   # default is super+space
```

### 3.3 Quick bar (assistant)
```bash
python -m zachary.assistant -v                # tray (if available) + global hotkey + auto-starts the indexer
python -m zachary.assistant --show -v         # also open the bar immediately
python -m zachary.assistant --hotkey ctrl+alt+space -v
python -m zachary.assistant --toggle          # toggle a running bar (bind this to a key if needed)
python -m zachary.assistant --no-hotkey --no-indexer --show   # just the UI
```
Quit: tray → Quit, or `pkill -f zachary.assistant`.

**Hotkey per desktop**

| Desktop | What happens |
|---|---|
| GNOME / KDE on Wayland | First start shows a system dialog asking to allow the shortcut: approve it (you can change the key there). Remembered afterwards. |
| X11 | Grabbed directly; if another app owns the key you'll see "already taken". |
| Windows | Registered directly. |
| Anything else / if it fails | Desktop Settings → Keyboard → Custom shortcut → command `<repo>/.venv/bin/python -m zachary.assistant --toggle`. |

The default hotkey is **Super+Space**. On GNOME, Super+Space is also the "switch input source"
shortcut. With a single keyboard layout it does nothing, but GNOME may still take the key, so free it
(keeps the dedicated keyboard-switch key):
```bash
gsettings set org.gnome.desktop.wm.keybindings switch-input-source "['XF86Keyboard']"
gsettings set org.gnome.desktop.wm.keybindings switch-input-source-backward "['<Shift>XF86Keyboard']"
```
Undo: `gsettings reset org.gnome.desktop.wm.keybindings switch-input-source` (and `...-backward`).
If you add a second keyboard layout later, pick another key for one of them.

On Windows, Win+Space is reserved by the OS; use e.g. `--hotkey ctrl+alt+space` there.

**Quick bar keys**

| Key | Action |
|---|---|
| type | live results |
| Enter | do what you typed ("open fee receipt", "open the second one"); after ↑/↓, opens the highlighted row |
| ↑ / ↓ | move the highlight |
| Ctrl+Enter | show the highlighted item in its folder |
| Ctrl+R | files related to the highlighted item |
| Ctrl+1 … Ctrl+9 | open result N |
| click | open (projects open their folder); right-click: Open / Show in folder / Related files / Copy path |
| Esc | hide |

### 3.4 Voice (Phase 4)
Voice uses **paid Gnani credits**: every real call is logged; nothing is retried automatically.
```bash
sudo dnf install portaudio espeak-ng           # apt: libportaudio2 espeak-ng (espeak only for tests)
python -m zachary.cli voice setup              # key found? downloads whisper tiny (75 MB) once; shows the mic
python -m zachary.cli set voice_enabled true    # voice is off by default
python -m zachary.assistant -v                  # restart the assistant to pick it up
```
| Key / phrase | Action |
|---|---|
| **Super+Alt+Space** (global) or **Ctrl+M** (in the bar) | push-to-talk (default): the mic opens, you speak, Gnani detects the end; press again to stop early |
| "Hey Zach, open the fee receipt" | only with `cli set wake_word true` (runs whisper locally, ~300 MB; mic always open) |

Other voice commands (the ones that use credits say so):
```bash
python -m zachary.cli voice usage                       # Gnani calls so far (free)
python -m zachary.cli voice latency                     # last 10 commands, ms per stage (free)
python -m zachary.cli set stt_streaming false           # REST instead of streaming (if accuracy is poor)
python -m zachary.cli voice mics                        # microphones with a live level (free)
python -m zachary.cli voice mics bluez_input.XX         # use that mic ("" = system default)
python -m zachary.cli voice listen --offline            # press Enter, speak; local whisper only (free)
python -m zachary.cli voice listen                      # same with Gnani (1 STT + 1 TTS call)
python -m zachary.cli voice transcribe clip.wav --offline   # free
python -m zachary.cli voice say "Opening your file."    # 1 TTS call
python -m zachary.cli set speak_replies false           # text replies only (saves TTS credits)
python -m zachary.cli set wake_word false               # push-to-talk only
python -m zachary.cli set stt_language hi-IN            # default en-IN (handles Hinglish in Latin script)
```
Real-API smoke test (deliberate, costs credits): `python bench/gnani_smoke.py --yes` (3 calls).

---

## 4. Automated checks

Run all of these before committing (they are what CI runs):
```bash
ruff check .
ruff format --check .
mypy zachary tests
mypy --platform win32 zachary tests        # type-check the Windows code from Linux
pytest                                     # ~330 tests, ~20 s
```
Expected: no lint/type issues; on Linux about `320 passed, 7 skipped` (skips are Windows-only tests
and the real-model test).

Tests never call the real Gnani API: it's mocked, and a guard in `tests/conftest.py` blocks the real host.
Real local wake-word/whisper tests (free; downloads whisper tiny once; needs espeak-ng):
```bash
ZACHARY_TEST_WHISPER=1 pytest tests/test_voice.py
```

Real-model test (downloads the 30 MB model once):
```bash
ZACHARY_TEST_MODEL=1 pytest tests/test_semantic_search.py
```

Useful variants:
```bash
pytest -q tests/test_intents.py            # one file
pytest -k "followup or older"              # by name
pytest -x -vv                              # stop at first failure, verbose
xvfb-run -a pytest tests/test_hotkeys.py   # also run the X11 hotkey tests (needs xvfb + xdotool)
QT_QPA_PLATFORM=wayland pytest tests/test_ui.py   # UI tests on the real display (windows will flash)
```
- Qt tests run headless (`QT_QPA_PLATFORM=offscreen`) unless you override it.
- Some tests only run on their OS: Windows (USN journal, Recent Items, RegisterHotKey, hidden attribute) and X11 (XGrabKey). CI covers both.

| Test file | Area |
|---|---|
| `test_db`, `test_config`, `test_platform`, `test_roots` | Phase 0: schema, settings, excludes, default roots |
| `test_scanner`, `test_watcher`, `test_catchup`, `test_projects`, `test_service` | Phase 1 indexer (real file-system events) |
| `test_tokens`, `test_timeparse`, `test_search`, `test_cli` | Phase 1 search + time phrases + CLI |
| `test_platform_parsers`, `test_os_live` | OS data parsers (xbel, .lnk, USN) and live OS calls |
| `test_intents`, `test_context`, `test_handler` | Phase 2 brain: intents, follow-ups, confidence rule, safe actions |
| `test_ui`, `test_hotkeys`, `test_assistant` | Phase 2 Qt UI, global hotkeys, single instance |
| `test_extract`, `test_semantic`, `test_vectors`, `test_versions`, `test_relations`, `test_semantic_search`, `test_inotify_tree` | Phase 3: extraction, embeddings, vector index, versions, related files, meaning search, exclude-aware watcher |

**CI:** every push runs the checks on Ubuntu + Windows, Python 3.11 + 3.12:
```bash
gh run list --limit 5
gh run view <id> --log-failed
```

---

## 5. Benchmarks (performance budgets)

```bash
# 1. build the synthetic tree once (200k files + 47k excluded, ~35 s, on real disk)
python bench/make_tree.py ~/.cache/zachary-bench/home --files 200000
# 2. Phase 0: DB create/open, exclude-matcher speed
python bench/bench_phase0.py
# 3. Phase 1: scan, catch-up, search p95, new-file latency (~3 min; writes ~/.cache/zachary-bench/bench.db)
python bench/bench_phase1.py ~/.cache/zachary-bench/home
# 4. Phase 2: quick bar open/typing latency, UI stalls (needs bench.db from step 3)
python bench/bench_phase2.py --db ~/.cache/zachary-bench/bench.db
QT_QPA_PLATFORM=wayland python bench/bench_phase2.py --db ~/.cache/zachary-bench/bench.db   # real display
```

| Budget (CLAUDE.md) | Where to read it | Last measured |
|---|---|---|
| Search < 50 ms p95 on 200k files | `bench_phase1` → `[search]` | p95 29.7–33.7 ms |
| New file searchable in ~2 s | `bench_phase1` → `[watch]` | p95 1.62 s |
| First metadata index: minutes | `bench_phase1` → `[scan]` | 27.2 s / 200k files |
| Quick bar open → results < 150 ms | `bench_phase2` → `[open]` | p95 23.3 ms (Wayland) |
| UI never blocks | `bench_phase2` → `[ui]` | longest stall 26–65 ms |
| Search with meaning < 50 ms p95 | `bench_phase3` → B `[search]` | p95 28–36 ms (assistant, in-memory index) |
| "Related files" < 10 ms | `bench_phase3` → `[related]` | p95 0.5 ms |

```bash
# 5. Phase 3: content corpus + semantic memory on 200k (~15 min)
python bench/make_corpus.py ~/.cache/zachary-bench/corpus --files 2000
python bench/bench_phase3.py --corpus ~/.cache/zachary-bench/corpus --big-db ~/.cache/zachary-bench/bench.db
```

Clean up afterwards: `rm -rf ~/.cache/zachary-bench`.

---

## 6. Manual test checklist

Do these in the sandbox (`export ZACHARY_DB=/tmp/zach-test.db`) or on your real index.

### Phase 1: indexer, search, CLI
| # | Do | Expect |
|---|---|---|
| 1 | `python -m zachary.cli scan` | "scanned … in N s", entry counts; no watch-limit warning (or exact `sysctl` advice) |
| 2 | `python -m zachary.cli projects` | your projects, newest activity first; no app/extension folders |
| 3 | Open any PDF in your normal viewer, then `python -m zachary.cli scan` and `python -m zachary.cli search <word from its name> today` | it's listed with "opened today" |
| 4 | Start `python -m zachary.indexer -v`, create `~/Documents/zz probe.txt`, run `python -m zachary.cli search zz probe` within ~2 s | found |
| 5 | Rename it to `zz renamed.txt` | search finds the new name; the old one is gone |
| 6 | Delete it | no longer found |
| 7 | `mkdir ~/.zzhidden && touch ~/.zzhidden/x.txt` then `search x` | **not** found (hidden folder); `mv ~/.zzhidden ~/zzvisible` → found |
| 8 | `python -m zachary.cli forget ~/zzvisible` | hidden from search immediately; `status` still fine |
| 9 | `python -m zachary.cli pause`, create a file, wait, `resume` | file appears only after resume |
| 10 | `python -m zachary.cli search "kal wali fees"` / `"recipt"` / `"pichhle hafte report"` | Hinglish time words and typo correction work |

### Phase 2: quick bar
| # | Do | Expect |
|---|---|---|
| 1 | `python -m zachary.assistant -v` | first time on Wayland: GNOME dialog to allow the shortcut → approve |
| 2 | press the hotkey | bar appears instantly with recent files; you can type immediately (if not, click it once: note this) |
| 3 | type `fees yesterday` | results update as you type, with "why matched" lines |
| 4 | type `my projects` | project list; click one → its folder opens |
| 5 | type `open report` + Enter (with several report files) | "Which one? I found N matches." nothing opens |
| 6 | type `open the second one` + Enter | the 2nd result opens; bar hides |
| 7 | hotkey, `show its folder` + Enter | file manager opens with that file selected |
| 8 | hotkey, `the older one` + Enter | the next-older match opens |
| 9 | `open <exact unique file name>` + Enter | opens directly ("Opening …") |
| 10 | ↑/↓ then Enter; Ctrl+2; Ctrl+Enter; Esc | open highlighted; open 2nd; show in folder; hide |
| 11 | run `python -m zachary.assistant --toggle` in a terminal | the running bar toggles; no second app starts |
| 12 | Hinglish/typos in the bar: `mere projects dikhao`, `doosra wala kholo`, `opne fees` | same as English |

### Phase 3: semantic memory
| # | Do | Expect |
|---|---|---|
| 1 | `python -m zachary.cli embed` (or let the indexer do it in the background) | "embedded N files … with text", vector index, related links |
| 2 | `python -m zachary.cli status` | `semantic: N/M files embedded, … passages` |
| 3 | `python -m zachary.cli search "notes about <a topic you study>"` | files about that topic even when the name doesn't say so; `about: "…passage…"` lines |
| 4 | `python -m zachary.cli search <a word only inside a document>` | that document (content keywords) |
| 5 | `python -m zachary.cli related <path to a notes file>` | similar notes / same folder / opened together, in ~1 ms |
| 6 | Save a document again without changing it, wait, `status` | it is not re-embedded (no new passages) |
| 7 | In the bar: search, highlight a result, **Ctrl+R** | related files list; then "open the first one" |
| 8 | Have `report.docx`, `report_final.docx`, `report_final_v2.docx`; search `report` | one entry "latest of 3 versions"; "the older one" opens the previous version |

### Phase 4: voice
| # | Do | Expect |
|---|---|---|
| 1 | `cli voice setup` | "Gnani key: found", "local speech model ready", your mic name |
| 2 | `cli voice listen --offline`, say "open fee receipt" | the text (whisper), then the file opens, no credits used |
| 3 | `cli voice listen`, say "open fee receipt" | Gnani text, the file opens, the reply is spoken; `cli voice usage` shows +1 STT, +1 TTS |
| 4 | With voice on: say "Hey Zach, open the fee receipt" | the bar shows the transcript and opens the file; spoken "Opening …" |
| 5 | Say a sentence without "Hey Zach" | nothing happens and nothing is sent (`cli voice usage` unchanged) |
| 6 | Super+Alt+Space, say "show my projects" | the projects list (push-to-talk, no wake word) |
| 7 | Say "Hey Zach, kal wali fees file kholo" | Hinglish works like typing it |
| 8 | Say "Hey Zach, open the <something vague> I opened yesterday" | if nothing matches by name, content or close meaning: "Which one?" with a list, **never** a guess |
| 9 | Turn Wi-Fi off, say a command | it still works via local whisper (fallback) |
| 10 | `tail -1 ~/.local/share/zachary/latency.jsonl` | per-stage timings: wake, speech_end, stt, intent_search, tts_first_audio, tts_done |

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| "inotify limit" warning | `sudo sysctl fs.inotify.max_user_watches=524288` and persist with `echo fs.inotify.max_user_watches=524288 \| sudo tee /etc/sysctl.d/90-zachary.conf` |
| Hotkey does nothing (GNOME) | GNOME's input-source binding still owns Super+Space (free it, see 3.3), or the dialog was dismissed: check Settings → Keyboard → Shortcuts → Apps, or use `--hotkey ctrl+alt+space`, or bind `--toggle` manually |
| "Hotkey unavailable" message in the bar | follow the hint shown (bind `python -m zachary.assistant --toggle`) |
| Bar opens without keyboard focus (Wayland) | click it once; report it (depends on GNOME passing an activation token) |
| No tray icon on GNOME | needs the AppIndicator extension; use the hotkey and `cli pause/resume` instead |
| Search shows nothing | `python -m zachary.cli status`: is anything indexed? run `cli scan`; check `cli roots` and `cli exclude` |
| A folder is missing | hidden folders and app data are skipped by default (`cli set index_hidden_folders true` to include) |
| Indexer won't start ("already running") | another indexer holds the lock: `pkill -f zachary.indexer` |
| Meaning search not working | `cli status` shows "semantic: 0/…" or an error: the model downloads on first `cli embed` / indexer run (needs internet once); check `semantic.error` in status |
| Other apps complain about file watching (Linux) | an old indexer (before Phase 3) watched excluded folders too: `pkill -f zachary.indexer` and restart the assistant |
| Start completely fresh | stop everything, then delete the DB (`rm ~/.local/share/zachary/zachary.db*`) and run `cli scan` |
| Voice: "microphone unavailable" | install PortAudio (`sudo dnf install portaudio`), check the input device in system sound settings, run `cli voice setup` |
| Voice: "Gnani credits are used up" / "rejected the API key" | replies still work through local whisper; check the key in `.env` and your Gnani dashboard |
| Voice: "Hey Zach" not recognised | speak a little slower; or use push-to-talk (Super+Alt+Space); `cli voice listen --offline` shows what whisper hears |
| Watcher/inotify tests fail locally while the indexer runs | the indexer uses most inotify watches; raise the limit (first row) |
