# Phase 6: Ask your files (report)

Status: **built and tested; waiting for review.** Branch `phase-6`.

## 1. "Done when"

| Check | Result |
|---|---|
| "when is my fee due?" answers from the fee document and cites it | ✅ With the Claude API (mocked in tests): a one-line spoken answer ("Your next fee installment is due on 15 June 2026."), the cited passage + file name in the answer box, and the cited file first in the list. **Not yet run against the real API**: no `ANTHROPIC_API_KEY` is set on this machine (see 6.1). |
| With the LLM off, it still shows the right passage | ✅ Tests: the fee passage with the due date, from `receipt_sem5.pdf`. On your real files: `FeeReceipt (3rdyear53k).pdf` (University Fee 3rd year 20,000 … Total 53,000), 47–75 ms. Your receipts have no due date in them, so with the LLM on, expect "your receipts don't say when it's due" plus the latest receipt. |

## 2. What was built

| Module | What it does |
|---|---|
| `brain/qa.py` | Question → passages, all local. Candidate files come from the existing hybrid search (keywords + meaning, plus older versions of the top files). Their chunks (from Phase 3) are scored by meaning (chunk vectors) 55%, question words present 35% and file rank 10%. Code counts 0.7× unless the question is about code (so "fee due" doesn't answer from a `next_due` variable). Keeps the top 4 passages, at most 2 per file, each with a ~280-character snippet centred on the question's words. |
| `brain/llm.py` | Optional Claude API client (official `anthropic` SDK, imported only on first use because it takes 2.7 s). **Q&A**: sends the question plus the 4 passages (file name and date, no paths) and gets back JSON (answer, found, sources) through structured outputs. **Unclear commands**: sends only the text and gets one command from a fixed list (find, open, ask, recent, projects, topics, resume, timeline, none). That command goes back through the normal parser, so the safety rules still apply. Server-side refusal fallback is on (`fallbacks: "default"`). 20 s timeout, 1 retry. Every call is logged to `llm_usage.jsonl` (model, tokens, ms, status; no text). |
| Intent `ask` | "when/what/how much/who … ?", "fee kab bharna hai", "meri fee kitni hai", "ask …", typos ("whn is my fee due"). Requests for a file still search ("where is my resume", "which file has my marks"). Also fixed: "where is my resume" used to trigger *resume*. |
| Handler | `ask` → passages; while typing, never the LLM. On Enter or voice: the LLM answer if it's on, else the best passage. If the LLM fails, it falls back to the passage. The LLM reads a command only when it's a sentence (≥ 4 words, ≥ 2 "sentence" words) that the parser couldn't place or that found nothing strong. Plain searches never call it. |
| Quick bar | An answer box (answer + quoted passage + file) under the status line. "Thinking…" if the reply takes longer than 300 ms. Styles live in `ui/style.py`. |
| CLI | `cli ask <question>` (`--local` = never call the API), `cli llm status`, `cli llm usage`. |
| Settings | `llm_enabled` (off), `llm_model` (`claude-opus-5-5`), `llm_effort` (`low`). The key is `ANTHROPIC_API_KEY` in the environment or `.env` (added to `.env.example`). |

## 3. Decisions (please review)

1. **Model: `claude-opus-5-5` at effort `low`** (the current default model). Switching to Haiku 5.5 is one command (`cli set llm_model claude-haiku-5-5`). It's faster and much cheaper, which may suit voice better. To be decided after a real run (see 6.2).
2. **What is sent:** the question, ≤ 4 passages of ≤ 1,000 characters, file names and modified dates. No paths, no whole files. For unclear commands, only the sentence. Tests check this (every excerpt sent is a stored chunk; no home path in the message).
3. **The LLM never acts.** It returns an answer or a command name. Commands go through the parser and safe actions, and "open" still needs a clear winner.
4. **`anthropic` is a normal dependency** (~45 MB with pydantic/httpx2). It's never imported unless the LLM is turned on and used.
5. **Refusal fallback** (`server-side-fallback-2026-07-01`, `fallbacks: "default"`) is on. Remove it if you prefer a plain decline.
6. **Prompt-injection guard:** excerpts are wrapped in `<excerpt>` tags, and the system prompt says they are data, not instructions. The worst case is a wrong sentence on screen, never an action.

## 4. Timings

| What | Measured | Budget |
|---|---|---|
| Question → passages, 200k files + 268k chunks, with meaning search (`bench_phase6`) | **p50 20 ms, p95 45 ms** (first version: 254 ms; sqlite-vec scanned all 268k vectors for an `IN (...)` lookup, so it now looks them up one id at a time) | search < 50 ms |
| Same, keywords only | p50 16 ms, p95 53–55 ms | |
| Your real home folder (`cli ask`) | 47–75 ms, including opening the DB and the first query | |
| LLM answer (network) | **not measured yet** (no key). Expect about 1–2 s with Opus at low effort; Haiku is faster. | as low as the API allows |

## 5. Testing

- `tests/test_qa.py` (19 tests, Claude always mocked; `conftest.py` now also blocks `*.anthropic.com` and hides `ANTHROPIC_API_KEY`):
  - retrieval finds the due-date passage, with and without meaning search;
  - snippets centre on the question's words;
  - LLM off shows the passage;
  - Hinglish questions;
  - an answer with a citation, plus exactly what was sent;
  - the ledger holds no text;
  - typing never calls the LLM;
  - an LLM failure falls back to the passage;
  - not-found answers;
  - refusal and max_tokens;
  - source validation;
  - unclear-command routing (only the text is sent);
  - routed "open" keeps the confidence rule;
  - unsafe or "none" commands do nothing;
  - plain searches never call the LLM;
  - the provider needs both opt-in and a key.
- `test_intents.py`: 20 new cases (questions vs. file requests). `test_ui.py`: answer box shows and hides.
- ruff, ruff format, mypy strict (+ win32): clean. Full suite passes locally.

## 6. Open questions

1. **API key:** add `ANTHROPIC_API_KEY` to `.env` so I can run a few real questions (about 5 calls) and measure the answer latency, or test it yourself with the commands in `run-and-test.md` 3.5.
2. **Model:** keep Opus 5.5 (best answers) or default to Haiku 5.5 (faster and cheaper for voice)?
3. Your fee receipts don't contain due dates. Is there a fee notice or circular somewhere that should be indexed (e.g. an email PDF in Downloads)?
