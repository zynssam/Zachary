"""Voice: Gnani clients (against a local mock), VAD, wake word, pipeline, UI.

The real Gnani API is never called here.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.fakes import FakePlatform, wait_until, write
from tests.gnani_mock import KEY, MockGnani, start_mock
from zachary.core.platform import RecentItem
from zachary.indexer.recent import ingest_recent
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.voice.audio import FRAME_BYTES, EnergyVAD
from zachary.voice.gnani_stt import GnaniRestSTT, GnaniStreamingSTT
from zachary.voice.gnani_tts import GnaniRestTTS, GnaniWsTTS, speakable
from zachary.voice.offline_stt import Word
from zachary.voice.pipeline import Heard, VoiceListener
from zachary.voice.types import SAMPLE_RATE, Transcript, VoiceError
from zachary.voice.usage import UsageLedger
from zachary.voice.wakeword import WakeResult, find_wake


@pytest.fixture
def gnani() -> Iterator[MockGnani]:
    yield from start_mock()


@pytest.fixture
def ledger(tmp_path: Path) -> UsageLedger:
    return UsageLedger(tmp_path / "gnani_usage.jsonl")


def speech(seconds: float, seed: int = 0) -> bytes:
    """Speech-like noise (loud enough for the VAD)."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SAMPLE_RATE)) * 6000).astype(np.int16).tobytes()


def silence(seconds: float) -> bytes:
    rng = np.random.default_rng(99)
    return (rng.standard_normal(int(seconds * SAMPLE_RATE)) * 15).astype(np.int16).tobytes()


# -- clients -----------------------------------------------------------------------------
def test_rest_stt(gnani: MockGnani, ledger: UsageLedger) -> None:
    stt = GnaniRestSTT(KEY, url=gnani.stt_url, ledger=ledger)
    t = stt.transcribe(speech(1.0))
    assert t.text == gnani.transcript and t.provider == "gnani-rest"
    call = gnani.calls[0]
    assert call["key"] == KEY and call["wav"]
    assert call["fields"] == {"language_code": "en-IN", "model": "prisma-v3.0"}
    assert ledger.summary()["stt-rest"]["calls"] == 1


@pytest.mark.parametrize(
    ("status", "kind"), [(402, "credits"), (429, "rate_limited"), (500, "server")]
)
def test_errors_are_classified_and_never_retried(
    gnani: MockGnani, ledger: UsageLedger, status: int, kind: str
) -> None:
    gnani.fail_status = status
    with pytest.raises(VoiceError) as e:
        GnaniRestSTT(KEY, url=gnani.stt_url, ledger=ledger).transcribe(speech(0.5))
    assert e.value.kind == kind
    assert gnani.count("stt-rest") == 1  # one request, no retry (limited quota)
    assert ledger.summary()["stt-rest"]["errors"] == 1


def test_bad_key_and_network(gnani: MockGnani) -> None:
    with pytest.raises(VoiceError) as e:
        GnaniRestSTT("wrong", url=gnani.stt_url).transcribe(speech(0.5))
    assert e.value.kind == "auth"
    with pytest.raises(VoiceError) as e:
        GnaniRestSTT(KEY, url="http://127.0.0.1:9/stt/v3", timeout=2).transcribe(speech(0.5))
    assert e.value.kind == "network" and e.value.offline
    with pytest.raises(VoiceError) as e:
        GnaniRestSTT("")
    assert e.value.kind == "config"
    with pytest.raises(VoiceError):
        GnaniRestSTT(KEY, url=gnani.stt_url).transcribe(speech(31))  # REST limit is 30 s
    assert gnani.count("stt-rest") == 1  # the 31 s clip was refused locally


def test_streaming_stt_frames_and_commit(gnani: MockGnani, ledger: UsageLedger) -> None:
    s = GnaniStreamingSTT(KEY, url=gnani.stt_ws, ledger=ledger)
    s.open()
    audio = speech(1.0)
    for i in range(0, len(audio), 700):  # odd sizes: the client re-frames to 1,024 bytes
        s.feed(audio[i : i + 700])
    t = s.finish()
    assert t.text == gnani.transcript
    call = gnani.calls[0]
    assert call["bad_frames"] == 0 and call["frames"] >= len(audio) // FRAME_BYTES
    assert call["committed"] and call["headers"]["language_code"] == "en-IN"
    h = call["headers"]  # server-side VAD tuned for commands, model pinned both ways
    assert h["model"] == h["x-model"] == "prisma-v3.0"
    assert h["x-vad-threshold"] == "0.5" and h["x-min-silence-ms"] == "400"
    assert ledger.summary()["stt-stream"]["audio_s"] == pytest.approx(1.0, abs=0.05)


def test_streaming_stt_rejects_bad_key(gnani: MockGnani) -> None:
    with pytest.raises(VoiceError) as e:
        GnaniStreamingSTT("nope", url=gnani.stt_ws).open()
    assert e.value.kind == "auth"


def test_tts_rest_and_ws(gnani: MockGnani, ledger: UsageLedger) -> None:
    a = GnaniRestTTS(KEY, url=gnani.tts_url, ledger=ledger).synthesize("Opening report_final.pdf")
    assert a.sample_rate == 24000 and len(a.pcm) > 1000
    req = gnani.calls[0]["request"]
    assert req["text"] == "Opening report final" and req["voice"] == "Shivani"
    assert req["model"] == "timbre-v3.0" and req["audio_config"]["encoding"] == "linear_pcm"
    chunks = list(GnaniWsTTS(KEY, url=gnani.tts_ws, ledger=ledger).stream("Hello"))
    assert len(chunks) == 3
    joined = b"".join(c.pcm for c in chunks)
    assert all(len(c.pcm) % 2 == 0 for c in chunks)  # whole samples despite odd splits
    assert not joined.startswith(b"RIFF")
    assert gnani.calls[-1]["request"]["audio_config"]["container"] == "raw"
    assert len(joined) == int((0.3 + 0.1 * len("Hello".split())) * 24000) * 2
    assert set(ledger.summary()) == {"tts-rest", "tts-ws"}


def test_tts_ws_error(gnani: MockGnani) -> None:
    gnani.fail_status = 402
    with pytest.raises(VoiceError):
        list(GnaniWsTTS(KEY, url=gnani.tts_ws).stream("Hello"))


def test_tts_reuses_one_connection(gnani: MockGnani, ledger: UsageLedger) -> None:
    tts = GnaniWsTTS(KEY, url=gnani.tts_ws, ledger=ledger)
    tts.warm()
    for text in ("Opening fee receipt.", "Found three."):
        assert b"".join(c.pcm for c in tts.stream(text))
    assert tts.connects == 1  # no handshake per reply
    conns = [c for c in gnani.calls if c["endpoint"] == "tts-conn"]
    assert len(conns) == 1 and conns[0]["requests"] == 2
    assert ledger.summary()["tts-ws"]["calls"] == 2  # one ledger row per reply
    tts.close()


def test_tts_replies_never_shift_by_one(gnani: MockGnani) -> None:
    """Real bug: Gnani ends each reply with *two* "complete" messages. Stopping at the
    first left one on the socket; the next reply read it (silence) and the reply after
    played the previous answer. Each reply must get exactly its own audio."""
    tts = GnaniWsTTS(KEY, url=gnani.tts_ws)
    texts = ["Found three.", "Opening fee receipt sem five now.", "Two projects."]
    lengths = [len(b"".join(c.pcm for c in tts.stream(t))) for t in texts * 2]
    expect = [int((0.3 + 0.1 * len(speakable(t).split())) * 24000) * 2 for t in texts * 2]
    assert lengths == expect and tts.connects == 1


