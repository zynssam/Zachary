from __future__ import annotations

import os

import pytest

from zachary.search.tokens import name_text, path_text, words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FeeReceipt_sem5 (final).PDF", ["fee", "receipt", "sem", "5", "final", "pdf"]),
        ("HTMLParser-v2", ["html", "parser", "v", "2"]),
        ("linked_list.cpp", ["linked", "list", "cpp"]),
        ("report_final_v2.docx", ["report", "final", "v", "2", "docx"]),
        ("फीस रसीद.pdf", ["फीस", "रसीद", "pdf"]),  # Devanagari vowel signs stay in the word
        ("ಶುಲ್ಕ ರಸೀದಿ_2024", ["ಶುಲ್ಕ", "ರಸೀದಿ", "2024"]),  # Kannada
        ("café Résumé", ["café", "résumé"]),
        ("", []),
    ],
)
def test_words(text: str, expected: list[str]) -> None:
    assert words(text) == expected


def test_name_and_path_text() -> None:
    assert name_text("Fee Receipt Sem 5.pdf") == "fee receipt sem 5 pdf"
    root = os.path.join(os.sep, "home", "u")
    p = os.path.join(root, "College", "Sem5", "Fees", "x.pdf")
    assert path_text(p, root) == "college sem 5 fees"
    assert path_text(os.path.join(root, "x.pdf"), root) == ""
