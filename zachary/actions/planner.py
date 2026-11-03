"""Cleaning and organizing *plans*. Nothing here changes a file.

A plan is a list of proposed operations (``trash`` or ``move``), each with a reason,
saved as an ``action_plans`` row with status ``proposed``. The user reviews it
(approve / edit / reject per item); only ``executor.py`` changes files, and only for
approved items of an approved plan. Every change is undoable (``undo.py``).

Cleaning looks for:

* **duplicates**: same size -> same quick hash (first/last 64 KB) -> same full hash.
  One copy is kept (outside Downloads, not a "(1)"/"copy" name, most used, oldest);
  the others are proposed for the trash.
* **empty folders** (checked on disk: the index doesn't see hidden files).
* **old installers** in Downloads (.exe/.msi/.deb/.dmg/... older than 30 days).
* **temp/junk** (partial downloads, Office lock files, *.tmp, Thumbs.db...). These are
  excluded from the index, so the folders Zachary indexes are listed on disk.
* **large, untouched** files (>= 500 MB, not modified or opened for 180 days):
  shown for review but *not* preselected.

Organizing ("organize my DSA stuff") proposes moving a topic's (or search's) files
into one folder: the folder most of them already share, else ``Documents/<Topic>``.

Never proposed: anything under a ``never_touch`` folder rule, inside a project folder
(code, repos), hidden, or an index root / standard user folder itself.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath
from typing import Any

from zachary.core.config import Config

QUICK_BLOCK = 64 * 1024
MIN_DUP_SIZE = 16 * 1024  # smaller duplicates aren't worth the reader's time
INSTALLER_DAYS = 30
INSTALLER_EXTS = frozenset(
    ".exe .msi .msix .dmg .pkg .deb .rpm .appimage .apk .snap .flatpakref .run".split()
)
LARGE_BYTES = 500 * 1024 * 1024
UNTOUCHED_DAYS = 180
JUNK_GLOBS = (
    "*.tmp", "*.temp", "~$*", ".~lock.*#", "*.crdownload", "*.part", "*.partial",
    "Thumbs.db", ".DS_Store", "*.swp", "*.swo",
)  # fmt: skip
# One compiled regex for all globs: ~1 match per file instead of 11 fnmatch calls
# (200k files: 56 s -> about 1 s under the profiler).
_JUNK_RE = re.compile(
    "|".join(f"(?:{fnmatch.translate(g)})" for g in JUNK_GLOBS),
    re.IGNORECASE if os.name == "nt" else 0,  # (as fnmatch: case-insensitive on Windows)
)
JUNK_MIN_AGE_DAYS = 1  # a download/lock file younger than this may still be in use
STANDARD_FOLDERS = frozenset(
    """
    desktop documents downloads music pictures videos templates public onedrive
    """.split()
)
COPY_NAME = re.compile(r"(\(\d+\)|\bcopy\b|- copy|_copy|\bcopy of\b)", re.IGNORECASE)
MAX_ITEMS = 2000
# Only the user's own content counts as a duplicate worth removing. Code, libraries
# and binaries are often identical on purpose (bundled .jar/.so files, licences).
DUPLICATE_KINDS = ("document", "image", "video", "audio", "archive")
# A folder with this many libraries/binaries inside is installed software (a JDK,
# an app's lib/ folder...): nothing in it is ever proposed (seen on a real home:
# 575 bundled .jar "duplicates" and 66 empty conf/data/logs folders an app needs).
SOFTWARE_MIN_BINARIES = 10
_SHARED_LIB = re.compile(r"\.so(\.\d+)*$")

CATEGORIES = ("duplicate", "installer", "junk", "empty_folder", "large", "organize")
LABELS = {
    "duplicate": "Duplicates",
    "installer": "Old installers",
    "junk": "Temporary / junk files",
    "empty_folder": "Empty folders",
    "large": "Large files you haven't touched",
    "organize": "Moves",
}


@dataclass(slots=True)
class PlanItem:
    id: int
    op: str  # trash | move
    src: str
    dst: str | None
    category: str
    reason: str
    size: int
    mtime: float  # snapshot: the executor skips the item if the file changed since
    selected: bool = True
    is_dir: bool = False
    result: str = ""  # filled by the executor: done | skipped: ... | failed: ...


@dataclass(slots=True)
class Plan:
    kind: str  # clean | organize
    title: str
    items: list[PlanItem] = field(default_factory=list)
    id: int = 0
    status: str = "proposed"
    created_at: float = 0.0
    note: str = ""

    def selected(self) -> list[PlanItem]:
        return [i for i in self.items if i.selected]

    def bytes_selected(self) -> int:
        return sum(i.size for i in self.items if i.selected and i.op == "trash")

    def summary(self) -> str:
        if not self.items:
            return "Nothing to clean up." if self.kind == "clean" else "Nothing to move."
        by: dict[str, list[PlanItem]] = defaultdict(list)
        for it in self.items:
            by[it.category].append(it)
        parts = []
        for cat in CATEGORIES:
            if cat in by:
                n = len(by[cat])
                size = sum(i.size for i in by[cat])
                label = LABELS[cat].lower()
                parts.append(f"{n} {label}" + (f" ({human(size)})" if size else ""))
        return "; ".join(parts) + "."

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, "title": self.title, "note": self.note,
                           "items": [asdict(i) for i in self.items]})  # fmt: skip


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in {"B", "KB"} else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# -- persistence ---------------------------------------------------------------------------
def save(conn: sqlite3.Connection, plan: Plan) -> Plan:
    plan.created_at = plan.created_at or time.time()
    if plan.id:
        conn.execute("UPDATE action_plans SET json = ? WHERE id = ?", (plan.to_json(), plan.id))
    else:
        cur = conn.execute(
            "INSERT INTO action_plans(kind, json, status, created_at) VALUES (?, ?, ?, ?)",
            (plan.kind, plan.to_json(), plan.status, plan.created_at),
        )
        plan.id = int(cur.lastrowid or 0)
    return plan


def load(conn: sqlite3.Connection, plan_id: int) -> Plan | None:
    row = conn.execute(
        "SELECT id, kind, json, status, created_at FROM action_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row[2])
    items = [PlanItem(**it) for it in data.get("items", [])]
    return Plan(row[1], data.get("title", ""), items, int(row[0]), row[3], float(row[4]),
                data.get("note", ""))  # fmt: skip


def latest(conn: sqlite3.Connection, status: Iterable[str]) -> Plan | None:
    st = list(status)
    marks = ",".join("?" * len(st))
    row = conn.execute(
        f"SELECT id FROM action_plans WHERE status IN ({marks}) ORDER BY id DESC LIMIT 1", st
    ).fetchone()
    return load(conn, int(row[0])) if row else None


def edit(conn: sqlite3.Connection, plan: Plan, *, select: dict[int, bool] | None = None,
         dst: dict[int, str] | None = None) -> Plan:  # fmt: skip
    """Change which items are selected and/or where moves go (only before approval)."""
    if plan.status != "proposed":
        raise ValueError(f"plan {plan.id} is {plan.status}; only a proposed plan can be edited")
    by_id = {i.id: i for i in plan.items}
    for k, v in (select or {}).items():
        if k in by_id:
            by_id[k].selected = v
    for k, where in (dst or {}).items():
        if k in by_id and by_id[k].op == "move" and where.strip():
            by_id[k].dst = os.path.abspath(os.path.expanduser(where.strip()))
    return save(conn, plan)


def set_status(conn: sqlite3.Connection, plan: Plan, status: str) -> None:
    approved = time.time() if status == "approved" else None
    conn.execute(
        "UPDATE action_plans SET status = ?, approved_at = coalesce(?, approved_at) WHERE id = ?",
        (status, approved, plan.id),
    )
    plan.status = status


# -- shared checks -------------------------------------------------------------------------
class Guard:
    """Where Zachary may propose changes at all."""

    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.config = config
        self.roots = [str(r) for r in config.index_roots()]
        self.rules = config.path_rules()
        self.never = [
            r[0] for r in conn.execute("SELECT path FROM folder_rules WHERE rule = 'never_touch'")
        ]
        self.projects = [
            r[0]
            for r in conn.execute(
                "SELECT f.path FROM projects p JOIN files f ON f.id = p.folder_id"
            )
        ]
        # Standard folders (Documents, Downloads...) under the home folder and under
        # every index root (another drive can have its own Documents).
        bases = [os.path.expanduser("~"), *self.roots]
        self.standard = {
            os.path.join(b.rstrip("/\\"), name)
            for b in bases
            for n in STANDARD_FOLDERS
            for name in (n, n.capitalize())
        }
        self._blocked: set[str] | None = None
        self._landmarks = {r.rstrip("/\\") for r in self.roots} | self.standard
        self._landmarks.add(os.path.expanduser("~"))
        self.software = self._software_folders(conn)

    def _software_folders(self, conn: sqlite3.Connection) -> list[str]:
        counts: dict[str, int] = defaultdict(int)
        for (path,) in conn.execute(
            "SELECT path FROM files WHERE is_dir = 0 AND (kind = 'binary' OR name LIKE '%.so.%')"
        ):
            if not _SHARED_LIB.search(path) and not path.lower().endswith(
                (".jar", ".dll", ".so", ".dylib", ".class", ".pyd", ".bin", ".exe", ".war")
            ):
                continue
            root = self._root_of(path)
            if root is None:
                continue
            # Count only folders strictly inside the index root (never the root, a
            # standard folder such as Documents, or anything above them).
            d = os.path.dirname(path)
            while len(d) > len(root) and d not in self._landmarks:
                counts[d] += 1
                d = os.path.dirname(d)
        landmarks = self._landmarks
        found = [d for d, n in counts.items() if n >= SOFTWARE_MIN_BINARIES and d not in landmarks]
        # Keep only the outermost ones (a folder's parent may qualify too).
        found.sort(key=len)
        out: list[str] = []
        for d in found:
            if not self._under(d, out):
                out.append(d)
        return out

    @staticmethod
    def _under(path: str, folders: Iterable[str]) -> bool:
        for f in folders:
            f = f.rstrip("/\\")
            if path == f or path.startswith(f + "/") or path.startswith(f + "\\"):
                return True
        return False

    def _blocked_set(self) -> set[str]:
        if self._blocked is None:
            self._blocked = {f.rstrip("/\\") for f in (*self.never, *self.projects, *self.software)}
        return self._blocked

    def _root_of(self, path: str) -> str | None:
        best = None
        for r in self.roots:
            r = r.rstrip("/\\")
            if (path == r or path.startswith(r + "/") or path.startswith(r + "\\")) and (
                best is None or len(r) > len(best)
            ):
                best = r
        return best

    def protected(self, path: str, *, junk: bool = False) -> bool:
        """never_touch, a project, installed software, hidden, excluded, or not under an
        index root. ``junk``: temp/lock files are excluded from the index by design; for
        them only the folder they're in must be allowed (not hidden/excluded itself)."""
        root = self._root_of(path)
        if root is None:
            return True
        # Any ancestor (or the path itself) blocked? O(depth) set lookups, not a scan
        # over every project folder.
        blocked = self._blocked_set()
        d = path.rstrip("/\\")
        while len(d) >= len(root):
            if d in blocked:
                return True
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        # Hidden only counts below the root ("~/.cache/bench/home" may itself be a root).
        rel = PurePath(path[len(root) :].lstrip("/\\")).parts
        if any(p.startswith(".") for p in (rel[:-1] if junk else rel)):
            return True
        if junk:
            parent = os.path.dirname(path)
            return self.rules.is_excluded(parent, True) and not self.is_landmark(parent)
        return self.rules.is_excluded(path, False)

    def is_landmark(self, path: str) -> bool:
        """An index root or a standard user folder: never removed, even when empty."""
        p = path.rstrip("/\\")
        return p in {r.rstrip("/\\") for r in self.roots} or p in self.standard


