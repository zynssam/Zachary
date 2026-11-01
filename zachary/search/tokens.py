"""Tokenization shared by the indexer (what goes into FTS) and search (queries).

Both sides must split text the same way, so ``FeeReceipt_sem5.pdf`` and the
query "fee receipt sem 5" produce the same words.
"""

from __future__ import annotations

import os
import re

# Python's \w excludes combining marks, which would split Indic words at every vowel
# sign, so the Indic blocks (Devanagari .. Malayalam, incl. Kannada) count as word chars.
_INDIC = "\u0900-\u0963\u0966-\u0dff"
_SEPARATORS = re.compile(rf"(?:[^\w{_INDIC}]|_)+", re.UNICODE)
# camelCase, ACRONYMWord, and letter/digit boundaries.
_BOUNDARY = re.compile(
    r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])"
)


def words(text: str) -> list[str]:
    """Split text into lowercase words on separators, camelCase and digit boundaries."""
    out: list[str] = []
    for chunk in _SEPARATORS.split(text):
        if not chunk:
            continue
        out.extend(w.lower() for w in _BOUNDARY.split(chunk) if w)
    return out


def name_text(name: str) -> str:
    """FTS text for a file name: its words, including the extension."""
    return " ".join(words(name))


def path_text(path: str, root: str | None) -> str:
    """FTS text for the folders between the index root and the file (not the name)."""
    parent = os.path.dirname(path)
    if root and (parent == root or parent.startswith(root.rstrip("\\/") + os.sep)):
        rel = parent[len(root) :]
    else:
        rel = parent
    return " ".join(words(rel))
