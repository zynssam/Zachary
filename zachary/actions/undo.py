"""One-click undo: replay a plan's ``undo_log`` backwards.

* ``move`` src -> dst: move dst back to src (never over an existing file).
* ``trash`` src (dst = place in the OS trash): restore it from the trash.
* ``mkdir`` d: remove the folder again if it's empty.

Each step is independent: one that can't be undone (e.g. the trash was emptied) is
reported and the rest still run. The plan becomes ``undone`` when everything was.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field

from zachary.actions import planner
from zachary.actions.planner import Plan
from zachary.core.platform import Platform

log = logging.getLogger(__name__)


@dataclass(slots=True)
class UndoResult:
    restored: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        s = f"Undid {self.restored} change{'s' if self.restored != 1 else ''}."
        if self.problems:
            s += f" {len(self.problems)} couldn't be undone."
        return s


def undo(conn: sqlite3.Connection, platform: Platform, plan: Plan) -> UndoResult:
    if plan.status not in {"executed", "failed", "executing"}:
        raise ValueError(f"plan {plan.id} is {plan.status}; nothing to undo")
    rows = conn.execute(
        "SELECT id, op, src, dst FROM undo_log WHERE plan_id = ? ORDER BY id DESC", (plan.id,)
    ).fetchall()
    res = UndoResult()
    for _id, op, src, dst in rows:
        try:
            if op == "move":
                if os.path.lexists(src):
                    raise OSError(f"{planner._short(src)} exists again")
                if not dst or not os.path.lexists(dst):
                    raise OSError(f"{planner._short(dst)} is gone")
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
            elif op == "trash":
                platform.restore(dst or "", src)
            elif op == "mkdir":
                if os.path.isdir(src) and not os.listdir(src):
                    os.rmdir(src)
                else:
                    continue  # still in use (or already gone): leave it, not a problem
            res.restored += op != "mkdir"
        except Exception as e:
            log.warning("undo of %s %s failed: %s", op, src, e)
            res.problems.append(f"{planner._short(src)}: {e}")
    planner.set_status(conn, plan, "undone" if not res.problems else plan.status)
    return res
