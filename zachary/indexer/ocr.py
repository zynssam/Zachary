"""Optional OCR (Phase 8): text from images and scanned PDFs, using Tesseract.

Off by default (setting ``ocr_enabled``). Tesseract is installed separately:
Linux ``dnf install tesseract tesseract-langpack-hin`` / ``apt install tesseract-ocr
tesseract-ocr-hin``; Windows: the UB-Mannheim installer (found in Program Files even if
it isn't on PATH). English, plus Hindi when its language pack is there.

Runs the ``tesseract`` program directly (no shell), image bytes on stdin, with a time
limit; nothing leaves the computer. Only the first pages of a scanned PDF are read
("embed less"), and images over ``MAX_IMAGE_BYTES`` are skipped.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from zachary.indexer.extract import OCR_IMAGE_EXTS as IMAGE_EXTS

log = logging.getLogger(__name__)


MAX_IMAGE_BYTES = 20 * 1024 * 1024
PDF_PAGES = 3  # scanned PDFs: OCR the first pages only
PDF_DPI = 200
TIMEOUT_S = 60
SCANNED_CHARS_PER_PAGE = 25  # a PDF page with less text than this is probably a scan


@functools.cache
def tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name == "nt":  # the usual Windows install places, even when not on PATH
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):  # fmt: skip
            exe = Path(base) / "Tesseract-OCR" / "tesseract.exe"
            if base and exe.is_file():
                return str(exe)
    return None


def available() -> bool:
    return tesseract() is not None


@functools.cache
def languages() -> str:
    exe = tesseract()
    if exe is None:
        return "eng"
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True,
                             timeout=10).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return "eng"
    have = {ln.strip() for ln in out.splitlines()}
    return "eng+hin" if "hin" in have else "eng"


def ocr_bytes(image: bytes) -> str:
    """Text in an image (PNG/JPEG/... bytes); "" if Tesseract is missing or fails."""
    exe = tesseract()
    if exe is None or not image:
        return ""
    try:
        r = subprocess.run([exe, "stdin", "stdout", "-l", languages(), "--psm", "3"],
                           input=image, capture_output=True, timeout=TIMEOUT_S)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("ocr failed: %s", type(e).__name__)
        return ""
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""


def ocr_image(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            return ""
        return ocr_bytes(path.read_bytes())
    except OSError:
        return ""


def ocr_pdf(path: Path, pages: int = PDF_PAGES) -> str:
    """OCR the first ``pages`` pages of a (scanned) PDF."""
    import pymupdf

    out: list[str] = []
    try:
        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc):
                if i >= pages:
                    break
                png = page.get_pixmap(dpi=PDF_DPI).tobytes("png")
                text = ocr_bytes(png)
                if text:
                    out.append(text)
    except Exception as e:  # malformed PDFs can raise anything
        log.debug("pdf ocr failed for %s: %s", path, type(e).__name__)
    return "\n".join(out)


def looks_scanned(text: str, pages: int) -> bool:
    return len(text.strip()) < SCANNED_CHARS_PER_PAGE * max(1, min(pages, PDF_PAGES))


def queue_for_ocr(conn: sqlite3.Connection) -> int:
    """OCR was just turned on: read images and text-less PDFs again (only those)."""
    exts = ",".join(f"'{e}'" for e in sorted(IMAGE_EXTS))
    cur = conn.execute(
        "UPDATE files SET embedded_at = NULL WHERE is_dir = 0 AND (lower(ext) IN (" + exts + ")"
        " OR (lower(ext) = '.pdf' AND id NOT IN (SELECT DISTINCT file_id FROM chunks)))"
    )
    conn.commit()
    return cur.rowcount
