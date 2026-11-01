"""Group versions of the same document: ``report.docx``, ``report_final.docx``,
``report_final_v2.docx``, ``Report (1).docx``, ``Copy of report.docx`` ...

Only files in the same folder with the same extension, only documents and code,
and only real version markers (v2, final, draft, copy, (1), old, latest, ...).
Dates and plain numbers are *not* markers: "meeting 2024-05-01" and
"meeting 2024-05-08" are different meetings, "chapter1"/"chapter2" different
chapters. Search shows the latest version (newest mtime) with the others collapsed.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable

VERSIONED_KINDS = ("document", "code")

_MARKERS = re.compile(
    r"""(?ix)
    ^copy[\s_-]+of[\s_-]+                         # "Copy of report"
    | [\s_.-]*-?[\s_]*copy(?:[\s_]*\(?\d+\)?)?$     # "report - Copy", "report copy 2"
    | [\s_.-]*\(\d{1,3}\)$                          # "report (1)"
    | [\s_.-]*(?:v|ver|version|rev|revision)[\s_.-]*\d+(?:\.\d+)*$   # v2, ver 3, rev 1.2
    | [\s_.-]*(?:final|draft|old|new|latest|updated|edited|revised|backup|bak|orig|original)$
    """
)


def version_key(stem: str) -> str:
    """Name with version markers removed (lowercase, separators normalized)."""
    s = stem.strip().lower()
    prev = None
    while prev != s:
        prev = s
        s = _MARKERS.sub("", s).strip(" _-.")
    return re.sub(r"[\s_.-]+", " ", s).strip()


def _stem(name: str) -> str:
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


def groups_for(rows: Iterable[tuple[int, int | None, str, str, float]]) -> list[list[int]]:
    """``rows``: (id, parent_id, name, ext, mtime). Returns groups (latest first) of 2+ ids."""
    buckets: dict[tuple[int | None, str, str], list[tuple[float, int, str]]] = defaultdict(list)
    for fid, parent, name, ext, mtime in rows:
        stem = _stem(name)
        key = version_key(stem)
        if key:
            buckets[(parent, ext, key)].append((mtime, fid, stem.lower()))
    out = []
    for members in buckets.values():
        if len(members) < 2 or len({m[2] for m in members}) < 2:
            continue
        members.sort(reverse=True)  # newest first
        out.append([fid for _, fid, _ in members])
    return out


def rebuild(conn: sqlite3.Connection, parent_ids: Iterable[int] | None = None) -> int:
    """Recompute version groups (all, or only inside ``parent_ids``). Returns groups."""
    kinds = ",".join(f"'{k}'" for k in VERSIONED_KINDS)
    sql = (
        f"SELECT id, parent_id, name, ext, mtime FROM files WHERE is_dir = 0 AND kind IN ({kinds})"
    )
    params: list[int] = []
    if parent_ids is not None:
        params = sorted(set(parent_ids))
        if not params:
            return 0
        sql += f" AND parent_id IN ({','.join('?' * len(params))})"
    rows = conn.execute(sql, params).fetchall()
    groups = groups_for((r[0], r[1], r[2], r[3], r[4]) for r in rows)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if parent_ids is None:
            conn.execute("DELETE FROM version_groups")
        else:
            ids = [r[0] for r in rows]
            for i in range(0, len(ids), 900):
                chunk = ids[i : i + 900]
                conn.execute(
                    f"DELETE FROM version_groups WHERE file_id IN ({','.join('?' * len(chunk))})",
                    chunk,
                )
        conn.executemany(
            "INSERT OR REPLACE INTO version_groups(group_id, file_id, is_latest) VALUES (?, ?, ?)",
            ((g[0], fid, int(i == 0)) for g in groups for i, fid in enumerate(g)),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(groups)


def versions_of(conn: sqlite3.Connection, file_ids: list[int]) -> dict[int, list[int]]:
    """For each given file in a version group: all members of its group, newest first."""
    if not file_ids:
        return {}
    marks = ",".join("?" * len(file_ids))
    gids = dict(
        conn.execute(
            f"SELECT file_id, group_id FROM version_groups WHERE file_id IN ({marks})", file_ids
        ).fetchall()
    )
    if not gids:
        return {}
    groups = sorted(set(gids.values()))
    gm = ",".join("?" * len(groups))
    members: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for gid, fid, mtime in conn.execute(
        f"SELECT v.group_id, v.file_id, f.mtime FROM version_groups v JOIN files f"
        f" ON f.id = v.file_id WHERE v.group_id IN ({gm})",
        groups,
    ):
        members[gid].append((mtime, fid))
    return {fid: [m for _, m in sorted(members[gid], reverse=True)] for fid, gid in gids.items()}
