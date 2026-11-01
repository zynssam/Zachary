"""All writes to the ``files`` / ``files_fts`` tables go through :class:`FileStore`.

The scanner, watcher and catch-up share this code so they agree on identity:
- a path that exists in the DB is updated in place (keeps its id and derived data);
- a new path whose (dev, inode) matches a row whose old path is gone is a
  rename/move: the row (and, for folders, every descendant) gets the new path;
- a path that no longer exists is deleted with all derived data (FK cascades +
  triggers).
"""

from __future__ import annotations

import os
import sqlite3
import stat as stat_mod
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from zachary.core.config import Config, ExcludeMatcher, PathRules
from zachary.core.db import transaction
from zachary.indexer.kinds import classify
from zachary.search.tokens import name_text, path_text

# Windows reparse tags for links we must not follow (loops / duplicate trees).
# Cloud placeholders (OneDrive) are also reparse points but are real files: keep them.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_LINK_TAGS = frozenset({_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK})

EDIT_LOG_MIN_GAP_S = 300.0  # at most one 'edit' access_log row per file per 5 minutes


@dataclass(slots=True)
class Entry:
    """Metadata for one file or folder on disk."""

    path: str
    name: str
    is_dir: bool
    size: int
    mtime: float
    ctime: float
    inode: int | None
    dev: int | None


@dataclass(slots=True)
class Row:
    id: int
    path: str
    is_dir: bool
    size: int
    mtime: float
    inode: int | None
    dev: int | None


@dataclass(slots=True)
class ReconcileResult:
    inserted: int = 0
    updated: int = 0
    moved: int = 0
    deleted: int = 0
    # (file id, path, mtime) of files that are new or whose content may have changed.
    changed_files: list[tuple[int, str, float]] = field(default_factory=list)
    # Folders whose direct children changed (project markers may have appeared/vanished).
    touched_dirs: set[str] = field(default_factory=set)
    # New folders whose contents still need a subtree scan.
    new_dirs: list[str] = field(default_factory=list)
    # Folder -> project markers seen while scanning (see projects.py).
    markers: dict[str, set[str]] = field(default_factory=dict)

    def merge(self, other: ReconcileResult) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.moved += other.moved
        self.deleted += other.deleted
        self.changed_files += other.changed_files
        self.touched_dirs |= other.touched_dirs
        self.new_dirs += other.new_dirs
        self.markers.update(other.markers)


_U64 = (1 << 64) - 1


def inode_key(v: int | None) -> int | None:
    """Fold an inode / file id / device id into SQLite's signed 64-bit INTEGER.

    Windows ``st_ino`` can be a 128-bit file id (Python >= 3.12), and NTFS file
    reference numbers use the top bits for a sequence number, so values >= 2**63
    are normal. Keep the low 64 bits (where NTFS puts the FRN) as a signed value;
    journal ids go through the same function so they still compare equal.
    """
    if not v:
        return None
    v &= _U64
    return v - (1 << 64) if v >= 1 << 63 else v


def is_link_like(st: os.stat_result) -> bool:
    """True for symlinks and Windows junctions, which are never followed."""
    if stat_mod.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


def entry_from_stat(
    path: str, name: str, st: os.stat_result, *, inode: int | None = None, dev: int | None = None
) -> Entry:
    is_dir = stat_mod.S_ISDIR(st.st_mode)
    return Entry(
        path=path,
        name=name,
        is_dir=is_dir,
        size=0 if is_dir else st.st_size,
        mtime=st.st_mtime,
        ctime=st.st_ctime,
        inode=inode_key(inode if inode is not None else st.st_ino),
        dev=inode_key(dev if dev is not None else st.st_dev),  # 64-bit volume serial on Win
    )