def test_tts_ignores_stale_messages_before_start(gnani: MockGnani) -> None:
    import json as _json

    tts = GnaniWsTTS(KEY, url=gnani.tts_ws)
    tts.warm()
    # Something left over on the socket (e.g. an interrupted earlier exchange).
    gnani_stale = {"type": "audio", "data": {"audio": "AAAA", "chunk_index": 9}}
    tts._ws.send(_json.dumps({"text": "x", "voice": "Shivani", "model": "timbre-v3.0",
                              "language": "en-IN", "audio_config": {"container": "raw"}}))  # fmt: skip
    time.sleep(0.3)  # its reply is now waiting unread
    out = b"".join(c.pcm for c in tts.stream("Found three."))
    assert len(out) == int((0.3 + 0.1 * 2) * 24000) * 2  # only this reply's audio
    del gnani_stale


def test_tts_reconnects_when_the_server_closed_it(gnani: MockGnani) -> None:
    gnani.tts_close_after_request = True
    tts = GnaniWsTTS(KEY, url=gnani.tts_ws)
    assert list(tts.stream("one"))
    time.sleep(0.1)
    assert list(tts.stream("two"))  # the idle socket was gone: reconnect, not an error
    assert tts.connects == 2 and gnani.count("tts-ws") == 2  # each text synthesized once


def test_tts_abandoned_stream_is_not_reused(gnani: MockGnani) -> None:
    tts = GnaniWsTTS(KEY, url=gnani.tts_ws)
    it = tts.stream("first reply")
    next(it)
    it.close()  # type: ignore[attr-defined]  # a generator  # interrupted after one chunk: the rest must not leak into the next reply
    chunks = list(tts.stream("second reply"))
    assert tts.connects == 2 and len(b"".join(c.pcm for c in chunks)) > 10000


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("Opening Fee Receipt Sem 5.pdf.", "Opening Fee Receipt Sem five."),
        ("3 projects. Most recent: zachary.", "three projects. Most recent: zachary."),
        ("Opening budget_2024.xlsx", "Opening budget twenty twenty four"),
        ("Which one? I found 12 matches.", "Which one? I found twelve matches."),
        ("Showing “notes-v2.md” in its folder.", "Showing notes v two in its folder."),
    ],
)
def test_speakable(text: str, spoken: str) -> None:
    assert speakable(text) == spoken


# -- VAD / wake ----------------------------------------------------------------------------
def test_vad_start_and_end() -> None:
    vad = EnergyVAD()
    pcm = silence(0.6) + speech(1.0) + silence(1.2)
    events = []
    for i in range(0, len(pcm), FRAME_BYTES):
        ev = vad.push(pcm[i : i + FRAME_BYTES])
        if ev:
            events.append((ev.kind, round(i / 2 / SAMPLE_RATE, 2)))
    kinds = [k for k, _ in events]
    assert kinds == ["start", "end"]
    assert 0.55 <= events[0][1] <= 0.75 and 2.1 <= events[1][1] <= 2.3  # 1.6 s + 0.6 s of quiet


@pytest.mark.parametrize(
    ("words", "found"),
    [
        (["Hey", "Zach,", "open", "my", "fees"], True),
        (["hey", "zack", "open"], True),
        (["Zach,", "open", "it"], True),
        (["Okay", "Zach."], True),
        (["I", "think", "Zach", "is", "late"], False),  # mentioned, not addressed
        (["Hey", "Mac,", "open"], False),
        (["open", "my", "notes"], False),
    ],
)
def test_find_wake(words: list[str], found: bool) -> None:
    ws = [Word(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(words)]
    r = find_wake(ws)
    assert r.found == found
    if found:
        idx = next(i for i, w in enumerate(words) if w.lower().strip(",.") in {"zach", "zack"})
        assert r.cut_s == pytest.approx(ws[idx].end_s)


# -- pipeline ------------------------------------------------------------------------------
class ListSource:
    """Feeds prepared audio as 1,024-byte frames, then near-silence."""

    def __init__(self, pcm: bytes, realtime: bool = False) -> None:
        self.q: queue.Queue[bytes] = queue.Queue()
        self.realtime = realtime
        self.load(pcm)

    def load(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), FRAME_BYTES):
            f = pcm[i : i + FRAME_BYTES]
            self.q.put(f + b"\0" * (FRAME_BYTES - len(f)))

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def get(self, timeout: float) -> bytes | None:
        try:
            f = self.q.get_nowait()
        except queue.Empty:
            time.sleep(min(timeout, 0.01))
            return silence(FRAME_BYTES / 2 / SAMPLE_RATE)
        if self.realtime:
            time.sleep(0.002)
        return f


class ZeroSource(ListSource):
    """A 'microphone' that delivers exact digital silence (a dead or virtual input)."""

    def __init__(self) -> None:
        super().__init__(b"")

    def get(self, timeout: float) -> bytes | None:
        time.sleep(0.001)
        return b"\0" * FRAME_BYTES


class FakeSpotter:
    """Says "Hey Zach" was heard, ending 0.5 s into the utterance."""

    def __init__(self, found: bool = True) -> None:
        self.found = found
        self.calls = 0

    def check(self, pcm: bytes) -> WakeResult:
        self.calls += 1
        return WakeResult(self.found, "hey zach" if self.found else "something else", 0.5)


class FakeOffline:
    name = "whisper"

    def __init__(self, text: str = "offline words") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        self.calls += 1
        return Transcript(self.text, self.name)


def _listen(source: ListSource, **kw: object) -> Heard:
    got: list[Heard] = []
    lis = VoiceListener(source, on_heard=got.append, **kw)  # type: ignore[arg-type]
    lis.start()
    try:
        if kw.get("wake_word") is False:
            lis.push_to_talk()
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    return got[0]


def test_one_breath_wake_command_uses_rest(gnani: MockGnani) -> None:
    src = ListSource(silence(0.5) + speech(2.5) + silence(1.5))
    h = _listen(
        src,
        rest_stt=GnaniRestSTT(KEY, url=gnani.stt_url),
        stream_stt=lambda: GnaniStreamingSTT(KEY, url=gnani.stt_ws),
        offline_stt=FakeOffline(),
        spotter=FakeSpotter(),
    )
    assert h.text == gnani.transcript and h.via == "wake"
    assert gnani.count("stt-rest") == 1 and gnani.count("stt-stream") == 0
    stages = h.timeline.durations()
    assert list(stages) == ["wake", "speech_end", "stt"]
    assert 1.5 <= h.audio_s <= 3.2  # audio after "Zach" only (cut at 0.5 s)


def test_not_addressed_sends_nothing(gnani: MockGnani) -> None:
    src = ListSource(silence(0.5) + speech(2.0) + silence(1.0))
    spotter = FakeSpotter(found=False)
    got: list[Heard] = []
    lis = VoiceListener(src, on_heard=got.append, rest_stt=GnaniRestSTT(KEY, url=gnani.stt_url),
                        stream_stt=None, offline_stt=FakeOffline(), spotter=spotter)  # type: ignore[arg-type]  # fmt: skip
    lis.start()
    wait_until(lambda: spotter.calls >= 1)
    time.sleep(0.5)
    lis.stop()
    lis.join(3)
    assert got == [] and gnani.calls == []  # privacy: no audio left the machine


