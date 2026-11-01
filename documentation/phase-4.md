# Phase 4: Voice (report)

Status: **redone after real-use testing (section 0); waiting for review.** Branch `phase-4`.

## 0. Redo after real use (2 Nov 2026)

Using it on the laptop (Bluetooth earbuds; the built-in AMD mic has no Linux driver) showed:
replies arriving very late, voice not activating a second time, "didn't hear anything", and the PC
swapping to a halt. Causes and fixes:

| Problem | Cause | Fix |
|---|---|---|
| Reply heard late / with the *next* command | `Player.stop()` (called on every press) left an end marker in the queue; the next reply's playback quit immediately and its audio played during the following command | Player rewritten: one long-lived thread and output stream, generation numbers, `stop()` clears everything |
| Playback misrouted | the mic set `PIPEWIRE_NODE` for the whole process; the speaker stream then targeted the *mic* node | routing env vars are set only while the input stream opens, then restored |
| Super+Alt+Space often did nothing | two portal sessions under one app id; GNOME bound only one, randomly | **one** portal session binds both shortcuts |
| Voice dead after one command | any exception killed the listener thread | every command is guarded; push-to-talk restarts a dead listener |
| "Didn't hear anything" | our energy VAD gated what was sent; quiet earbud mic | **Gnani's server VAD** decides start/end (`x-vad-threshold 0.5`, `x-min-silence-ms 450`); every frame from the press is streamed |
| Slow / crash | local whisper wake-word check on a 6 GB machine (34 s cold, while swapping) | **push-to-talk by default**; the mic opens only while you talk; whisper loads only if Gnani fails |
| Music sounds like a call | earbuds kept in headset mode | headset mode only while talking, restored 8 s after the last command |
| Ctrl+M only worked in the text box | key handled by the edit only | window-wide shortcut |

Also: the TTS WebSocket stays open and is reused for every reply (docs allow several requests per
connection), raw PCM (`container: raw`), one speaker thread (a new reply cancels the old one),
`cli voice latency` shows the last commands' stages.

**Real API, after the redo** (2 Nov smoke, laptop under load):

| Stage | Before | After |
|---|---|---|
| End of speech → first reply audio | 3–5 s (typ. 2.2–4.6 s TTS alone) | **0.55 s** |
| TTS first audio | 0.45–4.6 s (new connection each time) | **0.30 s** (warm socket; reused for the 2nd reply) |
| Gnani transcript latency (server) | – | 0.22–0.39 s |

