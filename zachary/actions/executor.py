"""Runs an *approved* plan. The only code in Zachary that moves or removes files.

Rules (CLAUDE.md, non-negotiable):

* Only a plan with status ``approved`` runs, and only its selected items.
* Removal is always ``send2trash`` (through the platform), never a permanent delete.
* Every change is written to ``undo_log`` right after it happens (autocommit), so
  undo works even if Zachary is killed half-way.
* Checked again at run time, item by item: the file still exists, is unchanged since
  the plan (size + mtime), isn't protected (``never_touch``, a project, hidden), and a
  move never overwrites anything. Anything that fails a check is skipped, not forced.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from zachary.actions import planner
from zachary.actions.planner import Guard, Plan, PlanItem
from zachary.core.config import Config
from zachary.core.platform import Platform

log = logging.getLogger(__name__)

MTIME_SLACK_S = 2.0  # file systems round mtimes (FAT: 2 s)


class NotApproved(PermissionError):
    pass


@dataclass(slots=True)
class RunResult:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    freed: int = 0  # bytes sent to the trash

    def summary(self) -> str:
        parts = [f"{self.done} done"]
        if self.freed:
            parts[0] += f" ({planner.human(self.freed)} to the trash)"
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        return ", ".join(parts) + "."


def _log(conn: sqlite3.Connection, plan_id: int, op: str, src: str, dst: str | None) -> None:
    conn.execute(
        "INSERT INTO undo_log(plan_id, op, src, dst, ts) VALUES (?, ?, ?, ?, ?)",
        (plan_id, op, src, dst, time.time()),
    )


def _unchanged(item: PlanItem) -> str | None:
    """Why the item can't run any more, or None."""
    p = item.src
    if not os.path.lexists(p):
        return "it no longer exists"
    try:
        st = os.lstat(p)
    except OSError as e:
        return f"can't read it ({e.strerror})"
    if item.is_dir:
        try:
            if item.category == "empty_folder" and os.listdir(p):
                return "the folder isn't empty any more"
        except OSError as e:
            return f"can't read it ({e.strerror})"
        return None
    if st.st_size != item.size or abs(st.st_mtime - item.mtime) > MTIME_SLACK_S:
        return "it changed since the plan was made"
    return None


def execute(conn: sqlite3.Connection, platform: Platform, config: Config, plan: Plan
            ) -> RunResult:  # fmt: skip
    if plan.status != "approved":
        raise NotApproved(f"plan {plan.id} is {plan.status}, not approved")
    planner.set_status(conn, plan, "executing")
    guard = Guard(conn, config)
    res = RunResult()
    for item in plan.items:
        if not item.selected:
            item.result = "not selected"
            continue
        why = _unchanged(item)
        if why is None and guard.protected(item.src, junk=item.category == "junk"):
            why = "the folder is protected (never touch / project)"
        if why is None and item.op == "move":
            if not item.dst:
                why = "no destination"
            elif os.path.lexists(item.dst):
                why = "something already exists at the destination"
            elif guard.protected(item.dst) and not _new_folder_ok(guard, item.dst):
                why = "the destination folder is protected"
        if why is not None:
            item.result = f"skipped: {why}"
            res.skipped += 1
            continue
        try:
            if item.op == "trash":
                where = platform.trash(Path(item.src))
                _log(conn, plan.id, "trash", item.src, where)
                res.freed += item.size
            elif item.op == "move":
                assert item.dst is not None
                for d in _missing_parents(item.dst):
                    os.mkdir(d)
                    _log(conn, plan.id, "mkdir", d, None)
                shutil.move(item.src, item.dst)
                _log(conn, plan.id, "move", item.src, item.dst)
            else:
                raise ValueError(f"unknown op {item.op!r}")
            item.result = "done"
            res.done += 1
        except Exception as e:  # one bad file never stops the rest
            log.warning("plan %s item %s failed: %s", plan.id, item.id, e)
            item.result = f"failed: {e}"
            res.failed += 1
    planner.save(conn, plan)
    planner.set_status(conn, plan, "failed" if res.failed and not res.done else "executed")
    return res


def _missing_parents(dst: str) -> list[str]:
    out: list[str] = []
    p = os.path.dirname(dst)
    while p and not os.path.isdir(p):
        out.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return list(reversed(out))


def _new_folder_ok(guard: Guard, dst: str) -> bool:
    """A move may create its target folder (e.g. Documents/DSA) under an index root, as
    long as no existing ancestor is never_touch / a project / hidden."""
    p = os.path.dirname(dst)
    while p and not os.path.isdir(p):
        p = os.path.dirname(p)
    return bool(p) and not guard.protected(os.path.join(p, "x"))