def test_push_to_talk_streams_live(gnani: MockGnani) -> None:
    src = ListSource(b"")
    got: list[Heard] = []
    lis = VoiceListener(src, on_heard=got.append, rest_stt=GnaniRestSTT(KEY, url=gnani.stt_url),
                        stream_stt=lambda: GnaniStreamingSTT(KEY, url=gnani.stt_ws),
                        offline_stt=FakeOffline(), spotter=FakeSpotter(), wake_word=False)  # type: ignore[arg-type]  # fmt: skip
    lis.start()
    lis.push_to_talk()
    time.sleep(0.2)
    src.load(speech(1.5) + silence(1.2))
    wait_until(lambda: got, timeout=10)
    lis.stop()
    lis.join(3)
    h = got[0]
    assert h.text == gnani.transcript and h.via == "ptt"
    assert gnani.count("stt-stream") == 1 and gnani.count("stt-rest") == 0
    assert gnani.calls[0]["bad_frames"] == 0


@pytest.mark.parametrize("failure", ["network", "credits"])
def test_falls_back_to_local_whisper(gnani: MockGnani, failure: str) -> None:
    url = "http://127.0.0.1:9/stt/v3" if failure == "network" else gnani.stt_url
    if failure == "credits":
        gnani.fail_status = 402
    offline = FakeOffline("open the second one")
    src = ListSource(silence(0.3) + speech(2.0) + silence(1.2))
    h = _listen(src, rest_stt=GnaniRestSTT(KEY, url=url, timeout=2), stream_stt=None,
                offline_stt=offline, spotter=FakeSpotter())  # fmt: skip
    assert h.text == "open the second one" and offline.calls == 1
    assert h.transcript is not None and h.transcript.extra["fallback_reason"] == failure


# -- end to end through the assistant engine ----------------------------------------------
class StubPlayer:
    def __init__(self) -> None:
        self.pcm = b""
        self.ended = threading.Event()

    def play(self, pcm: bytes, sample_rate: int) -> None:
        self.pcm += pcm

    def end(self) -> None:
        self.ended.set()

    def stop(self) -> None: ...

    def wait(self, timeout: float = 30.0) -> None: ...


def test_done_when_hey_zach_open_fees_document_by_voice(
    qtbot: object, gnani: MockGnani, store: FileStore, home: Path, db_path: Path,
    fake_platform: FakePlatform, tmp_path: Path,
) -> None:  # fmt: skip
    """'Hey Zach, open the fees document I opened yesterday' -> opens it and says so."""
    from zachary.ui.engine import Engine
    from zachary.ui.quickbar import QuickBar
    from zachary.ui.voice import VoiceController
    from zachary.voice.factory import VoiceKit

    old = time.time() - 86400 * 100
    fee = write(home / "College" / "Fee Receipt Sem 5.pdf", mtime=old)
    write(home / "College" / "fee_structure_2024.pdf", mtime=old)
    write(home / "Work" / "report.docx", mtime=old)
    scan(store)
    y = (datetime.now() - timedelta(days=1)).replace(hour=14).timestamp()
    fake_platform.recent = [RecentItem(fee, y)]
    ingest_recent(store, fake_platform)

    engine = Engine(db_path, fake_platform, semantic_factory=lambda _c, _cfg: None)
    engine.start()
    bar = QuickBar(engine)
    kit = VoiceKit(
        rest_stt=GnaniRestSTT(KEY, url=gnani.stt_url),
        stream_stt=lambda: GnaniStreamingSTT(KEY, url=gnani.stt_ws),
        offline_stt=FakeOffline(),
        spotter=FakeSpotter(),  # type: ignore[arg-type]
        tts=GnaniWsTTS(KEY, url=gnani.tts_ws),
        ledger=UsageLedger(None),
        has_key=True,
    )
    player = StubPlayer()
    latency = tmp_path / "latency.jsonl"
    voice = VoiceController(engine, bar, kit, latency_path=latency, player=player)  # type: ignore[arg-type]
    records: list[dict[str, object]] = []
    voice.spoken.connect(records.append)
    voice.start(ListSource(silence(0.4) + speech(3.0) + silence(1.5)))
    try:
        qtbot.waitUntil(lambda: bool(records), timeout=30000)  # type: ignore[attr-defined]
    finally:
        voice.stop()
        engine.stop()
    assert [p.name for p in fake_platform.opened] == ["Fee Receipt Sem 5.pdf"]
    assert bar.edit.text() == gnani.transcript
    assert bar.status.text() == "Opening Fee Receipt Sem 5.pdf."
    spoken = gnani.calls[-1]["request"]["text"]
    assert spoken == "Opening Fee Receipt Sem five."
    assert player.ended.is_set() and len(player.pcm) > 1000
    stages = dict(records[0]["stages_ms"])  # type: ignore[call-overload]
    assert list(stages) == ["wake", "speech_end", "stt", "intent_search", "tts_first_audio",
                            "tts_done"]  # fmt: skip
    assert latency.exists() and "intent_search" in latency.read_text()


# -- real local wake spotting (whisper.cpp + espeak-ng; no Gnani) -------------------------
def _espeak(text: str, out: Path) -> bytes:
    import shutil
    import subprocess

    from zachary.voice.audio import resample, wav_to_pcm

    if not shutil.which("espeak-ng"):
        pytest.skip("espeak-ng not installed")
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "140", "-w", str(out), text], check=True)
    pcm, sr = wav_to_pcm(out.read_bytes())
    return resample(pcm, sr)


real_whisper = pytest.mark.skipif(
    not __import__("os").environ.get("ZACHARY_TEST_WHISPER"),
    reason="set ZACHARY_TEST_WHISPER=1 (downloads the 75 MB whisper model)",
)


@pytest.fixture(scope="module")
def whisper():  # type: ignore[no-untyped-def]
    import os

    from zachary.voice.offline_stt import WhisperSTT

    models = Path(os.environ.get("ZACHARY_MODELS_DIR", Path.home() / ".cache" / "zachary-models"))
    return WhisperSTT(models / "whisper", "tiny")


