# Running Zachary on Windows

Everything you need to install, run, test and troubleshoot Zachary on Windows 10/11 (64-bit).
It covers the current branch **`phase-4`** (indexer, search, quick bar, meaning search, voice).

> **Honest status:**
> - The Windows code is type-checked and passes the full test suite on GitHub's Windows machines
>   (Windows Server 2022, Python 3.11 and 3.12).
> - It has **not been tried by hand on a real Windows desktop yet**: the hotkeys, the microphone and
>   the tray have only been tested automatically.
> - If something misbehaves, the troubleshooting table at the end and the `-v` logs are the place to start.

All commands are for **PowerShell**. Run them from the project folder (e.g. `C:\Users\you\Zachary`).

---

## 1. Install the tools (once)

| What | Where | Notes |
|---|---|---|
| **Python 3.12** (64-bit) | <https://www.python.org/downloads/windows/> | During install, tick **"Add python.exe to PATH"**. 3.11 also works. Not the Microsoft Store version. |
| **Git** | <https://git-scm.com/download/win> | Defaults are fine. |

Check them in a new PowerShell window:
```powershell
py -3.12 --version
git --version
```

Allow PowerShell to run the venv activation script (once, for your user only):
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 2. Get the code and install

```powershell
cd $HOME
git clone https://github.com/zynssam/Zachary.git
cd Zachary
git checkout phase-4

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1          # the prompt now starts with (.venv)
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

- Every later command assumes the venv is active. In a new window, run `cd $HOME\Zachary; .\.venv\Scripts\Activate.ps1` first.
- No compiler is needed. All packages ship Windows wheels: PySide6, sqlite-vec, PyMuPDF, sounddevice (includes PortAudio), pywhispercpp and model2vec.

Update later:
```powershell
git pull
pip install -e ".[dev]"
```

---

## 3. Gnani API key (for voice)

```powershell
Copy-Item .env.example .env
notepad .env          # set:  GNANI_API_KEY=your-key-here   then save
```

- `.env` is git-ignored, so it's never committed. The key is sent only to Gnani, as a header, and is never logged.
- Zachary looks for `.env` in the folder you run it from, then in `%LOCALAPPDATA%\Zachary\.env`.
- To make it work from anywhere, copy it there:
```powershell
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\Zachary" | Out-Null
Copy-Item .env "$env:LOCALAPPDATA\Zachary\.env"
```

---

## 4. Windows settings to check

1. **Microphone access for desktop apps** (otherwise the mic records silence):
   - Settings → Privacy & security → **Microphone**;
   - turn on **Microphone access**, and **Let desktop apps access your microphone**.
2. **Default microphone**: Settings → System → Sound → Input. Pick the mic you'll talk into.
   - **Bluetooth earbuds** appear as "Headset (… Hands-Free)". When an app uses that mic, Windows switches
     the earbuds to call mode by itself; there's nothing to set up. Linux needed extra code for this.
3. **Hotkeys:** Windows reserves **Win+Space** (keyboard layout switch), so Zachary's Linux defaults
   (Super+Space, Super+Alt+Space) won't work. Set Windows-friendly keys once:
```powershell
python -m zachary.cli set quickbar_hotkey ctrl+alt+space
python -m zachary.cli set voice_hotkey ctrl+alt+m
```
   If a key is taken by another app (e.g. PowerToys), the bar says "Hotkey unavailable". Pick another
   combination with the same command.

---

## 5. First run: index your files

What gets indexed by default:
- your user folder (`C:\Users\you`);
- Desktop, Documents and Downloads, even when OneDrive redirects them;
- **other drives** (D:, E:, …). C: is covered through your user folder.

Skipped by default:
- Windows, Program Files and ProgramData;
- AppData, Temp and browser profiles;
- hidden folders;
- `node_modules`, `.git` and similar.

```powershell
python -m zachary.cli roots                 # what will be indexed
python -m zachary.cli scan                  # first full scan (a few minutes for ~200k files)
python -m zachary.cli status                # counts, roots, indexer state
python -m zachary.cli search "fee receipt"  # try a search
python -m zachary.cli embed                 # meaning search: downloads a 30 MB model once, then indexes contents
```

Change what's indexed:
```powershell
python -m zachary.cli roots add D:\College
python -m zachary.cli roots remove E:\
python -m zachary.cli exclude add "D:\Games"
python -m zachary.cli set index_hidden_folders true
```

> **About admin rights:** Zachary never asks for admin. Without admin, the fast NTFS change journal (USN)
> can't be read, so after a restart Zachary catches up by re-scanning, which is slower but complete.
> While it runs, changes are picked up live (ReadDirectoryChangesW via watchdog), with or without admin.

---

## 6. Run the assistant

```powershell
python -m zachary.assistant -v
```
- It starts the **indexer** in the background, sets up the **tray icon** and registers the **hotkeys**.
- `-v` prints logs to the window. Keep it open while testing.
- Press **Ctrl+Alt+Space** to open the quick bar, type "open fee receipt" and press Enter.

| Key (in the bar) | Action |
|---|---|
| type | live results |
| Enter | do it ("open fee receipt", "open the second one") |
| ↑ / ↓ | move the highlight |
| Ctrl+Enter | show in Explorer |
| Ctrl+R | related files |
| Ctrl+1 … Ctrl+9 | open result N |
| **Ctrl+M** | talk (voice) |
| Esc | hide |

Other ways to start:
```powershell
python -m zachary.assistant --show -v       # open the bar right away
pythonw -m zachary.assistant                # no console window (logs off); quit from the tray
python -m zachary.assistant --toggle        # toggle a running bar (e.g. from a shortcut)
```

**Stop everything** (only Zachary's Python processes, not your other Python programs):
```powershell
Get-CimInstance Win32_Process -Filter "Name like 'python%.exe'" |
  Where-Object { $_.CommandLine -match 'zachary' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```
Or use the tray icon → Quit (this leaves the indexer running).

**Start with Windows (optional, manual for now):**
1. Press Win+R, type `shell:startup` and press Enter.
2. Create a shortcut there with this target:
```
C:\Users\you\Zachary\.venv\Scripts\pythonw.exe -m zachary.assistant
```
3. Set "Start in" to `C:\Users\you\Zachary`. Replace `you` with your user name in both places.

---

## 7. Voice

```powershell
python -m zachary.cli voice setup            # key found? downloads whisper tiny (75 MB) once; shows your mic
python -m zachary.cli voice mics             # every mic with a live level: SPEAK while it runs
python -m zachary.cli voice listen --offline # one command, local whisper only (free)
python -m zachary.cli voice listen           # one command through Gnani (1 STT + 1 TTS call)
python -m zachary.cli set voice_enabled true
python -m zachary.assistant -v
```
Then press **Ctrl+Alt+M** (or Ctrl+M in the bar), say "open fee receipt", and pause. Gnani detects the
end of your sentence, and the reply is spoken.

### Choosing the microphone
By default Zachary uses the **Windows default input**, and Windows switches that by itself when you plug in
earphones or connect Bluetooth earbuds. Usually there's nothing to set. Pin a mic only if Windows picks the wrong one:
Windows lists each mic several times, once per audio system (MME, DirectSound, WASAPI…).
`voice mics` shows each one with an **id (a number)** and a level bar.
1. Run it and talk.
2. Pick an entry with a moving level; the **MME** ones are the most reliable at 16 kHz.
3. Select it:
```powershell
python -m zachary.cli voice mics 1           # use device id 1
python -m zachary.cli voice mics default     # back to the Windows default input
```
Device numbers can change when you plug devices in or out. If voice goes quiet after that, run `voice mics` again.

### Voice settings
```powershell
python -m zachary.cli set speak_replies false     # text replies only (saves TTS credits)
python -m zachary.cli set stt_streaming false     # REST instead of streaming (if it mishears short commands)
python -m zachary.cli set stt_language hi-IN      # default en-IN (also handles Hinglish in Latin letters)
python -m zachary.cli set wake_word true          # "Hey Zach" (local whisper; ~300 MB RAM; mic always on)
python -m zachary.cli voice usage                 # Gnani calls so far
python -m zachary.cli voice latency               # timings of the last 10 commands
```

---

## 8. Where things are stored

| What | Location |
|---|---|
| Index database | `%LOCALAPPDATA%\Zachary\zachary.db` |
| Models (meaning search, whisper) | `%LOCALAPPDATA%\Zachary\models\` |
| Gnani usage ledger, voice timings | `%LOCALAPPDATA%\Zachary\gnani_usage.jsonl`, `latency.jsonl` |
| Settings | inside the database (`python -m zachary.cli set` lists them all) |

- Open the folder: `explorer "$env:LOCALAPPDATA\Zachary"`.
- **Sandbox:** to try things without touching the real index, run `$env:ZACHARY_DB="$env:TEMP\zach-test.db"`
  in that window. `Remove-Item Env:ZACHARY_DB` goes back to the real one.
- **Start fresh:** stop everything, delete `%LOCALAPPDATA%\Zachary\zachary.db*`, then run `cli scan`.

---

## 9. Run the tests on Windows

```powershell
ruff check .
ruff format --check .
mypy zachary tests
pytest                                   # ~350 tests, ~1-2 min; Gnani is always mocked
pytest tests/test_voice.py -q            # just voice
```
- Expected result: all pass, with about 11 skipped (Linux-only tests: inotify, X11, the Wayland portal).
- **Windows-only tests** do run here: Recent Items, the USN journal, RegisterHotKey and the hidden attribute.
- Real local whisper tests are optional. They need espeak-ng (<https://github.com/espeak-ng/espeak-ng/releases>):
  `$env:ZACHARY_TEST_WHISPER="1"; pytest tests/test_voice.py`.

---

## 10. What's different from Linux

| Area | Linux | Windows |
|---|---|---|
| Quick-bar hotkey | Super+Space (GNOME portal dialog first time) | **Ctrl+Alt+Space** (Win+Space is reserved); RegisterHotKey, no dialog |
| Voice hotkey | Super+Alt+Space | **Ctrl+Alt+M** (set in section 4) |
| Live file watching | inotify (watch limit) | ReadDirectoryChangesW (no limit) |
| Catch-up after a restart | scan-based diff | USN journal if admin, otherwise scan-based diff |
| Recently opened files | `recently-used.xbel` | Recent Items (`.lnk`); Jump Lists not read yet |
| Bluetooth earbud mic | Zachary switches the profile (pactl) | Windows does it automatically |
| Choosing a mic | PipeWire source names | device numbers from `voice mics` |
| Tray icon | needs AppIndicator on GNOME | built in |
| Show in folder | file manager over D-Bus | Explorer with the file selected |
| Start at login | – | manual Startup shortcut (section 6) |

---

## 11. Troubleshooting

| Problem | What to do |
|---|---|
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then open a new window |
| `py` or `python` not found | Reinstall Python from python.org with "Add to PATH" ticked; open a new window |
| `pip install` fails building something | Make sure it's **64-bit Python 3.11/3.12** (`python -c "import struct;print(struct.calcsize('P')*8)"` should print 64) |
| Bar says "Hotkey unavailable" | Another app owns the key: `python -m zachary.cli set quickbar_hotkey ctrl+shift+space`, then restart the assistant |
| Hotkey does nothing, no message | Restart the assistant with `-v` and look for "hotkey" lines; try `python -m zachary.assistant --toggle` |
| "the microphone is silent" / "didn't hear anything (… level 0.000)" | Windows mic privacy (section 4.1); wrong input → `voice mics`, pick one whose level moves |
| "didn't hear anything (… too quiet)" | Speak closer; raise the input volume (Settings → Sound → your mic → Input volume) |
| `voice mics` shows "can't open (PortAudioError)" for a device | That entry doesn't support 16 kHz (often WASAPI); pick the MME entry of the same mic |
| Gnani "rejected the API key" / "credits are used up" | Check `.env` (section 3). Voice still works through local whisper, just slower |
| Reply text appears but nothing is spoken | Check Windows output device / volume; `python -m zachary.cli voice say "test"` (1 TTS call) |
| Search finds nothing | `python -m zachary.cli status`; run `cli scan`; check `cli roots` |
| A folder is missing | Hidden folders and AppData are skipped by default; `cli roots add PATH` or `cli set index_hidden_folders true` |
| "already running" | Another indexer/assistant holds the lock: stop everything (section 6) |
| Indexer uses CPU after first start | Normal during the first scan/embedding; it runs at low priority and pauses on battery (`cli set embed_on_battery true` to allow) |
| Windows Defender slows the first scan | Optional: exclude `%LOCALAPPDATA%\Zachary` from real-time scanning |

When reporting a problem, include:
- the `-v` log lines;
- the output of `python -m zachary.cli status`;
- for voice, `python -m zachary.cli voice latency`.

---

## 12. Cheat sheet

```powershell
cd $HOME\Zachary; .\.venv\Scripts\Activate.ps1      # every new window
git pull; pip install -e ".[dev]"                   # update
python -m zachary.assistant -v                      # run (Ctrl+Alt+Space = bar, Ctrl+Alt+M = talk)
python -m zachary.cli status                        # what's indexed
python -m zachary.cli search "notes about trees"    # search from the console
python -m zachary.cli voice mics                    # check the microphone
python -m zachary.cli voice latency                 # voice timings
python -m zachary.cli voice usage                   # Gnani calls used
pytest                                              # tests
```
