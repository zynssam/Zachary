# Phase 4: Voice, working notes (temporary)

A readable map of how voice works right now, what's done, what's broken, and where to change
things. It reflects branch `phase-4` at commit `093d1c6`. The formal report is `phase-4.md`; this file is
for working on the phase.

---

## 1. What voice does, in one picture

```
 you press Super+Alt+Space (or Ctrl+M in the bar)
        │
        ▼
 ┌─────────────── listener thread (voice/pipeline.py) ───────────────┐
 │ 1. open the mic (Bluetooth earbuds -> headset mode)                │
 │ 2. open a Gnani streaming STT socket   (in parallel, ~0.25 s)      │
 │ 3. send every 32 ms mic frame to Gnani                             │
 │ 4. Gnani's VAD says: speech_start ... transcript (final text)      │
 │ 5. close mic + socket                                              │
 └────────────────────────────────────────────────────────────────────┘
        │ text, e.g. "open fee receipt"
        ▼
 quick bar shows the text -> Engine -> Handler (same as typing)
        │ reply: say = "Opening Fee Receipt Sem 5.pdf.", items = [...]
        ▼
 ┌─────────── speaker thread (ui/voice.py) ───────────┐
 │ Gnani TTS WebSocket (already connected) -> chunks   │
 │ -> Player (voice/audio.py) -> speakers / earbuds    │
 └─────────────────────────────────────────────────────┘
        │
        ▼
 one line appended to ~/.local/share/zachary/latency.jsonl
```

**Privacy rules (from CLAUDE.md, do not weaken):**
- Audio goes to Gnani only after the key press (or "Hey Zach"), and only one command.
- The mic is never streamed continuously.
- File contents never leave the machine.
- Voice can only run the safe actions in `actions/safe_actions.py`.
- The API key comes from `.env` and is never logged.

---

## 2. Two ways to start a command

| Mode | Setting | Mic | Status |
|---|---|---|---|
| **Push-to-talk** (default) | `wake_word false` | opens only while you talk | **redone**: server VAD, fast, tested |
| **"Hey Zach"** | `wake_word true` | always open; whisper checks speech locally | **old flow, not redone yet** (see 7.1) |

⚠ **Your config currently has `wake_word = true`.** Your last runs (00:42, 00:45) went through the old
"Hey Zach" path, which is why one came back empty after 4 s. To try the new path:
```bash
python -m zachary.cli set wake_word false
```

---

## 3. Files: what lives where

| File | What it does | Lines |
|---|---|---|
| `zachary/voice/types.py` | Interfaces: `STT`, `StreamingSTT`, `TTS`; `Transcript`, `SpeechAudio`, `VoiceError(kind)` | 78 |
| `zachary/voice/gnani_stt.py` | Gnani speech-to-text: `GnaniRestSTT` (whole clip, ≤ 30 s) and `GnaniStreamingSTT` (live, server events) | 323 |
| `zachary/voice/gnani_tts.py` | Gnani text-to-speech: `GnaniRestTTS`, `GnaniWsTTS` (one persistent socket), `speakable()` | 317 |
| `zachary/voice/net.py` | Connections: IPv4 first (IPv6 to Gnani was ~10× slower), keep-alive | 65 |
| `zachary/voice/audio.py` | Mic (`MicStream`), choosing a mic, Bluetooth headset mode, `Player`, `EnergyVAD`, WAV helpers | 517 |
| `zachary/voice/pipeline.py` | `VoiceListener` thread: push-to-talk sessions, wake-word flow, fallbacks | 618 |
| `zachary/voice/wakeword.py` | "Hey Zach" detection from whisper words | 53 |
| `zachary/voice/offline_stt.py` | whisper.cpp (tiny), loaded only when needed | 117 |
| `zachary/voice/usage.py` | Ledger of every real Gnani call → `gnani_usage.jsonl` | 51 |
| `zachary/voice/factory.py` | Builds everything (`VoiceKit`) from settings + key | 54 |
| `zachary/ui/voice.py` | `VoiceController`: connects listener ↔ bar/engine ↔ speaker; latency log | 214 |
| `zachary/core/platform/linux_portal.py` | Global hotkeys on Wayland: **one** portal session for both keys | 239 |
| `zachary/telemetry/latency.py` | `Timeline`: per-stage timings | – |
| `zachary/cli.py` (`cmd_voice`) | `voice usage / setup / mics / latency / transcribe / say / listen` | – |
| `tests/gnani_mock.py` | Fake Gnani server (HTTP + WebSocket) used by every test | 239 |
| `tests/test_voice.py` | ~50 voice tests | 863 |
| `bench/gnani_smoke.py` | **Real** API smoke test (3 calls, needs `--yes`) | – |

