"""Create new files and folders ("create a text file and paste what I copied",
"make a folder called hackathon ideas", dictated documents).

Rules:

* Only ever **new** paths: an existing file is never overwritten (a " (2)" name is
  used instead) and nothing existing is changed.
* Every creation is an ``action_plans`` row (kind ``create``) with ``undo_log``
  entries (``mkdir`` for new folders, ``create`` for the file with its size/mtime),
  so "undo" moves it to the trash again, but only if it wasn't edited since.
* Where it goes when no place is said: ``Documents/Zachary/<subfolder>``, the
  subfolder chosen by content (see ``choose_folder``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from zachary.actions import planner
from zachary.actions.planner import Plan, PlanItem
from zachary.indexer.semantic import keywords
from zachary.search.tokens import words

log = logging.getLogger(__name__)

FORMATS = ("docx", "txt", "md")
DEFAULT_FORMAT = "docx"
SORT_MIN = 0.28  # below this a new subfolder is made (measured: fits 0.34-0.48, unrelated <= 0.07)
NAME_WORDS = 6
Embed = Callable[[list[str]], np.ndarray]
NameFolder = Callable[[str, list[str]], str]  # (excerpt, existing names) -> folder name


@dataclass(slots=True)
class Created:
    path: Path
    folder: Path
    plan_id: int
    sorted_into: str  # "" (a place was given) | "existing" | "new"


def base_folder() -> Path:
    return Path(os.path.expanduser("~")) / "Documents" / "Zachary"


# -- names ---------------------------------------------------------------------------------
def safe_name(text: str, limit: int = 60) -> str:
    """A file/folder name from free text: no path separators or odd characters."""
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", text)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].strip(" .")


def name_from_content(content: str) -> str:
    first = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
    w = first.split()[:NAME_WORDS]
    name = safe_name(" ".join(w))
    return name or datetime.now().strftime("Note %Y-%m-%d %H.%M")


def _free(path: Path) -> Path:
    if not os.path.lexists(path):
        return path
    stem, ext = path.stem, path.suffix
    n = 2
    while True:
        cand = path.with_name(f"{stem} ({n}){ext}")
        if not os.path.lexists(cand):
            return cand
        n += 1


# -- sorting by content ------------------------------------------------------------------------
def _profile(conn: sqlite3.Connection | None, folder: Path) -> str:
    """What a subfolder is about: its name, its files' names and a little of their text."""
    parts = [folder.name] * 3
    try:
        names = sorted(p.name for p in folder.iterdir() if not p.name.startswith("."))[:50]
    except OSError:
        names = []
    parts += [Path(n).stem for n in names]
    if conn is not None:
        prefix = str(folder).rstrip(os.sep) + os.sep
        rows = conn.execute(
            """SELECT c.text_excerpt FROM chunks c JOIN files f ON f.id = c.file_id
               WHERE f.path >= ? AND f.path < ? AND c.ord = 0 LIMIT 5""",
            (prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)),
        ).fetchall()
        parts += [str(r[0])[:400] for r in rows]
    return " ".join(parts)