@real_whisper
def test_real_wake_spotting(whisper, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from zachary.voice.wakeword import WakeSpotter

    spotter = WakeSpotter(whisper)
    yes = _espeak("Hey Zach, open my fees document", tmp_path / "a.wav")
    no = _espeak("I think we should order pizza tonight", tmp_path / "b.wav")
    r = spotter.check(yes)
    assert r.found and 0.3 < r.cut_s < 1.6, r
    assert not spotter.check(no).found


@real_whisper
def test_real_wake_then_command_through_pipeline(  # type: ignore[no-untyped-def]
    whisper, gnani: MockGnani, tmp_path: Path
) -> None:
    from zachary.voice.wakeword import WakeSpotter

    utt = _espeak("Hey Zach, open the fees document I opened yesterday", tmp_path / "c.wav")
    src = ListSource(silence(0.5) + utt + silence(1.5))
    h = _listen(src, rest_stt=GnaniRestSTT(KEY, url=gnani.stt_url), stream_stt=None,
                offline_stt=whisper, spotter=WakeSpotter(whisper))  # fmt: skip
    assert h.text == gnani.transcript and "wake" in h.timeline.durations()
    assert gnani.count("stt-rest") == 1


def test_tests_cannot_reach_the_real_api() -> None:
    """The conftest guard: a real-URL client fails locally, before any request."""
    ledger = UsageLedger(None)
    with pytest.raises(VoiceError) as e:
        GnaniRestSTT("not-a-key", ledger=ledger).transcribe(silence(0.5))
    assert e.value.kind == "network"


def test_silent_microphone_is_reported() -> None:
    """A monitor/virtual input gives exact zeros: say so instead of waiting forever."""
    states: list[str] = []
    lis = VoiceListener(ZeroSource(), on_heard=lambda _h: None,
                        on_state=states.append, rest_stt=None, stream_stt=None,
                        offline_stt=None, spotter=None)  # fmt: skip
    lis.start()
    lis.push_to_talk()
    deadline = time.monotonic() + 10
    while not any(s.startswith("error: the microphone is silent") for s in states):
        assert time.monotonic() < deadline, states
        time.sleep(0.05)
    lis.stop()
    lis.join(5)


LAPTOP_IN = "alsa_input.pci-0000_03_00.6.analog-stereo"
LAPTOP_OUT = "alsa_output.pci-0000_03_00.6.analog-stereo"
USB_IN = "alsa_input.usb-KTMICRO_JBL_TUNE_310C_USB-C_2020-02-20-00.mono-fallback"
USB_OUT = "alsa_output.usb-KTMICRO_JBL_TUNE_310C_USB-C_2020-02-20-00.analog-stereo"
BT_IN, BT_OUT = "bluez_input.88:D6:38:17:48:20", "bluez_output.88:D6:38:17:48:20.1"
VIRTUAL = {"name": "zach_virtual_mic", "description": "VoiceSender_mic",
           "properties": {"node.virtual": "true", "device.class": "filter"}}  # fmt: skip


def _system(monkeypatch: pytest.MonkeyPatch, inputs: list[object], default_in: str,
            default_out: str) -> None:  # fmt: skip
    from zachary.voice import audio

    srcs = [x if isinstance(x, dict) else {"name": x, "description": x} for x in inputs]
    monkeypatch.setattr(audio, "_pactl_sources", lambda: (srcs, default_in))
    monkeypatch.setattr(audio, "_pactl_default_sink", lambda: default_out)


def test_device_key_pairs_a_headsets_speaker_and_mic() -> None:
    from zachary.voice.audio import device_key

    assert device_key(BT_IN) == device_key(BT_OUT) == "88:D6:38:17:48:20"
    assert device_key(USB_IN) == device_key(USB_OUT)
    assert device_key(LAPTOP_IN) == device_key(LAPTOP_OUT) == "pci-0000_03_00.6"
    assert device_key(USB_IN) != device_key(LAPTOP_IN)


@pytest.mark.parametrize(
    ("default_in", "default_out", "expect"),
    [
        (LAPTOP_IN, BT_OUT, BT_IN),  # earbuds connected: talk into the earbuds
        (LAPTOP_IN, USB_OUT, USB_IN),  # wired/USB headset plugged in: its mic
        (USB_IN, LAPTOP_OUT, LAPTOP_IN),  # listening on laptop speakers: laptop mic
        ("bluez_output.x.monitor", "", BT_IN),  # default input is a monitor: a real mic
        (USB_IN, "some_hdmi_output", USB_IN),  # output has no mic: the default input
    ],
)
def test_auto_mic_follows_the_device_you_listen_on(
    monkeypatch: pytest.MonkeyPatch, default_in: str, default_out: str, expect: str
) -> None:
    from zachary.voice import audio

    _system(monkeypatch, [LAPTOP_IN, VIRTUAL, USB_IN, BT_IN], default_in, default_out)
    assert audio.auto_input() == expect
    assert audio.resolve_input("") == (None, expect)


def test_auto_mic_never_picks_virtual_or_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    from zachary.voice import audio

    _system(monkeypatch, [VIRTUAL, LAPTOP_IN, USB_IN], "zach_virtual_mic", LAPTOP_OUT)
    assert audio.auto_input() == LAPTOP_IN
    assert audio.auto_input(exclude={LAPTOP_IN}) == USB_IN  # laptop mic found dead
    assert audio.auto_input(exclude={LAPTOP_IN, USB_IN}) == ""  # nothing left: system default
    assert [d.virtual for d in audio.list_inputs()] == [True, False, False]


def test_manual_mic_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    from zachary.voice import audio

    _system(monkeypatch, [LAPTOP_IN, BT_IN], LAPTOP_IN, LAPTOP_OUT)
    assert audio.resolve_input("88:D6") == (None, BT_IN)  # part of a name
    assert audio.resolve_input(LAPTOP_IN) == (None, LAPTOP_IN)
    assert audio.resolve_input("3") == (3, "")  # a device number (Windows)


def test_mic_is_chosen_again_for_every_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plug in a headset between two commands: the next command uses it. A dead mic
    (digital silence) is skipped from then on."""
    from zachary.voice import audio, pipeline

    opened: list[str] = []

    class FakeStream:
        def __init__(self, device: str = "") -> None:
            self.device = device
            self.source_name = device
            self.active = False
            self.frames: queue.Queue[bytes] = queue.Queue()

        def start(self) -> None:
            self.active = True
            opened.append(self.device)

        def stop(self) -> None:
            self.active = False

        def drain(self) -> None: ...

    monkeypatch.setattr(pipeline, "MicStream", FakeStream)
    _system(monkeypatch, [LAPTOP_IN], LAPTOP_IN, LAPTOP_OUT)
    src = pipeline.MicSource()
    src.start()
    src.stop()
    _system(monkeypatch, [LAPTOP_IN, USB_IN], USB_IN, USB_OUT)  # JBL plugged in
    src.start()
    src.stop()
    _system(monkeypatch, [LAPTOP_IN, BT_IN], LAPTOP_IN, BT_OUT)  # JBL out, earbuds on
    src.start()
    assert opened == [LAPTOP_IN, USB_IN, BT_IN]
    _system(monkeypatch, [LAPTOP_IN, BT_IN], LAPTOP_IN, LAPTOP_OUT)
    src.stop()
    src.start()  # laptop speakers again -> laptop mic, but it's dead (zeros) ...
    assert src.try_next() == BT_IN  # ... so it switches and remembers
    src.stop()
    src.start()
    assert opened[-1] == BT_IN and src.dead == {LAPTOP_IN}
    del audio


def test_unplugged_manual_mic_falls_back_to_automatic(monkeypatch: pytest.MonkeyPatch) -> None:
    from zachary.voice import pipeline

    class FakeStream:
        def __init__(self, device: str = "") -> None:
            self.device = self.source_name = device
            self.active = False

        def start(self) -> None:
            if self.device == "gone-headset":
                raise RuntimeError("no such device")
            self.active = True

        def stop(self) -> None:
            self.active = False

    monkeypatch.setattr(pipeline, "MicStream", FakeStream)
    _system(monkeypatch, [LAPTOP_IN, USB_IN], USB_IN, USB_OUT)
    src = pipeline.MicSource(device="gone-headset")
    src.start()
    assert src.mic.device == USB_IN and src.mic.active


def test_silent_microphone_switches_to_another() -> None:
    """Laptop mic without a driver (zeros) -> the listener asks the source for the next mic."""

    class TwoMics(ZeroSource):
        switched = False

        def try_next(self) -> str | None:
            self.switched = True
            return "Bullets Z2"

    states: list[str] = []
    src = TwoMics()
    lis = VoiceListener(src, on_heard=lambda _h: None, on_state=states.append, rest_stt=None,
                        stream_stt=None, offline_stt=None, spotter=None)  # fmt: skip
    lis.start()
    lis.push_to_talk()
    deadline = time.monotonic() + 10
    while "info: using microphone Bullets Z2" not in states:
        assert time.monotonic() < deadline, states
        time.sleep(0.05)
    lis.stop()
    lis.join(5)
    assert src.switched and not any(s.startswith("error") for s in states)


# -- push-to-talk with the server's VAD ---------------------------------------------------
class CountingSource(ListSource):
    """Records how often the mic was opened/closed (push-to-talk opens it per command)."""

    def __init__(self, pcm: bytes = b"") -> None:
        super().__init__(pcm)
        self.starts = self.stops = 0
        self.active = False

    def start(self) -> None:
        self.starts += 1
        self.active = True

    def stop(self) -> None:
        self.stops += 1
        self.active = False


def _ptt_listener(
    gnani: MockGnani, src: ListSource, got: list[Heard], **kw: object
) -> VoiceListener:
    args: dict[str, object] = {
        "rest_stt": GnaniRestSTT(KEY, url=gnani.stt_url),
        "stream_stt": lambda: GnaniStreamingSTT(KEY, url=gnani.stt_ws),
        "offline_stt": FakeOffline(),
        "spotter": None,
        "wake_word": False,
    }
    args.update(kw)
    lis = VoiceListener(src, on_heard=got.append, **args)  # type: ignore[arg-type]
    lis.start()
    return lis


def test_command_ends_as_soon_as_you_stop(gnani: MockGnani) -> None:
    """Local end of speech (350 ms quiet) commits at once: no waiting for Gnani's timer."""
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        assert src.starts == 0  # mic closed until the key is pressed
        lis.push_to_talk()
        src.load(silence(0.3) + speech(1.2) + silence(1.0))
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    h = got[0]
    assert h.text == gnani.transcript and h.via == "ptt" and h.error is None
    call = gnani.calls[0]
    assert call["committed"] and h.timeline.info["ended_by"] == "local"
    assert call["bad_frames"] == 0
    assert src.starts == 1 and src.stops >= 1 and not src.active  # mic closed again
    stages = h.timeline.stages()
    assert stages[:2] == ["mic_open", "stream_open"] and "speech_start" in stages
    assert stages.index("speech_end") < stages.index("stt")
    assert h.timeline.info["gnani_latency_ms"] == [120]


def noise(seconds: float, rms: float) -> bytes:
    rng = np.random.default_rng(7)
    return (
        (rng.standard_normal(int(seconds * SAMPLE_RATE)) * rms * 32768).astype(np.int16).tobytes()
    )


def test_noisy_room_ends_by_gnanis_vad(gnani: MockGnani) -> None:
    """Background noise keeps the local detector 'in speech': Gnani's VAD ends it."""
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        lis.push_to_talk()
        # A long noise tail: Gnani's VAD (~0.4 s after the speech) must end it well before
        # the noise runs out and local silence could end it instead (slow CI runners).
        src.load(noise(0.3, 0.01) + speech(1.2) + noise(4.0, 0.01))
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    assert got[0].text == gnani.transcript
    if sys.platform == "win32" and os.environ.get("CI"):
        # The mock server's thread is sometimes not scheduled until the audio has run
        # out on GitHub's Windows runners (seen 3x), and then local silence ends it.
        # The code path is the same on every OS; the strict check runs on Linux.
        return
    assert "committed" not in gnani.calls[0] and "ended_by" not in got[0].timeline.info


def test_two_push_to_talk_commands_in_a_row(gnani: MockGnani) -> None:
    """The bug: after one command, voice never activated again."""
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        for n in (1, 2, 3):
            lis.push_to_talk()
            src.load(speech(0.8) + silence(0.8))
            wait_until(lambda n=n: len(got) == n, timeout=10)  # type: ignore[misc]
    finally:
        lis.stop()
        lis.join(3)
    assert [h.text for h in got] == [gnani.transcript] * 3
    assert gnani.count("stt-stream") == 3 and src.starts == 3 and not src.active


def test_a_pause_mid_command_keeps_both_halves(gnani: MockGnani) -> None:
    """Real smoke run: "open ... the fee receipt" came back as just "Open." because the
    second segment started before the first transcript arrived."""
    gnani.numbered_segments = True
    gnani.transcript_delay_s = 0.5
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        lis.push_to_talk()
        src.load(speech(0.6) + silence(0.55) + speech(0.6, seed=1) + silence(1.0))
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    assert got[0].text == f"{gnani.transcript} 1 {gnani.transcript} 2"


def test_hold_to_talk_records_until_release(gnani: MockGnani) -> None:
    """Holding the key: pauses don't end the command; letting go sends it all."""
    gnani.numbered_segments = True
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        lis.push_to_talk(hold=True)
        src.load(speech(0.8) + silence(1.2) + speech(0.8, seed=1) + silence(0.6))
        time.sleep(3.0)  # long pause in the middle: still recording
        assert got == []
        lis.release()
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    h = got[0]
    assert h.text == f"{gnani.transcript} 1 {gnani.transcript} 2"
    assert h.timeline.info["ended_by"] == "release" and gnani.calls[0]["committed"]


def test_hold_released_without_speech(gnani: MockGnani) -> None:
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        lis.push_to_talk(hold=True)
        time.sleep(0.5)
        lis.release()
        wait_until(lambda: got, timeout=5)
    finally:
        lis.stop()
        lis.join(3)
    assert got[0].error is not None and "didn't hear anything" in str(got[0].error)


def test_warm_mic_keeps_the_first_word(gnani: MockGnani) -> None:
    """Bar open: mic warm, last 0.6 s kept locally. The word said as the key goes down
    is sent; nothing at all goes to Gnani before the press."""

    class WarmSource(CountingSource):
        def keep_last(self, n: int) -> None:
            while self.q.qsize() > n:
                self.q.get_nowait()

    src = WarmSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        lis.set_warm(True)  # the bar opened
        wait_until(lambda: src.active)
        src.load(silence(2.0))  # the bar sits open for a while
        time.sleep(0.4)
        assert gnani.calls == []  # privacy: warm audio stays on the machine
        assert src.q.qsize() <= 20  # only ~0.6 s is kept
        first = speech(0.3, seed=3)
        src.load(first)  # "hackathon", said right as the key goes down
        lis.push_to_talk(hold=True)
        src.load(speech(0.6, seed=4) + silence(0.4))
        time.sleep(0.6)
        lis.release()
        wait_until(lambda: got, timeout=10)
        assert src.active and src.starts == 1  # still warm: the bar is still open
        lis.set_warm(False)  # the bar hid
        wait_until(lambda: not src.active)
    finally:
        lis.stop()
        lis.join(3)
    h = got[0]
    assert h.text == gnani.transcript and h.timeline.info["preroll_s"] == 0.6
    assert gnani.count("stt-stream") == 1


def test_second_press_commits(gnani: MockGnani) -> None:
    gnani.stream_vad = False  # the server never ends it by itself
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        lis.push_to_talk()
        src.load(speech(1.0))
        time.sleep(0.6)
        lis.push_to_talk()  # pressed again: "that's it"
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    assert got[0].text == gnani.transcript and gnani.calls[0]["committed"]


def test_nothing_said_reports_the_mic_and_level(
    gnani: MockGnani, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zachary.voice import pipeline

    monkeypatch.setattr(pipeline, "NO_SPEECH_S", 1.0)
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got)
    try:
        lis.push_to_talk()
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    h = got[0]
    assert h.text == "" and h.error is not None and "didn't hear anything" in str(h.error)
    assert "too quiet" in str(h.error) and float(h.timeline.info["mic_peak"]) > 0  # type: ignore[arg-type]
    assert gnani.count("stt-rest") == 0  # nothing said: nothing transcribed


@pytest.mark.parametrize("how", ["no_connection", "breaks_midway"])
def test_stream_failure_falls_back_to_rest(gnani: MockGnani, how: str) -> None:
    url = "ws://127.0.0.1:9/stt/v3/stream" if how == "no_connection" else gnani.stt_ws
    if how == "breaks_midway":
        gnani.stream_fail_after = 10
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got,
                        stream_stt=lambda: GnaniStreamingSTT(KEY, url=url, connect_timeout=2))  # fmt: skip
    try:
        lis.push_to_talk()
        src.load(silence(0.2) + speech(1.2) + silence(1.0))
        wait_until(lambda: got, timeout=15)
    finally:
        lis.stop()
        lis.join(3)
    assert got[0].text == gnani.transcript and got[0].transcript is not None
    assert got[0].transcript.provider == "gnani-rest" and gnani.count("stt-rest") == 1


