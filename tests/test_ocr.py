"""Optional OCR (Phase 8): images and scanned PDFs, through Tesseract (faked here)."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from zachary.indexer import extract as ex
from zachary.indexer import ocr


@pytest.fixture
def fake_tesseract(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        assert "shell" not in kw  # never through a shell
        assert kw.get("timeout")  # always time-limited
        return subprocess.CompletedProcess(cmd, 0, b"Hostel fee due 15 November", b"")

    monkeypatch.setattr(ocr, "tesseract", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(ocr, "languages", lambda: "eng+hin")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_images_are_read_only_when_ocr_is_on(
    tmp_path: Path, fake_tesseract: list[list[str]]
) -> None:
    img = tmp_path / "receipt.png"
    img.write_bytes(b"\x89PNG fake")
    assert not ex.extractable(".png", "image")  # off: names only (as before)
    assert ex.extract(img, ".png", "image", img.stat().st_size, 4096) is None
    assert ex.extractable(".png", "image", ocr=True)
    got = ex.extract(img, ".png", "image", img.stat().st_size, 4096, ocr=True)
    assert got is not None and "15 November" in got.text
    cmd = fake_tesseract[-1]
    assert cmd[1:3] == ["stdin", "stdout"] and cmd[cmd.index("-l") + 1] == "eng+hin"


def test_scanned_pdf_gets_ocr_a_text_pdf_does_not(tmp_path: Path,
                                                  fake_tesseract: list[list[str]]) -> None:  # fmt: skip
    import pymupdf

    scan = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    doc.new_page()  # an image-only page: no text layer
    doc.save(scan)
    text_pdf = tmp_path / "text.pdf"
    doc2 = pymupdf.open()
    doc2.new_page().insert_text((72, 72), "Semester 5 tuition fee paid in full. " * 4)
    doc2.save(text_pdf)
    got = ex.extract(scan, ".pdf", "document", scan.stat().st_size, 4096, ocr=True)
    assert got is not None and "Hostel fee" in got.text
    n = len(fake_tesseract)
    got2 = ex.extract(text_pdf, ".pdf", "document", text_pdf.stat().st_size, 4096, ocr=True)
    assert got2 is not None and "tuition" in got2.text and len(fake_tesseract) == n  # no OCR


def test_no_tesseract_means_no_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "tesseract", lambda: None)
    assert ocr.ocr_bytes(b"x") == "" and not ocr.available()


def test_turning_ocr_on_requeues_only_images_and_scans(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "INSERT INTO files (id, path, name, ext, embedded_at) VALUES"
        " (1, '/a.png', 'a.png', '.png', 5), (2, '/b.pdf', 'b.pdf', '.pdf', 5),"
        " (3, '/c.pdf', 'c.pdf', '.pdf', 5), (4, '/d.docx', 'd.docx', '.docx', 5);"
        " INSERT INTO chunks (file_id, ord, text_excerpt, char_start, char_end) VALUES (3, 0, 'text', 0, 4);"
    )
    assert ocr.queue_for_ocr(conn) == 2
    pending = [r[0] for r in conn.execute("SELECT id FROM files WHERE embedded_at IS NULL")]
    assert sorted(pending) == [1, 2]  # the image and the PDF without text; not c.pdf or d.docx


@pytest.mark.skipif(not ocr.available(), reason="needs Tesseract installed")
def test_real_tesseract_reads_an_image(tmp_path: Path) -> None:
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter

    _app = QGuiApplication.instance() or QGuiApplication([])
    img = QImage(900, 160, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    f = QFont()
    f.setPixelSize(56)
    p.setFont(f)
    p.setPen(QColor("black"))
    p.drawText(30, 100, "Hostel fee due")
    p.end()
    path = tmp_path / "shot.png"
    img.save(str(path))
    assert "fee" in ocr.ocr_image(path).lower()
