# Zachary

**Your files, one keystroke (or one sentence) away.** Zachary is a desktop assistant for
Windows and Linux. It remembers the files on your computer, so you find, open, ask about
and tidy them by typing or speaking naturally: "fees yesterday", "open the second one",
"which project was about crime scene detection", "clean my downloads".

## What it does
- **Instant search:** press Super+Space and type. Time words ("last week", "kal"),
  typos and Hinglish work. Results appear as you type, ranked by how you use your files.
- **Follow-ups:** "my projects", then "the second one", "the older one", "show its
  folder".
- **Ask your files:** "when is my hostel fee due?" is answered from your documents, with
  the passage it came from.
- **Voice:** push-to-talk (Super+Alt+Space, or hold Space in the bar) or "Hey Zach".
  Replies can be spoken.
- **Clean up and organize:** duplicates, old installers, junk, empty folders. You review
  every item. Nothing moves until you approve, removals go to the trash, and Undo puts
  everything back.
- **Notes:** "new note", "take a note". You can type, paste or dictate, and notes are
  saved as `.txt` files in `Documents/Zachary/Notes`.
- **Open apps and folders:** "open spotify", "open zachary in vs code".

## Install
**Linux: the ready-made app.** On GitHub go to **Actions → build** → the latest run →
**Artifacts → Zachary-Linux**, or from a terminal:
```bash
RUN=$(gh run list -R zynssam/Zachary -w build -s success -L 1 --json databaseId -q '.[0].databaseId')
gh run download "$RUN" -R zynssam/Zachary -n Zachary-Linux -D ~/Downloads/zachary
mkdir -p ~/Apps && tar xzf ~/Downloads/zachary/Zachary-linux-x64.tar.gz -C ~/Apps
sudo dnf install -y portaudio            # (Ubuntu: sudo apt install libportaudio2)
~/Apps/Zachary/Zachary
```
It shows up in your app menu after the first start. To uninstall, delete `~/Apps/Zachary`.

**Windows (or Linux) from source** (Python 3.11+, Git):
```bash
git clone https://github.com/zynssam/Zachary && cd Zachary
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e .
python -m zachary                                      # (= the assistant)
```
On Linux from source you may need `libportaudio2` (the microphone) and `libxcb-cursor0`
(Qt on X11).

