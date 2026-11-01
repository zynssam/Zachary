"""File kind classification by extension."""

from __future__ import annotations

DOCUMENT_EXTS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".tex",
        ".ppt",
        ".pptx",
        ".odp",
        ".key",
        ".xls",
        ".xlsx",
        ".ods",
        ".csv",
        ".tsv",
        ".epub",
        ".pages",
        ".numbers",
        ".org",
        ".log",
    }
)
CODE_EXTS = frozenset(
    {
        ".py",
        ".pyi",
        ".ipynb",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".hh",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".swift",
        ".m",
        ".mm",
        ".dart",
        ".lua",
        ".r",
        ".jl",
        ".hs",
        ".ml",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".sql",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".vue",
        ".svelte",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".xml",
        ".gradle",
        ".cmake",
        ".mk",
    }
)
IMAGE_EXTS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".heic",
        ".tif",
        ".tiff",
        ".svg",
        ".ico",
        ".raw",
        ".cr2",
        ".nef",
        ".psd",
        ".xcf",
    }
)
AUDIO_EXTS = frozenset({".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus", ".wma"})
VIDEO_EXTS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v"})
ARCHIVE_EXTS = frozenset(
    {
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".zst",
        ".iso",
    }
)
BINARY_EXTS = frozenset(
    {
        ".exe",
        ".msi",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".img",
        ".dmg",
        ".deb",
        ".rpm",
        ".appimage",
        ".apk",
        ".jar",
        ".class",
        ".o",
        ".obj",
        ".pyc",
        ".sqlite",
        ".db",
    }
)

_BY_EXT: dict[str, str] = {}
for _kind, _exts in (
    ("document", DOCUMENT_EXTS),
    ("code", CODE_EXTS),
    ("image", IMAGE_EXTS),
    ("audio", AUDIO_EXTS),
    ("video", VIDEO_EXTS),
    ("archive", ARCHIVE_EXTS),
    ("binary", BINARY_EXTS),
):
    for _e in _exts:
        _BY_EXT[_e] = _kind

KINDS = ("dir", "document", "code", "image", "audio", "video", "archive", "binary", "other")


def split_ext(name: str) -> str:
    """Lowercase extension including the dot ('' if none). Dotfiles have no extension."""
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return name[dot:].lower()


def classify(name: str, is_dir: bool) -> tuple[str, str]:
    """Return ``(ext, kind)`` for an entry."""
    if is_dir:
        return "", "dir"
    ext = split_ext(name)
    return ext, _BY_EXT.get(ext, "other")
