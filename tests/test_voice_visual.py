"""The voice orb: states, timer only while animating, follows the fed level."""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from zachary.ui import style
from zachary.ui.voice_visual import STATES, VoiceOrb, level_from_rms


def test_level_from_rms_maps_speech_to_0_1() -> None:
    assert level_from_rms(0.0) == 0.0
    assert level_from_rms(0.001) == 0.0  # -60 dB: silence
    assert 0.3 < level_from_rms(0.05) < 0.8  # normal speech
    assert level_from_rms(1.0) == 1.0


def test_idle_uses_no_timer_and_active_states_animate(qtbot: QtBot) -> None:
    orb = VoiceOrb()
    qtbot.addWidget(orb)
    assert orb.state == "idle" and not orb.animating()
    for s in STATES[1:]:
        orb.set_state(s)
        assert orb.animating(), s
    orb.set_state("idle")
    assert not orb.animating() and orb.level == 0.0
    orb.set_state("nonsense")
    assert orb.state == "idle"


def test_listening_follows_the_level_and_wake_turns_into_listening(qtbot: QtBot) -> None:
    orb = VoiceOrb()
    qtbot.addWidget(orb)
    loud = [0.9]
    orb.level_source = lambda: loud[0]
    orb.set_state("wake")
    qtbot.waitUntil(lambda: orb.state == "listening", timeout=2000)
    qtbot.waitUntil(lambda: orb.level > 0.7, timeout=2000)
    loud[0] = 0.0
    qtbot.waitUntil(lambda: orb.level < 0.1, timeout=3000)  # (falls slowly, but falls)


def test_speaking_makes_ripples_and_a_broken_source_is_harmless(qtbot: QtBot) -> None:
    orb = VoiceOrb()
    qtbot.addWidget(orb)
    orb.level_source = lambda: 0.8
    orb.set_state("speaking")
    qtbot.waitUntil(lambda: len(orb._ripples) > 0, timeout=2000)

    def boom() -> float:
        raise RuntimeError

    orb.level_source = boom
    orb._tick()  # no exception
    orb.set_state("error")
    qtbot.waitUntil(lambda: not orb.animating(), timeout=2000)  # shakes once, then still


def test_every_state_paints_in_both_themes(qtbot: QtBot) -> None:
    orb = VoiceOrb()
    qtbot.addWidget(orb)
    for s in STATES:
        orb.set_state(s)
        assert not orb.grab().isNull()
    assert style.DARK.dark and not style.LIGHT.dark


def test_wave_draws_every_moment_and_hides_itself(qtbot: QtBot) -> None:
    from PySide6.QtGui import QImage, QPainter

    from zachary.ui.wave_intro import DURATION_S, WaveIntro

    wave = WaveIntro()
    qtbot.addWidget(wave)
    img = QImage(800, 280, QImage.Format.Format_ARGB32_Premultiplied)
    for t in (0.0, 0.2, 0.5, 0.8, DURATION_S + 0.1):
        img.fill(0)
        p = QPainter(img)
        wave.draw(p, 800, 280, t)
        p.end()
    wave.level_source = lambda: 0.5
    wave.play()
    assert wave.playing() and wave.isVisible()
    qtbot.waitUntil(lambda: not wave.playing(), timeout=3000)
    assert not wave.isVisible()


def test_bar_fades_in_with_the_wave_and_is_usable_at_once(qtbot: QtBot) -> None:
    from PySide6.QtCore import QObject, Signal

    from zachary.ui.quickbar import QuickBar
    from zachary.ui.wave_intro import WaveIntro

    class Engine(QObject):
        replied = Signal(int, object)
        failed = Signal(int, str)

        def ask(self, *_a: object) -> int:
            return 0

    bar = QuickBar(Engine())  # type: ignore[arg-type]
    qtbot.addWidget(bar)
    wave = bar.intro = WaveIntro()
    bar.show_bar()
    assert bar.isVisible() and wave.playing()
    assert bar._reveal < 1.0 and bar.edit.graphicsEffect() is not None
    qtbot.waitUntil(lambda: bar._reveal == 1.0 and not wave.playing(), timeout=3000)
    assert bar.edit.graphicsEffect() is None  # effects dropped once shown
    bar.hide()