## First run
A five-step setup opens on the first start:
1. **Folders to remember.** Your home folder by default; add or exclude any folder.
2. **Voice.** Push-to-talk, "Hey Zach", spoken replies, microphone.
3. **Your API keys.** Paste your own; they're saved only on your computer:
   - **Gnani.ai** for voice (https://docs.gnani.ai/api/introduction/quick-start);
   - **Groq** for questions, free tier (https://console.groq.com/keys);
   - **Gemini** as the questions fallback, free tier (https://aistudio.google.com/apikey).

   If any key is missing, the setup says what won't work without it. Click Next again
   to add it later; search, notes and clean-up work without keys.
4. **Shortcut.** Super+Space by default on Linux; on GNOME you confirm it once in a
   system dialog. On Windows the defaults are **Ctrl+Alt+Space** (open) and
   **Ctrl+Shift+Space** (talk), because Windows keeps Win+Space for switching keyboard
   language.
5. **Language** (English with Hinglish, or Hindi), **OCR** for scanned files, and
   **start at login**.

Finishing the setup restarts Zachary once, so your shortcut and voice settings are live
straight away. Change anything later in **Settings**: the tray menu, or type "open
settings" in the bar. After changing a shortcut or voice setting there, click **Restart
now**.

## Privacy: what leaves your computer
| What | Where it goes | When |
|---|---|---|
| The index (file names, dates, the start of each document, embeddings) | nowhere; a local SQLite database in your data folder | always |
| Your voice command | Gnani.ai (speech-to-text) | only after you press the talk key or say "Hey Zach"; the wake word itself is detected locally (Vosk) |
| Names of your projects, apps and folders (recognition hints) | Gnani.ai | with voice, if `stt_keyterms` is on (`zachary cli set stt_keyterms false` to keep them local) |
| A question + a few short excerpts | Groq or Gemini (or Claude) | only when you ask a question and a key is set |
| Reply text to speak | Gnani.ai (text-to-speech) | if "speak replies" is on |

- API keys are saved in Zachary's own `.env` in its data folder, readable only by you.
  They're never logged or shown again.
- OCR runs locally with Tesseract.
- Zachary never runs programs from voice or text: installers and scripts are shown in
  their folder, not opened.
- It never changes system folders, and never deletes permanently.

Data folder: `~/.local/share/zachary` on Linux, `%LOCALAPPDATA%\Zachary` on Windows.

## Everyday commands (CLI)
`python -m zachary cli <command>`; in a release build, `Zachary cli <command>`.

| Command | Does |
|---|---|
| `status` | index size, roots, whether the indexer is running |
| `search fees yesterday` · `recent "last week"` · `projects` | search from the terminal |
| `ask "when is my fee due"` | a question answered from your files |
| `roots add PATH` · `exclude add PATH` · `forget PATH` | what's indexed |
| `clean downloads` · `plan show ID` · `undo` | clean-up plans (always reviewed) |
| `set` · `set voice_enabled true` | show or change settings |
| `autostart on\|off` | start Zachary at login |
| `voice mics` · `voice wake-test` | microphones and the wake word |
| `pause` · `resume` | pause indexing |

## Troubleshooting
- **"inotify watch limit" (Linux):** Zachary watches folders for changes, and big
  homes need a higher limit.
  ```bash
  sudo sysctl fs.inotify.max_user_watches=524288
  echo fs.inotify.max_user_watches=524288 | sudo tee /etc/sysctl.d/90-zachary.conf
  ```
- **The shortcut does nothing (GNOME / Wayland):** confirm the shortcut dialog GNOME
  shows on first start. If it's gone, run `python -m zachary.assistant -v` from a
  terminal and look for "hotkey". You can also add a custom keyboard shortcut that runs
  `Zachary --toggle` (from source: `python -m zachary.assistant --toggle`).
- **Voice: "I didn't hear anything" or wrong words:**
  - Check the microphone with `cli voice mics`. It lists each mic with a live level; pick
    one with `cli voice mics <id>`, or choose it in Settings.
  - A Gnani key must be set; `cli voice setup` shows whether it is found (and downloads
    the local speech models).
  - On networks that intercept HTTPS (some campus/office Wi-Fi), the Gnani and LLM calls
    fail. Use another network.
- **"Gnani key missing":** add it in Settings → Voice & keys, or put
  `GNANI_API_KEY=...` in the data-folder `.env`.
- **Scanned PDFs or images aren't searchable:** turn on OCR in Settings, then install
  Tesseract:
  - Fedora: `sudo dnf install tesseract tesseract-langpack-hin`
  - Ubuntu: `sudo apt install tesseract-ocr tesseract-ocr-hin`
  - Windows: the UB-Mannheim installer
- **Zachary is already running:** starting it again shows the running bar. To restart
  with logs: `pkill -f zachary.assistant && python -m zachary.assistant -v`.

## Building the app
```bash
pip install -e ".[build]"
pyinstaller packaging/zachary.spec --noconfirm     # -> dist/Zachary/
```
CI (`.github/workflows/build.yml`) builds and smoke-tests the Linux app when packaging
changes, on version tags, or by hand. (The spec also builds on Windows; there's no
Windows download for now.) Models aren't bundled; they download on first use: the embedding model
(~30 MB) and, if you turn on "Hey Zach", the Vosk model (~36 MB).

## Development
```bash
pip install -e ".[dev]"
pytest -q && ruff check . && ruff format --check . && mypy zachary tests
```
Architecture: `claude.md`. Per-phase notes: `documentation/`.