def stat_entry(path: str) -> Entry | None:
    """lstat a path; ``None`` if missing, a link, or not a regular file/folder."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if is_link_like(st) or not (stat_mod.S_ISDIR(st.st_mode) or stat_mod.S_ISREG(st.st_mode)):
        return None
    return entry_from_stat(path, os.path.basename(path) or path, st)


def _row(r: Any) -> Row:
    return Row(
        id=int(r[0]),
        path=str(r[1]),
        is_dir=bool(r[2]),
        size=int(r[3]),
        mtime=float(r[4]),
        inode=None if r[5] is None else int(r[5]),
        dev=None if r[6] is None else int(r[6]),
    )


_ROW_COLS = "id, path, is_dir, size, mtime, inode, dev"
# Snapshot row: (id, is_dir, size, mtime, inode, dev)
SnapRow = tuple[int, int, int, float, int | None, int | None]


class FileStore:
    """Thread-safe writer for file metadata. One instance per indexer process."""

    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.conn = conn
        self.config = config
        self.lock = threading.RLock()
        self.sep = os.sep
        self.roots: list[str] = []
        self.matcher: ExcludeMatcher
        self.rules: PathRules
        self._ptok: dict[tuple[str, str | None], str] = {}
        self.reload()

    # -- configuration -------------------------------------------------------
    def reload(self) -> None:
        """Re-read roots and exclude rules (after a config change)."""
        with self.lock:
            self.rules = self.config.path_rules()
            self.roots = list(self.rules.roots)
            self.matcher = self.rules.matcher
            self._ptok = {}

    def norm(self, path: str | os.PathLike[str]) -> str:
        """Canonical stored form of a path (see :func:`canonical_path`)."""
        return self.config.canonical(path)

    def root_for(self, path: str) -> str | None:
        return self.rules.root_for(path)

    def is_excluded(self, path: str, is_dir: bool) -> bool:
        return self.rules.is_excluded(path, is_dir)

    def in_hidden_folder(self, path: str, is_dir: bool) -> bool:
        """True if ``path`` (a folder) or a folder between its root and it has the Windows
        hidden attribute. Dot-folders are handled by name rules; on Linux this is a no-op.
        Un-hiding a folder makes it (and its contents) indexable again.
        """
        if not self.matcher.skip_hidden_dirs or not hasattr(os.stat_result, "st_file_attributes"):
            return False
        root = self.root_for(path)
        if root is None:
            return False
        d = path if is_dir else os.path.dirname(path)
        while len(d) > len(root):
            try:
                if self.matcher.hidden_attr(os.lstat(d)):
                    return True
            except OSError:
                return False
            nxt = os.path.dirname(d)
            if nxt == d:
                break
            d = nxt
        return False

    # -- reads -----------------------------------------------------------------
    def get_row(self, path: str) -> Row | None:
        r = self.conn.execute(f"SELECT {_ROW_COLS} FROM files WHERE path = ?", (path,)).fetchone()
        return None if r is None else _row(r)

    def rows_by_inode(self, dev: int | None, inode: int) -> list[Row]:
        if dev is None:
            sql = f"SELECT {_ROW_COLS} FROM files WHERE inode = ?"
            return [_row(r) for r in self.conn.execute(sql, (inode,))]
        sql = f"SELECT {_ROW_COLS} FROM files WHERE dev = ? AND inode = ?"
        return [_row(r) for r in self.conn.execute(sql, (dev, inode))]

    def descendant_range(self, path: str) -> tuple[str, str]:
        """``(lo, hi)`` such that ``lo < p < hi`` selects every path strictly inside ``path``."""
        base = path.rstrip(self.sep)
        return base + self.sep, base + chr(ord(self.sep) + 1)

    def snapshot(self, root: str) -> dict[str, SnapRow]:
        """Every row at or under ``root``: path -> (id, is_dir, size, mtime, inode, dev).

        Plain tuples: this loads one entry per indexed file, so it must be cheap.
        """
        lo, hi = self.descendant_range(root)
        cur = self.conn.execute(
            "SELECT path, id, is_dir, size, mtime, inode, dev FROM files"
            " WHERE path = ? OR (path > ? AND path < ?)",
            (root, lo, hi),
        )
        cur.row_factory = None
        return {r[0]: r[1:] for r in cur}

    # -- write helpers (caller holds the lock and an open transaction) ---------
    @contextmanager
    def batch(self) -> Iterator[None]:
        """Hold the writer lock and one transaction."""
        with self.lock, transaction(self.conn):
            yield

    def path_tokens(self, path: str, root: str | None) -> str:
        """FTS folder words for ``path``, cached per parent folder."""
        parent = os.path.dirname(path)
        key = (parent, root)
        hit = self._ptok.get(key)
        if hit is None:
            if len(self._ptok) > 50_000:
                self._ptok.clear()
            hit = self._ptok[key] = path_text(path, root)
        return hit

    def _fts_put(
        self, file_id: int, path: str, name: str, root: str | None, *, fresh: bool = False
    ) -> None:
        if not fresh:
            self.conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
        self.conn.execute(
            "INSERT INTO files_fts(rowid, name, path_tokens) VALUES (?, ?, ?)",
            (file_id, name_text(name), self.path_tokens(path, root)),
        )

    def insert_many(self, entries: list[Entry], parent_id: int | None, root: str) -> list[int]:
        """Insert new entries of one folder with two ``executemany`` calls.

        Ids are assigned here, inside the caller's write transaction (which holds
        SQLite's write lock), so they can't collide with another writer.
        """
        if not entries:
            return []
        start = int(self.conn.execute("SELECT coalesce(max(id), 0) + 1 FROM files").fetchone()[0])
        now = time.time()
        rows = []
        fts = []
        ptok = self.path_tokens(entries[0].path, root)
        for i, e in enumerate(entries):
            ext, kind = classify(e.name, e.is_dir)
            fid = start + i
            rows.append(
                (fid, e.path, parent_id, e.name, ext, e.size, e.mtime, e.ctime, e.inode, e.dev,
                 kind, int(e.is_dir), now)
            )  # fmt: skip
            fts.append((fid, name_text(e.name), ptok))
        self.conn.executemany(
            "INSERT INTO files(id, path, parent_id, name, ext, size, mtime, ctime, inode, dev,"
            " kind, is_dir, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.executemany(
            "INSERT INTO files_fts(rowid, name, path_tokens) VALUES (?, ?, ?)", fts
        )
        return [start + i for i in range(len(entries))]

    def insert(self, e: Entry, parent_id: int | None, root: str | None = None) -> int:
        ext, kind = classify(e.name, e.is_dir)
        # Upsert: if the row appeared since the caller looked (a race between event
        # batches), update it in place instead of failing the whole batch.
        cur = self.conn.execute(
            "INSERT INTO files(path, parent_id, name, ext, size, mtime, ctime, inode, dev,"
            " kind, is_dir, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET parent_id = excluded.parent_id,"
            " size = excluded.size, mtime = excluded.mtime, ctime = excluded.ctime,"
            " inode = excluded.inode, dev = excluded.dev, indexed_at = excluded.indexed_at"
            " RETURNING id",
            (
                e.path,
                parent_id,
                e.name,
                ext,
                e.size,
                e.mtime,
                e.ctime,
                e.inode,
                e.dev,
                kind,
                int(e.is_dir),
                time.time(),
            ),
        )
        file_id = int(cur.fetchone()[0])
        self._fts_put(file_id, e.path, e.name, root or self.root_for(e.path))
        return file_id

    def update(self, row_id: int, e: Entry) -> None:
        self.conn.execute(
            "UPDATE files SET size = ?, mtime = ?, ctime = ?, inode = ?, dev = ?, indexed_at = ?"
            " WHERE id = ?",
            (e.size, e.mtime, e.ctime, e.inode, e.dev, time.time(), row_id),
        )

    @staticmethod
    def changed(row: Row, e: Entry) -> bool:
        return (
            row.size != e.size
            or row.mtime != e.mtime
            or row.inode != e.inode
            or row.dev != e.dev
            or row.is_dir != e.is_dir
        )

    def move(self, row: Row, e: Entry, parent_id: int | None) -> None:
        """Give ``row`` the path ``e.path``; for folders, move every descendant too."""
        # Anything recorded at the destination is stale (the move replaced it).
        self.delete_path(e.path, keep_id=row.id)
        ext, kind = classify(e.name, e.is_dir)
        self.conn.execute(
            "UPDATE files SET path = ?, parent_id = ?, name = ?, ext = ?, kind = ?, size = ?,"
            " mtime = ?, ctime = ?, inode = ?, dev = ?, indexed_at = ? WHERE id = ?",
            (
                e.path,
                parent_id,
                e.name,
                ext,
                kind,
                e.size,
                e.mtime,
                e.ctime,
                e.inode,
                e.dev,
                time.time(),
                row.id,
            ),
        )
        root = self.root_for(e.path)
        self._fts_put(row.id, e.path, e.name, root)
        if row.is_dir:
            lo, hi = self.descendant_range(row.path)
            self.conn.execute(
                "UPDATE files SET path = ? || substr(path, ?) WHERE path > ? AND path < ?",
                (e.path, len(row.path) + 1, lo, hi),
            )
            nlo, nhi = self.descendant_range(e.path)
            for fid, path, name in self.conn.execute(
                "SELECT id, path, name FROM files WHERE path > ? AND path < ?", (nlo, nhi)
            ).fetchall():
                self._fts_put(int(fid), str(path), str(name), root)

    def delete_path(self, path: str, keep_id: int | None = None) -> int:
        """Delete the row at ``path`` and everything under it. Returns rows removed."""
        lo, hi = self.descendant_range(path)
        where = "(path = ? OR (path > ? AND path < ?)) AND id IS NOT ?"
        args = (path, lo, hi, keep_id)
        # Count first: FK cascades remove children before the range reaches them,
        # so the DELETE's rowcount undercounts.
        n = int(self.conn.execute(f"SELECT count(*) FROM files WHERE {where}", args).fetchone()[0])
        if n:
            self.conn.execute(f"DELETE FROM files WHERE {where}", args)
        return n

    def parent_id_for(self, path: str, result: ReconcileResult) -> int | None:
        """Id of ``path``'s parent folder, inserting missing ancestors up to the root."""
        root = self.root_for(path)
        if root is None or path == root:
            return None
        parent = os.path.dirname(path)
        row = self.get_row(parent)
        if row is not None:
            return row.id
        e = stat_entry(parent)
        if e is None or not e.is_dir:
            return None
        pid = self.parent_id_for(parent, result)
        new_id = self.insert(e, pid, root)
        result.inserted += 1
        result.touched_dirs.add(os.path.dirname(parent))
        return new_id

    def find_moved(self, e: Entry) -> Row | None:
        """A row whose old path is gone and that is the same object as ``e`` (by inode).

        For files, size and mtime must also match (a rename doesn't change them), so a
        recycled inode number on a different file isn't mistaken for a move.
        """
        if e.inode is None:
            return None
        for row in self.rows_by_inode(e.dev, e.inode):
            if row.path == e.path or row.is_dir != e.is_dir:
                continue
            if not e.is_dir and (row.size != e.size or row.mtime != e.mtime):
                continue
            if not os.path.lexists(row.path):
                return row
        return None

    def apply_entry(self, e: Entry, result: ReconcileResult) -> None:
        """Insert, update or move one existing on-disk entry. Caller holds a batch."""
        row = self.get_row(e.path)
        if row is not None and row.is_dir != e.is_dir:
            result.deleted += self.delete_path(e.path)
            row = None
        if row is not None:
            if self.changed(row, e):
                self.update(row.id, e)
                result.updated += 1
                if not e.is_dir:
                    result.changed_files.append((row.id, e.path, e.mtime))
            return
        parent_id = self.parent_id_for(e.path, result)
        moved = self.find_moved(e)
        result.touched_dirs.add(os.path.dirname(e.path))
        if moved is not None:
            self.move(moved, e, parent_id)
            result.moved += 1
            result.touched_dirs.add(os.path.dirname(moved.path))
            return
        file_id = self.insert(e, parent_id)
        result.inserted += 1
        if e.is_dir:
            result.new_dirs.append(e.path)
        else:
            result.changed_files.append((file_id, e.path, e.mtime))

    # -- public operations -------------------------------------------------------
    def reconcile_paths(
        self, paths: Iterable[str], *, scan_new_dirs: bool = True
    ) -> ReconcileResult:
        """Bring the DB in line with the disk for specific paths (watcher / journal events)."""
        result = ReconcileResult()
        present: list[Entry] = []
        missing: list[str] = []
        for raw in {self.norm(p) for p in paths}:
            e = stat_entry(raw)
            if e is None:
                missing.append(raw)
            elif self.is_excluded(raw, e.is_dir) or self.in_hidden_folder(raw, e.is_dir):
                missing.append(raw)  # excluded now (or hidden): drop whatever we had
            else:
                present.append(e)
        # Parents before children; existing paths before deletions so moves keep identity.
        present.sort(key=lambda x: (x.path.count(self.sep), x.path))
        with self.batch():
            for e in present:
                self.apply_entry(e, result)
            for p in sorted(missing, key=len):
                n = self.delete_path(p)
                if n:
                    result.deleted += n
                    result.touched_dirs.add(os.path.dirname(p))
        if scan_new_dirs and result.new_dirs:
            from zachary.indexer.scanner import scan_subtree  # local: scanner imports store

            for d in result.new_dirs:
                sub = scan_subtree(self, d)
                result.merge(sub.result)
        return result

    def ensure_fts(self) -> int:
        """Rebuild keyword rows if they are out of sync with ``files`` (e.g. after migration #2)."""
        n_files = self.conn.execute("SELECT count(*) FROM files").fetchone()[0]
        n_fts = self.conn.execute("SELECT count(*) FROM files_fts").fetchone()[0]
        if n_files == n_fts:
            return 0
        with self.batch():
            self.conn.execute("DELETE FROM files_fts")
            rows = self.conn.execute("SELECT id, path, name FROM files").fetchall()
            for fid, path, name in rows:
                self.conn.execute(
                    "INSERT INTO files_fts(rowid, name, path_tokens) VALUES (?, ?, ?)",
                    (fid, name_text(name), self.path_tokens(path, self.root_for(path))),
                )
        return len(rows)

    def purge_excluded(self) -> int:
        """Delete rows that current rules exclude, or that sit outside every root."""
        removed = 0
        with self.batch():
            for (path,) in self.conn.execute(
                "SELECT path FROM files WHERE parent_id IS NULL"
            ).fetchall():
                if path not in self.roots:
                    removed += self.delete_path(path)
            for p in self.matcher.paths:
                removed += self.delete_path(p)
            doomed = [
                path
                for path, is_dir in self.conn.execute("SELECT path, is_dir FROM files")
                if self.is_excluded(path, bool(is_dir))
            ]
            for p in sorted(doomed, key=len):
                removed += self.delete_path(p)
        return removed

    def record_access(self, items: Iterable[tuple[str, float]], source: str) -> int:
        """Add ``access_log`` rows for known paths. Unknown paths are ignored."""
        added = 0
        with self.batch():
            for path, ts in items:
                row = self.conn.execute(
                    "SELECT id FROM files WHERE path = ?", (self.norm(path),)
                ).fetchone()
                if row is None:
                    continue
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO access_log(file_id, ts, source) VALUES (?, ?, ?)",
                    (row[0], ts, source),
                )
                added += max(cur.rowcount, 0)
        return added

    def log_edits(self, changed: Iterable[tuple[int, str, float]]) -> int:
        """Record file edits seen live, at most one row per file per few minutes."""
        added = 0
        with self.batch():
            for file_id, _path, mtime in changed:
                last = self.conn.execute(
                    "SELECT max(ts) FROM access_log WHERE file_id = ? AND source = 'edit'",
                    (file_id,),
                ).fetchone()[0]
                if last is not None and mtime - float(last) < EDIT_LOG_MIN_GAP_S:
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO access_log(file_id, ts, source) VALUES (?, ?, 'edit')",
                    (file_id, mtime),
                )
                added += 1
        return added
