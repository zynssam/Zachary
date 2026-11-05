**Zachary for Linux:** find, open, ask about and tidy your files by typing or speaking.

## Install
**Windows:**
1. Download `Zachary-windows-x64.zip` and unzip it.
2. Run `Zachary\Zachary.exe`. At "Windows protected your PC", click *More info → Run anyway* (the app isn't signed).

**Linux:** download `Zachary-linux-x64.tar.gz`, then:
```bash
mkdir -p ~/Apps && tar xzf Zachary-linux-x64.tar.gz -C ~/Apps
sudo dnf install -y portaudio      # Ubuntu/Debian: sudo apt install libportaudio2
~/Apps/Zachary/Zachary
```
Everything else is done in the app: no terminal needed.
The first start opens a short setup:
- folders to remember;
- voice;
- **your own API keys**: Gnani.ai for voice, Groq and Gemini for questions. All optional; search, notes and clean-up work without them;
- shortcut (Linux: Super+Space; Windows: Ctrl+Alt+Space, and Ctrl+Shift+Space to talk);
- language and extras.

Everything can be changed later in **Settings** (the tray icon, or type "open settings" in the bar):
- folders;
- shortcuts, with a live "working / pressed N times" check;
- voice;
- API keys, and AI answers on or off;
- pausing indexing;
- OCR;
- start at login.

If something doesn't work, Settings → **Open log file** shows what happened.

To uninstall, delete the app folder. Your data is in `~/.local/share/zachary` (Linux) or `%LOCALAPPDATA%\Zachary` (Windows).

## What's in it
- Instant search with time words, typos and Hinglish; follow-ups like "the second one".
- Ask your files ("when is my fee due?"), answered with the source passage.
- Voice: push-to-talk, hold Space in the bar, or "Hey Zach".
- Clean up duplicates, old installers and junk. You review everything; it goes to the trash, and Undo puts it back.
- Notes you can type, paste or dictate, saved as `.txt`.
- Open apps and folders: "open spotify", "open zachary in vs code".
- Optional OCR for scanned PDFs and images (needs Tesseract).

Privacy: the index stays on your computer. Only voice commands go to Gnani, and only questions with a few short excerpts go to Groq/Gemini, using your keys. See the README.

Requires Windows 10/11 (64-bit), or x86-64 Linux with glibc 2.35+ (Ubuntu 22.04, Fedora 36 or newer).