def _downloads() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


def _last_access(conn: sqlite3.Connection, ids: list[int]) -> dict[int, float]:
    out: dict[int, float] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        out.update(
            conn.execute(
                f"SELECT file_id, max(ts) FROM access_log WHERE file_id IN ({marks})"
                " GROUP BY file_id",
                chunk,
            ).fetchall()
        )
    return out


# -- hashing ------------------------------------------------------------------------------
def quick_hash(path: str, size: int) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(QUICK_BLOCK))
        if size > 2 * QUICK_BLOCK:
            f.seek(-QUICK_BLOCK, os.SEEK_END)
            h.update(f.read(QUICK_BLOCK))
    return h.hexdigest()


def full_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# -- detectors ------------------------------------------------------------------------------
@dataclass(slots=True)
class _Row:
    id: int
    path: str
    name: str
    size: int
    mtime: float
    ctime: float


def _scope_sql(scope: str | None) -> tuple[str, list[Any]]:
    if not scope:
        return "", []
    s = scope.rstrip("/\\")
    sep = "\\" if "\\" in s and "/" not in s else "/"
    return " AND (path = ? OR (path >= ? AND path < ?))", [s, s + sep, s + chr(ord(sep) + 1)]


def find_duplicates(conn: sqlite3.Connection, guard: Guard, scope: str | None = None
                    ) -> list[tuple[_Row, list[_Row]]]:  # fmt: skip
    """[(kept, [copies...])]: byte-identical files, verified by a full hash."""
    where, params = _scope_sql(scope)
    sizes = [
        r[0]
        for r in conn.execute(
            f"SELECT size FROM files WHERE is_dir = 0 AND size >= ? AND kind IN {DUPLICATE_KINDS}"
            f" {where} GROUP BY size HAVING count(*) > 1",
            [MIN_DUP_SIZE, *params],
        )
    ]
    out: list[tuple[_Row, list[_Row]]] = []
    for size in sizes:
        rows = [
            _Row(int(r[0]), r[1], r[2], int(r[3]), float(r[4]), float(r[5]))
            for r in conn.execute(
                f"SELECT id, path, name, size, mtime, ctime FROM files"
                f" WHERE is_dir = 0 AND size = ? AND kind IN {DUPLICATE_KINDS} {where}",
                [size, *params],
            )
        ]
        rows = [r for r in rows if not guard.protected(r.path) and _plain_file(r.path)]
        rows = _drop_hardlinks(rows)
        if len(rows) < 2:
            continue
        for group in _split_by(rows, lambda r: quick_hash(r.path, r.size)):
            for same in _split_by(group, lambda r: full_hash(r.path)):
                kept, copies = _keep(conn, same)
                out.append((kept, copies))
    return out