def test_listener_survives_a_crash_in_a_command(gnani: MockGnani) -> None:
    class Exploding:
        name = "boom"

        def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
            raise RuntimeError("unexpected")

    src = CountingSource()
    got: list[Heard] = []
    states: list[str] = []
    lis = _ptt_listener(gnani, src, got, stream_stt=None, rest_stt=Exploding(),
                        offline_stt=None, on_state=states.append)  # fmt: skip
    try:
        lis.push_to_talk()
        src.load(speech(0.8) + silence(1.0))
        wait_until(lambda: any(s.startswith("error: voice command failed") for s in states))
        assert lis.is_alive() and src.stops == 1  # the mic was still closed
        lis.rest_stt = GnaniRestSTT(KEY, url=gnani.stt_url)
        lis.push_to_talk()
        src.load(speech(0.8) + silence(1.0))
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    assert got[0].text == gnani.transcript


# -- playback ---------------------------------------------------------------------------------
class FakeOut:
    def __init__(self) -> None:
        self.written = bytearray()
        self.opened = 0

    def open(self, sr: int) -> FakeOut:
        self.opened += 1
        return self

    def write(self, pcm: bytes) -> None:
        time.sleep(len(pcm) / 2 / 24000 / 20)  # "plays" 20x faster than real time
        self.written += pcm

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


