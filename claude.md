# CLAUDE.md — Zachary ("Zach")

Zachary is a cross-platform (Windows + Linux) desktop assistant with **file memory**. It indexes the user's files, understands what they are *about*, links related files together, and lets the user find, open, and organize them by voice (Gnani) or a hotkey search bar.

Example requests it must handle:
- "Hey Zach, I opened a document about my fees yesterday, I forgot the name, open it."
- "Hey Zach, show me all my project files across my storage." → clickable, ordered project list; clicking opens the project's folder.
- "Show me everything on data structures." → old DSA notes + recent linked-list code + related PDFs, across drives.
- "When is my fee due?" → answers from the file's contents.

---

## Priorities (in order)

1. **Speed.** Every user query is answered from Zachary's own database, never by scanning the disk. Expensive work (extraction, embedding, clustering) happens once, in the background.
2. **Safety.** Zachary never deletes or moves files without an approved plan, and every change can be undone.
3. **Privacy.** File contents are processed locally. Only voice audio goes to Gnani. Cloud LLM use is opt-in and sends only small relevant chunks.
4. **Usefulness of memory.** File memory is the product. Don't add general-chatbot features (weather, jokes, news).

When a design choice trades off these, follow this order.

---

## Architecture

Two processes sharing one SQLite database (WAL mode):

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ zach-indexer (daemon)       │        │ zach-assistant (on demand)   │
│ - starts with the OS        │        │ - hotkey quick bar (UI)      │
│ - low CPU/IO priority       │ SQLite │ - wake word + voice (Gnani)  │
│ - FS watcher + catch-up     │◄──────►│ - intent parser + LLM fallbk │
│ - text extraction           │  (WAL) │ - hybrid search + ranking    │
│ - embeddings, relations     │        │ - safe actions (open/reveal) │
│ - nightly clustering        │        │ - results window, follow-ups │
└─────────────────────────────┘        └──────────────────────────────┘
```

The assistant only reads the DB (plus writes access logs, feedback, and approved action plans). The indexer owns all writes to file, chunk, and vector tables.

### Module layout

```
zachary/
  core/
    config.py          # settings, paths, exclude lists, per-folder rules
    db.py              # SQLite connection, migrations, schema
    platform/          # OS-specific code ONLY lives here
      windows.py       # USN journal, Recent Items / jump lists, reveal in Explorer
      linux.py         # recently-used.xbel, inotify limit check, xdg-open / file manager reveal
  indexer/
    scanner.py         # first full metadata scan (os.scandir, parallel per drive/root)
    watcher.py         # watchdog events → queue (debounce, batch)
    catchup.py         # startup reconciliation (USN journal on Windows, mtime diff on Linux)
    extract.py         # text extraction per file type
    chunker.py
    embedder.py        # Model2Vec default; optional ONNX MiniLM/bge upgrade
    relations.py       # top-k neighbours, co-access, co-location, name similarity
    projects.py        # project detection via marker files
    versions.py        # version grouping (report_final_v2...)
    clustering.py      # nightly HDBSCAN topics + auto labels
  brain/
    intents.py         # fast rule/fuzzy parser (open, find, show, recent, projects, related, resume, timeline, ask, clean, organize)
    timeparse.py       # "yesterday", "last Tuesday", "kal", ...
    llm.py             # optional fallback + Q&A (opt-in, chunk-only)
    context.py         # conversation state for follow-ups ("the second one")
  search/
    hybrid.py          # FTS5 + vector + recency + feedback → merged ranking
    explain.py         # "why it matched"
  voice/
    wakeword.py        # openWakeWord (local)
    gnani_stt.py       # REST + streaming
    gnani_tts.py       # REST / SSE / WebSocket
    offline_stt.py     # whisper.cpp fallback
    audio.py           # sounddevice capture/playback
  actions/
    safe_actions.py    # whitelist: open, reveal, search, show, suggest
    planner.py         # clean/organize plans (never executes without approval)
    executor.py        # executes approved plans, writes undo log
    undo.py
  ui/
    quickbar.py        # Super+Space search bar (configurable)
    results.py         # clickable list, project view, topic view, timeline
    tray.py
  telemetry/
    latency.py         # per-stage timing (local only)
