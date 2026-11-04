# Design pass: "Orb"

The bar, the result rows, the answer card, dictation and the clean/organize review window
were restyled. The theme **follows the system** (light/dark). Everything visual comes from
`zachary/ui/style.py`.

## What changed
| Where | What |
|---|---|
| `ui/style.py` | `Theme` (DARK / LIGHT) and `theme()` (system colour scheme); violet→blue accent `#7C5CFF → #3B82F6`; Inter font; Lucide icons per kind/extension (`icon_for`, cached `icon_pixmap`); CSS helpers (`answer_css`, `notepad_css`, `card_css`, `tree_css`, buttons) |
| `ui/voice_visual.py` | **VoiceOrb**, the animated voice indicator (below) |
| `ui/quickbar.py` | glass card with gradient edge, orb to the left of the input, re-themes live when the system theme changes |
| `ui/results.py` | file-type icons in tinted tiles, rounded selection with an accent stripe, hover, "why" pills |
| `ui/plan_review.py` | header with a big number ("492 MB can be freed"), a card per category, icons in the tree, themed buttons |
| dictation | `● REC · N words` above a "paper" card that fills as you speak (`Reply.words`) |
| `ui/voice.py`, `voice/pipeline.py`, `voice/audio.py` | live levels: microphone RMS (`listener.level`) and Zach's voice (`Player.level`) feed the orb; a `wake` state when "Hey Zach" is heard |

## The orb (`VoiceOrb`)
| State | When | Look |
|---|---|---|
| idle | nothing happening | small dim orb; **no timer, no CPU** |
| wake | "Hey Zach" heard | 180 ms pop + glow, then listening |
| listening | mic open | grows/brightens with **your voice level** |
| thinking | search / LLM (also typed questions) | rotating violet→blue ring |
| speaking | Zach replying | ripples in time with **Zach's audio** |
| dictating | dictation mode | listening + blinking red dot |
| error | no mic / failure | amber, shakes once, then still |

It's drawn with QPainter at 60 fps only while a state is active. Levels are smoothed (fast
attack, slow release). Levels are *pulled* by the UI timer, so no audio thread touches the
widget. `reduce_motion = True` turns it into a static icon.

## Assets (`zachary/ui/assets/`, licences in `LICENSES.md`)
- Lucide icons (ISC)
- Inter variable font (SIL OFL)
- `logo.svg` (our own)

## Check it
```bash
pytest -q tests/test_voice_visual.py tests/test_ui.py
python -m zachary.assistant --show -v   # then say "Hey Zach"
```