def test_player_stop_when_idle_does_not_eat_the_next_reply() -> None:
    """The bug: stop() left a marker that made the next reply silent (it then came
    out with the following command, 'really late')."""
    from zachary.voice.audio import Player

    out = FakeOut()
    p = Player(open_stream=out.open)
    p.stop()  # push-to-talk pressed while nothing was playing
    p.stop()
    p.play(b"\1\0" * 4800, 24000)
    p.end()
    assert p.wait(5) and len(out.written) == 9600 and p.first_audio_at is not None
    p.play(b"\2\0" * 2400, 24000)  # next reply: same open stream, no new device open
    p.end()
    assert p.wait(5) and len(out.written) == 9600 + 4800 and out.opened == 1


def test_player_stop_drops_the_old_reply() -> None:
    from zachary.voice.audio import Player

    out = FakeOut()
    p = Player(open_stream=out.open)
    p.play(b"\1\0" * 240000, 24000)  # 10 s
    time.sleep(0.05)
    p.stop()
    p.play(b"\2\0" * 2400, 24000)
    p.end()
    assert p.wait(5)
    time.sleep(0.1)
    assert len(out.written) < 480000  # the old reply was cut short
    assert out.written.endswith(b"\2\0" * 2400)  # and the new one played in full


def test_player_keeps_the_speaker_awake_while_the_bar_is_open() -> None:
    """Bluetooth sleeps after a few quiet seconds and takes 1-3 s to wake: while the
    bar is open the player writes silence, so a reply starts on the open stream."""
    from zachary.voice.audio import Player

    out = FakeOut()
    p = Player(open_stream=out.open)
    p.keep_alive(True)
    time.sleep(0.3)
    assert out.opened == 1 and len(out.written) > 0 and not any(out.written)  # silence
    before: float | None = p.first_audio_at
    assert before is None  # silence isn't "the reply started"
    p.stop()
    p.play(b"\3\0" * 2400, 24000)
    p.end()
    assert p.wait(5) and p.first_audio_at is not None
    time.sleep(0.1)
    assert bytes(out.written).count(b"\3\0") == 2400 and out.opened == 1  # same stream
    p.keep_alive(False)
    time.sleep(0.1)  # a silence block already in flight may still finish
    n = len(out.written)
    time.sleep(0.3)
    assert len(out.written) == n  # no more silence once the bar is closed


