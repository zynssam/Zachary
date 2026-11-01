"""Voice: Gnani clients (against a local mock), VAD, wake word, pipeline, UI.

The real Gnani API is never called here.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

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
    assert not joined.startswith(b"RIFF")  # header stripped from the stream
    assert abs(len(joined) - len(a.pcm)) < 100  # same audio as the REST call
    assert set(ledger.summary()) == {"tts-rest", "tts-ws"}


def test_tts_ws_error(gnani: MockGnani) -> None:
    gnani.fail_status = 402
    with pytest.raises(VoiceError):
        list(GnaniWsTTS(KEY, url=gnani.tts_ws).stream("Hello"))


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
    lis = VoiceListener(ListSource(b"\0" * int(3.5 * SAMPLE_RATE) * 2), on_heard=lambda _h: None,
                        on_state=states.append, rest_stt=None, stream_stt=None,
                        offline_stt=None, spotter=None)  # fmt: skip
    lis.start()
    deadline = time.monotonic() + 10
    while not any(s.startswith("error: the microphone is silent") for s in states):
        assert time.monotonic() < deadline, states
        time.sleep(0.05)
    lis.stop()
    lis.join(5)


def test_input_device_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Bluetooth output *monitor* as the default input is never used as the mic."""
    from zachary.voice import audio

    srcs = [{"name": "alsa_input.pci.analog-stereo", "description": "Internal Microphone"},
            {"name": "bluez_input.88:D6", "description": "Bullets Z2"}]  # fmt: skip
    monkeypatch.setattr(audio, "_pactl_sources", lambda: (srcs, "bluez_output.88:D6.monitor"))
    assert audio.resolve_input("") == (None, "bluez_input.88:D6")  # headset mic preferred
    assert audio.resolve_input("bullets") == (None, "bluez_input.88:D6")
    assert audio.resolve_input("alsa_input.pci.analog-stereo") == (
        None,
        "alsa_input.pci.analog-stereo",
    )
    assert audio.resolve_input("3") == (3, "")
    monkeypatch.setattr(audio, "_pactl_sources", lambda: (srcs, "alsa_input.pci.analog-stereo"))
    assert audio.resolve_input("") == (None, "")  # a real default: follow the system
    assert [d.is_default for d in audio.list_inputs()] == [True, False]


def test_silent_microphone_switches_to_another() -> None:
    """Laptop mic without a driver (zeros) -> the listener asks the source for the next mic."""

    class TwoMics(ListSource):
        switched = False

        def try_next(self) -> str | None:
            self.switched = True
            return "Bullets Z2"

    states: list[str] = []
    src = TwoMics(b"\0" * int(3.5 * SAMPLE_RATE) * 2)
    lis = VoiceListener(src, on_heard=lambda _h: None, on_state=states.append, rest_stt=None,
                        stream_stt=None, offline_stt=None, spotter=None)  # fmt: skip
    lis.start()
    deadline = time.monotonic() + 10
    while "info: using microphone Bullets Z2" not in states:
        assert time.monotonic() < deadline, states
        time.sleep(0.05)
    lis.stop()
    lis.join(5)
    assert src.switched and not any(s.startswith("error") for s in states)
