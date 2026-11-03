# Phase 5: Topics, timeline, and learning (report)

Status: **built and tested; waiting for review.** Branch `phase-5`.

## 1. "Done when"

| Check | Result |
|---|---|
| Topics look sensible on a real home folder | ✅ for you to judge. On your home folder (490 documents with vectors): **Fees, Attendance, Machine Learning, Sports Day, Legal, Leetcode problems (Easy, Leetcode), Zachary, Durdrsti, AI Email Task Extractor, Infaagent, Hackfest SVIT, CSE committee…**, 18 topics in 2.8 s. Some labels are clumsy ("Male, Bangalore" for face-model files); see 3.4. |
| Resume reopens the right set | ✅ "resume where I left off" picks your last real work session (your Zachary session: `test_voice.py`, `pipeline.py`, `quickbar.py`, …) and **asks first** ("…8 files. Reopen them?"). Only "yes" opens them; "no" cancels; typing never arms it. Tested end to end. |
| Ranking measurably adapts to repeated choices | ✅ Three equally good "report" files: after choosing the 3rd one twice, it ranks **1st** with "you chose this before". A similar query ("work report") is boosted too; unrelated queries aren't. Choices fade with a 30-day half-life. Tested. |

## 2. What was built

| Module | What it does |
|---|---|
| `indexer/clustering.py` | Nightly (in the indexer's idle maintenance, at most once a day): HDBSCAN (scikit-learn) over **document** vectors after PCA, with members re-checked in full dimensions. Code joins a topic only when it's very close to its notes (at most 2 code files per note). Labels: the most specific shared folder ("DSA", "Fees"), else the topic's most distinctive words and two-word phrases (class TF-IDF). Topic ids stay stable across rebuilds. **Virtual only**: nothing on disk changes. |
| `brain/sessions.py` | Work sessions from the access log (opens and edits; a gap of 45 min or more starts a new one). Shell history and hidden folders are ignored. Each is labelled by its project, else its topic, else its folder. Computed on demand (8 ms for a week), so always current. |
| `search/feedback.py` + migration #4 | Every result opened or revealed from a list is logged with the query's words and rank. A search adds a boost for files chosen for the same or similar words (≥ 50% overlap) × recency, and shows "you chose this before". Kept in memory, indexed by word (1.8 ms p95 with 5,000 choices). |
| Intents | `topic` ("show my topics", "topic machine learning", "my subjects"), `resume` ("resume where I left off", "reopen my last session", "jahan chhoda tha"), `timeline` ("what was I working on last Tuesday", "kal kya kaam kiya"), `confirm`/`cancel` ("yes", "haan", "no", "nahi"). "open my resume" still searches for a file called resume. |
| Handler / UI | A topics list with a **T** badge (Enter shows its files); a timeline list with an **S** badge per session (Enter → "Reopen them?"); spoken summaries. |
| `ui/style.py` | All colours and badges in one file, ready for the design pass before Phase 8. |
| CLI | `cli topics` (list), `cli topics --rebuild` (cluster now). |
| Search fix | A kind word alone ("folders", "photos") lists recent items of that kind (from Phase 4 testing). |

## 3. Decisions (please review)

1. **scikit-learn added** (HDBSCAN, as `claude.md` specifies). Download about 45 MB; installed **156 MB** (scikit-learn plus SciPy). This matters for the Phase 8 installers. Only the indexer imports it, only during the nightly rebuild, so the assistant's memory is unchanged. The alternative would be writing our own density clustering, which is real work.
2. **Topics come from documents; code only joins.** Clustering code too made the top topics "CSS", "tsconfig" and "imports". Projects already cover code.
3. **Big collections cluster a sample.** HDBSCAN runs on the newest 15,000 documents. Everything else joins the nearest topic if it's similar enough (≥ 0.6, code ≥ 0.75). Under 5,000 documents, 32 PCA dimensions are used (more precise; your folder); above that, 16 (faster).
4. **Label quality is limited without an LLM.** A folder label is used when ≥ 60% of the members share a specific folder. Otherwise it's keywords, which sometimes read oddly ("Srn Semester, Based Attendance"). Phase 6 (optional LLM) could rename topics from a few member titles.
5. **Sessions are computed, not stored.** The `sessions` tables from Phase 0 stay unused for now; computing from the access log is always correct and takes ~8 ms. If history ever gets pruned, storing them is an easy change.
6. **Reopening asks first and caps at 8 files.** Folders are reopened only if a session had no files.

## 4. Timings (200k-file benchmark tree, 89k documents + 60k code files with vectors)

| What | Measured | Notes |
|---|---|---|
| Topic rebuild (nightly, background) | **21 s**, peak **378 MB** | First version: 187 s and 990 MB. Fixed by sorting ids instead of rows-with-vectors, int8 storage, a smaller sample, and naming topics from a 300-file sample. ~130 MB of the peak is scikit-learn itself. |
| Second rebuild (stable ids) | 28 s | |
| List topics | **0.21 ms** | was 106 ms (aggregating 88k members); now stored size/last activity (migration #4) |
| A topic's files | 2.8 ms p95 | |
| Sessions for a week (your log) | 8 ms | |
| Learning boost per search | 1.8 ms p95 | 5,000 past choices; first use 18 ms (loads the index) |
| Your home folder: topic rebuild | 2.8 s | 2,349 docs+code |

## 5. Testing

- ruff / ruff format / mypy strict (+ `--platform win32`): clean.
- New tests:
  - `test_topics.py` (11): topics found and labelled; close code joins, unrelated code and stray notes don't; stable ids; too few files; sessions split and labelled, noise ignored; resume picks the right session; ranking adapts; old choices fade; topics drill-in; timeline → "Reopen them?" → yes/no; preview never arms; choices recorded through the handler.
  - Intent cases for all new commands, including that "open my resume" stays a search.
- The full suite passes locally (watcher/inotify tests need the higher inotify limit while your indexer runs; CI runs them).

## 6. Open questions

1. Is the +156 MB install size for scikit-learn OK (see 3.1)?
2. Should topic names be improvable by an LLM in Phase 6 (sending only a few file *titles*, opt-in)?
3. Resume reopens up to 8 files. More, fewer, or should it also reopen their folders?
