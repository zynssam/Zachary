"""One place for the look: colours, fonts, icons, sizes. Widgets read everything from here.

Design "Orb": a glass card with a violet -> blue accent; the theme follows the system
(dark / light). Icons are Lucide SVGs (ISC), the font is Inter (SIL OFL); see
``ui/assets/LICENSES.md``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtGui import QPixmap

ASSETS = Path(__file__).with_name("assets")

# -- accent (both themes) -------------------------------------------------------------------
ACCENT = "#7C5CFF"  # violet
ACCENT_2 = "#3B82F6"  # blue (gradient end)
ACCENT_RGBA = (124, 92, 255)
# The orb and the opening wave: blue -> violet -> magenta.
ORB_BLUE = "#3B6BFF"
ORB_VIOLET = "#8B5CF6"
ORB_MAGENTA = "#E040FB"
WARN = "#F59E0B"  # amber: errors / no microphone
REC = "#EF4444"  # dictation recording dot
SELECTION_ALPHA = 46  # highlighted row: accent at this alpha
BADGE_TEXT = "white"
BORDER_RGBA = (128, 128, 128, 90)  # (older widgets; themed code uses Theme.border)
RADIUS = 14
ROW_HEIGHT = 54


@dataclass(frozen=True, slots=True)
class Theme:
    dark: bool
    card: tuple[int, int, int, int]  # the bar's glass card (RGBA)
    surface: str  # answer card, hovered row
    text: str
    dim: str  # paths, hints
    border: tuple[int, int, int, int]
    chip: str  # "why" pills
    shadow: tuple[int, int, int, int]


DARK = Theme(True, (17, 19, 24, 238), "#1C1F27", "#E8EAF0", "#9AA0AE",
             (255, 255, 255, 22), "#262A35", (0, 0, 0, 120))  # fmt: skip
LIGHT = Theme(False, (255, 255, 255, 246), "#F2F3F7", "#14161C", "#5F6675",
              (0, 0, 0, 22), "#E8EAF2", (20, 20, 40, 60))  # fmt: skip


def theme() -> Theme:
    """Follows the desktop's light/dark setting (Qt 6.5+), else the palette's brightness."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None:
            scheme = QGuiApplication.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return DARK
            if scheme == Qt.ColorScheme.Light:
                return LIGHT
            window = QGuiApplication.palette().window().color()
            return DARK if window.lightness() < 128 else LIGHT
    except Exception:  # pragma: no cover - (no Qt / no app)
        pass
    return DARK


# -- font -------------------------------------------------------------------------------------
FONT_FAMILY = "Inter"
FONT_SIZE = 10.5  # pt: rows; the input is ~1.45x


@functools.cache
def load_fonts() -> str:
    """Register the bundled Inter (once); returns the family to use."""
    from PySide6.QtGui import QFontDatabase

    fid = QFontDatabase.addApplicationFont(str(ASSETS / "fonts" / "InterVariable.ttf"))
    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    return fams[0] if fams else ""


# -- icons --------------------------------------------------------------------------------------
# kind -> (icon file, colour); some extensions get their own icon.
KIND_ICONS: dict[str, tuple[str, str]] = {
    "document": ("file-text", "#60A5FA"),
    "code": ("file-code", "#34D399"),
    "image": ("image", "#FBBF24"),
    "video": ("film", "#F87171"),
    "audio": ("file-music", "#A78BFA"),
    "archive": ("file-archive", "#94A3B8"),
    "binary": ("file", "#94A3B8"),
    "dir": ("folder", "#38BDF8"),
    "project": ("folder-git-2", "#F472B6"),
    "topic": ("sparkles", "#C084FC"),
    "session": ("clock", "#2DD4BF"),
    "app": ("app-window", "#4ADE80"),
    "other": ("file", "#94A3B8"),
}
EXT_ICONS: dict[str, tuple[str, str]] = {
    ".xlsx": ("file-spreadsheet", "#22C55E"), ".xls": ("file-spreadsheet", "#22C55E"),
    ".csv": ("file-spreadsheet", "#22C55E"), ".ods": ("file-spreadsheet", "#22C55E"),
    ".pptx": ("presentation", "#FB923C"), ".ppt": ("presentation", "#FB923C"),
    ".odp": ("presentation", "#FB923C"), ".pdf": ("file-text", "#F87171"),
}  # fmt: skip