**Finding:** on a short *robotic* espeak clip, streaming STT returned wrong text ("Open.", "Oh, and he
received.") while REST got it right ("Open the fee receipt"), with and without our VAD/format
headers. Real voices streamed correctly earlier. If recognition is poor with your voice:
`python -m zachary.cli set stt_streaming false` (REST: about +0.3–0.5 s, but full-clip accuracy).

API calls used for the redo: 9 (smoke 3 + 3, debugging 3). Total for Phase 4: see `cli voice usage`.

## 1. "Done when"

| Check | Result |
|---|---|
| "Hey Zach, open the fees document I opened yesterday" works end to end by voice | ✅ Tested in CI with Gnani mocked: wake word, then STT, then search, then the file opens, then the spoken reply ("Opening Fee Receipt Sem five."). ✅ Real Gnani on your real index (9.5k files): the transcript is exactly right ("Open the fees document I opened yesterday"). On your index, Zachary asks "Which one?" because no file there matches "fees" by name or content (see 3.6). |
| The latency breakdown is logged | ✅ Every voice command adds one line to `<data dir>/latency.jsonl` with the stages wake → speech_end → stt → intent_search → tts_first_audio → tts_done. |
| The offline fallback works | ✅ Any Gnani failure (network down, credits used up, rate limit, bad key, server error) falls back to whisper.cpp on the same audio. Tested for each error type. The reason is recorded. |
| Gnani is mocked in tests | ✅ `tests/gnani_mock.py` runs local HTTP and WebSocket servers that mimic the documented API. A test guard (`conftest.py`) makes the real host unreachable and hides any real key, so tests **cannot** spend credits. |

## 2. What was built

| Module | What it does |
|---|---|
| `voice/types.py` | `STT`, `StreamingSTT` and `TTS` interfaces; `Transcript`; `VoiceError` (auth, credits, scope, rate_limited, network, server, bad_request, config). |
| `voice/audio.py` | Microphone capture (16 kHz mono, 1,024-byte frames, which is exactly what Gnani streaming needs), playback that can start before all audio arrives, WAV helpers, and an adaptive energy VAD. |
| `voice/gnani_stt.py` | REST (`/stt/v3`, ≤ 30 s clips) and realtime WebSocket streaming (`/stt/v3/stream`, real-time frames, `commit`). Checked against docs.gnani.ai. **No automatic retries.** |
| `voice/gnani_tts.py` | REST (WAV) and WebSocket (streamed chunks, for the fastest first audio). Voice `Shivani`, model `timbre-v3.0`, `en-IN`. `speakable()` turns file names into speech ("Fee_Receipt_Sem5.pdf" becomes "Fee Receipt Sem five"). |
| `voice/net.py` | IPv4-first connections with keep-alive reuse (see 3.3). |
| `voice/wakeword.py` | A **local** "Hey Zach" spotter: whisper.cpp tiny on the first 1.6 s of speech, prompted with "Hey Zach,". Word timestamps cut the wake phrase off the command audio. |
| `voice/offline_stt.py` | whisper.cpp (`pywhispercpp`, tiny model, ~75 MB, downloaded once into `<data dir>/models/whisper`). |
| `voice/pipeline.py` | The listener thread. While waiting, audio stays on the machine (VAD and the wake spotter only). After "Hey Zach" or push-to-talk, **one** utterance goes to Gnani: streaming if you pause after "Hey Zach" or use push-to-talk, REST if you say it in one breath. Gnani fails → whisper. |
| `voice/usage.py` | A local ledger of every real Gnani call (`<data dir>/gnani_usage.jsonl`): endpoint, status, seconds of audio or characters. The key is never logged. |
| `voice/factory.py`, `core/env.py` | Builds the voice kit from settings. The key comes from `GNANI_API_KEY` (environment, then `.env` in the working folder, then `.env` in the data folder). |
| `telemetry/latency.py` | `Timeline`: per-stage timings, written to `latency.jsonl`. |
| `ui/voice.py` | Connects voice to the quick bar. The transcript appears in the bar, goes through the same handler as typing (so Hinglish works the same way), the short reply is spoken, and the details stay on screen. |
| Hotkeys | Push-to-talk: **Super+Alt+Space** (global) and **Ctrl+M** in the bar. Uses the same portal / X11 / Windows paths as Super+Space. |
| CLI | `cli voice usage | setup | transcribe WAV [--offline] | say TEXT | listen [--offline] [--no-speak]`. Commands that use credits say so. |
| `bench/gnani_smoke.py` | A deliberate real-API smoke test (needs `--yes`; 7 calls, or 2 with `--only-e2e`). |

Voice is **off by default** (`voice_enabled false`). Turn it on with `python -m zachary.cli set voice_enabled true`.

## 3. Decisions and findings (please review)

1. **Wake word: local whisper spotter instead of openWakeWord** (your choice). openWakeWord has no "Hey Zach" model, and training one needs a large synthetic dataset. The spotter runs only after the VAD hears speech, then checks the first 1.6 s locally. Cost: 0.4–0.9 s per check (more under heavy load). Known trade-off: similar names like "Hey Jack" can trigger it. A trained openWakeWord model could replace it later behind the same interface.
2. **Credits.** Real calls this phase: **9** (stt-rest 4 = 12.6 s of audio, stt-stream 1 = 3.2 s, tts-rest 1 = 29 chars, tts-ws 3 = 113 chars). You reported 0.42 credits for the first 7. Calls are never retried automatically, and `cli voice usage` shows the totals.
3. **Network fix: IPv4 first, connection reuse.** The first real end-to-end run took 5.7 s for speech-to-text, but Gnani's server needed only 0.37 s. Measured with curl: over **IPv6** (which clients try first) the request took 0.96–1.88 s; over **IPv4** it took 0.11 s. Now REST uses a shared keep-alive IPv4 client and WebSockets connect over IPv4. If IPv4 can't connect at all (an IPv6-only network), it falls back to the default. A failed connect never reaches the server, so the fallback can't spend credits.

   | Real API, same command | Before | After |
   |---|---|---|
   | STT round trip | 5,694 ms | **1,000 ms** (server 0.39 s) |
   | TTS first audio | 3,300 ms | **445 ms** |
   | Repeat request on a warm connection (no key, free) | 1,153 ms | **69 ms** |

4. **End-of-speech → spoken reply, warm, on this laptop:** about 0.8 s VAD end-of-speech wait + 1.0 s STT + 0.01–0.02 s intent and search + 0.45 s TTS first audio ≈ **2.3 s**. The wake check adds 0.4–0.9 s before that. (The smoke script's intent_search of 2.2 s includes loading the meaning model once, which the app does at startup. Warm, it's 8–21 ms.) Streaming STT (push-to-talk, or a pause after "Hey Zach") has the text ready right after you stop talking, which saves most of the 1.0 s.
5. **Safety fix, found by the real test: "open" now needs a strong match.** On your index, "open the fees document I opened yesterday" opened `Roommate_AI_Specification_and_Architecture.docx`. No file matched "fees", but that doc was the only one opened yesterday and had a weak meaning similarity (0.18). New rule: open without asking only if the top result has a **keyword hit** (name, folder or content), an exact name, or **meaning similarity ≥ 0.40**. Measured: right answers 0.44–0.61, wrong picks 0.18–0.27. Time-only questions ("what I opened yesterday") are still answered directly. Zachary also asks instead of acting whenever it had to guess: a corrected typo ("Did you mean …?") or nothing in the requested time range. Three tests were added.
6. **Typo correction can pick odd words** ("rent" → "ret", "hostel" → "hotel" on your index, because those words exist in file names). With rule 5 that only changes what is *shown*, never what is opened. It could be improved later (prefer real dictionary words, show both).
7. **Hinglish:** `en-IN` handles mixed Hindi-English and returns Latin script, which the intent parser already understands. The Hinglish smoke test used espeak, which can't pronounce Hinglish (Gnani heard "Hello"), so it proved nothing. A real-voice Hinglish test is still needed, ideally by you saying "Hey Zach, kal wali fees file kholo".
8. **TTS WebSocket audio ends with ~1 s of silence.** It's harmless (playback just ends a bit later), so it's left as is.

## 4. Testing

| Check | Result |
|---|---|
| ruff / ruff format / mypy strict (+ `--platform win32`) | clean |
| Linux CI 3.11 / 3.12 | 351 passed, 9 skipped / **354 passed, 6 skipped** (+ real whisper wake-word tests) |
| Windows CI 3.11 / 3.12 | 349 passed, 11 skipped |
| Voice tests (`test_voice.py`) | all pass: clients, error mapping (no retry), streaming frame sizes, TTS header stripping, `speakable`, VAD, wake-phrase rules, every pipeline path, offline fallback, the "done when" end to end through the real Engine and quick bar, the real-API guard |
| Real whisper tests (`ZACHARY_TEST_WHISPER=1`) | pass locally; CI runs them on Linux 3.12 (espeak-ng + cached model) |
| Handler tests | all pass, including 3 new confidence-rule tests |
| Watcher / inotify / service tests **locally** | ⚠ fail on this machine *right now*, and also on the committed Phase 3 code: your running indexer holds 40,470 of the 46,173 allowed inotify watches, so tests can't add more. That's an environment problem, not a code one; CI runs them. Restarting the indexer doesn't change it (it needs those watches). To run them locally, raise the limit: `sudo sysctl fs.inotify.max_user_watches=524288`. |

## 5. Open questions

1. Is the ~2.3 s from end of speech to reply acceptable for now? The next big win would be keeping the TTS WebSocket open (−0.2 s) and using streaming STT for one-breath commands too (−0.5–0.8 s). Streaming everything means more WebSocket sessions, so it's worth checking first how Gnani bills them.
2. Should the "Which one?" reply read out the top 2–3 names, or keep it short and let you look at the screen? Currently it stays short.