tests/
```

**Rule:** platform-specific code goes only in `core/platform/`. Everything else is OS-agnostic.

---

## Tech stack

- Python 3.11+
- SQLite + FTS5 (keyword) + `sqlite-vec` (vectors), WAL mode
- `watchdog` for file events; `pywin32` for the USN journal on Windows
- Extraction: PyMuPDF (PDF), python-docx, python-pptx, openpyxl, plain read for code/text; Tesseract OCR for scans (later phase, optional)
- Embeddings: **Model2Vec** (static, very fast) by default; optional higher-quality model via ONNX Runtime (int8) for important files
- Clustering: HDBSCAN
- Voice: openWakeWord, `sounddevice`, Gnani (httpx / websockets), whisper.cpp fallback
- UI: PySide6 (keep everything in Python; no Electron)
- Safe file removal: `send2trash`
- Packaging: PyInstaller

Add a dependency only when it removes real work. Prefer stdlib.

---

## Data model (SQLite)

- `files(id, path UNIQUE, parent_id, name, ext, size, mtime, ctime, content_hash, kind, is_dir, is_excluded, indexed_at, embedded_at)`
- `files_fts` — FTS5 over name, path tokens, title, extracted keywords
- `chunks(id, file_id, ord, text_excerpt, char_start, char_end)`
- `vec_chunks` / `vec_files` — sqlite-vec tables, int8-quantized vectors (file vector = mean of chunk vectors + name/path vector)
- `relations(src_id, dst_id, score, kind)` — kind ∈ content, name, location, coaccess, feedback; store **top-k (10) per file** precomputed
- `projects(folder_id, markers, last_activity)`
- `version_groups(group_id, file_id, is_latest)`
- `topics(id, label, updated_at)` and `topic_members(topic_id, file_id, weight)` — virtual collections only
- `access_log(file_id, ts, source)` — source ∈ zach, os_recent, edit
- `sessions(id, start, end)` and `session_files(session_id, file_id)` — for "resume where I left off"
- `feedback(query_hash, file_id, rank_shown, chosen, ts)`
- `action_plans(id, kind, json, status, created_at, approved_at)` and `undo_log(plan_id, op, src, dst, ts)`
- `settings`, `exclude_rules`, `folder_rules`

Use numbered migrations in `core/db.py`. Never change a schema without a migration.

---

## Indexing rules

- **Two speeds:** a metadata-only pass first (usable within minutes), then content embedding in the background, prioritizing recent files and documents/code.
- **Hash before embedding.** Skip re-embedding if `content_hash` is unchanged. Use a fast hash (xxhash) of size + first/last blocks for change detection and a full hash only for duplicate checks.
- **Debounce:** treat a file as ready only after it has been stable for ~1.5 s. Batch events through a queue (an unzip may create thousands).
- **Renames/moves:** update the path and keep the embeddings (match by hash/inode). Don't re-embed.
- **Deletes:** remove the file, chunks, vectors, and relations.
- **New folders:** record them immediately and run project detection.
- **Catch-up on start:** Windows reads the USN journal from the last saved USN; Linux diffs mtimes against the DB.
- **Default excludes:** OS/system dirs, `node_modules`, `.git` internals, `__pycache__`, `venv`/`.venv`, build/dist/target, caches, browser profiles, temp files (`~$*`, `*.tmp`, `*.swp`, `*.part`, `*.crdownload`). Media and binaries are indexed by **name only**, not contents.
- **Embed less:** filename + path + title + first ~2 pages/first N KB per document by default; full content only for small text/code files or on demand.
- **Linux:** check `fs.inotify.max_user_watches`. If it's too low, tell the user how to raise it. `fanotify` whole-drive mode is optional and needs elevation; never require root.
- Run the indexer at low CPU/IO priority. Do heavy embedding when idle or on AC power.
- **Recent-file sources:** Windows Recent Items/jump lists, Linux `~/.local/share/recently-used.xbel`, plus Zachary's own `access_log`. Don't rely on atime.

## Projects

A folder is a project if it contains any of: `.git`, `package.json`, `pyproject.toml`, `requirements.txt`, `setup.py`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`, `*.sln`, `*.csproj`, `CMakeLists.txt`, `Makefile`. Don't count nested projects inside an already-detected project unless they have their own `.git`. Sort by the latest edit inside the project.

---

## Search and ranking

Hybrid score, merged with reciprocal rank fusion and boosts:
- FTS5 keyword match (exact names and words)
- vector similarity (meaning)
- recency boost (from `access_log` / mtime, matched to time phrases like "yesterday")
- feedback boost (files the user chose for similar queries)
- version grouping: show only the latest version, with the others collapsed

**Confidence rule:** if the top result clearly wins (score gap above a threshold), act directly ("Opening Fee Receipt Sem 5"). Otherwise show the top 3–5 and ask.

