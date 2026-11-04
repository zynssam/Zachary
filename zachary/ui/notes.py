"""Zachary's notes app: a small window to write, paste or dictate notes.

* Notes are plain ``.txt`` files in ``~/Documents/Zachary/Notes`` (indexed and
  searchable like any other file). A new note is named after its first line.
* Everything is saved automatically (a moment after you stop typing, when you switch
  notes and when the window closes). Writes are atomic (temp file + replace) and only
  ever go inside the notes folder. Nothing is ever deleted.
* Paste: Ctrl+V (as plain text) or the Paste button.
* Dictate: the mic button (or Ctrl+M, or the voice hotkey while this window is in
  front) starts listening; what you say is typed at the cursor until you click again or
  say "stop dictation". "new paragraph" starts a paragraph, "scratch that" removes the
  last piece, "paste" inserts what you copied. Only then is audio sent to Gnani.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from zachary.actions.creator import base_folder, safe_name
from zachary.brain.intents import NEW_PARAGRAPH, SCRATCH_THAT, STOP_DICTATION
from zachary.ui import style

log = logging.getLogger(__name__)

SAVE_AFTER_MS = 700
DEFAULT_PREFIX = "Note "
PATH_ROLE = Qt.ItemDataRole.UserRole
_PASTE_RE = re.compile(
    r"^\s*(?:please\s+)?paste(?:\s+(?:it|that|this|here|what i copied|what i have copied|"
    r"the clipboard|my clipboard))?\s*[.!]?\s*$",
    re.IGNORECASE,
)


def notes_folder() -> Path:
    return base_folder() / "Notes"


class NotesStore:
    """The .txt files in one folder; never writes outside it, never deletes."""

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or notes_folder()

    def list(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        notes = [p for p in self.folder.glob("*.txt") if p.is_file()]
        return sorted(notes, key=lambda p: p.stat().st_mtime, reverse=True)

    def _inside(self, path: Path) -> bool:
        try:
            return path.resolve().parent == self.folder.resolve()
        except OSError:
            return False

    def _free(self, stem: str) -> Path:
        path = self.folder / f"{stem}.txt"
        n = 2
        while path.exists():
            path = self.folder / f"{stem} ({n}).txt"
            n += 1
        return path

    def new_path(self) -> Path:
        return self._free(DEFAULT_PREFIX + time.strftime("%Y-%m-%d %H%M"))

    def load(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    def save(self, path: Path, text: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        if not self._inside(path):
            raise ValueError(f"not a note: {path}")
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".note-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def named_after(self, path: Path, text: str) -> Path:
        """A still-untitled note gets the name of its first line (once)."""
        if not path.name.startswith(DEFAULT_PREFIX):
            return path
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        stem = safe_name(first[:50]) if first else ""
        if not stem or stem == path.stem:
            return path
        new = self._free(stem)
        if path.exists():
            os.replace(path, new)
        return new


def dictated(text: str, before: str) -> str:
    """Speech -> text to insert after ``before``: sentences capitalised and ended,
    "new paragraph" -> a blank line, a space between pieces."""
    out = ""
    for i, piece in enumerate(NEW_PARAGRAPH.split(text)):
        ctx = before + out
        if i > 0:
            out += "\n\n" if ctx.strip() else ""
            ctx = before + out
        piece = piece.strip(" ,")
        if not piece:
            continue
        start = not ctx.strip() or ctx.rstrip(" ")[-1:] in {".", "!", "?", "\n"}
        if start:
            piece = piece[0].upper() + piece[1:]
        if piece[-1] not in ".!?:;\"')":
            piece += "."
        if ctx and not ctx[-1].isspace():
            out += " "
        out += piece
    return out


class RecDot(QWidget):
    """(look) A small red dot that pulses while the note's mic is on."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self.setVisible(False)
        self._a = 1.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(1.0)
        self._anim.setKeyValueAt(0.5, 0.25)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1100)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._step)

    def set_on(self, on: bool) -> None:
        self.setVisible(on)
        if on:
            self._anim.start()
        else:
            self._anim.stop()

    def _step(self, v: object) -> None:
        self._a = float(v)  # type: ignore[arg-type]
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(style.REC)
        c.setAlphaF(self._a)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(7, 7), 4.5, 4.5)
        p.end()


