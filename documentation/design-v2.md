# Design v2: transparent, minimal, animated (look only)

This is a new look with the same behaviour: no feature, command, timing or text was
changed. All existing tests pass unedited. Branch `design-2`, based on `notes`.

## What changed (only these files)
| File | Look |
|---|---|
| `ui/style.py` | `Theme` (dark / light, follows the system); a soft violet accent `#8B7CFF`; Inter font; Lucide icons (`icon_for`, `icon_pixmap`); stylesheets for buttons, windows and pills. Old names (`BADGES`, `ANSWER_CSS`, ...) are kept. |
| `ui/voice_dot.py` (new) | the glowing voice dot |
| `ui/quickbar.py` | translucent glass card, hairline border; dot left of the input; content fades in when it opens (180 ms) |
| `ui/results.py` | file-type icons; faint "why" pills; hover; a selection pill that glides between rows; rows fade in one after another |
| `ui/notes.py` | flat icon buttons, sidebar with rounded items, roomy editor, status pill, a red dot that pulses while dictating |
| `ui/plan_review.py` | header with a big number ("492 MB can be freed"), a chip per category, file icons in the tree, flat buttons; the header fades in |
| `ui/tray.py` | a dot app icon |
| `assistant.py` | 2 wiring lines: the voice controller's existing `state` / `spoken` signals feed the dot |
| `ui/assets/` | Lucide icons (ISC), Inter (OFL), licences |

## The voice dot
| State (from the voice controller) | Look |
|---|---|
| idle | still, soft dot (no timer, no CPU) |
| listening | breathes (1.6 s) |
| thinking | a thin arc orbits it (kept until the reply arrives) |
| speaking | soft ripples (reply arrived, until Zach finishes) |
| error | amber, one shake, then still |

## Check it
```bash
pytest -q                      # everything, unchanged tests + tests/test_look.py
python -m zachary.assistant -v
```