# (Kept for older importers: letter badges are no longer drawn.)
BADGES: dict[str, tuple[str, str]] = {k: (k[:1].upper(), c) for k, (_f, c) in KIND_ICONS.items()}


def icon_for(kind: str, path: str = "") -> tuple[str, str]:
    ext = Path(path).suffix.lower()
    return EXT_ICONS.get(ext) or KIND_ICONS.get(kind) or KIND_ICONS["other"]


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
    r = QSvgRenderer(QByteArray(_svg(name, color)))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p)
    p.end()
    return px


# -- widget styles ----------------------------------------------------------------------------
def answer_css(t: Theme) -> str:
    return (
        f"padding: 10px 12px; border-radius: 10px; color: {t.text};"
        f" background: {t.surface}; border-left: 3px solid {ACCENT};"
    )


def notepad_css(t: Theme) -> str:
    """Dictation: the growing document, like a sheet of paper."""
    paper = "#FBFAF6" if not t.dark else "#1A1C22"
    return (
        f"padding: 14px 16px; border-radius: 10px; color: {t.text}; background: {paper};"
        f" border: 1px solid rgba{t.border}; border-top: 3px solid {REC}; line-height: 150%;"
    )


def status_css(t: Theme) -> str:
    return f"color: {t.dim}; padding: 2px 4px;"


def edit_css(t: Theme) -> str:
    return (
        f"QLineEdit {{ background: transparent; border: none; color: {t.text};"
        f" selection-background-color: {ACCENT}; }}"
    )


# (Older names kept for importers.)
ANSWER_CSS = answer_css(DARK)


def secondary_button_css(t: Theme) -> str:
    return (
        f"QPushButton {{ background: {t.surface}; color: {t.text}; padding: 8px 16px;"
        f" border-radius: 8px; border: 1px solid rgba{t.border}; }}"
        f" QPushButton:hover {{ background: {t.chip}; }}"
    )


# The plan review window (Phase 7): category -> (icon, colour) for its summary cards.
CATEGORY_ICONS: dict[str, tuple[str, str]] = {
    "duplicate": ("copy", "#60A5FA"), "installer": ("app-window", "#FB923C"),
    "junk": ("trash", "#F87171"), "empty_folder": ("folder", "#38BDF8"),
    "large": ("file-archive", "#A78BFA"), "organize": ("sparkles", ACCENT),
}  # fmt: skip


def card_css(t: Theme) -> str:
    return (
        f"QFrame#card {{ background: {t.surface}; border-radius: 12px;"
        f" border: 1px solid rgba{t.border}; }}"
        f" QLabel {{ color: {t.text}; background: transparent; }}"
    )


def tree_css(t: Theme) -> str:
    return (
        f"QTreeWidget {{ background: {t.surface}; color: {t.text}; border: none;"
        f" border-radius: 12px; padding: 6px; alternate-background-color: {t.chip}; }}"
        f" QTreeWidget::item {{ padding: 4px 2px; }}"
        f" QTreeWidget::item:selected {{ background: rgba({ACCENT_RGBA[0]}, {ACCENT_RGBA[1]},"
        f" {ACCENT_RGBA[2]}, {SELECTION_ALPHA}); color: {t.text}; }}"
        f" QHeaderView {{ background: transparent; border: none; }}"
        f" QHeaderView::section {{ background: {t.surface}; color: {t.dim}; border: none;"
        f" padding: 4px; font-weight: 600; }}"
    )


PLAN_WINDOW_SIZE = (900, 600)
PRIMARY_BUTTON_CSS = (
    f"QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT},"
    f" stop:1 {ACCENT_2}); color: white; padding: 8px 18px; border-radius: 8px;"
    " font-weight: 600; } QPushButton:disabled { background: #94a3b8; }"
)
