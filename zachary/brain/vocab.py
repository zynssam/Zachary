"""Your names: projects, apps, topics, top-level folders. Two uses:

1. **Speech hints.** Gnani's Prisma v3 takes up to 200 ``keyterms`` (names, rare words)
   to favour. Only these *names* are sent, never file contents (``stt_keyterms``
   setting, on by default).
2. **Sound-alike correction.** Speech recognition still mishears names ("Zakari",
   "Zachery", "zack ary"); a word that isn't in the index but *sounds like* one of your
   names is replaced by it before the request is understood.
"""

from __future__ import annotations

import difflib
import os
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from zachary.search.tokens import words

MAX_KEYTERMS = 200  # Gnani's limit
# Only words this long are corrected, and only to names this long: short words are
# too often ordinary ones ("many" was once turned into a folder called "main").
MIN_LEN = 5
CACHE_S = 600.0
GENERIC = frozenset(
    """
    desktop documents downloads music pictures videos templates public new folder untitled
    src lib bin build dist test tests docs data assets images files misc temp tmp
    main backend frontend server client app apps dummy sample samples scripts utils config
    public static components pages models views
    """.split()
)


def names(conn: sqlite3.Connection, roots: list[str], app_names: list[str] | None = None
          ) -> list[str]:  # fmt: skip
    """Distinctive names, most important first (projects, apps, topics, folders)."""
    out: list[str] = []

    def add(n: str) -> None:
        n = re.sub(r"[_\-]+", " ", n).strip()
        if len(n) >= 3 and n.lower() not in GENERIC and n not in out and not n.startswith("."):
            out.append(n)

    for (name,) in conn.execute(
        "SELECT f.name FROM projects p JOIN files f ON f.id = p.folder_id"
        " ORDER BY p.last_activity DESC"
    ):
        add(str(name))
    for n in app_names or []:
        add(n)
    for (label,) in conn.execute("SELECT label FROM topics ORDER BY last_ts DESC LIMIT 60"):
        for part in str(label).split(","):
            add(part.strip())
    for root in roots:  # top-level folders and their children
        for depth_one in _dirs(Path(root)):
            add(depth_one.name)
            for depth_two in _dirs(depth_one)[:15]:
                add(depth_two.name)
    return out[:MAX_KEYTERMS]


def _dirs(p: Path) -> list[Path]:
    try:
        return sorted(
            (d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")),
            key=lambda d: -d.stat().st_mtime,
        )[:40]
    except OSError:
        return []


# -- sound-alike matching --------------------------------------------------------------------
_RULES = (
    ("ph", "f"), ("ck", "k"), ("ch", "k"), ("sch", "sk"), ("gh", ""), ("kn", "n"),
    ("wr", "r"), ("dg", "j"), ("qu", "kw"), ("q", "k"), ("x", "ks"), ("z", "s"),
    ("c", "k"), ("y", "i"), ("w", ""), ("h", ""), ("v", "f"), ("b", "p"), ("d", "t"),
    ("g", "k"),
)  # fmt: skip


def sound_key(word: str) -> str:
    """A rough pronunciation key: "zachary", "zakari", "zackery" -> "skr"."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    for a, b in _RULES:
        w = w.replace(a, b)
    first, rest = w[0], re.sub(r"[aeiou]", "", w[1:])
    key = (first if first not in "aeiou" else "a") + rest
    return re.sub(r"(.)\1+", r"\1", key)


class Corrector:
    """Replaces misheard words by your names when they sound alike."""

    def __init__(self, vocabulary: list[str], known: Callable[[str], bool]) -> None:
        self.known = known
        self.by_key: dict[str, list[str]] = {}
        for name in vocabulary:
            for w in words(name):
                if len(w) >= MIN_LEN and w not in GENERIC:
                    self.by_key.setdefault(sound_key(w), [])
                    if w not in self.by_key[sound_key(w)]:
                        self.by_key[sound_key(w)].append(w)

    def _match(self, heard: str, *, strict: bool = False) -> str | None:
        cands = self.by_key.get(sound_key(heard), [])
        best, best_r = None, 0.0
        for c in cands:
            r = difflib.SequenceMatcher(None, heard, c).ratio()
            # Same first letter ("zakari" / "zachary"), or spelled almost the same: a real
            # word that merely sounds similar ("scary") is left alone.
            ok = r >= 0.8 if strict else (c[0] == heard[0] and r >= 0.5) or r >= 0.8
            if ok and r > best_r:
                best, best_r = c, r
        return best

    def correct(self, text: str) -> tuple[str, dict[str, str]]:
        toks = text.split()
        out: list[str] = []
        fixed: dict[str, str] = {}
        i = 0
        while i < len(toks):
            raw = toks[i]
            w = re.sub(r"[^\w']", "", raw.lower())
            # One name heard as two words ("zack ary"): only a near-exact spelling counts,
            # since both halves may be ordinary words.
            if len(w) >= 2 and i + 1 < len(toks):
                nxt = re.sub(r"[^\w']", "", toks[i + 1].lower())
                both = w + nxt
                if nxt and len(nxt) <= 5 and len(both) >= 5 and not self.known(both):
                    joined = self._match(both, strict=True)
                    if joined is not None:
                        out.append(joined)
                        fixed[f"{raw} {toks[i + 1]}"] = joined
                        i += 2
                        continue
            if len(w) >= MIN_LEN and not self.known(w):
                m = self._match(w)
                if m is not None and m != w:
                    out.append(m)
                    fixed[raw] = m
                    i += 1
                    continue
            out.append(raw)
            i += 1
        return " ".join(out), fixed


def _parser_words() -> frozenset[str]:
    from zachary.brain import intents as i

    sets = (
        i.QUESTION_LEAD, i.STAT_THINGS, i.CREATE_THINGS, i.CREATE_VERBS, i.CLEAN_WORDS,
        i.ORGANIZE_WORDS, i.PROJECT_WORDS, i.TOPIC_WORDS, i.EXPLAIN_WORDS, i.DICTATE_WORDS,
        i.PASTE_WORDS, i.YES_WORDS, i.NO_WORDS, i.WORK_WORDS,
    )  # fmt: skip
    return frozenset().union(*sets)


class Vocabulary:
    """Cached names + corrector for one database (rebuilt every few minutes)."""

    def __init__(self, conn: sqlite3.Connection, roots: Callable[[], list[str]],
                 app_names: Callable[[], list[str]] | None = None) -> None:  # fmt: skip
        self.conn = conn
        self.roots = roots
        self.app_names = app_names
        self._at = 0.0
        self._names: list[str] = []
        self._corrector: Corrector | None = None

    def _known(self, w: str) -> bool:
        from zachary.brain.intents import _known as intent_word

        if intent_word(w) or w in _parser_words():
            return True
        row = self.conn.execute(
            "SELECT 1 FROM files_fts_vocab WHERE term = ? LIMIT 1", (w,)
        ).fetchone()
        return row is not None

    def refresh(self) -> None:
        if time.monotonic() - self._at < CACHE_S and self._corrector is not None:
            return
        apps = self.app_names() if self.app_names else []
        self._names = names(self.conn, self.roots(), apps)
        self._corrector = Corrector(self._names, self._known)
        self._at = time.monotonic()

    def keyterms(self) -> list[str]:
        self.refresh()
        return list(self._names)

    def correct(self, text: str) -> tuple[str, dict[str, str]]:
        self.refresh()
        assert self._corrector is not None
        return self._corrector.correct(text)


def home_roots() -> list[str]:
    return [os.path.expanduser("~")]
