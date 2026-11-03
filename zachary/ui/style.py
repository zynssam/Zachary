"""One place for the look: colours, badges, sizes. The design pass (before Phase 8)
should only need to change this file; widgets read everything from here."""

from __future__ import annotations

ACCENT = "#3b82f6"
ACCENT_RGBA = (59, 130, 246)
SELECTION_ALPHA = 46  # highlighted row: accent at this alpha
BADGE_TEXT = "white"
BORDER_RGBA = (128, 128, 128, 90)

# Result badges: letter + colour per item kind.
BADGES: dict[str, tuple[str, str]] = {
    "document": ("D", "#3b82f6"),
    "code": ("C", "#10b981"),
    "image": ("I", "#f59e0b"),
    "video": ("V", "#ef4444"),
    "audio": ("A", "#8b5cf6"),
    "archive": ("Z", "#64748b"),
    "binary": ("B", "#64748b"),
    "dir": ("F", "#0ea5e9"),
    "project": ("P", "#ec4899"),
    "topic": ("T", "#a855f7"),
    "session": ("S", "#14b8a6"),
    "app": ("▶", "#22c55e"),
    "other": ("·", "#94a3b8"),
}

# The answer panel in the quick bar (question answers and their source passage).
ANSWER_CSS = (
    "padding: 8px 10px; border-radius: 8px;"
    f" background: rgba({ACCENT_RGBA[0]}, {ACCENT_RGBA[1]}, {ACCENT_RGBA[2]}, 22);"
)

# The plan review window (Phase 7).
PLAN_WINDOW_SIZE = (900, 560)
PRIMARY_BUTTON_CSS = (
    f"QPushButton {{ background: {ACCENT}; color: white; padding: 6px 14px; border-radius: 6px; }}"
    " QPushButton:disabled { background: #94a3b8; }"
)