---

## 4. How each part works

### 4.1 Push-to-talk session (`pipeline.py`: `_ptt_session`, `_ptt_listen`)
1. The key press sets an event, and the listener thread wakes up.
2. `source.start()` opens the mic. A background thread opens the Gnani stream at the same time.
3. Loop, roughly every 30 ms:
   - read a mic frame and keep it in `audio` (for fallback);
   - feed frames to Gnani. Frames captured while connecting are sent at 2× speed until caught up;
   - read server events:
     - `speech_start`: counts one open segment.
     - `transcript`: adds the text and closes a segment.
     - `error` or `closed`: the stream failed, so fall back.
   - **Done** when every started segment has a transcript and nothing new began for 0.2 s.
4. Other endings:

   | Event | What happens |
   |---|---|
   | Key pressed again | `commit` (finalize now) |
   | Nothing said for 6 s | "I didn't hear anything (mic X, level Y)" |
   | 15 s total | commit |
   | Local VAD heard speech but Gnani never started a segment | commit after 1 s |

5. Fallbacks, in order:
   - Gnani streaming fails → **Gnani REST** on the recorded audio;
   - that fails too → **whisper** locally (lazy-loaded).
6. Mic and socket are always closed. Any exception is logged and the listener goes back to idle; it never dies.

Tunables at the top of `pipeline.py`:

| Constant | Value |
|---|---|
| `NO_SPEECH_S` | 6.0 |
| `SEGMENT_GRACE_S` | 0.2 |
| `COMMIT_WAIT_S` | 3.0 |
| `SERVER_LAG_S` | 1.0 |
| `MAX_COMMAND_S` | 15.0 |

### 4.2 Gnani streaming STT (`gnani_stt.py`: `GnaniStreamingSTT`)
- URL `wss://api.vachana.ai/stt/v3/stream`. All config goes in the headers:

| Header | Value | Why |
|---|---|---|
| `x-api-key-id` | your key | auth |
| `language_code` | `en-IN` | also handles Hinglish, in Latin script |
| `model` + `x-model` | `prisma-v3.0` | an unknown value silently falls back to v2.5, so both names are sent |
| `x-vad-threshold` | `0.5` | docs default 0.7; earbud mics are quiet |
| `x-min-silence-ms` | `450` | how long a pause ends speech; the main latency knob |
| `x-max-speech-s` | `15` | cap |
| `x-idle-timeout-sec` | `15` | server closes idle sockets |

- A receiver thread puts every server message on `stream.events`.
- Observed messages: `connected`, `speech_start`, `transcript` (with a `latency` ms field). The docs also list
  `processing`, but **it was never seen** in real runs.
- `feed()` sends exact 1,024-byte frames. `commit()` finalizes. `close()` always ends the session (Gnani limits
  concurrent sessions) and writes one ledger row.

### 4.3 Gnani TTS (`gnani_tts.py`: `GnaniWsTTS`)
- URL `wss://api.vachana.ai/api/v1/tts`.
- Request: `{text, voice: Shivani, model: timbre-v3.0, language: en-IN, audio_config: {sample_rate: 24000, container: raw}}`.
- **One socket for all replies.** `warm()` connects at startup, and each reply reuses it. If the server closed it,
  it reconnects once, but only if nothing was synthesized yet, so you're never charged twice.
- If a reply is interrupted midway, the socket is dropped; otherwise its leftover audio would play with the next reply.
- Chunks are cut to whole 16-bit samples.
- `speakable()` turns file names into speech: "Fee_Receipt_Sem5.pdf" becomes "Fee Receipt Sem five".

### 4.4 Speaking (`ui/voice.py`) and playback (`audio.py`: `Player`)
- One **speaker thread** with a queue. A new reply (or a key press) cancels the current one.
- `Player`: one long-lived thread and output stream.
  - Every utterance has a generation number; `stop()` bumps it and clears the queue.
  - Audio is written in 50 ms blocks, so stopping is near instant.
  - The output stream closes after 10 s of silence.

