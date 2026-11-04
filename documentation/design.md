# Design pass: "Orb"

The bar, the result rows, the answer card, dictation and the clean/organize review window
were restyled. The theme **follows the system** (light/dark). Everything visual comes from
`zachary/ui/style.py`.

## What changed
| Where | What |
|---|---|
| `ui/style.py` | `Theme` (DARK / LIGHT) and `theme()` (system colour scheme); violet→blue accent `#7C5CFF → #3B82F6`; Inter font; Lucide icons per kind/extension (`icon_for`, cached `icon_pixmap`); CSS helpers (`answer_css`, `notepad_css`, `card_css`, `tree_css`, buttons) |
| `ui/voice_visual.py` | **VoiceOrb**, the animated voice indicator (below) |
| `ui/wave_intro.py` | the opening wave along the bottom of the screen (below) |
| `ui/quickbar.py` | glass card with gradient edge, orb to the left of the input, re-themes live when the system theme changes |
| `ui/results.py` | file-type icons in tinted tiles, rounded selection with an accent stripe, hover, "why" pills |
| `ui/plan_review.py` | header with a big number ("492 MB can be freed"), a card per category, icons in the tree, themed buttons |
| dictation | `● REC · N words` above a "paper" card that fills as you speak (`Reply.words`) |
| `ui/voice.py`, `voice/pipeline.py`, `voice/audio.py` | live levels: microphone RMS (`listener.level`) and Zach's voice (`Player.level`) feed the orb; a `wake` state when "Hey Zach" is heard |

## The orb (`VoiceOrb`, v2: wireframe sphere)
A glowing wireframe sphere (blue `#3B6BFF` → violet `#8B5CF6` → magenta `#E040FB`) with a
halo of particles and a bright rim. Its surface wobbles with the audio. The mesh is
computed with numpy and drawn with QPainter (about 3 ms a frame at bar size). The idle
frame is drawn once and cached.

| State | When | Look |
|---|---|---|
| idle | nothing happening | still, dim sphere; **no timer, no CPU** |
| wake | "Hey Zach" heard | 180 ms pop + glow, then listening |
| listening | mic open | turns slowly; the surface swells and wobbles with **your voice** |
| thinking | search / LLM (also typed questions) | spins faster, particles orbit, size pulses |
| speaking | Zach replying | waves travel over the surface; particles pushed out by **Zach's audio** |
| dictating | dictation mode | listening + blinking red dot |
| error | no mic / failure | amber/orange, shakes once, then still |

Levels are smoothed (fast attack, slow release) and *pulled* by the UI timer, so no
audio thread touches the widget. `reduce_motion = True` makes it static.

## Opening animation: "volcanic" (`ui/wave_intro.py`)
When the bar opens (hotkey or "Hey Zach"), a glowing wave rises along the bottom of the
screen and erupts (1.3 s). Three vents push the wave lines up into pointed plumes over a
heat glow. Embers shoot up, arc back down under gravity, and cool from white-hot to
magenta, violet and blue as they fade. On "Hey Zach" the plumes grow with your voice. The
bar is usable at once (type straight away) and fades in over ~0.4 s.
- The overlay is click-through and never takes focus; it's hidden after the animation
  (no CPU after). It belongs to the bar, so it's cleaned up with it.
- On X11 and Windows it's a strip at the bottom of the screen (520 px). On Wayland a
  window can't place itself, so it covers the work area (transparent) and draws at its
  bottom.
- Speed: ≤ 14 ms a frame at 1920 px wide. The glow is drawn at ¼ resolution; lines are
  1 px (Qt's fast path); embers are drawn in ~16 batches.
- Off: `python -m zachary.cli set open_animation false`

## "Hey Zach" any time
- `python -m zachary.cli autostart on` starts Zachary when you log in (`autostart off` /
  `autostart` to check). Linux: `~/.config/autostart/ai.zachary.Zachary.desktop`;
  Windows: `HKCU\...\Run\Zachary` (pythonw, no console). With `wake_word` on, the
  microphone is listened to locally (Vosk) from login; only what follows "Hey Zach" goes
  to Gnani.
- Every "Hey Zach" brings the bar up: it opens if hidden, or is raised and focused if it
  was left open behind other windows.

## Assets (`zachary/ui/assets/`, licences in `LICENSES.md`)
- Lucide icons (ISC)
- Inter variable font (SIL OFL)
- `logo.svg` (our own)

## Check it
```bash
pytest -q tests/test_voice_visual.py tests/test_ui.py
python -m zachary.assistant --show -v   # then say "Hey Zach"
```
