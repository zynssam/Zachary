# Phase 3: Semantic memory (report)

Status: **complete, waiting for review.** Branch `phase-3` (not merged). CI is green on Linux and Windows, Python 3.11 and 3.12.
The Linux 3.12 job also runs the acceptance test with the real Model2Vec model.

## 1. "Done when"

| Check | Result |
|---|---|
| "show me everything on data structures" returns old DSA notes and recent linked-list code together | ✅ Real model (`test_real_model_acceptance`): DSA notes from 600 days ago, `ll.cpp` from 2 days ago, and a BST assignment, all in the top 3. Content-only code (`ll.cpp` has no DSA words in its name) is found by meaning. |
| "related to this file" is a < 10 ms lookup | ✅ p95 **0.17 ms** (2k corpus), **0.50 ms** (200k files) |
| Re-saving an unchanged file doesn't re-embed | ✅ 2,000 files touched → extracted **0**, reused 2,000 (1,470 files/s). Tested, including renames (only the name part of the vector is refreshed). |
| First-run embedding throughput reported | ✅ See section 5: 73–76 content files/s on a mixed corpus; **160 files/s on your real home folder** (7,905 files in 49 s) |

## 2. What was built

| Module | What it does |
|---|---|
| `indexer/extract.py` | PDF (PyMuPDF), DOCX, PPTX, XLSX, text and code. "Embed less": stops after `embed_max_kb` (64 KB) of text. Titles come from metadata or the first line. Media, archives and binaries are never opened. Broken files fall back to name-only, never a crash. |
| `indexer/chunker.py` | ~1,000-character chunks with a 120-character overlap, cut at paragraph or sentence boundaries. Offsets refer to the extracted text. |
| `indexer/embedder.py` | Model2Vec `minishlab/potion-base-8M` (256-d, CPU, no PyTorch). Downloaded once (~30 MB) into `<data dir>/models`, then loaded offline. int8 quantisation. A deterministic `HashEmbedder` for tests. |
| `indexer/semantic.py` | The pipeline, in batches. A quick hash (size + first and last 64 KB) decides whether content changed. One model call per batch. The file vector is 75% content (mean of chunks) and 25% name/path/title. Code is embedded as its identifiers. Chunk text and vectors are stored for passages. The FTS `title` and `keywords` (top content words) are filled, so **words inside files are now keyword-searchable**. |
| `indexer/semantic_worker.py` | A background thread in the indexer. It waits while paused, on battery (`embed_on_battery`) or while the system is busy, works ≤ ~50% of one core, and handles documents and code first, newest first. Between batches it rebuilds the vector index when it has grown, updates related files (weekly full pass) and regroups versions. |
| `search/vectors.py` | **IVF vector index:** k-means clusters stored in SQLite (`vec_ivf`, clustered `WITHOUT ROWID`), plus an **in-memory copy in the assistant** (`MemoryVectorIndex`). |
| `indexer/relations.py` | The top-10 related files per file, precomputed from content (cosine), name words, folder distance and co-access (opened within 30 minutes). Incremental updates also offer the new file to its neighbours. Lookup is one indexed query. |
| `indexer/versions.py` | Groups `report`, `report_final`, `report_final_v2`, `Report (1)`, `Copy of report` in the same folder with the same extension (documents and code only; dates and plain numbers are *not* versions). Search shows the latest, with "latest of N versions". |
| `search/explain.py` | "Why it matched": exact name, opened/edited in range, name/folder terms, **`about: "<best matching passage>"`**, latest of N versions, last opened/modified. |
| `search/hybrid.py` | A **meaning list** (vector nearest neighbours, or exact scores inside a time range) fused with the keyword and recency lists via RRF; version collapsing. |
| `brain/intents.py`, `handler.py`, `context.py` | The `related` intent ("related to this file", "similar to the second one", "files related to fee receipt", "isse related files", "idakke sambandhisida files"). "The older one" walks a document's versions. |
| UI / CLI | **Ctrl+R** and right-click → "Related files" in the quick bar. `cli embed` (index contents now), `cli related PATH`, semantic progress in `cli status`. |

## 3. Decisions and deviations (please review)

1. **File vectors live in our own IVF table (`vec_ivf`), not in sqlite-vec's `vec_files`.** sqlite-vec 0.1 only does brute-force search, measured at **169 ms** per query on 200k int8 vectors (the budget is 50 ms). `vec_files` (from migration 1) is left empty, because a second copy cost 55 MB per 200k files and was never read. Chunk vectors stay in sqlite-vec (`vec_chunks`) as specified.
2. **The assistant keeps the file vectors in RAM: +61 MB for 200k files** (~256 bytes per file). On this 6 GB laptop under memory pressure, the on-disk index had 200–900 ms spikes from evicted pages. In memory, vector search is 1.1 ms and search p95 drops from 56–60 ms to 28–36 ms. ("Speed first" over RAM; open to discussion.)
3. **Meaning-match thresholds:** absolute ≥ 0.15 *and* ≥ 25% of the best match. Measured with the real model, relevant files score 0.23–0.73 and unrelated ones mostly < 0.1, though occasionally up to 0.28. A stricter cut dropped the linked-list code from "data structures". The cost is an occasional weak match at the *bottom* of the list; the right file was first in all 9 test queries.
4. **Hash for change detection:** stdlib `blake2b` on size + first and last 64 KB instead of xxhash. It's I/O-bound either way, and it avoids a dependency. Full-content hashes for duplicates come in Phase 7.
5. **The model is downloaded from the Hugging Face hub once**, by the indexer, into the data folder. That's the only network use in this phase. File contents never leave the machine. The assistant never downloads; it works keyword-only until the model is present.

