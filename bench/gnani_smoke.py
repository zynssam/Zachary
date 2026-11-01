"""Real-API smoke test for Gnani. COSTS QUOTA: 7 calls, all short. Run deliberately.

    python bench/gnani_smoke.py --yes [--db ~/.local/share/zachary/zachary.db]

Calls (each recorded in <data dir>/gnani_usage.jsonl):
  1 STT REST    English command (~3 s, espeak voice)
  2 STT REST    Hinglish phrase (~2 s)
  3 STT stream  English command, paced in real time
  4 TTS REST    one short reply
  5 TTS WS      same reply (first-audio latency)
  6-7           "Hey Zach, open the fees document I opened yesterday" end to end:
                local wake word -> Gnani STT -> search (dry run: nothing opened)
                -> Gnani TTS
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from zachary.core.config import Config, default_db_path
from zachary.core.db import open_db
from zachary.core.env import get_secret
from zachary.voice.audio import FRAME_BYTES, pcm_to_wav, resample, seconds, wav_to_pcm
from zachary.voice.gnani_stt import GnaniRestSTT, GnaniStreamingSTT
from zachary.voice.gnani_tts import GnaniRestTTS, GnaniWsTTS
from zachary.voice.types import VoiceError
from zachary.voice.usage import UsageLedger

CMD = "open the fees document I opened yesterday"
HINGLISH = "kal wali fees file kholo"
REPLY = "Opening Fee Receipt Sem 5.pdf."


def espeak(text: str, out: Path) -> bytes:
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "145", "-w", str(out), text], check=True)
    pcm, sr = wav_to_pcm(out.read_bytes())
    return resample(pcm, sr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="really call the paid API (7 calls)")
    ap.add_argument("--only-e2e", action="store_true", help="just steps 6-7 (2 calls)")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="keep generated/received audio here")
    ap.add_argument("--ledger", type=Path, default=None, help="usage ledger (default: data dir)")
    args = ap.parse_args()
    print(__doc__)
    if not args.yes:
        print("dry run: add --yes to make the 7 real calls")
        return
    key = get_secret("GNANI_API_KEY")
    if not key or not shutil.which("espeak-ng"):
        sys.exit("need GNANI_API_KEY (.env) and espeak-ng")
    db = args.db or default_db_path()
    ledger = UsageLedger(args.ledger or default_db_path().parent / "gnani_usage.jsonl")
    out = args.out or Path(tempfile.mkdtemp(prefix="gnani-smoke-"))
    out.mkdir(parents=True, exist_ok=True)
    cmd_pcm = espeak(CMD, out / "cmd.wav")
    hin_pcm = espeak(HINGLISH, out / "hinglish.wav")

    def step(n: int, label: str, fn):  # type: ignore[no-untyped-def]
        t = time.perf_counter()
        try:
            res = fn()
            print(f"[{n}] {label}: {res}  ({(time.perf_counter() - t) * 1e3:.0f} ms)")
        except VoiceError as e:
            print(f"[{n}] {label}: ERROR {e.kind}: {e}  ({(time.perf_counter() - t) * 1e3:.0f} ms)")

    rest = GnaniRestSTT(key, ledger=ledger)
    if args.only_e2e:
        return e2e(args, db, key, rest, ledger, out)
    step(1, f"STT REST ({seconds(cmd_pcm):.1f}s)", lambda: repr(rest.transcribe(cmd_pcm).text))
    step(2, f"STT REST Hinglish ({seconds(hin_pcm):.1f}s)",
         lambda: repr(rest.transcribe(hin_pcm).text))  # fmt: skip

    def streamed() -> str:
        s = GnaniStreamingSTT(key, ledger=ledger)
        s.open()
        for i in range(0, len(cmd_pcm), FRAME_BYTES):
            s.feed(cmd_pcm[i : i + FRAME_BYTES])
            time.sleep(0.032)  # real-time pace, as the docs require
        t = time.perf_counter()
        tr = s.finish()
        return f"{tr.text!r} (text {((time.perf_counter() - t) * 1e3):.0f} ms after speech end)"

    step(3, "STT stream", streamed)

    def tts_rest() -> str:
        a = GnaniRestTTS(key, ledger=ledger).synthesize(REPLY)
        (out / "tts_rest.wav").write_bytes(pcm_to_wav(a.pcm, a.sample_rate))
        return f"{seconds(a.pcm, a.sample_rate):.2f}s of audio at {a.sample_rate} Hz"

    step(4, "TTS REST", tts_rest)

    def tts_ws() -> str:
        t = time.perf_counter()
        first = None
        chunks = []
        for c in GnaniWsTTS(key, ledger=ledger).stream(REPLY):
            first = first or time.perf_counter()
            chunks.append(c.pcm)
        pcm = b"".join(chunks)
        (out / "tts_ws.wav").write_bytes(pcm_to_wav(pcm, 24000))
        starts_riff = pcm[:4] == b"RIFF"
        ms = ((first or t) - t) * 1e3
        return f"{len(chunks)} chunks, {seconds(pcm, 24000):.2f}s, first audio {ms:.0f} ms, RIFF left in stream: {starts_riff}"  # noqa: E501

    step(5, "TTS WS", tts_ws)
    e2e(args, db, key, rest, ledger, out)


def e2e(args, db, key, rest, ledger, out) -> None:  # type: ignore[no-untyped-def]
    """6-7: end to end on the real index (dry run: actions are not executed)."""
    from zachary.brain.handler import Handler
    from zachary.search.semantic import local_semantic
    from zachary.voice.offline_stt import WhisperSTT
    from zachary.voice.pipeline import VoiceListener
    from zachary.voice.wakeword import WakeSpotter

    class DryRun:
        def open(self, item):  # type: ignore[no-untyped-def]
            print(f"     (would open {item.path})")
            return True

        def reveal(self, item):  # type: ignore[no-untyped-def]
            return True

    conn = open_db(db)
    cfg = Config(conn)
    whisper = WhisperSTT(Path(db).parent / "models" / "whisper", "tiny")
    utt = espeak("Hey Zach, " + CMD, out / "e2e.wav")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.test_voice import ListSource, silence

    got: list = []  # type: ignore[type-arg]
    lis = VoiceListener(ListSource(silence(0.5) + utt + silence(1.5)), on_heard=got.append,
                        rest_stt=rest, stream_stt=None, offline_stt=whisper,
                        spotter=WakeSpotter(whisper))  # fmt: skip
    lis.start()
    deadline = time.monotonic() + 30
    while not got and time.monotonic() < deadline:
        time.sleep(0.05)
    lis.stop()
    if not got:
        print("[6] e2e: wake word not detected")
        return
    h = got[0]
    extra = h.transcript.extra if h.transcript else {}
    print(f"[6] heard via {h.transcript.provider if h.transcript else '?'}: {h.text!r}  "
          f"(round trip {h.transcript.latency_ms if h.transcript else 0:.0f} ms, "
          f"Gnani server {extra.get('server_s')} s)")  # fmt: skip
    handler = Handler(conn, DryRun(), cfg, lambda: local_semantic(conn, cfg))
    reply = handler.submit(h.text)
    h.timeline.mark("intent_search")
    print(f"     zach: {reply.say}")
    t = time.perf_counter()
    first = None
    for _chunk in GnaniWsTTS(key, ledger=ledger).stream(reply.say):
        first = first or time.perf_counter()
    if first:
        h.timeline.mark("tts_first_audio", first)
    h.timeline.mark("tts_done")
    tts_ms = ((first or t) - t) * 1e3
    print(f"[7] e2e latency: {h.timeline.durations()}  (tts first audio {tts_ms:.0f} ms)")
    print(f"\naudio kept in {out}\nledger: {ledger.summary()}")


if __name__ == "__main__":
    main()