def _plain_file(path: str) -> bool:
    """A regular file, not a symlink (a link to a file isn't a second copy)."""
    return os.path.isfile(path) and not os.path.islink(path)


def _drop_hardlinks(rows: list[_Row]) -> list[_Row]:
    """Hard links share their data: removing one frees nothing. Keep one per inode."""
    seen: set[tuple[int, int]] = set()
    out = []
    for r in rows:
        try:
            st = os.stat(r.path)
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if st.st_ino and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _split_by(rows: list[_Row], key: Any) -> list[list[_Row]]:
    groups: dict[str, list[_Row]] = defaultdict(list)
    for r in rows:
        try:
            groups[key(r)].append(r)
        except OSError:
            continue  # unreadable / vanished: never proposed
    return [g for g in groups.values() if len(g) > 1]


def _keep(conn: sqlite3.Connection, rows: list[_Row]) -> tuple[_Row, list[_Row]]:
    used = _last_access(conn, [r.id for r in rows])
    downloads = _downloads()

    def rank(r: _Row) -> tuple[int, int, float, float, int]:
        in_dl = int(r.path.startswith(downloads + os.sep))
        copy = int(bool(COPY_NAME.search(Path(r.name).stem)))
        return (in_dl, copy, -used.get(r.id, 0.0), min(r.ctime, r.mtime), len(r.path))

    ordered = sorted(rows, key=rank)
    return ordered[0], ordered[1:]


