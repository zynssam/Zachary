**Zachary for Linux:** find, open, ask about and tidy your files by typing or speaking.

## Install
```bash
mkdir -p ~/Apps && tar xzf Zachary-linux-x64.tar.gz -C ~/Apps
sudo dnf install -y portaudio      # Ubuntu/Debian: sudo apt install libportaudio2
~/Apps/Zachary/Zachary
```
The first start opens a short setup:
- folders to remember;
- voice;
- **your own API keys**: Gnani.ai for voice, Groq and Gemini for questions. All optional; search, notes and clean-up work without them;
- shortcut (Super+Space);
- language and extras.

It adds itself to your app menu. To uninstall, delete `~/Apps/Zachary`. Your data is in `~/.local/share/zachary`.

## What's in it
- Instant search with time words, typos and Hinglish; follow-ups like "the second one".
- Ask your files ("when is my fee due?"), answered with the source passage.
- Voice: push-to-talk, hold Space in the bar, or "Hey Zach".
- Clean up duplicates, old installers and junk. You review everything; it goes to the trash, and Undo puts it back.
- Notes you can type, paste or dictate, saved as `.txt`.
- Open apps and folders: "open spotify", "open zachary in vs code".
- Optional OCR for scanned PDFs and images (needs Tesseract).

Privacy: the index stays on your computer. Only voice commands go to Gnani, and only questions with a few short excerpts go to Groq/Gemini, using your keys. See the README.

Requires x86-64 Linux with glibc 2.35+ (Ubuntu 22.04, Fedora 36 or newer).
