"""Settings and first-run setup (Phase 8).

One form (``SettingsForm``) backs both windows:

* ``SettingsWindow`` (tray → Settings…, or "open settings" in the bar): everything.
* ``FirstRun``: four steps on the first start: folders, voice + Gnani key, shortcut,
  language + extras. Finishing sets ``setup_done``.

Settings go to the settings table; API keys go to Zachary's own ``.env`` in the data
folder (readable only by you; ``core/env.set_secret``) and are never shown again, only
"saved". Hotkey and voice changes apply after Zachary restarts (the form says so).
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from zachary.core.config import Config
from zachary.core.env import has_secret, set_secret
from zachary.core.hotkeys import HotkeyError, parse_hotkey
from zachary.core.platform import Platform
from zachary.launch import self_command
from zachary.ui import style

LANGUAGES = [("en-IN", "English (with Hinglish)"), ("hi-IN", "Hindi")]
KEYS = [("GNANI_API_KEY", "Gnani.ai (voice)"), ("GROQ_API_KEY", "Groq (questions, free tier)"),
        ("GEMINI_API_KEY", "Gemini (questions, fallback)"),
        ("ANTHROPIC_API_KEY", "Claude (questions, paid, optional)")]  # fmt: skip
PROVIDERS = [("groq", "Groq (free tier, fastest)"), ("gemini", "Gemini (free tier)"),
             ("claude", "Claude (paid)")]  # fmt: skip
# Where to get each key, and what stops working without it.
KEY_HELP = {
    "GNANI_API_KEY": ("https://docs.gnani.ai/api/introduction/quick-start",
                      "voice commands and spoken replies"),
    "GROQ_API_KEY": ("https://console.groq.com/keys", "questions about your files and projects"),
    "GEMINI_API_KEY": ("https://aistudio.google.com/apikey",
                       "the backup for questions when Groq is busy"),
    "ANTHROPIC_API_KEY": ("https://console.anthropic.com", "Claude answers (optional, paid)"),
}  # fmt: skip
RESTART_KEYS = {"quickbar_hotkey", "voice_hotkey", "voice_enabled", "wake_word", "mic_device"}
WINDOWS_RESERVED = {"super+space", "super+alt+space", "super+shift+space", "alt+space"}


def tesseract_found() -> bool:
    return shutil.which("tesseract") is not None


class SettingsForm(QWidget):
    """The fields. ``apply()`` validates and saves; returns messages for the user."""

    def __init__(self, cfg: Config, platform: Platform, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.platform = platform
        s = cfg.load()
        t = style.theme()
        dim = f"color: {t.dim};"

        # folders to index / never index
        self.roots = QListWidget(self)
        self.roots.addItems([str(r) for r in cfg.index_roots()])
        self.excludes = QListWidget(self)
        self.excludes.addItems(sorted(p for p, kind, src, on in cfg.list_excludes()
                                      if kind == "path" and on and src == "user"))  # fmt: skip
        for lst in (self.roots, self.excludes):
            lst.setMaximumHeight(110)
            lst.setObjectName("plain")
            lst.setTextElideMode(Qt.TextElideMode.ElideMiddle)

        # shortcuts
        self.quickbar_hotkey = QLineEdit(s.quickbar_hotkey, self)
        self.voice_hotkey = QLineEdit(s.voice_hotkey, self)

        # voice
        self.voice_enabled = QCheckBox("Voice commands (push-to-talk)", self)
        self.voice_enabled.setChecked(s.voice_enabled)
        self.wake_word = QCheckBox("Listen for “Hey Zach” (on this computer; 36 MB model)", self)
        self.wake_word.setChecked(s.wake_word)
        self.speak_replies = QCheckBox("Speak replies", self)
        self.speak_replies.setChecked(s.speak_replies)
        self.language = QComboBox(self)
        for code, label in LANGUAGES:
            self.language.addItem(label, code)
        i = self.language.findData(s.stt_language)
        self.language.setCurrentIndex(max(0, i))
        self.mic = QComboBox(self)
        self.mic.addItem("Automatic (whatever gives sound)", "")
        try:
            from zachary.voice.audio import list_inputs

            for d in list_inputs():
                if not d.virtual:
                    self.mic.addItem(d.label, d.id)
        except Exception:  # (no audio stack: automatic only)
            pass
        self.mic.setCurrentIndex(max(0, self.mic.findData(s.mic_device)))

        # API keys: typed in, saved to the data folder .env, never shown again
        self.keys: dict[str, QLineEdit] = {}
        for name, _label in KEYS:
            e = QLineEdit(self)
            e.setEchoMode(QLineEdit.EchoMode.Password)
            e.setPlaceholderText("saved ✓ (type to replace)" if has_secret(name) else "not set")
            self.keys[name] = e

        # extras
        self.ocr = QCheckBox("Read scanned PDFs and images (OCR, slow, in the background)", self)
        self.ocr.setChecked(s.ocr_enabled)
        self.ocr_note = QLabel(
            "Tesseract found." if tesseract_found() else
            "Needs Tesseract: Linux “sudo dnf install tesseract tesseract-langpack-hin” "
            "(or apt install tesseract-ocr tesseract-ocr-hin); Windows: the UB-Mannheim "
            "installer.", self)  # fmt: skip
        self.ocr_note.setWordWrap(True)
        self.ocr_note.setStyleSheet(dim)
        # AI answers (questions about your files / projects), indexing, privacy
        self.llm = QCheckBox("Answer questions with AI (uses your Groq / Gemini key)", self)
        self.llm.setChecked(s.llm_enabled)
        self.provider = QComboBox(self)
        for code, label in PROVIDERS:
            self.provider.addItem(label, code)
        self.provider.setCurrentIndex(max(0, self.provider.findData(s.llm_provider)))
        self.paused = QCheckBox(
            "Pause indexing (Zachary stops reading new and changed files)", self
        )
        self.paused.setChecked(s.indexing_paused)
        self.hidden = QCheckBox("Include hidden folders (.config, .local…; usually app data)", self)
        self.hidden.setChecked(s.index_hidden_folders)
        self.keyterms = QCheckBox("Send my project / app / folder names to Gnani as hints, so "
                                  "they're heard right (names only, never file contents)",
                                  self)  # fmt: skip
        self.keyterms.setChecked(s.stt_keyterms)
        self.autostart = QCheckBox("Start Zachary when I log in", self)
        try:
            self.autostart.setChecked(platform.autostart(self_command("zachary.assistant"))[0])
        except Exception:
            self.autostart.setEnabled(False)

        self.needs_restart = False  # (shortcut / voice changed: apply() sets it)
        self.message = QLabel(self)
        self.message.setWordWrap(True)
        self.message.setStyleSheet(dim)

    # -- pieces the windows lay out -----------------------------------------------------------
    def folders_box(self) -> QWidget:
        box = QWidget(self)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(QLabel("<b>Folders Zachary remembers</b>", box))
        v.addWidget(self.roots)
        v.addLayout(self._buttons(self.roots, "Add folder…"))
        v.addWidget(QLabel("<b>Never look in</b> (excluded: forgotten and never read)", box))
        v.addWidget(self.excludes)
        v.addLayout(self._buttons(self.excludes, "Exclude folder…"))
        return box

    def _buttons(self, lst: QListWidget, add_text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        add = QPushButton(add_text, self)
        rem = QPushButton("Remove", self)
        add.clicked.connect(lambda: self._add_folder(lst))
        rem.clicked.connect(lambda: [lst.takeItem(lst.row(i)) for i in lst.selectedItems()])
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        return row

    def _add_folder(self, lst: QListWidget) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose a folder", str(Path.home()))
        if d and not lst.findItems(d, Qt.MatchFlag.MatchExactly):
            lst.addItem(d)

    def voice_box(self, with_keys: bool = True) -> QWidget:
        box = QWidget(self)
        f = QFormLayout(box)
        f.addRow(self.voice_enabled)
        f.addRow(self.wake_word)
        f.addRow(self.speak_replies)
        f.addRow("Microphone", self.mic)
        if with_keys:
            for name, label in KEYS:
                f.addRow(label, self.keys[name])
        return box

    def ai_box(self) -> QWidget:
        box = QWidget(self)
        f = QFormLayout(box)
        f.addRow(self.llm)
        f.addRow("Answer with", self.provider)
        return box

    def indexing_box(self) -> QWidget:
        box = QWidget(self)
        f = QFormLayout(box)
        f.addRow(self.paused)
        f.addRow(self.hidden)
        f.addRow(self.keyterms)
        return box

    def keys_box(self, with_ai: bool = False) -> QWidget:
        """The API keys, each with where to get it and what it's for."""
        box = QWidget(self)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        if with_ai:  # (first-run setup: answers are on as soon as there's a key)
            v.addWidget(self.llm)
        dim = style.theme().dim
        for name, label in KEYS:
            url, used_for = KEY_HELP[name]
            head = QLabel(f"<b>{label}</b>", box)
            hint = QLabel(f"For {used_for}. Get a key: <a href='{url}' style='color:"
                          f"{style.ACCENT}'>{url}</a>", box)  # fmt: skip
            hint.setOpenExternalLinks(True)
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {dim};")
            v.addWidget(head)
            v.addWidget(self.keys[name])
            v.addWidget(hint)
            v.addSpacing(6)
        v.addStretch(1)
        return box

    def missing_keys(self) -> list[str]:
        """Keys neither typed in now nor saved before."""
        return [name for name, _l in KEYS if not self.keys[name].text().strip()
                and not has_secret(name)]  # fmt: skip

    def shortcuts_box(self) -> QWidget:
        box = QWidget(self)
        f = QFormLayout(box)
        f.addRow("Open the quick bar", self.quickbar_hotkey)
        f.addRow("Push-to-talk", self.voice_hotkey)
        win = getattr(self.platform, "name", "") == "windows"
        hint = QLabel("e.g. ctrl+alt+space, ctrl+shift+space (Windows keeps Win+Space)." if win
                      else "e.g. super+space, ctrl+alt+space. On GNOME you confirm it once.",
                      box)  # fmt: skip
        hint.setStyleSheet(f"color: {style.theme().dim};")
        f.addRow(hint)
        return box

    def extras_box(self) -> QWidget:
        box = QWidget(self)
        f = QFormLayout(box)
        f.addRow("Speech language", self.language)
        f.addRow(self.ocr)
        f.addRow(self.ocr_note)
        f.addRow(self.autostart)
        return box

    # -- saving -----------------------------------------------------------------------------------
    def apply(self) -> list[str]:
        """Validate and save. Returns problems (nothing saved) or notes (saved)."""
        problems = []
        for field, label in ((self.quickbar_hotkey, "quick bar"), (self.voice_hotkey, "talk")):
            try:
                hk = parse_hotkey(field.text().strip())
            except HotkeyError as err:
                problems.append(f"The {label} shortcut isn't valid: {err}")
                continue
            if getattr(self.platform, "name", "") == "windows" and str(hk) in WINDOWS_RESERVED:
                problems.append(f"Windows keeps {hk} for itself (keyboard language and "
                                f"system keys): choose another {label} shortcut, e.g. "
                                "ctrl+alt+space.")  # fmt: skip
        roots = [self.roots.item(i).text() for i in range(self.roots.count())]
        if not roots:
            problems.append("Choose at least one folder to remember.")
        if problems:
            self.message.setText("\n".join(problems))
            return problems
        before = self.cfg.load()
        new = {
            "quickbar_hotkey": self.quickbar_hotkey.text().strip(),
            "voice_hotkey": self.voice_hotkey.text().strip(),
            "voice_enabled": self.voice_enabled.isChecked(),
            "wake_word": self.wake_word.isChecked(),
            "speak_replies": self.speak_replies.isChecked(),
            "stt_language": self.language.currentData(),
            "tts_language": self.language.currentData(),
            "mic_device": self.mic.currentData(),
            "ocr_enabled": self.ocr.isChecked(),
            "llm_enabled": self.llm.isChecked(),
            "llm_provider": self.provider.currentData(),
            "indexing_paused": self.paused.isChecked(),
            "index_hidden_folders": self.hidden.isChecked(),
            "stt_keyterms": self.keyterms.isChecked(),
        }
        # Answer with a provider you have a key for (Groq -> Gemini -> Claude).
        typed = {n for n, e in self.keys.items() if e.text().strip()}
        have = {n for n, _l in KEYS if n in typed or has_secret(n)}
        need = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}
        if new["llm_enabled"] and need[new["llm_provider"]] not in have:
            for code, _label in PROVIDERS:
                if need[code] in have:
                    new["llm_provider"] = code
                    break
        changed = {k for k, v in new.items() if getattr(before, k) != v}
        for k in changed:
            self.cfg.set_setting(k, new[k])
        if new["ocr_enabled"] and not before.ocr_enabled:
            from zachary.indexer.ocr import queue_for_ocr

            queue_for_ocr(self.cfg.conn)  # images and scanned PDFs get read again
        if [str(r) for r in self.cfg.index_roots()] != roots:
            self.cfg.set_index_roots(roots)
        old_ex = {p for p, kind, src, on in self.cfg.list_excludes()
                  if kind == "path" and on and src == "user"}  # fmt: skip
        new_ex = {self.excludes.item(i).text() for i in range(self.excludes.count())}
        for p in new_ex - old_ex:
            self.cfg.add_exclude(p, "path")
        for p in old_ex - new_ex:
            self.cfg.remove_exclude(p, "path")
        if "GNANI_API_KEY" in typed:
            self.needs_restart = True  # (voice reads its key at start)
        for name, field_ in self.keys.items():
            if field_.text().strip():
                set_secret(name, field_.text().strip())
                field_.clear()
                field_.setPlaceholderText("saved ✓ (type to replace)")
        if self.autostart.isEnabled():
            self.platform.autostart(self_command("zachary.assistant"), self.autostart.isChecked())
        notes = ["Saved."]
        self.needs_restart = bool(changed & RESTART_KEYS) or self.needs_restart
        if self.needs_restart:
            notes.append("Shortcut and voice changes apply after Zachary restarts.")
        if new["wake_word"] and not before.wake_word:
            self.fetch_wake_model()
            notes.append("Downloading the “Hey Zach” model (36 MB) in the background.")
        self.message.setText(" ".join(notes))
        return []

    def fetch_wake_model(self, on_done: Callable[[bool], None] | None = None) -> None:
        models = self.platform.data_dir() / "models"

        def run() -> None:
            ok = True
            try:
                from zachary.voice.wakeword import download_vosk

                download_vosk(models)
            except Exception:
                ok = False
            if on_done is not None:
                on_done(ok)

        threading.Thread(target=run, name="zach-vosk-download", daemon=True).start()