class NotesWindow(QWidget):
    mic_toggled = Signal(bool)  # dictation on/off (VoiceController listens)
    closed = Signal()

    def __init__(self, store: NotesStore | None = None) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Zachary Notes")
        self.resize(820, 560)
        self.store = store or NotesStore()
        self.path: Path | None = None  # the open note (None: new, not saved yet)
        self._last_piece = ""  # for "scratch that"
        self._loading = False

        self.theme = style.theme()  # (look)
        self.setObjectName("zwindow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFont(style.font(self.font()))
        self.notes = QListWidget(self)
        self.notes.setMinimumWidth(200)
        self.notes.currentItemChanged.connect(self._picked)
        self.new_btn = QPushButton("New note", self)
        self.new_btn.clicked.connect(lambda: self.new_note())
        left = QWidget(self)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("Notes", left)
        heading.setFont(style.font(heading.font(), 1.25, 600))
        lv.addWidget(heading)
        lv.addWidget(self.new_btn)
        lv.addWidget(self.notes, 1)

        self.editor = QPlainTextEdit(self)
        self.editor.setPlaceholderText("Write, paste (Ctrl+V) or press the mic and talk…")
        self.editor.setFont(style.font(self.editor.font(), 1.12))
        self.editor.textChanged.connect(self._changed)
        self.mic_btn = QPushButton("Dictate", self)
        self.mic_btn.setCheckable(True)
        self.mic_btn.setEnabled(False)
        self.mic_btn.setToolTip("Voice is off: python -m zachary.cli set voice_enabled true")
        self.mic_btn.toggled.connect(self._mic)
        self.paste_btn = QPushButton("Paste", self)
        self.paste_btn.clicked.connect(self.paste_clipboard)
        self.status = QLabel(self)
        # (look) flat icon buttons, a status pill, a pulsing red dot while dictating
        t = self.theme
        dpr = self.devicePixelRatioF()
        for btn, icon in ((self.new_btn, "notebook-pen"), (self.paste_btn, "copy"),
                          (self.mic_btn, "mic")):  # fmt: skip
            btn.setStyleSheet(style.button_css(t))
            btn.setIcon(QIcon(style.icon_pixmap(icon, t.text, 16, dpr)))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.status.setStyleSheet(style.pill_css(t))
        self.rec = RecDot(self)
        self.mic_btn.toggled.connect(self.rec.set_on)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        tools.addWidget(self.mic_btn)
        tools.addWidget(self.rec)
        tools.addWidget(self.paste_btn)
        tools.addStretch(1)
        tools.addWidget(self.status)
        right = QWidget(self)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(10)
        rv.addLayout(tools)
        rv.addWidget(self.editor, 1)

        split = QSplitter(self)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.addWidget(split)
        self.setStyleSheet(style.window_css(t))

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_AFTER_MS)
        self._save_timer.timeout.connect(self.save)
        mic = QShortcut(QKeySequence("Ctrl+M"), self)
        mic.activated.connect(self.mic_btn.click)
        save = QShortcut(QKeySequence.StandardKey.Save, self)
        save.activated.connect(self.save)

    # -- opening -------------------------------------------------------------------------
    def present(self, what: str = "open") -> None:
        """open (the latest note) | new | dictate (a new note, mic on)."""
        self.refresh()
        if what in {"new", "dictate"} or self.notes.count() == 0:
            self.new_note()
        elif self.path is None:
            self.notes.setCurrentRow(0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.editor.setFocus()
        if what == "dictate" and self.mic_btn.isEnabled() and not self.mic_btn.isChecked():
            self.mic_btn.setChecked(True)

    def refresh(self) -> None:
        self._loading = True
        self.notes.clear()
        for p in self.store.list():
            it = QListWidgetItem(p.stem)
            it.setData(PATH_ROLE, str(p))
            it.setToolTip(str(p))
            self.notes.addItem(it)
            if self.path is not None and p == self.path:
                self.notes.setCurrentItem(it)
        self._loading = False

    def new_note(self) -> None:
        self.save()
        self._loading = True
        self.path = None
        self.editor.clear()
        self.notes.clearSelection()
        self.notes.setCurrentRow(-1)
        self._loading = False
        self._last_piece = ""
        self.status.setText("New note")
        self.editor.setFocus()

    def open_note(self, path: Path) -> None:
        self.save()
        self._loading = True
        self.path = path
        self.editor.setPlainText(self.store.load(path))
        self.editor.moveCursor(self.editor.textCursor().MoveOperation.End)
        self._loading = False
        self._last_piece = ""
        self.status.setText("")

    def _picked(self, cur: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if self._loading or cur is None:
            return
        path = Path(str(cur.data(PATH_ROLE)))
        if path != self.path:
            self.open_note(path)

    # -- saving ----------------------------------------------------------------------------
    def _changed(self) -> None:
        if not self._loading:
            self.status.setText("Editing…")
            self._save_timer.start()

    def save(self) -> None:
        self._save_timer.stop()
        text = self.editor.toPlainText()
        if self.path is None and not text.strip():
            return  # an empty new note isn't a file
        try:
            if self.path is None:
                self.path = self.store.new_path()
            self.store.save(self.path, text)
            renamed = self.store.named_after(self.path, text)
            if renamed != self.path:
                self.path = renamed
            self.status.setText(f"Saved · {self.path.name}")
            if not self._loading:
                self.refresh()
        except (OSError, ValueError) as e:
            log.warning("notes: could not save %s: %s", self.path, e)
            self.status.setText(f"Couldn't save: {e}")

    def closeEvent(self, e: QCloseEvent) -> None:
        if self.mic_btn.isChecked():
            self.mic_btn.setChecked(False)
        self.save()
        self.closed.emit()
        super().closeEvent(e)

    # -- paste / dictation -----------------------------------------------------------------
    def paste_clipboard(self) -> bool:
        text = QGuiApplication.clipboard().text()
        if not text:
            self.status.setText("Nothing copied")
            return False
        self.editor.insertPlainText(text)
        self._last_piece = text
        return True

    def set_voice_available(self, on: bool) -> None:
        self.mic_btn.setEnabled(on)
        self.mic_btn.setToolTip("Dictate (Ctrl+M)" if on else self.mic_btn.toolTip())

    def _mic(self, on: bool) -> None:
        self.mic_btn.setText("Stop" if on else "Dictate")
        self.status.setText("Listening… say “stop dictation” to finish" if on else "")
        self.mic_toggled.emit(on)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def stop_dictation(self, why: str = "") -> None:
        if self.mic_btn.isChecked():
            self.mic_btn.setChecked(False)
        if why:
            self.status.setText(why)

    def insert_dictation(self, text: str) -> bool:
        """One piece of speech. Returns False when it said "stop dictation"."""
        stop = STOP_DICTATION.search(text)
        body = text[: stop.start()] if stop else text
        if SCRATCH_THAT.match(body):
            self._scratch()
        elif _PASTE_RE.match(body):
            self.paste_clipboard()
        elif body.strip(" .,!?"):
            cur = self.editor.textCursor()
            before = self.editor.toPlainText()[: cur.position()]
            piece = dictated(body, before)
            cur.insertText(piece)
            self.editor.setTextCursor(cur)
            self._last_piece = piece
        if stop:
            self.stop_dictation("Dictation stopped")
            self.save()
            return False
        return True

    def _scratch(self) -> None:
        piece = self._last_piece
        cur = self.editor.textCursor()
        pos = cur.position()
        if piece and self.editor.toPlainText()[:pos].endswith(piece):
            cur.setPosition(pos - len(piece), cur.MoveMode.MoveAnchor)
            cur.setPosition(pos, cur.MoveMode.KeepAnchor)
            cur.removeSelectedText()
            self._last_piece = ""