## 4. Testing

| Check | Result |
|---|---|
| ruff / ruff format / mypy strict (+ `--platform win32`) | clean (83 files) |
| Linux CI 3.11 / 3.12 | 319 passed, 7 skipped / **320 passed, 6 skipped** (+ real-model test) |
| Windows CI 3.11 / 3.12 | 317 passed, 9 skipped |
| Local (real model) | all pass with `ZACHARY_TEST_MODEL=1` |

New tests (~60): `test_extract` (12, every format, limits, broken files, chunk offsets), `test_semantic` (9, pipeline, re-save, changed content, rename, `name_only`, delete cascade, future mtime), `test_vectors` (3, exactness, recall ≥ 0.95, in-memory index freshness), `test_versions` (11), `test_relations` (4, content across folders, location/co-access, < 10 ms, incremental symmetry), `test_semantic_search` (5, meaning vs keyword, passages, content keywords, related follow-ups, **real-model acceptance**), `test_inotify_tree` (3), plus related-intent cases in `test_intents`.

### Bugs found and fixed in this phase
1. **Phase 1 bug, affecting your machine right now:** on Linux, watchdog's recursive inotify watched **every** subfolder, including excluded ones. Your running indexer holds **40,476 of your 46,173** watches for 1,609 indexed folders, which can starve VS Code and Nautilus. Replaced by an exclude-aware inotify watcher (`core/platform/linux_inotify.py`) that watches only indexed folders. **Restart your indexer to pick this up** (see section 7).
2. **Files with a future mtime were re-embedded forever** (`embedded_at` < mtime). Now `embedded_at` ≥ mtime.
3. **The chunker shifted offsets** by stripping its input.
4. **Windows:** `UNIQUE constraint failed` when two event batches raced on one new file. Inserts are now upserts.
5. **Windows:** single-instance toggles could time out and start a *second* app. Now the server acks first, and a connected but slow instance counts as running.
6. **Windows:** `name_only` folder rules didn't match (case-folded rule paths vs real-case file paths).
7. **Search on 200k files was 117–131 ms p95.** Fixes: fewer candidates (what can reach the top 10), the in-memory vector index, a removed redundant index, and no more `vec_files` copy. Now **28–36 ms**.

## 5. Timings

**Content corpus**: 2,000 files with real text (PDF 499, DOCX 402, MD 301, TXT 258, PPTX 198, PY 187, XLSX 155; 25.5 MB):

| Measurement | Result |
|---|---|
| First-run extract + embed | **26.5 s = 76 files/s** (0.21 MB text/s), 7,316 passages |
| Re-save 2,000 unchanged files | extracted 0, reused 2,000 in 1.4 s |
| Vector index / relations / versions | 0.12 s / 1.3 s / 0.04 s |
| Related lookup | p50 0.15 ms, **p95 0.19 ms** |
| Meaning search | p50 13.6 ms, **p95 15.3 ms**; the right topic is first for every query |
| Model load | 0.87 s |

**Your real home folder** (scratch DB): 7,905 files embedded in **49 s (160 files/s)**, 2,225 with text (17.2 MB, 21,385 passages). Vector index 0.4 s, relations 7.9 s, 18 version groups, DB 45 MB.

**200k-file index** (names only, since the synthetic files are empty):

| Measurement | Result | Budget |
|---|---|---|
| Name/path embedding | 371 s (541 files/s), a one-time background job | |
| Vector index build | 8.7 s | |
| Full related pass | 256 s (was 463 s), weekly background job | |
| **Search with meaning** | **p50 13–17 ms, p95 28–36 ms** (in-memory index) | p95 < 50 ms ✅ |
| Related lookup | p95 0.50 ms | < 10 ms ✅ |
| Quick bar: hotkey → results | p95 13.9 ms | < 150 ms ✅ |
| Quick bar: typing → results | p95 80 ms | |
| Assistant cold start (loads model + vectors) | 2.6 s | |
| Assistant memory for vectors | +61 MB | |
| DB size | 136 MB → 418 MB with vectors + 2M relation links | |

## 6. Known limitations
- **Model2Vec `potion-base-8M` is small and English-centred.** It links "data structures" ↔ "linked list" only weakly (0.17), and Hindi/Kannada *content* is matched by keywords, not meaning. CLAUDE.md allows an optional better model (ONNX MiniLM/bge) for important files; not done yet.
- **Long documents:** the file vector is the mean of the chunks from the first 64 KB, so a topic deep inside a big file may be missed. Searching chunk vectors directly would fix it (useful for Phase 6 Q&A anyway).
- **The full related pass is 256 s for 200k files** (weekly, background, ≤ 50% of a core). Incremental updates are per new file.
- **The DB grows with relations:** 2 million links ≈ 110 MB at 200k files.
- **"notes on data structures" on your real files** put the Data Structures planner third, behind two "notes" files that keyword-match better. Phase 5's feedback learning is meant for this.
- **The first search in a brand-new process** (e.g. the CLI) reads vectors from disk and is slower (~50 ms). The assistant preloads them.

## 7. To do on your machine
Your old indexer (Phase 2 code) is still running with 40k inotify watches. After this phase is merged:
```bash
pkill -f zachary.indexer; pkill -f zachary.assistant
.venv/bin/python -m zachary.assistant -v      # starts the new indexer; it downloads the model once
.venv/bin/python -m zachary.cli status        # watch "semantic: N/M files embedded"
```

## 8. Questions for review
1. Is **+61 MB of RAM per 200k files** in the assistant acceptable for fast meaning search (decision 2)?
2. Keep the **looser meaning thresholds** (more recall, occasional weak tail results; decision 3)?
3. Merge `phase-3` into `main`?
