from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from tests.semantic_corpus import FEE_TEXT, build_corpus
from zachary.indexer.chunker import chunk_text
from zachary.indexer.extract import extract, extractable
from zachary.indexer.kinds import classify


@pytest.fixture
def corpus(home: Path) -> dict[str, Path]:
    return build_corpus(home)


def _ex(p: Path, max_bytes: int = 65536):  # type: ignore[no-untyped-def]
    ext, kind = classify(p.name, False)
    return extract(p, ext, kind, p.stat().st_size, max_bytes)


@pytest.mark.parametrize(
    ("name", "must_contain", "title"),
    [
        ("Unit2 notes.pdf", "binary search tree", "Data Structures Unit 2"),
        ("receipt_sem5.pdf", "due on 15 June 2026", "Fee Receipt Semester 5"),
        ("tree assignment.docx", "binary search tree", None),
        ("lecture4.pptx", "round robin", "Process scheduling"),
        ("expenses.xlsx", "tuition fee | 50000", None),
        ("ll.cpp", "reverse(node* head)", None),
        ("goa trip.md", "baga", None),
        ("paneer.txt", "garam masala", None),
    ],
)
def test_formats(corpus: dict[str, Path], name: str, must_contain: str, title: str | None) -> None:
    ex = _ex(corpus[name])
    assert ex is not None
    assert must_contain.lower() in " ".join(ex.text.lower().split())
    if title:
        assert ex.title == title


def test_name_only_and_bad_files(corpus: dict[str, Path], home: Path) -> None:
    assert _ex(corpus["IMG_2041.jpg"]) is None  # media: never opened
    bad = home / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not a real pdf")
    assert _ex(bad) is None  # parser errors -> name only, no crash
    fake_text = home / "data.txt"
    fake_text.write_bytes(b"MZ\x00\x00binary\x00stuff")
    assert _ex(fake_text) is None  # NUL bytes: binary despite .txt
    empty = home / "empty.md"
    empty.write_text("")
    assert _ex(empty) is None
    assert extractable(".pdf", "document") and not extractable(".mp4", "video")


def test_embed_less_limit(home: Path) -> None:
    big = home / "big.txt"
    big.write_text("word " * 100_000)
    ex = _ex(big, max_bytes=4096)
    assert ex is not None and len(ex.text) <= 4096 and ex.truncated


def test_chunker_boundaries_and_offsets() -> None:
    text = "\n\n".join(f"Paragraph {i}. " + "Sentence here. " * 30 for i in range(6))
    chunks = chunk_text(text)
    assert len(chunks) > 3
    for c in chunks:
        assert c.text == text[c.start : c.end].strip()
        assert len(c.text) <= 1000
    # chunks overlap a little and cover the whole text
    assert chunks[0].start == 0 and chunks[-1].end == len(text.rstrip())
    assert all(b.start < a.end for a, b in itertools.pairwise(chunks))
    assert chunk_text("   ") == []
    assert [c.text for c in chunk_text(FEE_TEXT)] == [FEE_TEXT.strip()]


def test_chunk_offsets_refer_to_the_given_text() -> None:
    text = "\n\n   Leading blank lines. " + "More words here. " * 80
    for c in chunk_text(text):
        assert text[c.start : c.end].strip() == c.text
    assert chunk_text(text)[0].start == text.index("Leading")