def hotkey_html(rows: list[tuple[str, str, str | None, int]]) -> str:
    lines = []
    for what, key, problem, n in rows:
        if problem:
            lines.append(f"<span style='color:{style.WARN}'>✗</span> {what} ({key}): {problem}."
                         " Pick another shortcut, Save, then Restart now.")  # fmt: skip
        elif n:
            lines.append(f"<span style='color:#4ADE80'>✓</span> {what} ({key}): working "
                         f"(pressed {n} times).")  # fmt: skip
        else:
            lines.append(f"<span style='color:#4ADE80'>✓</span> {what} ({key}): ready. "
                         "Press it to test.")  # fmt: skip
    return "<br>".join(lines)


def _scroll(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QScrollArea.Shape.NoFrame)
    sa.setWidget(inner)
    return sa


class SettingsWindow(QWidget):
    def __init__(
        self,
        cfg: Config,
        platform: Platform,
        on_restart: Callable[[], None] | None = None,
        hotkey_status: Callable[[], list[tuple[str, str, str | None, int]]] | None = None,
        log_file: Path | None = None,
    ) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Zachary settings")
        self.resize(700, 760)
        self.setObjectName("zwindow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(style.window_css(style.theme()))
        self.form = SettingsForm(cfg, platform, self)
        inner = QWidget()
        v = QVBoxLayout(inner)
        for title, box in (("Folders", self.form.folders_box()),
                           ("Shortcuts", self.form.shortcuts_box()),
                           ("Voice", self.form.voice_box(with_keys=False)),
                           ("API keys", self.form.keys_box()),
                           ("Answers (AI)", self.form.ai_box()),
                           ("Indexing & privacy", self.form.indexing_box()),
                           ("Language & extras", self.form.extras_box())):  # fmt: skip
            head = QLabel(title, inner)
            head.setFont(style.font(head.font(), 1.2, 600))
            v.addWidget(head)
            v.addWidget(box)
        v.addStretch(1)
        # Are the shortcuts working? (live: registered, and how often pressed)
        self.hotkeys = QLabel(self)
        self.hotkeys.setWordWrap(True)
        self.hotkeys.setTextFormat(Qt.TextFormat.RichText)
        self._hotkey_status = hotkey_status
        if hotkey_status is not None:
            v.insertWidget(4, self.hotkeys)  # (under the Shortcuts fields)
            self._timer = QTimer(self)
            self._timer.setInterval(1000)
            self._timer.timeout.connect(self.refresh_hotkeys)
            self._timer.start()
            self.refresh_hotkeys()
        if log_file is not None:
            open_log = QPushButton("Open log file", inner)
            open_log.setToolTip(str(log_file))
            open_log.clicked.connect(lambda: platform.open_path(log_file))
            v.addWidget(open_log, 0, Qt.AlignmentFlag.AlignLeft)
        save = QPushButton("Save", self)
        save.setStyleSheet(style.primary_css(style.theme()))
        self.restart_btn = QPushButton("Restart now", self)
        self.restart_btn.setVisible(False)
        if on_restart is not None:
            self.restart_btn.clicked.connect(on_restart)

        def saved() -> None:
            if not self.form.apply():
                self.restart_btn.setVisible(self.form.needs_restart and on_restart is not None)

        save.clicked.connect(saved)
        lay = QVBoxLayout(self)
        lay.addWidget(_scroll(inner), 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.form.message, 1)
        bottom.addWidget(self.restart_btn)
        bottom.addWidget(save)
        lay.addLayout(bottom)

    def refresh_hotkeys(self) -> None:
        if self._hotkey_status is not None and self.isVisible():
            self.hotkeys.setText(hotkey_html(self._hotkey_status()))


class FirstRun(QWidget):
    """Four steps: folders → voice + Gnani key → shortcut → language + extras."""

    finished = Signal()

    STEPS: ClassVar[list[tuple[str, str]]] = [
        ("Which folders should Zachary remember?",
         "It indexes names, dates and the start of each document, on this computer only."),
        ("Talk to Zachary",
         "Voice uses Gnani.ai: only your voice command is sent, and only after you press "
         "the talk key (or say “Hey Zach”)."),
        ("Your API keys",
         "Paste your own keys for Gnani.ai, Groq and Gemini. They're saved only on this "
         "computer (readable just by you) and used for your own requests. Questions send "
         "only the question and a few short excerpts."),
        ("Your shortcut", "Press it anywhere to open Zachary."),
        ("Last touches", "You can change all of this later in Settings."),
    ]  # fmt: skip

    def __init__(self, cfg: Config, platform: Platform) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Welcome to Zachary")
        self.resize(640, 560)
        self.cfg = cfg
        self.setObjectName("zwindow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(style.window_css(style.theme()))
        self.form = SettingsForm(cfg, platform, self)
        self.pages = QStackedWidget(self)
        bodies = [self.form.folders_box(), self.form.voice_box(with_keys=False),
                  self.form.keys_box(with_ai=True), self.form.shortcuts_box(),
                  self.form.extras_box()]  # fmt: skip
        self._keys_page = 2
        self.form.llm.setChecked(True)  # (answers on; they work once a key is there)
        self.form.voice_enabled.setChecked(True)  # (push-to-talk: the mic opens only on a press)
        self._warned_keys = False
        for (title, sub), body in zip(self.STEPS, bodies, strict=True):
            page = QWidget()
            v = QVBoxLayout(page)
            h = QLabel(title, page)
            h.setFont(style.font(h.font(), 1.45, 600))
            s = QLabel(sub, page)
            s.setWordWrap(True)
            s.setStyleSheet(f"color: {style.theme().dim};")
            v.addWidget(h)
            v.addWidget(s)
            v.addSpacing(8)
            v.addWidget(body, 1)
            self.pages.addWidget(page)
        self.step = QLabel(self)
        self.back = QPushButton("Back", self)
        self.next = QPushButton("Next", self)
        self.next.setStyleSheet(style.primary_css(style.theme()))
        self.back.clicked.connect(lambda: self._go(-1))
        self.next.clicked.connect(lambda: self._go(1))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.addWidget(self.pages, 1)
        lay.addWidget(self.form.message)
        row = QHBoxLayout()
        row.addWidget(self.step)
        row.addStretch(1)
        row.addWidget(self.back)
        row.addWidget(self.next)
        lay.addLayout(row)
        self._go(0)

    def _go(self, d: int) -> None:
        if d > 0 and self.pages.currentIndex() == self._keys_page:
            missing = self.form.missing_keys()
            if missing and not self._warned_keys:  # ask once; Next again continues
                self._warned_keys = True
                names = ", ".join(dict(KEYS)[m] for m in missing)
                lost = "; ".join(KEY_HELP[m][1] for m in missing)
                self.form.message.setText(
                    f"Still missing: {names}. Without them: no {lost}. Paste them now, "
                    "or click Next again to add them later in Settings."
                )
                return
            self.form.message.setText("")
        i = self.pages.currentIndex() + d
        last = self.pages.count() - 1
        if i > last:
            if not self.form.apply():  # saved without problems
                self.cfg.set_setting("setup_done", True)
                self.finished.emit()
                self.close()
            return
        i = max(0, i)
        self.pages.setCurrentIndex(i)
        self.back.setEnabled(i > 0)
        self.next.setText("Finish" if i == last else "Next")
        self.step.setText(f"Step {i + 1} of {last + 1}")


def needs_first_run(cfg: Config) -> bool:
    """First start: not set up yet and nothing indexed (existing installs skip it)."""
    if cfg.get_setting("setup_done"):
        return False
    try:
        n = cfg.conn.execute("SELECT count(*) FROM files").fetchone()[0]
    except Exception:
        n = 0
    if n:
        cfg.set_setting("setup_done", True)  # (set up before Phase 8)
        return False
    return os.environ.get("ZACHARY_SKIP_SETUP") is None
