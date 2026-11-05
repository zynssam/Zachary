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
Download from **[Releases](https://github.com/zynssam/Zachary/releases/latest)**:
- **Windows:** unzip `Zachary-windows-x64.zip` and run `Zachary\Zachary.exe`. At "Windows
  protected your PC", click **More info → Run anyway** (the app isn't signed).
- **Linux:** run these once:
  ```bash
  mkdir -p ~/Apps && tar xzf Zachary-linux-x64.tar.gz -C ~/Apps
  sudo dnf install -y portaudio        # Ubuntu/Debian: sudo apt install libportaudio2
  ~/Apps/Zachary/Zachary
  ```
  It adds itself to your app menu.

After that, **everything is done in the app**: first-run setup, then Settings. No terminal
needed. The [step-by-step guide](#step-by-step-guide) below walks through it all.

**From source** (Python 3.11+):
```bash
git clone https://github.com/zynssam/Zachary && cd Zachary
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e .
python -m zachary
```

## Step-by-step guide

### Step 1: Get your API keys (optional, 5 minutes)
Search, notes and clean-up work without any keys. Keys add voice and answers to
questions. Each person uses **their own** keys; nothing is shared.

| Key | What it adds | Where to get it |
|---|---|---|
| **Gnani.ai** | voice commands, spoken replies | https://docs.gnani.ai/api/introduction/quick-start (sign-up at app.gnani.ai, or email speechstack@gnani.ai) |
| **Groq** | answers to questions (free tier) | https://console.groq.com/keys → *Create API Key* |
| **Gemini** | backup for answers (free tier) | https://aistudio.google.com/apikey → *Create API key* |
| Claude (optional) | answers with Claude (paid) | https://console.anthropic.com |

Copy each key somewhere for a minute. You'll paste them in Step 3.

### Step 2: Install
See [Install](#install) above. On Windows, run `Zachary.exe`; on Linux, run
`~/Apps/Zachary/Zachary`, or later pick **Zachary** from your app menu.

### Step 3: First-run setup (5 screens)
It opens by itself the first time. Click **Next** after each screen.
1. **Folders to remember.** Your home folder is already listed.
   - **Add folder…** adds another place, such as a second drive.
   - **Exclude folder…** keeps a folder private; it's never read.
2. **Voice.**
   - Keep **Voice commands** ticked. The microphone opens only while you talk.
   - Optionally tick **Listen for "Hey Zach"**. A 36 MB model downloads; listening
     happens on your computer.
   - Choose your **Microphone**, or leave **Automatic**.
3. **Your API keys.**
   - Paste each key from Step 1 into its box.
   - Keep **Answer questions with AI** ticked.
   - Missing a key? Clicking Next shows what won't work. Click **Next** again to add
     it later.
4. **Your shortcut.**
   - Linux: **Super+Space** (on GNOME, approve the shortcut dialog that appears).
   - Windows: **Ctrl+Alt+Space** to open and **Ctrl+Shift+Space** to talk. Win+Space
     can't be used on Windows.
5. **Last touches.**
   - Speech language: English (with Hinglish) or Hindi.
   - **OCR** reads scanned PDFs and images, and needs Tesseract (see
     [Troubleshooting](#troubleshooting)).
   - **Start Zachary when I log in**.

Click **Finish**. Zachary restarts once, starts reading your files in the background
(a few minutes the first time), and shows the bar.

### Step 4: Open Zachary
- Press your **shortcut** from anywhere, or
- click the **tray icon** (Windows, near the clock; Linux desktops with a tray), or
- open **Zachary** from the app menu / Start menu again. If it's already running, this
  shows the bar.

Press **Esc** to hide the bar; Zachary keeps running in the background.

### Step 5: Change and save settings
1. Open **Settings**, either way:
   - in the bar, type `open settings` and press Enter;
   - or tray icon → **Settings…**.
2. Change what you want. The sections are:
   - **Folders:** what to remember, and what to never look in.
   - **Shortcuts:** the keys, with a live check under them:
     - **✓ ready**: press it to test;
     - **✓ working (pressed N times)**: it works;
     - **✗ …**: why it can't be used. Type another shortcut, such as `ctrl+alt+z`.
   - **Voice:** voice commands, "Hey Zach", spoken replies, microphone.
   - **API keys:** paste a key to add or replace it.
   - **Answers (AI):** on/off, and which service answers (Groq, Gemini, Claude).
   - **Indexing & privacy:** pause indexing, hidden folders, name hints for Gnani.
   - **Language & extras:** speech language, OCR, start at login.
3. Click **Save** (bottom-right, always visible).
   - **"Saved."** means everything is stored, and stays saved after restarts.
   - An amber message means **nothing was saved yet**. Fix what it says, then click
     Save again.
   - A pasted key leaves its box and shows **"saved ✓ (type to replace)"**. It's
     stored, but never shown again.
4. If you changed a **shortcut, voice, the microphone or the Gnani key**, click **Restart
   now** (next to Save) so Zachary reloads with them.

Closing Settings without clicking **Save** discards your changes.

### Step 6: Everyday use
Open the bar, type naturally, press **Enter**:

| Type | What happens |
|---|---|
| `fees yesterday`, `resume pdf`, `photos last week` | finds files (time words, typos, Hinglish) |
| `my projects` → `the second one` | lists projects, then opens #2 |
| `the older one`, `show its folder` | follow-ups on the last result |
| `open spotify`, `open zachary in vs code` | opens apps, or a folder in an app |
| `when is my hostel fee due?` | answers from your documents, with the source passage |
| `which project is about crime scene detection` | picks the right project and opens it |
| `resume what I was working on` → `reopen them` | your last work session |
| `how many pdfs do I have` | counts and stats |

**Keys in the bar:**
- **↑/↓** select, **Enter** opens.
- **Ctrl+Enter** shows the file in its folder.
- **Ctrl+R** shows related files.
- **Ctrl+1…9** opens that row.
- **Esc** hides the bar.

### Step 7: Voice
1. Start talking in any of these ways:
   - press the **talk shortcut**, speak, and pause; it sends by itself;
   - in an empty bar, **hold Space** while you talk and let go to send;
   - press **Ctrl+M** in the bar;
   - say **"Hey Zach, …"**, if you turned it on.
2. The dot next to the search box shows what's happening:
   - it **breathes** while listening;
   - a **ring spins** while thinking;
   - **ripples** show while Zach speaks the answer.

### Step 8: Notes
1. Type `new note`, or say "take a note" to start with the microphone on.
2. Type, paste (**Ctrl+V** / **Paste**), or press **Dictate** (Ctrl+M) and talk. While
   dictating:
   - "new paragraph" starts a paragraph;
   - "scratch that" removes the last piece;
   - "paste" inserts what you copied;
   - "stop dictation" ends.
3. Notes save by themselves as `.txt` files in `Documents/Zachary/Notes`. The first line
   becomes the name.

### Step 9: Clean up and organize (you always review first)
1. Type `clean my downloads`, or `find duplicates`.
2. A review window lists duplicates, old installers, junk and empty folders, with how
   much space you'd free.
3. **Untick** anything you want to keep, then click **Move N to the trash**. Nothing
   changes before that click.
4. Changed your mind? Click **Undo** (or type `undo`). Everything goes back.

`organize my college files` works the same way: it proposes moves, you review them, and
you can undo.

### Step 10: Create files
- `create a text file and paste what I copied`
- `make a document I'll dictate`. Talk, then say "stop dictation".
- `create a folder called hackathon in documents`

### Updating to a new version
1. Quit Zachary: tray icon → **Quit**.
2. Download the new file from [Releases](https://github.com/zynssam/Zachary/releases/latest).
3. Replace the old app folder with the new one, and start it.

Your settings, keys, notes and index are kept: they live in the data folder, not the app
folder.

### Uninstalling
1. Quit Zachary.
2. Delete the app folder.
3. To also remove your index, settings and keys, delete the data folder:
   - Windows: `%LOCALAPPDATA%\Zachary`
   - Linux: `~/.local/share/zachary`

Notes in `Documents/Zachary` are your files and stay unless you delete them.

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

## Optional: terminal commands
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
Most answers are in **Settings**:
- each shortcut shows ✓ "working" or ✗ with the reason;
- **Open log file** shows what Zachary did.

- **The shortcut does nothing:**
  - Open Settings (the tray icon, or click the app again) and check the ✓/✗ next to
    each shortcut.
  - If it says "already used", type another (e.g. `ctrl+alt+z`), click **Save**, then
    **Restart now**.
  - On Windows, Win+Space can't be used (Windows switches keyboard language with it).
  - On GNOME, approve the shortcut dialog shown on first start.
- **Voice isn't heard, or holding Space does nothing:** in Settings:
  - tick **Voice commands**;
  - choose your **Microphone**;
  - paste your **Gnani** key under API keys;
  - then click **Restart now**.
- **Questions aren't answered:** in Settings, tick **Answer questions with AI** and add a
  Groq or Gemini key. Some campus or office Wi-Fi blocks these services; try another
  network.
- **Scanned PDFs or images aren't searchable:** turn on OCR in Settings and install
  Tesseract:
  - Fedora: `sudo dnf install tesseract tesseract-langpack-hin`
  - Ubuntu: `sudo apt install tesseract-ocr tesseract-ocr-hin`
  - Windows: the UB-Mannheim installer
- **"inotify watch limit" (Linux, very large homes):**
  ```bash
  sudo sysctl fs.inotify.max_user_watches=524288
  echo fs.inotify.max_user_watches=524288 | sudo tee /etc/sysctl.d/90-zachary.conf
  ```

## Building the app
```bash
pip install -e ".[build]"
pyinstaller packaging/zachary.spec --noconfirm     # -> dist/Zachary/
```
CI (`.github/workflows/build.yml`) builds and smoke-tests the Linux and Windows apps
when packaging changes, on version tags (and attaches them to the release), or by hand. Models aren't bundled; they download on first use: the embedding model
(~30 MB) and, if you turn on "Hey Zach", the Vosk model (~36 MB).

## Development
```bash
pip install -e ".[dev]"
pytest -q && ruff check . && ruff format --check . && mypy zachary tests
```
Architecture: `claude.md`. Per-phase notes: `documentation/`.