Every result carries a short **why-matched** explanation (terms hit, related topic, opened yesterday, etc.).

"Related files" is a **lookup** of the precomputed `relations` top-k, not a new search.

---

## Brain (intent)

- The rule/fuzzy parser handles common intents locally and instantly: `open`, `find`, `show_projects`, `recent`, `related`, `topic`, `resume`, `timeline`, `ask`, `clean`, `organize`, plus follow-ups (`the second one`, `the older one`, `show its folder`).
- Time phrases are parsed locally (English + common Hindi/Kannada words such as "kal").
- The LLM fallback runs only when the parser's confidence is low. It's opt-in, and it receives the query and minimal metadata, never whole files.
- Q&A ("when is my fee due?"): retrieve the top chunks locally and send only those chunks to the LLM. If no LLM is configured, show the matching passage instead.
- `context.py` keeps the last result list and the last focused file so follow-ups work.

---

## Voice (Gnani)

- Auth header: `X-API-Key-ID`. Key from the env var `GNANI_API_KEY` (never hard-code or log it).
- STT REST: `POST https://api.vachana.ai/stt/v3` (multipart: `audio_file`, `language_code`, e.g. `en-IN`, `hi-IN`)
- STT streaming: `wss://api.vachana.ai/stt/v3/stream` (16 kHz PCM)
- TTS REST: `POST https://api.vachana.ai/api/v1/tts/inference`; SSE: `/api/v1/tts/sse`; WebSocket: `wss://api.vachana.ai/api/v1/tts` (lowest latency)
- **Verify these against https://docs.gnani.ai before implementing.** APIs change. Keep all Gnani specifics inside `voice/gnani_*.py` behind an `STT`/`TTS` interface so they can be swapped.
- The wake word ("Hey Zach") runs **locally** (openWakeWord). Audio goes to Gnani only after the wake word or push-to-talk. Never stream the mic continuously.
- Offline fallback: whisper.cpp for STT when the network is down.
- Spoken replies are short (one line). Details go on screen.
- Support code-switched commands (Hinglish etc.). Gnani handles them; the intent parser must tolerate them.

---

## Safety rules (non-negotiable)

- **Never permanently delete.** Removal always goes through `send2trash`.
- **Never move/rename/delete without an approved `action_plan`.** Show the plan, let the user edit or reject it, execute only on approval, and write every op to `undo_log`. One-click undo.
- Topics/collections are **virtual** by default. Nothing on disk changes.
- Respect `folder_rules` (e.g. "Downloads may be auto-tidied", "never touch Projects").
- The voice action whitelist is in `actions/safe_actions.py`. **No arbitrary shell commands from voice or LLM output.**
- Destructive intents ("clean", "organize") always produce a plan, even when confidence is high.
- No admin/root by default. Elevation is only for optional features, and the code says why it's needed.
- Privacy controls: exclude folders, pause indexing, "forget this file" (purges all derived data).

---

## Performance budgets (targets; measure, don't guess)

- Keyword+vector search: < 50 ms p95 on 200k files
- "Related files": < 10 ms (lookup)
- Quick bar open → first results: < 150 ms
- Voice end-of-speech → action: as low as Gnani allows; log every stage
- Indexer idle CPU: ~0%; never noticeably slow the laptop
- First metadata index of a typical laptop: minutes, not hours

`telemetry/latency.py` logs per-stage timings locally (wake → STT → intent → search → TTS). Any PR touching a hot path must include a before/after timing.

---

## Conventions

- Type hints everywhere; `ruff` + `mypy` clean.
- Small, pure functions in `brain/` and `search/` so they're easy to unit test.
- No blocking I/O on the UI thread.
- Logs never contain file contents or API keys. Paths are fine at debug level.
- Tests: `pytest`. Use temporary directory trees for indexer tests. Mock Gnani in tests. Never call the real API in CI.
- Every intent gets parser tests with English, Hinglish, and typo variants.

## Commands

```bash
pip install -e ".[dev]"
python -m zachary.indexer        # run the indexer daemon
python -m zachary.assistant      # run the assistant (quick bar + voice)
python -m zachary.cli search "fees yesterday"   # debug search from the terminal
pytest
ruff check . && mypy zachary
```

## Don't

- Don't scan the disk at query time.
- Don't send file contents to any cloud service by default.
- Don't add general-chatbot features.
- Don't train custom STT or wake-word models.
- Don't use Electron.
- Don't put OS-specific code outside `core/platform/`.