def test_mic_routing_does_not_leak_to_playback(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from zachary.voice.audio import _routed_to

    monkeypatch.delenv("PIPEWIRE_NODE", raising=False)
    with _routed_to("bluez_input.88:D6"):
        assert os.environ["PIPEWIRE_NODE"] == "bluez_input.88:D6"
        assert "Communication" in os.environ["PIPEWIRE_PROPS"]
    assert "PIPEWIRE_NODE" not in os.environ and "PIPEWIRE_PROPS" not in os.environ


def test_bluetooth_profile_restored_after_a_grace_period(monkeypatch: pytest.MonkeyPatch) -> None:
    from zachary.voice import audio

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("shutil.which", lambda _n: None)  # no pactl: only our bookkeeping
    monkeypatch.setattr(audio, "_set_profile",
                        lambda card, prof: calls.append((card, prof)))  # fmt: skip
    audio._restore_later("bluez_card.88_D6", "a2dp-sink", delay=0.3)
    # A new command within the grace period: still in headset mode, nothing restored.
    assert audio._bt_headset_profile("bluez_input.88:D6") == ("bluez_card.88_D6", "a2dp-sink")
    time.sleep(0.5)
    assert calls == []
    audio._restore_later("bluez_card.88_D6", "a2dp-sink", delay=0.1)
    time.sleep(0.4)
    assert calls == [("bluez_card.88_D6", "a2dp-sink")]


# -- assistant: push-to-talk twice through the real engine and bar ---------------------------
def test_push_to_talk_twice_through_the_assistant(
    qtbot: object, gnani: MockGnani, store: FileStore, home: Path, db_path: Path,
    fake_platform: FakePlatform, tmp_path: Path,
) -> None:  # fmt: skip
    from zachary.ui.engine import Engine
    from zachary.ui.quickbar import QuickBar
    from zachary.ui.voice import VoiceController
    from zachary.voice.factory import VoiceKit

    write(home / "College" / "Fee Receipt Sem 5.pdf", mtime=time.time() - 86400 * 100)
    write(home / "code" / "app" / "pyproject.toml", mtime=time.time() - 3600)
    scan(store)
    engine = Engine(db_path, fake_platform, semantic_factory=lambda _c, _cfg: None)
    engine.start()
    bar = QuickBar(engine)
    tts = GnaniWsTTS(KEY, url=gnani.tts_ws)
    kit = VoiceKit(
        rest_stt=GnaniRestSTT(KEY, url=gnani.stt_url),
        stream_stt=lambda: GnaniStreamingSTT(KEY, url=gnani.stt_ws),
        offline_stt=FakeOffline(), spotter=FakeSpotter(),  # type: ignore[arg-type]
        tts=tts, ledger=UsageLedger(None), has_key=True,
    )  # fmt: skip
    out = FakeOut()
    from zachary.voice.audio import Player

    voice = VoiceController(engine, bar, kit, wake_word=False, latency_path=tmp_path / "l.jsonl",
                            player=Player(open_stream=out.open))  # fmt: skip
    records: list[dict[str, object]] = []
    voice.spoken.connect(records.append)
    src = CountingSource()
    voice.start(src)
    try:
        for n, said in ((1, "open fee receipt"), (2, "show my projects")):
            gnani.transcript = said
            voice.push_to_talk()
            src.load(speech(0.8) + silence(0.8))
            qtbot.waitUntil(lambda n=n: len(records) == n, timeout=15000)  # type: ignore[attr-defined]
    finally:
        voice.stop()
        engine.stop()
    assert [p.name for p in fake_platform.opened] == ["Fee Receipt Sem 5.pdf"]
    assert bar.edit.text() == "show my projects"
    assert tts.connects == 1  # warmed once, reused for both replies
    assert len(out.written) > 10000  # both replies actually played
    stages = list(dict(records[1]["stages_ms"]))  # type: ignore[call-overload]
    assert stages[:2] == ["mic_open", "stream_open"]
    assert stages[-3:] == ["intent_search", "tts_first_audio", "tts_done"]
    assert records[1]["audio_out_ms"] is not None and src.starts == 2


def test_tts_gives_up_when_audio_is_too_late(gnani: MockGnani, ledger: UsageLedger) -> None:
    """A flaky hotspot: a reply that would be spoken seconds late is dropped (the text is
    on screen), and the next reply still works and isn't mixed with the late one."""
    gnani.tts_first_audio_delay_s = 1.0
    tts = GnaniWsTTS(KEY, url=gnani.tts_ws, ledger=ledger, first_audio_timeout=0.3)
    t0 = time.monotonic()
    with pytest.raises(VoiceError, match="too slow"):
        list(tts.stream("Opening your file."))
    assert time.monotonic() - t0 < 0.9
    assert ledger.entries()[-1]["status"] == "late"
    gnani.tts_first_audio_delay_s = 0.0
    chunks = list(tts.stream("Hello"))
    assert chunks and sum(len(c.pcm) for c in chunks) > 0


def test_rest_fallback_only_gets_the_time_left(monkeypatch: pytest.MonkeyPatch) -> None:

    import httpx

    from zachary.voice import net

    seen: list[float] = []

    class SlowV4:
        def post(self, url: str, **kw: Any) -> httpx.Response:
            time.sleep(0.3)
            raise httpx.ConnectError("dns failed")

    def default_post(url: str, *, timeout: httpx.Timeout, **kw: Any) -> httpx.Response:
        seen.append(float(timeout.read or 0))
        return httpx.Response(200)

    monkeypatch.setattr(net, "http_client", lambda _t: SlowV4())
    monkeypatch.setattr(httpx, "post", default_post)
    assert net.post("https://x.test/", timeout=2.0).status_code == 200
    assert seen and seen[0] <= 1.75  # 2.0 total minus the 0.3 s already spent
    with pytest.raises(httpx.ConnectError):
        net.post("https://x.test/", timeout=1.2)  # under 1 s left: no second try


def test_segments_join_in_gnani_order_not_arrival_order() -> None:

    from zachary.voice.types import join_segments, segment_key

    arrived: list[dict[str, Any]] = [{"segment_index": 2, "text": "I opened yesterday"},
               {"segment_index": 1, "text": "open the fees document"},
               {"text": "please"}]  # fmt: skip
    parts = [(segment_key(m, i), str(m["text"])) for i, m in enumerate(arrived)]
    assert join_segments(parts) == "open the fees document I opened yesterday please"


def test_voice_dictation_rearms_until_stop(
    qtbot: object, gnani: MockGnani, store: FileStore, home: Path, db_path: Path,
    fake_platform: FakePlatform, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """ "make a document I'll dictate" -> the mic re-opens by itself after each piece ->
    "stop dictation" -> the document is written, then opened."""
    from zachary.actions import creator
    from zachary.ui import voice as voice_mod
    from zachary.ui.engine import Engine
    from zachary.ui.quickbar import QuickBar
    from zachary.ui.voice import VoiceController
    from zachary.voice.audio import Player
    from zachary.voice.factory import VoiceKit

    base = home / "Documents" / "Zachary"
    monkeypatch.setattr(creator, "base_folder", lambda: base)
    monkeypatch.setattr(voice_mod, "DICTATION_PAUSE_MS", 500)
    engine = Engine(db_path, fake_platform, semantic_factory=lambda _c, _cfg: None)
    engine.start()
    bar = QuickBar(engine)
    kit = VoiceKit(
        rest_stt=GnaniRestSTT(KEY, url=gnani.stt_url),
        stream_stt=lambda: GnaniStreamingSTT(KEY, url=gnani.stt_ws),
        offline_stt=FakeOffline(), spotter=FakeSpotter(),  # type: ignore[arg-type]
        tts=GnaniWsTTS(KEY, url=gnani.tts_ws), ledger=UsageLedger(None), has_key=True,
    )  # fmt: skip
    out = FakeOut()
    voice = VoiceController(engine, bar, kit, wake_word=False, latency_path=tmp_path / "l.jsonl",
                            player=Player(open_stream=out.open))  # fmt: skip
    records: list[dict[str, object]] = []
    voice.spoken.connect(records.append)
    src = CountingSource()
    voice.start(src)
    try:
        gnani.transcript = "make a document I'll dictate"
        voice.push_to_talk()
        src.load(speech(0.8) + silence(0.8))
        qtbot.waitUntil(lambda: voice.dictating and len(records) == 1, timeout=15000)  # type: ignore[attr-defined]
        # Nobody presses anything from here: the mic re-opens by itself.
        qtbot.waitUntil(lambda: src.starts == 2, timeout=5000)  # type: ignore[attr-defined]
        gnani.transcript = "the demo is on friday"
        src.load(speech(0.8) + silence(1.0))
        qtbot.waitUntil(lambda: len(records) == 2 and src.starts == 3, timeout=15000)  # type: ignore[attr-defined]
        assert not list(base.rglob("*.docx")) and not fake_platform.opened
        gnani.transcript = "stop dictation"
        src.load(speech(0.8) + silence(1.0))
        # Opened in the engine thread; the controller hears "done" when the reply lands.
        qtbot.waitUntil(lambda: bool(fake_platform.opened) and not voice.dictating,  # type: ignore[attr-defined]
                        timeout=15000)  # fmt: skip
    finally:
        voice.stop()
        engine.stop()
    (made,) = list(base.rglob("*.docx"))
    assert fake_platform.opened == [made] and not voice.dictating
    import docx

    assert [p.text for p in docx.Document(str(made)).paragraphs] == ["The demo is on friday."]


def test_keyterms_reach_gnani(gnani: MockGnani) -> None:
    """Your names go as Prisma v3 keyterms (stream header + REST form); approved setting."""
    names = lambda: ["Zachary", "Durdrsti"]  # noqa: E731
    stt = GnaniStreamingSTT(KEY, url=gnani.stt_ws, keyterms=names)
    stt.open()
    stt.feed(silence(0.2))
    stt.finish()
    hdr = gnani.calls[-1]["headers"]
    assert json.loads(hdr["keyterms"]) == ["Zachary", "Durdrsti"]
    GnaniRestSTT(KEY, url=gnani.stt_url, keyterms=names).transcribe(speech(0.5))
    assert json.loads(gnani.calls[-1]["fields"]["keyterms"]) == ["Zachary", "Durdrsti"]
    GnaniRestSTT(KEY, url=gnani.stt_url).transcribe(speech(0.5))
    assert "keyterms" not in gnani.calls[-1]["fields"]  # none when off


def test_keyterms_setting_off_sends_nothing(
    conn: Any, fake_platform: FakePlatform, tmp_path: Path
) -> None:
    from zachary.core.config import Config
    from zachary.voice import factory

    cfg = Config(conn, fake_platform)
    cfg.set_setting("stt_keyterms", False)
    kit = factory.build_voice(cfg, tmp_path, api_key="k")
    assert kit.rest_stt is not None and kit.rest_stt.keyterms is None  # type: ignore[attr-defined]
    cfg.set_setting("stt_keyterms", True)
    kit = factory.build_voice(cfg, tmp_path, api_key="k")
    assert isinstance(kit.rest_stt.keyterms, factory.Keyterms)  # type: ignore[union-attr]


# -- fast "Hey Zach" (Vosk) ---------------------------------------------------------------------
_REAL_MODELS = Path(os.path.expanduser("~")) / ".local" / "share" / "zachary" / "models"
_CLIPS = Path(os.path.expanduser("~")) / ".cache" / "zachary-bench" / "wake_clips.pkl"


class FakeFast:
    """Fires after ``after`` loud frames (stands in for Vosk hearing "Hey Zach")."""

    def __init__(self, after: int = 10) -> None:
        self.after, self.loud, self.resets, self.fired = after, 0, 0, 0

    def feed(self, frame: bytes) -> bool:
        x = np.frombuffer(frame, dtype=np.int16)
        self.loud = self.loud + 1 if x.size and np.abs(x).mean() > 500 else self.loud
        if self.loud >= self.after:
            self.fired += 1
            return True
        return False

    def reset(self) -> None:
        self.resets += 1
        self.loud = 0


def test_fast_wake_word_starts_a_command(gnani: MockGnani) -> None:
    """ "Hey Zach, open the fees" in one breath: Vosk fires right after "Zach", the rest
    is streamed to Gnani like push-to-talk (with ~0.3 s from just before the wake)."""
    src = CountingSource()
    got: list[Heard] = []
    fast = FakeFast(after=10)
    lis = _ptt_listener(gnani, src, got, wake_word=True, fast_spotter=fast)
    try:
        src.load(silence(0.5) + speech(1.5) + silence(1.0))
        wait_until(lambda: got, timeout=10)
    finally:
        lis.stop()
        lis.join(3)
    h = got[0]
    assert h.via == "wake" and h.text == gnani.transcript and fast.fired == 1
    assert float(h.timeline.info["preroll_s"]) > 0.25  # type: ignore[arg-type]  # kept
    assert fast.resets >= 2  # fresh after the wake and after the command
    assert gnani.calls[0]["endpoint"] == "stt-stream"  # nothing sent before the wake word


def test_no_wake_word_nothing_is_sent(gnani: MockGnani) -> None:
    src = CountingSource()
    got: list[Heard] = []
    lis = _ptt_listener(gnani, src, got, wake_word=True, fast_spotter=FakeFast(after=10**6))
    try:
        src.load(silence(0.3) + speech(1.5) + silence(1.0))
        time.sleep(2.5)
    finally:
        lis.stop()
        lis.join(3)
    assert not got and not gnani.calls  # talking without "Hey Zach" never reaches Gnani


@pytest.mark.skipif(
    not (_REAL_MODELS / "vosk" / "vosk-model-small-en-in-0.4" / "am").is_dir()
    or not _CLIPS.exists(),
    reason="needs the Vosk model (cli voice setup) and recorded clips (local only)",
)
def test_real_vosk_hears_hey_zach_not_hey_jack() -> None:
    import pickle

    from zachary.voice.wakeword import VoskSpotter

    clips: dict[str, bytes] = pickle.loads(_CLIPS.read_bytes())
    spot = VoskSpotter(_REAL_MODELS)
    assert spot.available()

    def fires(pcm: bytes) -> list[float]:
        spot.reset()
        out = []
        pcm = silence(0.5) + pcm + silence(0.5)
        for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            if spot.feed(pcm[i : i + FRAME_BYTES]):
                out.append(round(i / 32000 - 0.5, 2))
                spot.reset()
        return out

    t = time.perf_counter()
    wake = fires(clips["Hey Zach, open the fee receipt."])
    assert len(wake) == 1 and wake[0] < 1.2  # right after "Zach"
    assert fires(clips["okzach"])  # "Okay Zach"
    for no in ("Hey Jack, what's up?", "Open the zachary project.", "talk"):
        assert fires(clips[no]) == [], no
    audio_s = sum(len(c) for c in clips.values()) / 32000
    assert (time.perf_counter() - t) / audio_s < 0.2  # well under real time
    # The bug that made it deaf in the real app: listening continuously, after the
    # first sentence it never matched again. Talk, then "Hey Zach", in ONE stream:
    talk = clips["talk"] + silence(1.0)
    stream = talk + clips["Hey Zach, open the fee receipt."]
    got = fires(stream)
    start = len(talk) / 32000
    assert len(got) == 1 and start < got[0] < start + 1.5, got


class LaterMicSource(ListSource):
    """A dead mic at start; a working one "appears" (earbuds connected) later."""

    def __init__(self) -> None:
        super().__init__(b"")
        self.calls = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get(self, timeout: float) -> bytes | None:
        time.sleep(0.001)
        return b"\0" * FRAME_BYTES  # exact zeros: a mic with no driver

    def try_next(self) -> str | None:
        self.calls += 1
        return "bluez_input.earbuds" if self.calls >= 2 else None


def test_wake_mode_switches_to_a_mic_connected_later(gnani: MockGnani,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from zachary.voice import pipeline

    monkeypatch.setattr(pipeline, "RECHECK_FRAMES", 20)
    monkeypatch.setattr(pipeline, "SILENT_FRAMES", 20)
    src = LaterMicSource()
    states: list[str] = []
    fast: Any = FakeFast(after=10**6)
    lis = VoiceListener(src, on_heard=lambda _h: None, on_state=states.append,
                        rest_stt=None, stream_stt=None, offline_stt=FakeOffline(),
                        spotter=None, wake_word=True, fast_spotter=fast)  # fmt: skip
    lis.start()
    try:
        wait_until(lambda: any("using microphone bluez_input.earbuds" in s for s in states),
                   timeout=10)  # fmt: skip
    finally:
        lis.stop()
        lis.join(3)
    assert any("microphone is silent" in s for s in states) and src.calls >= 2
