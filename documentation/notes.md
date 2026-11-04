# Notes app

Zachary's own notes window (`zachary/ui/notes.py`). You can write in it, paste into it or
dictate into it. Everything is saved automatically.

## Open it
| Say or type | Does |
|---|---|
| "open my notes", "open the notes app", "notes app", "Zachary notes" | opens the window on your latest note |
| "new note", "make a note", "create a new note" | a new, empty note |
| "take a note", "dictate a note" | a new note with the mic already on |

Plain "open notes" / "open my physics notes" still look for *files* called notes.

## Saving
- Notes are plain `.txt` files in `~/Documents/Zachary/Notes`. Zachary indexes and
  searches them like any other file.
- A new note is named after its first line ("Groceries.txt"); until then it's
  "Note 2026-11-04 1030.txt". It's named once and not renamed afterwards.
- Saved 0.7 s after you stop typing, when you switch notes, when dictation stops, on
  Ctrl+S and when the window closes. Writes are atomic (temp file + replace) and only go
  inside the notes folder. Empty new notes aren't saved, and nothing is ever deleted.

## Paste
Ctrl+V (as plain text), the **Paste** button, or say "paste" while dictating.

## Dictate
- **🎤 Dictate** button, Ctrl+M, or the voice hotkey while the notes window is in front.
  It listens until you click again or say "stop dictation".
- What you say is typed at the cursor. Sentences are capitalised and ended with a full
  stop. "new paragraph" adds a blank line, "scratch that" removes the last piece, and
  "paste" inserts what you copied.
- Speech here is never run as a command, and the bar doesn't pop up. "Hey Zach"
  listening is paused while the note's mic is on (continuous dictation would sooner or
  later sound like it), and resumes when you stop.
- Audio goes to Gnani only while the mic is on. A listen with no speech ends after
  10 s; after 3 silent listens in a row the mic turns off by itself.

## Check it
```bash
pytest -q tests/test_notes.py
python -m zachary.assistant -v     # then Super+Space: "open my notes" / "take a note"
```
