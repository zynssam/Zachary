"""Text extraction per file type. "Embed less": only the start of each document.

PDF (PyMuPDF), DOCX, PPTX, XLSX, and plain text / code. Media, archives and
binaries are never opened (name only). Every extractor stops after
``max_bytes`` of text, so a 900-page PDF costs about as much as a 3-page one.
Failures return ``None``; the file is still findable by name.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".tex", ".csv", ".tsv", ".log", ".org", ".rtf",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
})  # fmt: skip
OFFICE_EXTS = frozenset({".pdf", ".docx", ".pptx", ".xlsx"})
MAX_FILE_BYTES = 200 * 1024 * 1024  # never open files bigger than this
TEXT_READ_FACTOR = 2  # read up to 2x max_bytes of raw bytes for text files


@dataclass(slots=True)
class Extracted:
    text: str
    title: str | None = None
    truncated: bool = False


def _clip(parts: list[str], max_bytes: int) -> tuple[str, bool]:
    out: list[str] = []
    size = 0
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if size + len(p) > max_bytes:
            out.append(p[: max(0, max_bytes - size)])
            return "\n".join(out), True
        out.append(p)
        size += len(p) + 1
    return "\n".join(out), False


def _pdf(path: Path, max_bytes: int) -> Extracted:
    import pymupdf

    with pymupdf.open(path) as doc:
        meta_title = (doc.metadata or {}).get("title") or None
        parts: list[str] = []
        size = 0
        for page in doc:
            t = page.get_text("text")
            parts.append(t)
            size += len(t)
            if size >= max_bytes:
                break
    text, truncated = _clip(parts, max_bytes)
    return Extracted(text, meta_title or _first_line(text), truncated or size >= max_bytes)


def _docx(path: Path, max_bytes: int) -> Extracted:
    import docx

    d = docx.Document(str(path))
    parts: list[str] = []
    size = 0
    for para in d.paragraphs:
        parts.append(para.text)
        size += len(para.text)
        if size >= max_bytes:
            break
    else:
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
                size += len(parts[-1])
                if size >= max_bytes:
                    break
    text, truncated = _clip(parts, max_bytes)
    title = d.core_properties.title or _first_line(text)
    return Extracted(text, title or None, truncated)


def _pptx(path: Path, max_bytes: int) -> Extracted:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts: list[str] = []
    title: str | None = None
    size = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                t = shape.text_frame.text
                if title is None and t.strip():
                    title = t.strip().splitlines()[0]
                parts.append(t)
                size += len(t)
        if size >= max_bytes:
            break
    text, truncated = _clip(parts, max_bytes)
    return Extracted(text, title, truncated)


def _xlsx(path: Path, max_bytes: int) -> Extracted:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    size = 0
    try:
        for ws in wb.worksheets:
            parts.append(f"[{ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    line = " | ".join(cells)
                    parts.append(line)
                    size += len(line)
                if size >= max_bytes:
                    break
            if size >= max_bytes:
                break
    finally:
        wb.close()
    text, truncated = _clip(parts, max_bytes)
    return Extracted(text, None, truncated)


def _plain(path: Path, max_bytes: int) -> Extracted | None:
    with open(path, "rb") as f:
        raw = f.read(max_bytes * TEXT_READ_FACTOR)
    if b"\x00" in raw[:4096]:
        return None  # binary despite the extension
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > max_bytes
    text = text[:max_bytes]
    return Extracted(text, _first_line(text), truncated)


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if 3 <= len(line) <= 200:
            return line
    return None


def extract(path: Path, ext: str, kind: str, size: int, max_bytes: int) -> Extracted | None:
    """Text from the start of a document / code / text file, or None (name only)."""
    if size <= 0 or size > MAX_FILE_BYTES:
        return None
    try:
        if ext == ".pdf":
            return _pdf(path, max_bytes)
        if ext == ".docx":
            return _docx(path, max_bytes)
        if ext == ".pptx":
            return _pptx(path, max_bytes)
        if ext == ".xlsx":
            return _xlsx(path, max_bytes)
        if kind == "code" or ext in TEXT_EXTS:
            return _plain(path, max_bytes)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as e:
        log.debug("extract failed for %s: %s", path, type(e).__name__)
        return None
    except Exception as e:  # malformed files can raise anything inside parsers
        log.debug("extract failed for %s: %s", path, type(e).__name__)
        return None
    return None


def extractable(ext: str, kind: str) -> bool:
    return ext in OFFICE_EXTS or kind == "code" or ext in TEXT_EXTS