### 4.5 Microphone and Bluetooth (`audio.py`: `auto_input`; `pipeline.py`: `MicSource`)
The mic is chosen **again at every command**, so it follows whatever you use. Plugging in wired or USB
earphones, connecting Bluetooth earbuds, or unplugging them needs no setting. The rules, in order:

1. **The mic of the device you're listening on.** The default output's own mic: earbuds → earbud mic;
   a USB or wired headset → its mic; laptop speakers → laptop mic. Speaker and mic are paired by
   `device_key()` (the Bluetooth MAC, or the USB/PCI card id).
2. Otherwise **the system default input**, if it's a real mic.
3. Otherwise any real mic, headsets first.

Never picked: speaker monitors, virtual sources (e.g. the `zach_virtual_mic` test feed), and mics
**found dead**. A mic that gives exact digital silence for 3 s is switched away from during the command and
remembered, so it's skipped from then on. This laptop's built-in AMD mic is such a case.

- **`mic_device` setting:** `""` (default) means automatic. A hand-picked mic that has since been unplugged
  falls back to automatic instead of failing. `cli voice mics` lists mics with live levels, `[virtual]` and
  `[selected]`; `cli voice mics default` goes back to automatic.
- **Bluetooth earbuds:** their mic only works in headset (HFP) mode, which makes music sound like a call.
  - On press: switch to headset mode.
  - After the mic closes: switch back **8 s later**, so quick follow-ups don't flip it each time (a flip takes 1–3 s).
- Routing env vars (`PIPEWIRE_NODE`, …) are set only while the input stream opens, so playback is never sent to a mic.
- Playback goes to the system's default output; PipeWire moves it when you switch devices.
- **Windows:** there's no PipeWire, so the mic is the Windows default input, which Windows switches by
  itself when a headset connects. A device number from `voice mics` pins one.

### 4.6 Hotkeys (`linux_portal.py`)
- GNOME binds only one portal session per app, so both shortcuts (`toggle` = Super+Space, `voice` =
  Super+Alt+Space) are bound in **one** session.
- Registrations within 0.4 s are batched. A later one rebinds everything.
- The first time, GNOME shows a dialog to approve the keys.
- Ctrl+M works anywhere inside the bar.

---

## 5. Settings (`python -m zachary.cli set <key> <value>`)

| Key | Default | Meaning |
|---|---|---|
| `voice_enabled` | false | turn voice on |
| `wake_word` | false | "Hey Zach" (local whisper, mic always open) |
| `voice_hotkey` | super+alt+space | push-to-talk key |
| `stt_streaming` | true | false = REST: about +0.3–0.5 s, but whole-clip accuracy |
| `stt_language` | en-IN | `hi-IN`, `kn-IN`, … or `indic-auto` |
| `stt_model` | prisma-v3.0 | |
| `tts_voice` / `tts_model` / `tts_language` | Shivani / timbre-v3.0 / en-IN | |
| `speak_replies` | true | false = text only (saves TTS credits) |
| `mic_device` | "" | see `cli voice mics` |
| `offline_stt_model` | tiny | whisper model for fallback / wake word |

The key `GNANI_API_KEY` goes in `.env` in the repo root (git-ignored).

---

## 6. Measured

**Real API, push-to-talk path** (bench, 2 Nov):

| Stage | ms |
|---|---|
| socket open | 256–288 |
| Gnani transcript latency (server) | 217–393 |
| intent + search | 14–18 |
| TTS first audio (warm socket) | 304–317 |
| **end of speech → first reply audio** | **551–558** (was 3,000–5,000 before the redo) |

**Your real runs** (`cli voice latency`), all through the old "Hey Zach" path:

| time | wake | speech_end | stt | intent | tts first | result |
|---|---|---|---|---|---|---|
| 00:42 | 1,274 | 1,372 | 4,017 | – | – | empty |
| 00:45 | 1,314 | 3,674 | 1,278 | 26 | 723 | find ✔ |

Those runs show:
- The empty result waited 4 s. The old path waits up to 3 s after its own commit.
- The mic level was 0.0046 on the empty one and 0.22 on the good one, so the earbud mic level varies a lot.