def _overlap(a: str, b: str) -> float:
    wa = {w for w in words(a) if len(w) > 2}
    wb = {w for w in words(b) if len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def new_folder_name(content: str) -> str:
    kw = [w for w in keywords(content, 3).split() if len(w) > 2][:2]
    return safe_name(" ".join(w.capitalize() for w in kw)) or "Notes"


def choose_folder(
    content: str,
    base: Path,
    *,
    conn: sqlite3.Connection | None = None,
    embed: Embed | None = None,
    name_folder: NameFolder | None = None,
) -> tuple[Path, str]:
    """(folder, "existing" | "new"): the subfolder of ``base`` this content belongs in."""
    try:
        existing = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        existing = []
    text = content.strip()
    if not text:
        return base, "existing"
    if name_folder is not None:
        try:
            chosen = safe_name(name_folder(text[:800], [p.name for p in existing]))
        except Exception:
            log.info("LLM folder naming failed; sorting locally", exc_info=True)
            chosen = ""
        if chosen:
            match = next((p for p in existing if p.name.lower() == chosen.lower()), None)
            return (match, "existing") if match else (base / chosen, "new")
    if existing:
        profiles = [_profile(conn, p) for p in existing]
        scores = [0.0] * len(existing)
        if embed is not None:
            v = embed([text[:2000], *profiles])
            v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
            sims = v[1:] @ v[0]
            scores = [0.6 * float(s) for s in sims]
        for i, prof in enumerate(profiles):
            scores[i] += (0.4 if embed is not None else 1.0) * _overlap(text, prof)
        best = int(np.argmax(scores))
        if scores[best] >= SORT_MIN:
            return existing[best], "existing"
    return base / new_folder_name(text), "new"


# -- writing ----------------------------------------------------------------------------------
def _write(path: Path, content: str, fmt: str) -> None:
    if fmt == "docx":
        import docx

        d = docx.Document()
        paras = [p.strip() for p in re.split(r"\n\s*\n|\n", content)] if content else []
        for p in paras:
            if p:
                d.add_paragraph(p)
        d.save(str(path))
    else:
        path.write_text(content + ("\n" if content and not content.endswith("\n") else ""),
                        encoding="utf-8")  # fmt: skip


def _record(conn: sqlite3.Connection, title: str, made_dirs: list[Path], file: Path | None
            ) -> int:  # fmt: skip
    items: list[PlanItem] = []
    for d in made_dirs:
        items.append(PlanItem(len(items) + 1, "mkdir", str(d), None, "create", "new folder", 0,
                              time.time(), True, True, "done"))  # fmt: skip
    if file is not None:
        st = file.stat()
        items.append(PlanItem(len(items) + 1, "create", str(file), None, "create", "new file",
                              st.st_size, st.st_mtime, True, False, "done"))  # fmt: skip
    plan = planner.save(conn, Plan("create", title, items, status="executed",
                                   note=title))  # fmt: skip
    now = time.time()
    for d in made_dirs:
        conn.execute("INSERT INTO undo_log(plan_id, op, src, dst, ts) VALUES (?, 'mkdir', ?, "
                     "NULL, ?)", (plan.id, str(d), now))  # fmt: skip
    if file is not None:
        st = file.stat()
        conn.execute(
            "INSERT INTO undo_log(plan_id, op, src, dst, ts) VALUES (?, 'create', ?, ?, ?)",
            (plan.id, str(file), json.dumps({"size": st.st_size, "mtime": st.st_mtime}), now),
        )
    return plan.id


def _mkdirs(folder: Path) -> list[Path]:
    made: list[Path] = []
    p = folder
    missing = []
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        d.mkdir()
        made.append(d)
    return made


def create_file(
    conn: sqlite3.Connection,
    content: str,
    *,
    fmt: str = DEFAULT_FORMAT,
    name: str | None = None,
    folder: Path | None = None,
    embed: Embed | None = None,
    name_folder: NameFolder | None = None,
) -> Created:
    """Write a new file (never overwriting) and record it for undo."""
    fmt = fmt if fmt in FORMATS else DEFAULT_FORMAT
    sorted_into = ""
    if folder is None:
        base = base_folder()
        folder, sorted_into = choose_folder(
            content or (name or ""), base, conn=conn, embed=embed, name_folder=name_folder
        )
    stem = safe_name(name) if name else name_from_content(content)
    path = _free(folder / f"{stem or 'Untitled'}.{fmt}")
    made = _mkdirs(folder)
    try:
        _write(path, content, fmt)
    except Exception:
        for d in reversed(made):  # don't leave empty new folders behind
            with contextlib.suppress(OSError):
                d.rmdir()
        raise
    pid = _record(conn, f"Create {path.name}", made, path)
    return Created(path, folder, pid, sorted_into)


def create_folder(conn: sqlite3.Connection, name: str, parent: Path | None = None) -> Created:
    parent = parent or base_folder()
    target = _free(parent / (safe_name(name) or "New folder"))
    made = _mkdirs(target)
    pid = _record(conn, f"Create folder {target.name}", made, None)
    return Created(target, target, pid, "")
