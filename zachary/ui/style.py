"""One place for the look: colours, fonts, icons, sizes. Widgets read everything here.

Design v2: minimal, animated. A dark, slightly translucent glass card with a hairline
border and one soft violet accent (``ALWAYS_DARK``; a light theme exists too). Icons
are Lucide line icons (ISC), the font is Inter (SIL OFL): ``ui/assets/LICENSES.md``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtGui import QFont, QPixmap

ASSETS = Path(__file__).with_name("assets")

ACCENT = "#8B7CFF"  # soft violet, used sparingly
ACCENT_RGBA = (139, 124, 255)
WARN = "#F5A524"  # errors / no microphone
REC = "#FF5C5C"  # dictating
SELECTION_ALPHA = 40  # (older name) selected row: accent at this alpha
BADGE_TEXT = "white"
BORDER_RGBA = (128, 128, 128, 70)
RADIUS = 16


@dataclass(frozen=True, slots=True)
class Theme:
    dark: bool
    glass: tuple[int, int, int, int]  # the bar's translucent card
    window: str  # sub-windows (notes, review)
    surface: str  # cards, the editor, hovered rows
    text: str
    dim: str  # folders, hints
    faint: str  # pills, hairlines
    border: tuple[int, int, int, int]
    hover: tuple[int, int, int, int]
    select: tuple[int, int, int, int]


DARK = Theme(True, (16, 18, 24, 242), "#14161C", "#1B1E26", "#ECEDF2", "#8E94A3",
             "#262A34", (255, 255, 255, 26), (255, 255, 255, 12), (139, 124, 255, 46))  # fmt: skip
LIGHT = Theme(False, (255, 255, 255, 245), "#F7F7FA", "#FFFFFF", "#15171D", "#6B7180",
              "#ECEDF3", (0, 0, 0, 22), (0, 0, 0, 9), (139, 124, 255, 38))  # fmt: skip


ALWAYS_DARK = True  # dark everywhere, whatever the system uses (False: follow the system)


def theme() -> Theme:
    """Dark (``ALWAYS_DARK``), or the desktop's light/dark setting (Qt 6.5+)."""
    if ALWAYS_DARK:
        return DARK
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is not None:
            scheme = QGuiApplication.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return DARK
            if scheme == Qt.ColorScheme.Light:
                return LIGHT
            window = QGuiApplication.palette().window().color()
            return DARK if window.lightness() < 128 else LIGHT
    except Exception:  # pragma: no cover - (no Qt)
        pass
    return DARK


def use_dark(app: object) -> None:
    """(look) Ask Qt for dark menus, checkboxes and window decorations too."""
    if not ALWAYS_DARK:
        return
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except Exception:  # pragma: no cover - (older Qt)
        pass


def rgba(c: tuple[int, int, int, int]) -> str:
    return f"rgba({c[0]}, {c[1]}, {c[2]}, {c[3]})"


# -- font -------------------------------------------------------------------------------
@functools.cache
def load_fonts() -> str:
    """Register the bundled Inter (once); returns its family ("" if unavailable)."""
    from PySide6.QtGui import QFontDatabase

    fid = QFontDatabase.addApplicationFont(str(ASSETS / "fonts" / "InterVariable.ttf"))
    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    return fams[0] if fams else ""


def font(base: QFont | None = None, scale: float = 1.0, weight: int | None = None) -> QFont:
    """A QFont in Inter (falls back to the system font), scaled from ``base``."""
    from PySide6.QtGui import QFont

    f = QFont(base) if base is not None else QFont()
    family = load_fonts()
    if family:
        f.setFamily(family)
    if scale != 1.0:
        f.setPointSizeF(f.pointSizeF() * scale)
    if weight is not None:
        f.setWeight(QFont.Weight(weight))
    return f


# -- icons --------------------------------------------------------------------------------
# kind -> (icon, colour): muted tints, the icon is a thin line drawing.
KIND_ICONS: dict[str, tuple[str, str]] = {
    "document": ("file-text", "#7EA6F7"),
    "code": ("file-code", "#6CCB9F"),
    "image": ("image", "#E8B65C"),
    "video": ("film", "#EE8A8A"),
    "audio": ("file-music", "#B49CF2"),
    "archive": ("file-archive", "#9AA3B5"),
    "binary": ("file", "#9AA3B5"),
    "dir": ("folder", "#72BEEB"),
    "project": ("folder-git-2", "#E98FC0"),
    "topic": ("sparkles", "#B49CF2"),
    "session": ("clock", "#64C9BE"),
    "app": ("app-window", "#7DD39A"),
    "other": ("file", "#9AA3B5"),
}
EXT_ICONS: dict[str, tuple[str, str]] = {
    ".xlsx": ("file-spreadsheet", "#6CCB9F"), ".xls": ("file-spreadsheet", "#6CCB9F"),
    ".csv": ("file-spreadsheet", "#6CCB9F"), ".ods": ("file-spreadsheet", "#6CCB9F"),
    ".pptx": ("presentation", "#EFA06B"), ".ppt": ("presentation", "#EFA06B"),
    ".odp": ("presentation", "#EFA06B"), ".pdf": ("file-text", "#EE8A8A"),
}  # fmt: skip
# The review window: plan category -> icon, colour.
CATEGORY_ICONS: dict[str, tuple[str, str]] = {
    "duplicate": ("copy", "#7EA6F7"), "installer": ("app-window", "#EFA06B"),
    "junk": ("trash", "#EE8A8A"), "empty_folder": ("folder", "#72BEEB"),
    "large": ("file-archive", "#B49CF2"), "organize": ("sparkles", ACCENT),
}  # fmt: skip

