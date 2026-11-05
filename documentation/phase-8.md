# Phase 8: Polish and ship

| Requirement (`prompt.md`) | Done | Where |
|---|---|---|
| Start at login (assistant, which starts the indexer) on Linux and Windows | ✅ | `core/platform/*.autostart` (XDG autostart / `HKCU\…\Run`, pythonw/no console), `cli autostart on\|off`, Settings, first-run setup |
| Tray controls: pause, settings, exclude folders | ✅ | `ui/tray.py`: Open, Pause indexing, Exclude a folder…, Settings…, Quit. Without a tray (GNOME): "open settings" in the bar |
| Optional OCR (Tesseract) for scanned PDFs and images | ✅ | `indexer/ocr.py`, `indexer/extract.py`, setting `ocr_enabled` (off) |
| PyInstaller builds for Windows and Linux | ✅ | `packaging/zachary.spec` (both). The download (`build.yml`) is Linux only, at your choice; Windows runs from source |
| First-run setup: folders, Gnani key, hotkey, language | ✅ | `ui/settings.py` `FirstRun` (4 steps), `needs_first_run` |
| README: setup, privacy, troubleshooting | ✅ | `README.md` |

## How it works
- **One entry point.** `python -m zachary [assistant|indexer|cli]`; a frozen build is
  one executable (`Zachary`, `Zachary indexer …`, `Zachary cli …`).
  - `launch.self_command()` gives the right command line in both cases, for the
    indexer, `--toggle`, autostart and the desktop entry.
  - `multiprocessing.freeze_support()` is called for worker processes in frozen builds.
- **Settings and first-run setup** share one form (`SettingsForm`):
  - **Folders:** what's indexed, plus your own excludes (the built-in system excludes
    are hidden).
  - **Shortcuts:** validated with `parse_hotkey`.
  - **Voice:** on/off, "Hey Zach", speak replies, microphone.
  - **Keys:** Gnani, Groq, Gemini.
  - **Extras:** language, OCR, start at login.
  - **Keys storage:** `core/env.set_secret`. Only Zachary's key names are accepted, line
    breaks are rejected, writes are atomic, and the file is 0600. The value is never
    shown again.
  - Shortcut and voice changes say "apply after you restart Zachary".
  - Turning on "Hey Zach" downloads the Vosk model in the background.
- **First start.**
  - The setup opens on a fresh install; the indexer starts only after you've picked
    your folders.
  - Existing installs, where files are already indexed, skip it.
  - `ZACHARY_SKIP_SETUP` skips it for scripted runs.
- **OCR.**
  - Images become readable, and so do PDFs with almost no text (scans; first 3 pages at
    200 dpi).
  - The `tesseract` program runs directly: no shell, bytes on stdin, a 60 s limit.
    English, plus Hindi if its language pack is installed.
  - Turning OCR on re-queues only images and text-less PDFs (`queue_for_ocr`).
- **Build.**
  - One folder, about 465 MB on Linux (PySide6, PyMuPDF and SciPy are most of it).
  - Models download on first use.
  - `vec0` (sqlite-vec) is placed in its package folder. `onnxruntime` (unused) is
    excluded.
  - Windows is a windowed app, so the CLI prints nothing there; exit codes still work.

## Checked
- Tests:
  - `tests/test_launch.py`: source vs frozen command lines; dispatch.
  - `tests/test_settings.py`: keys saved privately, replaced and removed, no injection;
    first run only on fresh installs; validate-then-save; the four steps; "open settings".
  - `tests/test_ocr.py`: image and scanned-PDF OCR through a fake Tesseract (no shell,
    time-limited); text PDFs skip OCR; re-queue scope. A real Tesseract test runs where
    it's installed.
- Local Linux frozen build:
  - `Zachary cli … scan` / `search` (3.1 ms) / `indexer --once` all work;
  - `Zachary` (the assistant) runs, and waits for first-run setup on a fresh profile.
- CI build (`build.yml`): Linux + Windows build, smoke test (index, search, indexer
  once), artifacts.

## Done when
"A fresh Windows machine and a fresh Linux machine can install, index, and answer voice
and quick-bar queries within the performance budgets."
- **Linux:** verified with the frozen build locally (fresh profile).
- **Windows:** the CI build installs from scratch and passes the smoke test on a clean
  runner. The voice and quick-bar *by hand* check on a real Windows PC is still yours to
  do: unzip the artifact and run `Zachary.exe`.
