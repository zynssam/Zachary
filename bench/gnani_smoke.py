"""Real-API smoke test for Gnani. COSTS QUOTA: 3 short calls. Run deliberately.

    python bench/gnani_smoke.py --yes [--db PATH] [--out DIR]

1. Push-to-talk command through the real listener: an espeak recording of
   "open the fee receipt" is fed at real-time pace into Gnani streaming STT, whose
   server VAD ends the command (1 stt-stream call). Checks the model in use.
2. The reply is spoken twice over ONE TTS WebSocket (2 tts-ws calls): the second
   reply shows the warm-connection latency.
3. Prints the per-stage timeline. Every call is recorded in gnani_usage.jsonl.
Nothing is opened (dry run).
"""

from __future__ import annotations

import argparse
import queue
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from zachary.core.config import Config, default_db_path
from zachary.core.db import open_db
from zachary.core.env import get_secret
from zachary.voice.audio import FRAME_BYTES, resample, wav_to_pcm
from zachary.voice.gnani_stt import GnaniStreamingSTT
from zachary.voice.gnani_tts import GnaniWsTTS
from zachary.voice.pipeline import Heard, VoiceListener
from zachary.voice.usage import UsageLedger

CMD = "open the fee receipt"


class RealTimeSource:
    """A 'microphone' playing a recording at real-time pace, then room noise."""

    def __init__(self, pcm: bytes) -> None:
        self.q: queue.Queue[bytes] = queue.Queue()
        for i in range(0, len(pcm), FRAME_BYTES):
            f = pcm[i : i + FRAME_BYTES]
            self.q.put(f + b"\0" * (FRAME_BYTES - len(f)))
        self.next_at = 0.0

    def start(self) -> None:
        self.next_at = time.monotonic()

    def stop(self) -> None: ...

    def get(self, timeout: float) -> bytes | None:
        delay = self.next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_at = max(self.next_at, time.monotonic() - 0.1) + 0.032
        try:
            return self.q.get_nowait()
        except queue.Empty:
            import numpy as np

            return (np.random.default_rng().standard_normal(512) * 20).astype(np.int16).tobytes()


def espeak(text: str, out: Path) -> bytes:
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(out), text], check=True)
    pcm, sr = wav_to_pcm(out.read_bytes())
    return resample(pcm, sr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="really call the paid API (3 calls)")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    print(__doc__)
    if not args.yes:
        print("dry run: add --yes to make the 3 real calls")
        return
    key = get_secret("GNANI_API_KEY")
    if not key or not shutil.which("espeak-ng"):
        sys.exit("need GNANI_API_KEY (.env) and espeak-ng")
    db = args.db or default_db_path()
    ledger = UsageLedger(default_db_path().parent / "gnani_usage.jsonl")
    out = args.out or Path(tempfile.mkdtemp(prefix="gnani-smoke-"))
    out.mkdir(parents=True, exist_ok=True)
    utt = espeak(CMD, out / "cmd.wav")

    from zachary.brain.handler import Handler
    from zachary.search.semantic import local_semantic

    conn = open_db(db)
    c = Config(conn)
    sem = local_semantic(conn, c)  # loaded first: the app has it warm

    tts = GnaniWsTTS(key, ledger=ledger)
    t = time.perf_counter()
    tts.warm()  # what the assistant does at startup
    print(f"TTS socket warm in {(time.perf_counter() - t) * 1e3:.0f} ms (free: no request)")

    streams: list[GnaniStreamingSTT] = []

    def make_stream() -> GnaniStreamingSTT:
        s = GnaniStreamingSTT(key, ledger=ledger)
        streams.append(s)
        return s

    got: list[Heard] = []
    lis = VoiceListener(RealTimeSource(utt), on_heard=got.append, rest_stt=None,
                        stream_stt=make_stream, offline_stt=None, spotter=None,
                        wake_word=False)  # fmt: skip
    lis.start()
    lis.push_to_talk()
    deadline = time.monotonic() + 20
    while not got and time.monotonic() < deadline:
        time.sleep(0.02)
    lis.stop()
    if not got:
        sys.exit("[1] no result within 20 s")
    h = got[0]
    cfg = streams[0].server_config if streams else {}
    print(f"[1] heard {h.text!r} (error: {h.error})  server config: {cfg}")
    print(f"    gnani latency field: {h.timeline.info.get('gnani_latency_ms')}")

    class DryRun:
        def open(self, item):  # type: ignore[no-untyped-def]
            print(f"    (would open {item.path})")
            return True

        def reveal(self, item):  # type: ignore[no-untyped-def]
            return True

    reply = Handler(conn, DryRun(), c, lambda: sem).submit(h.text or CMD)
    h.timeline.mark("intent_search")
    print(f"    zach: {reply.say}")
    for n in (1, 2):
        t = time.perf_counter()
        first = None
        for _chunk in tts.stream(reply.say):
            first = first or time.perf_counter()
        if n == 1 and first:
            h.timeline.mark("tts_first_audio", first)
            h.timeline.mark("tts_done")
        print(f"[2] TTS reply {n}: first audio {((first or t) - t) * 1e3:.0f} ms "
              f"(connects so far: {tts.connects})")  # fmt: skip
    tts.close()
    print(f"[3] timeline (ms since previous stage): {h.timeline.durations()}")
    print(
        f"    end of speech -> first reply audio: {h.timeline.total_ms(since='speech_end'):.0f} ms"
    )
    print(f"ledger: {ledger.summary()}")


if __name__ == "__main__":
    main()
