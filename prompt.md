# prompt.md — Build Zachary

> Paste this into Claude Code (or any coding agent) at the root of an empty repo that contains `CLAUDE.md`.

---

You are building **Zachary ("Zach")**, a fast, cross-platform (Windows + Linux) desktop assistant with file memory, voice via the Gnani API, and a hotkey search bar.

**Read `CLAUDE.md` first and follow it exactly.** It defines the architecture, data model, safety rules, performance budgets, and conventions. If something here conflicts with `CLAUDE.md`, `CLAUDE.md` wins.

## How to work

- Build **one phase at a time**, in order. At the end of each phase:
  1. Run `pytest`, `ruff`, and `mypy` and fix everything.
  2. Report what was built, timings against the budgets in `CLAUDE.md`, and any open questions.
  3. **Stop and wait for my review** before starting the next phase.
- Measure performance with real numbers (create a synthetic tree of ~200k files for benchmarks). Don't claim speed you didn't measure.
- Before writing Gnani code, check https://docs.gnani.ai for the current endpoints and request formats.
- Ask me only when a choice is genuinely mine (e.g. which folders to index by default). Otherwise pick the sensible default and note it.
- Never weaken the safety rules to make a feature easier.

---

## Phase 0 — Scaffold

- `pyproject.toml` with dev extras, the package layout from `CLAUDE.md`, ruff/mypy/pytest config.
- `core/db.py` with migration #1 (full schema from `CLAUDE.md`), WAL mode, sqlite-vec loaded.
- `core/config.py`: index roots, default exclude rules, per-folder rules, settings stored in the DB.
- `core/platform/` stubs for Windows and Linux.

**Done when:** the DB is created with all tables, and tests run green on Linux (and Windows CI if available).

## Phase 1 — Metadata memory (fast core, no AI yet)

- `scanner.py`: parallel first scan of the configured roots with `os.scandir`. Store metadata only. Apply excludes.
- `watcher.py`: watchdog events → debounced, batched queue → DB updates. Handle create, modify, rename/move (keep identity), and delete.
- `catchup.py`: Windows USN journal reconciliation; Linux mtime diff on startup.
- `projects.py`: marker-based project detection, sorted by latest activity.
- Recent-file ingestion: Windows Recent Items/jump lists, Linux `recently-used.xbel` → `access_log`.
- `timeparse.py`: "yesterday", "last week", "last Tuesday", "kal", etc.
- `search/hybrid.py` (keyword + recency only for now) and `cli.py` (`search`, `projects`, `recent`).
- Linux inotify watch-limit check with a clear message.
- Privacy controls: exclude folders, pause indexing, forget a file.

**Done when:** `zachary.cli search "fees yesterday"` finds a fee document opened yesterday; `zachary.cli projects` lists projects in order; a new file appears in search within ~2 s of creation; search meets the < 50 ms p95 budget on the synthetic tree.

## Phase 2 — Quick bar and results UI

- PySide6 quick bar on a global hotkey (Alt+Space, configurable).
- Results window: clickable list. Clicking a file opens it; "show in folder" reveals it in Explorer or the Linux file manager. The projects view opens the project folder on click.
- `brain/intents.py` rule/fuzzy parser for: open, find, show_projects, recent.
- `brain/context.py`: follow-ups ("open the second one", "the older one", "show its folder").
- Confidence rule: one clear match → act; otherwise show the top 3–5.
- Tray icon; the indexer runs as a separate background process.

**Done when:** the quick bar opens and shows results in < 150 ms, follow-ups work, and the UI never blocks.

## Phase 3 — Semantic memory (encoding)

- `extract.py` for PDF, DOCX, PPTX, XLSX, code, and text. Media and binaries are name-only.
- `chunker.py` plus `embedder.py` (Model2Vec default, int8 vectors in sqlite-vec). Embed less: name + path + title + first pages by default.
- Hash-based skipping. Background, low-priority, idle-aware embedding queue that does recent files and documents first.
- `relations.py`: precompute the top-10 related files per file (content + name + location + co-access).
- Hybrid search now includes vector similarity, fused with RRF.
- `versions.py`: group `report_final_v2...` style versions and show the latest.
- `search/explain.py`: a "why it matched" line per result.
- Intents: `related`, `find` by meaning.

**Done when:** "show me everything on data structures" returns old DSA notes and recent linked-list code together; "related to this file" is a < 10 ms lookup; re-saving an unchanged file doesn't re-embed; first-run embedding throughput is reported.

## Phase 4 — Voice

- `voice/audio.py` capture/playback; push-to-talk hotkey first.
- `gnani_stt.py` (REST, then streaming) and `gnani_tts.py` (REST, then WebSocket for lowest latency), behind `STT`/`TTS` interfaces. Key from `GNANI_API_KEY`.
- `wakeword.py`: local "Hey Zach" with openWakeWord. Audio is sent only after the wake word.
- `offline_stt.py`: whisper.cpp fallback when the network is down.
- Short spoken replies; details on screen.
- Hinglish/code-switched commands work through the same parser.
- `telemetry/latency.py`: per-stage timings (wake → STT → intent → search → TTS).

**Done when:** "Hey Zach, open the fees document I opened yesterday" works end-to-end by voice, the latency breakdown is logged, the offline fallback works, and Gnani is mocked in tests.

## Phase 5 — Topics, timeline, and learning

- `clustering.py`: nightly HDBSCAN over file vectors, auto-labeled topics, stored as **virtual** collections.
- Topic view in the UI ("Data Structures & Algorithms" spanning many folders and drives).
- Sessions + "resume where I left off" (reopen a session's files and folders).
- Timeline view ("what was I working on last Tuesday?").
- Feedback learning: log chosen results and boost them for similar queries.

**Done when:** topics look sensible on a real home folder, resume reopens the right set, and ranking measurably adapts to repeated choices.

## Phase 6 — Ask your files

- Optional LLM in `brain/llm.py` (opt-in in settings): intent fallback for low-confidence queries and Q&A.
- Q&A: retrieve the top chunks locally and send **only those chunks**. Answer briefly by voice and show the source passage plus the file link.
- No LLM configured → show the best-matching passage instead.

**Done when:** "when is my fee due?" answers from the fee document and cites it; with the LLM off, it still shows the right passage.

## Phase 7 — Cleaning and organizing (permission-based)

- `planner.py`: detect duplicates (size → hash), empty folders, old installers in Downloads, temp/cache junk, large untouched files.
- "Organize my DSA stuff" → a proposed move plan based on topics.
- Plan review UI: approve, edit, or reject per item. `executor.py` runs only approved items, uses `send2trash` for removal, and writes `undo_log`. One-click undo.
- Respect `folder_rules`.

**Done when:** no file is ever changed without approval, undo fully restores every operation (tested), and duplicates are detected correctly.

## Phase 8 — Polish and ship

- Autostart for the indexer on Windows and Linux; tray controls (pause, settings, exclude folders).
- Optional OCR (Tesseract) for scanned PDFs and images.
- PyInstaller builds for Windows and Linux; first-run setup (choose folders, Gnani key, hotkey, language).
- A README with setup, privacy notes, and troubleshooting (inotify limit, Gnani key, microphone).

**Done when:** a fresh Windows machine and a fresh Linux machine can install, index, and answer voice and quick-bar queries within the performance budgets.

---

Start with **Phase 0** now.