# (Kept for older importers: letter badges are no longer drawn.)
BADGES: dict[str, tuple[str, str]] = {k: (k[:1].upper(), c) for k, (_i, c) in KIND_ICONS.items()}


def icon_for(kind: str, path: str = "") -> tuple[str, str]:
    return EXT_ICONS.get(Path(path).suffix.lower()) or KIND_ICONS.get(kind) or KIND_ICONS["other"]


@functools.cache
def _svg(name: str, color: str) -> bytes:
    raw = (ASSETS / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    return raw.replace("currentColor", color).encode()


@functools.cache
def icon_pixmap(name: str, color: str, size: int, dpr: float = 1.0) -> QPixmap:
    """A crisp QPixmap of a Lucide icon in ``color`` (cached)."""
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    px = QPixmap(int(size * dpr), int(size * dpr))
    px.fill(Qt.GlobalColor.transparent)
    px.setDevicePixelRatio(dpr)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(_svg(name, color))).render(p)
    p.end()
    return px


# -- stylesheets ------------------------------------------------------------------------------
def edit_css(t: Theme) -> str:
    return (
        f"QLineEdit {{ background: transparent; border: none; color: {t.text};"
        f" selection-background-color: {rgba(t.select)}; }}"
    )


def status_css(t: Theme) -> str:
    return f"color: {t.dim}; padding: 0 2px; background: transparent;"


def answer_css(t: Theme) -> str:
    return (
        f"padding: 10px 12px; border-radius: 10px; color: {t.text};"
        f" background: {rgba(t.hover)}; border: 1px solid {rgba(t.border)};"
    )


def button_css(t: Theme) -> str:
    """Flat buttons for the sub-windows; checked = accent tint."""
    return (
        f"QPushButton {{ background: transparent; color: {t.text}; border: 1px solid"
        f" {rgba(t.border)}; border-radius: 9px; padding: 6px 14px; }}"
        f" QPushButton:hover {{ background: {rgba(t.hover)}; }}"
        f" QPushButton:checked {{ background: {rgba(t.select)}; border-color: {ACCENT}; }}"
        f" QPushButton:disabled {{ color: {t.dim}; }}"
    )


def primary_css(t: Theme) -> str:
    return (
        f"QPushButton {{ background: {ACCENT}; color: white; border: none; border-radius: 9px;"
        " padding: 7px 18px; font-weight: 600; }"
        " QPushButton:hover { background: #9C8FFF; }"
        f" QPushButton:disabled {{ background: {t.faint}; color: {t.dim}; }}"
    )


def window_css(t: Theme) -> str:
    """Notes / review windows: soft surface, borderless lists and editors."""
    return (
        (
            f"QWidget#zwindow {{ background: {t.window}; }}"
            f" QLabel {{ color: {t.text}; background: transparent; }}"
            f" QListWidget {{ background: transparent; border: none; color: {t.text};"
            "  outline: none; }"
            " QListWidget::item { padding: 8px 10px; border-radius: 8px; margin: 1px 4px; }"
            f" QListWidget::item:hover {{ background: {rgba(t.hover)}; }}"
            f" QListWidget::item:selected {{ background: {rgba(t.select)}; color: {t.text}; }}"
            f" QPlainTextEdit {{ background: {t.surface}; color: {t.text}; border: 1px solid"
            f"  {rgba(t.border)}; border-radius: 12px; padding: 14px 16px;"
            f"  selection-background-color: {rgba(t.select)}; }}"
            f" QTreeWidget {{ background: {t.surface}; color: {t.text}; border: 1px solid"
            f"  {rgba(t.border)}; border-radius: 12px; padding: 6px; outline: none;"
            f"  alternate-background-color: {rgba(t.hover)}; }}"
            " QTreeWidget::item { padding: 4px 2px; }"
            f" QTreeWidget::item:selected {{ background: {rgba(t.select)}; color: {t.text}; }}"
            f" QHeaderView {{ background: transparent; border: none; }}"
            f" QHeaderView::section {{ background: {t.surface}; color: {t.dim}; border: none;"
            "  padding: 6px 4px; font-weight: 600; }"
            " QSplitter::handle { background: transparent; width: 8px; }"
            " QScrollArea { background: transparent; border: none; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }"
            f" QCheckBox {{ color: {t.text}; spacing: 8px; background: transparent; }}"
            f" QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;"
            f"  border: 1.5px solid {t.dim}; background: transparent; }}"
            f" QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}"
            f" QLineEdit, QComboBox {{ background: {t.surface}; color: {t.text}; border: 1px solid"
            f"  {rgba(t.border)}; border-radius: 8px; padding: 6px 10px;"
            f"  selection-background-color: {rgba(t.select)}; }}"
            f" QComboBox QAbstractItemView {{ background: {t.surface}; color: {t.text}; }}"
            f" QListWidget#plain {{ background: {t.surface}; border: 1px solid {rgba(t.border)};"
            "  border-radius: 10px; }"
            " QScrollBar:horizontal { height: 0; }"
            " QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }"
            f" QScrollBar::handle:vertical {{ background: {t.faint}; border-radius: 4px;"
            "  min-height: 24px; }"
            " QScrollBar::add-line, QScrollBar::sub-line { height: 0; }"
        )
        + " "
        + button_css(t)
    )


def pill_css(t: Theme) -> str:
    return f"color: {t.dim}; background: {rgba(t.hover)}; border-radius: 10px; padding: 3px 10px;"


# -- older names (kept for importers) ------------------------------------------------------
ANSWER_CSS = answer_css(DARK)
PLAN_WINDOW_SIZE = (900, 600)
PRIMARY_BUTTON_CSS = primary_css(DARK)
