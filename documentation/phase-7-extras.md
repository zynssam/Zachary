# Extras after Phase 7: apps, open-in-app, creating files, dictation

Status: **built and tested; waiting for review.** Branch `phase-7-extras` (on top of `phase-7`).

## 1. What you asked for

| Request | Result |
|---|---|
| "open spotify" runs the app | ✅ Installed apps come from the desktop's own list: `.desktop` entries (system, Flatpak, Snap) on Linux; Start Menu shortcuts and Store apps (`Get-StartApps`) on Windows. On your laptop that's 51 apps, read in 66 ms. Spotify (Flatpak) and VS Code match. Typos are tolerated ("spotfy" works), and so is Hinglish ("spotify kholo"). |
| "open zachary in vscode" | ✅ Also "open the zachary project in vs code", "zachary ko vscode mein kholo", "open my resume with writer". The thing to open is a project by name first, else the best folder or file. If several match: "Which one should I open in Visual Studio Code?", then "the second one". "open notes in downloads" stays a search, because Downloads isn't an app. |
| "make a document I'll dictate" | ✅ Dictation runs until you say **"stop dictation"**. Each piece you say (a pause of 1.5 s ends a piece) is added and the mic re-opens by itself. "new paragraph", "scratch that" (drops the last piece) and "cancel dictation" work, and typing in the bar adds text too. A draft is saved locally after every piece. The document is written and **opened only after "stop dictation"**. |
| "create a text file and paste whatever I copied last" | ✅ The clipboard is read only for such a request (Qt, with a `wl-paste` fallback on Wayland). Also: "create a folder called Hackathon Ideas", "make a new folder called fees 2027 in downloads", "create a markdown note called Todo in the zachary project", "create a text file called shopping list saying milk eggs bread", "ek text file banao aur copy kiya hua paste karo". |
| Where files go | `Documents/Zachary/<subfolder>`, sorted by content: an existing subfolder if the text fits it (meaning-model similarity plus shared words, with the subfolder's name, file names and some of their text), else a new one named from the content's main words ("Grocery List"). With an LLM turned on, it picks or names the folder from the first ≤ 800 characters. Saying "in downloads" or "in the zachary project" overrides this. |
| Formats | **.docx** by default; **.txt** for "text file", "txt" or "notepad"; .md for "markdown". |
| Ask before creating? | No (your choice). Creating only ever makes **new** files and never overwrites (" (2)" is added). "undo" moves the new file to the trash, and removes any folder it made, but **only if you haven't edited it since**; edited files are left in place. |

## 1b. Fixes after trying it (names, questions)

| Problem | Fix |
|---|---|
| "Zachary" not understood; another file was opened | **Root cause:** "zachary" was on the search stop-word list (it's also the wake word), so every search for your Zachary project dropped the word and matched other words. Removed. The wake word is now only stripped after "hey/ok" or right before a command ("hey zach open …"), not when it's the thing to open. "open zachary" opens the project whose name matches exactly. |
| Misheard names | Corrected **locally** by sound before the request is understood: "zakari", "zachery", "zack ary" and "sachary" → Zachary; "spotfy" and "spot ify" → Spotify; "durdristi" → Durdrsti. The vocabulary is your project, app, topic and top-level folder names. A word is only replaced if it isn't in your index, sounds like one of your names, and starts with the same letter (or is spelled almost the same), so "scary movie" stays. Cost: < 1 ms per request; the vocabulary is built at start-up in about 0.15 s. |
| Questions not about your files should go to the API | "how many / which / when / how / what / who …" questions **not about your things** go to the LLM (Groq, Gemini or Claude) with **only the question** (real run: "how many planets…" → "There are eight planets in the Solar System.", Groq, 0.6 s). A question counts as about your things if it says "my / mine / our / mera / hamara…" or names a file word (file, notes, assignment, receipt…), or if the best passage contains all of its words or the best file's name does ("when was the hackfest pitch"). Then it's answered from your files. If the files don't say and the question wasn't personal, it falls back to the general answer. "how do I …" questions are general. |
| Gnani keyterms (**on, approved by you**) | Prisma v3 is given up to 200 of your names (projects, apps, topic labels, top-level folders; **never file contents**) as `keyterms`, in the stream header and the REST form, built in the background at start-up and refreshed every 10 minutes. Real API test, Gnani's own voice saying "Open Zachary in VS Code": **without** keyterms "Open **Zakre** in VS Code"; **with** keyterms "Open **Zachary** in Visual Studio Code" (1 TTS + 2 STT calls). Turn off with `cli set stt_keyterms false`; the local correction still fixes "Zakre" → Zachary. |

| "How many files do I have?" showed search results | Counting questions are answered **from the index on this machine** (no LLM, a few ms): "how many files / PDFs / photos / Python files / folders / projects do I have", "how many files in Downloads", "how many files did I edit yesterday", "how much space do my videos take", "kitni files hain", "downloads mein kitni files hain". On your laptop: 7,974 files (8.3 GB), 73 PDFs, 31 projects, 990 files in Downloads, plus a breakdown by kind. Also fixed: the name corrector turned "many" into "main" (one of your folders). Only words of 5+ letters are corrected now, only into names of 5+ letters, never into generic folder names (main, backend…), and the parser's own words count as known. |

| "Hey Zach" was slow / effectively off | New **fast wake word with Vosk** (your choice): a small offline Indian-English model (36 MB download, 55 MB on disk) listening only for a fixed phrase list ("hey zack", "hi/ok/okay zack"), with **decoys** ("hey jack", "hey jake", "hey sack", "zachary") so similar-sounding phrases don't wake it. It fires when the partial result shows the phrase **3 updates in a row** (~0.1 s); one-off flickers ("hey Sara" briefly read as "hey zack") don't count. On "Hey Zach" the command is captured exactly like push-to-talk (Gnani streaming + its end-of-speech), starting with the last 0.3 s of audio. **Measured:** model load 0.6 s, **~5% of one CPU core**, ~145 MB for the whole process (Whisper: ~300 MB, up to 34 s cold). On natural speech (Gnani's voice): "Hey Zach, open the fee receipt" fires at 0.9 s (right after "Zach"), and "Okay Zach" fires. "Hey Jack, what's up?", "Open the zachary project" and an ordinary paragraph with "hey Jack / hey Sara / they act" never fire. Only a handful of clips were tested, so real-world false wakes will show over time. With the wake word on, the mic is open all the time, but audio stays on your laptop until "Hey Zach". |

| "Hey Zach" still didn't work in the app | **Root cause:** the detector never read Vosk's final results, so while listening continuously each new utterance was appended to the old text, and **after the first sentence it could never match again**. The 1-second test clips couldn't show this. Fixed, with a regression test (ordinary talk, then "Hey Zach", in one continuous stream: wakes exactly once, at "Hey Zach"). Reading results exposed one weak spot: "hey Sara" briefly read as "hi zack". So "hi zack" was dropped, and everyday words ("open", "the", "project"…) were added as decoys. Measured again: "Hey Zach…" and "Okay Zach…" wake at 0.9 s; "Hey Jack", "Open the zachary project" and the paragraph don't. **New: `cli voice wake-test`** (you run it; local only) prints what the detector hears live, plus a log line whenever it hears something but doesn't wake. |
| Questions about a project weren't sent to the API | "what is the zachary project about", "how do I run the zachary project", "what does this project do", "what is the durdrsti project" were taken as **project searches**. Now a question naming a (singular) project goes to the LLM with excerpts **from that project's own files only**: README and top-level docs first, then passages matching the question; build output (`*.egg-info`, dist, build) is skipped. "this / my project" = your last work session's project. A folder that isn't a code project (Durdrsti) is found by name. Lists and counts stay local ("what are my projects", "how many projects do I have"). Real runs (Groq, ~0.8 s): "Zachary is a cross-platform desktop assistant that indexes files…" (from claude.md); "Run it via the CLI, e.g. `python -m zachary.cli` …" (from run-and-test.md). |

## 2. Safety

- **Apps:** only apps the desktop lists can start, through `gio launch <entry>` or `os.startfile`, never a shell or a typed command. "launch /bin/sh" or "rm -rf /" does nothing (tested). Open-with passes exactly one indexed file or folder.
- **New files:** opened only if they're in the creation log. Nothing existing is changed.
- **Undo:** creations are recorded like plans (`action_plans` kind `create`, `undo_log` `mkdir`/`create`). This needed **migration #5**, which rebuilds `undo_log` with a new `create` op and keeps your undo history (tested).
- **Tests:** they now run with a **temporary home folder** (`HOME`/`USERPROFILE`). One early test run created two empty `fees 2027` folders in your real `~/Downloads`; I removed them (they were empty, so `rmdir` was safe). The place lookup now checks the index roots first.

## 3. Measured

| What | |
|---|---|
| App list | 51 apps in 66 ms (cached afterwards) |
| Sorting calibration (real model) | correct folder for hackathon / fees / meeting notes / DSA text, with scores 0.34–0.48. Unrelated text (groceries, workouts) scores at most 0.07 and gets a new folder. The threshold is 0.28. |
| Voice dictation end to end (mock Gnani) | the mic re-arms about 0.15 s after each piece, and the document is written and opened after "stop dictation" |

## 4. Tests

- `test_apps.py` (19): desktop-entry parsing (hidden entries, user overrides, our own launcher left out), name matching, the "X in Y" parse (including Hinglish order and typos), launching, open in VS Code, "in downloads" staying a search, apps that can't open files, and only installed apps.
- `test_create.py` (26): create/dictate parsing; never overwriting; .docx content; sorting into existing or new folders; the LLM folder namer gets an excerpt only; undo trashes unless edited; migration 5 keeps history; clipboard paste opened only after writing; folder in a place; undo after create; dictation (pieces, new paragraph, scratch that, nothing written until stop, draft removed), cancel/empty; named .txt dictation.
- `test_voice.py`: voice dictation through the real listener, engine and bar with the mock Gnani.
- `test_vocab.py` (24): sound keys, misheard names fixed, real words left alone, "zachary" searchable, open misheard project in VS Code, exact project name, keyterms header value (200 max, ASCII, `<|` rejected), personal vs general questions, a general question sends only the question, no-LLM message, a file-name hit is answered from the file.
- ruff, mypy strict (+ win32) and the full suite pass locally and on CI.

## 5. Commands

```bash
python -m zachary.cli apps                 # what can be opened
python -m zachary.cli apps spotify         # what a name matches
# bar / voice:
#   open spotify | open zachary in vscode | zachary ko vscode mein kholo
#   make a document I'll dictate ... "new paragraph" ... "stop dictation"
#   create a text file and paste what I copied
#   create a folder called Hackathon Ideas | ... in downloads
#   undo -> yes
```

## 6. Open questions

1. Is 1.5 s right for the dictation pause? Shorter means pieces get added sooner, but more full stops appear mid-sentence.
2. Gnani's text often has no punctuation, so each dictated piece becomes a sentence (capital letter and full stop). Is that OK, or would you prefer the text exactly as heard?
3. Windows: Start Menu and Store apps are implemented but only tested with fake data here. Could you try "open spotify" on your Windows machine?