**API use so far** (`cli voice usage`): stt-rest 7 calls (24.5 s), stt-stream 19 (62.8 s), tts-rest 1, tts-ws 17 (341 chars).
That's well under 1% of your credits.

---

## 7. Open problems / what to work on next

### 7.1 Move "Hey Zach" onto the new flow (biggest gap)
`_utterance` → `_command` in `pipeline.py` is still the old code:
- local energy VAD decides the end;
- then `commit` + `finish()` waits up to 3 s.

Plan:
- After the wake word is found, run the same session as push-to-talk (`_ptt_listen`).
- Seed it with the audio after "Zach" (`buffered`) so the stream starts with it.
- Keep the one-breath REST path only as a fallback.

### 7.2 Streaming accuracy on short commands
- On a robotic espeak clip ("open the fee receipt"), **streaming** returned "Open." or "Oh, and he received."
  **REST** on the same clip returned "Open the fee receipt".
- Tried, with no change: format `transcribe`, minimal headers, no VAD tuning.
- Real voices streamed correctly so far. This needs testing with your voice.

Options:
1. `stt_streaming false` (REST: always the whole clip, about +0.3–0.5 s);
2. hybrid: stream for fast end-of-speech, then REST on the captured clip only when the stream text looks
   wrong (no intent found). That costs a second call only then;
3. `keyterms` header (Prisma v3): common command words and file names.

### 7.3 Quiet / variable earbud mic
- Peak levels seen: 0.0046 to 0.22.
- Server VAD threshold is 0.5 now. If commands get missed, try 0.4 (`vad_threshold` in `GnaniStreamingSTT`).
- An input gain setting may also help.

### 7.4 Hinglish / Kannada by voice
- Not tested with a real voice yet. Try:
  - "kal wali fees file kholo"
  - "nenne open madida file"
- `en-IN` returns Latin script. For Kannada, `stt_language kn-IN` returns Kannada script, which the parser
  partly handles. `format: transliterate` (Latin output) is an option to evaluate.

### 7.5 Speaking the right language
- TTS always uses `en-IN`. For Hinglish replies, the docs offer `language: hi-en`.

### 7.6 Smaller items
- The `processing` event (end of speech) never arrives, so the timeline marks `speech_end` when the transcript
  arrives. To find the real end of speech, use the local VAD time.
- Wake word "Hey Jack" false positives (whisper tiny). A trained openWakeWord model is an option for later.
- The Player keeps the output device open for 10 s after a reply; check that it doesn't keep earbuds in a bad state.
- `audio_out_ms` (press → sound actually playing) is logged but not shown in `cli voice latency`.

---

## 8. How to run and test

```bash
# run
pkill -9 -f zachary
python -m zachary.cli set voice_enabled true
python -m zachary.cli set wake_word false          # new push-to-talk path
python -m zachary.assistant -v
#   Super+Alt+Space -> "open fee receipt" -> pause
#   press again     -> "show my projects"

# look
python -m zachary.cli voice latency                 # stages of the last 10 commands
python -m zachary.cli voice usage                   # Gnani calls so far
python -m zachary.cli voice mics                    # mics + live level (speak while it runs)

# tests (Gnani is mocked; the real host is blocked in tests/conftest.py)
pytest tests/test_voice.py tests/test_hotkeys.py
ZACHARY_TEST_WHISPER=1 pytest tests/test_voice.py   # + real local whisper tests

# real API (costs credits, 3 calls)
python bench/gnani_smoke.py --yes
```

Test names worth knowing (in `tests/test_voice.py`):

| Test | Covers |
|---|---|
| `test_server_vad_ends_the_command_without_commit` | the normal push-to-talk path |
| `test_two_push_to_talk_commands_in_a_row` | the "voice doesn't activate again" bug |
| `test_a_pause_mid_command_keeps_both_halves` | the "Open." split-segment bug |
| `test_player_stop_when_idle_does_not_eat_the_next_reply` | the "reply arrives late" bug |
| `test_stream_failure_falls_back_to_rest` | fallbacks |
| `test_push_to_talk_twice_through_the_assistant` | end to end through the real bar and engine |

Gnani docs used: <https://docs.gnani.ai/llms.txt> (index), `api/STT/stt-websocket.md`, `api/TTS/tts-websocket.md`,
`api/STT/limits.md`, `api/STT/models/prisma-v3.md`, `api/introduction/changelog.md`.