def find_installers(conn: sqlite3.Connection, guard: Guard, now: float) -> list[_Row]:
    dl = _downloads()
    where, params = _scope_sql(dl)
    cutoff = now - INSTALLER_DAYS * 86400
    rows = conn.execute(
        f"SELECT id, path, name, size, mtime, ctime, ext FROM files WHERE is_dir = 0"
        f" AND mtime < ? {where}",
        [cutoff, *params],
    ).fetchall()
    return [
        _Row(int(r[0]), r[1], r[2], int(r[3]), float(r[4]), float(r[5]))
        for r in rows
        if ("." + str(r[6]).lstrip(".")).lower() in INSTALLER_EXTS
        and not guard.protected(r[1])
        and os.path.isfile(r[1])
    ]


def find_junk(conn: sqlite3.Connection, guard: Guard, now: float, scope: str | None = None
              ) -> list[_Row]:  # fmt: skip
    """Junk files are excluded from the index, so list the indexed folders on disk."""
    where, params = _scope_sql(scope)
    dirs = [r[0] for r in conn.execute(f"SELECT path FROM files WHERE is_dir = 1 {where}",
                                       params)]  # fmt: skip
    dirs += [r for r in guard.roots if not scope or Guard._under(r, [scope])]
    cutoff = now - JUNK_MIN_AGE_DAYS * 86400
    out: list[_Row] = []
    seen: set[str] = set()
    for d in dirs:
        if d in seen or (guard.protected(d) and not guard.is_landmark(d)):
            continue
        seen.add(d)
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not _JUNK_RE.match(e.name):
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_mtime > cutoff or guard._under(e.path, guard.never + guard.projects):
                continue
            out.append(_Row(0, e.path, e.name, st.st_size, st.st_mtime, st.st_ctime))
    return out


def find_empty_folders(conn: sqlite3.Connection, guard: Guard, scope: str | None = None
                       ) -> list[_Row]:  # fmt: skip
    where, params = _scope_sql(scope)
    rows = conn.execute(
        f"""SELECT d.id, d.path, d.name, d.mtime, d.ctime FROM files d
            WHERE d.is_dir = 1 {where.replace("path", "d.path")}
              AND NOT EXISTS (SELECT 1 FROM files c WHERE c.parent_id = d.id)""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        path = r[1]
        if guard.protected(path) or guard.is_landmark(path):
            continue
        try:
            if os.listdir(path):  # hidden or excluded files the index doesn't list
                continue
        except OSError:
            continue
        out.append(_Row(int(r[0]), path, r[2], 0, float(r[3]), float(r[4])))
    return out


def find_large(conn: sqlite3.Connection, guard: Guard, now: float, scope: str | None = None
               ) -> list[_Row]:  # fmt: skip
    where, params = _scope_sql(scope)
    cutoff = now - UNTOUCHED_DAYS * 86400
    rows = [
        _Row(int(r[0]), r[1], r[2], int(r[3]), float(r[4]), float(r[5]))
        for r in conn.execute(
            f"SELECT id, path, name, size, mtime, ctime FROM files WHERE is_dir = 0"
            f" AND size >= ? AND mtime < ? {where} ORDER BY size DESC LIMIT 200",
            [LARGE_BYTES, cutoff, *params],
        )
    ]
    used = _last_access(conn, [r.id for r in rows])
    return [
        r for r in rows
        if used.get(r.id, 0.0) < cutoff and not guard.protected(r.path) and os.path.isfile(r.path)
    ]  # fmt: skip


# -- plans ------------------------------------------------------------------------------------
def _ago(ts: float, now: float) -> str:
    days = int((now - ts) // 86400)
    if days >= 365:
        return f"{days // 365} year{'s' if days >= 730 else ''} old"
    if days >= 60:
        return f"{days // 30} months old"
    return f"{days} days old"


def clean_plan(
    conn: sqlite3.Connection,
    config: Config,
    *,
    scope: str | None = None,
    categories: Iterable[str] | None = None,
    now: float | None = None,
) -> Plan:
    """Build (but don't save) a cleaning plan."""
    now = time.time() if now is None else now
    cats = set(categories or ("duplicate", "installer", "junk", "empty_folder", "large"))
    guard = Guard(conn, config)
    items: list[PlanItem] = []

    def add(op: str, r: _Row, cat: str, reason: str, *, selected: bool = True,
            is_dir: bool = False) -> None:  # fmt: skip
        if len(items) < MAX_ITEMS:
            items.append(PlanItem(len(items) + 1, op, r.path, None, cat, reason, r.size,
                                  r.mtime, selected, is_dir))  # fmt: skip

    if "duplicate" in cats:
        for kept, copies in find_duplicates(conn, guard, scope):
            for c in copies:
                add("trash", c, "duplicate", f"same content as {_short(kept.path)} (kept)")
    if "installer" in cats and (not scope or Guard._under(_downloads(), [scope])
                                or Guard._under(scope, [_downloads()])):  # fmt: skip
        for r in find_installers(conn, guard, now):
            if not any(i.src == r.path for i in items):
                add("trash", r, "installer", f"installer, {_ago(r.mtime, now)}")
    if "junk" in cats:
        for r in find_junk(conn, guard, now, scope):
            add("trash", r, "junk", "temporary file")
    if "empty_folder" in cats:
        for r in find_empty_folders(conn, guard, scope):
            add("trash", r, "empty_folder", "empty folder", is_dir=True)
    if "large" in cats:
        for r in find_large(conn, guard, now, scope):
            if not any(i.src == r.path for i in items):
                add("trash", r, "large", f"{human(r.size)}, not used for "
                    f"{int((now - max(r.mtime, 0)) // 86400)} days", selected=False)  # fmt: skip
    where = f" in {_short(scope)}" if scope else ""
    plan = Plan("clean", f"Clean up{where}", items)
    plan.note = plan.summary()
    return plan


def organize_plan(conn: sqlite3.Connection, config: Config, file_ids: list[int], label: str,
                  *, target: str | None = None) -> Plan:  # fmt: skip
    """Move these files (a topic or a search) into one folder."""
    guard = Guard(conn, config)
    rows: list[_Row] = []
    for i in range(0, len(file_ids), 500):
        chunk = file_ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        rows += [
            _Row(int(r[0]), r[1], r[2], int(r[3]), float(r[4]), float(r[5]))
            for r in conn.execute(
                f"SELECT id, path, name, size, mtime, ctime FROM files"
                f" WHERE is_dir = 0 AND id IN ({marks})",
                chunk,
            )
        ]
    rows = [r for r in rows if not guard.protected(r.path) and os.path.isfile(r.path)]
    if target is None:
        parents: dict[str, int] = defaultdict(int)
        for r in rows:
            parents[os.path.dirname(r.path)] += 1
        best = max(parents.items(), key=lambda kv: kv[1], default=("", 0))
        if best[1] * 2 >= len(rows) and best[0] and not guard.is_landmark(best[0]):
            target = best[0]
        else:
            safe = re.sub(r"[^\w .,&()-]+", " ", label).strip() or "Organized"
            target = os.path.join(os.path.expanduser("~"), "Documents", safe)
    items: list[PlanItem] = []
    taken: set[str] = set()
    for r in rows:
        if os.path.dirname(r.path) == target:
            continue
        dst = _free_name(target, r.name, taken)
        taken.add(dst)
        items.append(PlanItem(len(items) + 1, "move", r.path, dst, "organize",
                              f"part of “{label}”", r.size, r.mtime))  # fmt: skip
        if len(items) >= MAX_ITEMS:
            break
    plan = Plan("organize", f"Organize “{label}” into {_short(target)}", items)
    plan.note = (f"Move {len(items)} files into {_short(target)}." if items
                 else f"Everything in “{label}” is already in one place.")  # fmt: skip
    return plan


def _free_name(folder: str, name: str, taken: set[str]) -> str:
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    n = 2
    while candidate in taken or os.path.lexists(candidate):
        candidate = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return candidate


def _short(path: str | None) -> str:
    if not path:
        return ""
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path
