from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.fakes import write
from zachary.brain.context import Context, Item
from zachary.indexer import versions
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.search.hybrid import search


@pytest.mark.parametrize(
    ("stem", "key"),
    [
        ("report_final_v2", "report"),
        ("Report (1)", "report"),
        ("Copy of report", "report"),
        ("report - Copy", "report"),
        ("thesis_v1.3", "thesis"),
        ("budget_final_FINAL", "budget"),
        ("meeting 2024-05-01", "meeting 2024 05 01"),  # dates are not versions
        ("chapter1", "chapter1"),  # numbers are not versions
        ("final", ""),
    ],
)
def test_version_key(stem: str, key: str) -> None:
    assert versions.version_key(stem) == key


def test_groups_same_folder_same_ext_only() -> None:
    rows = [
        (1, 10, "report.docx", ".docx", 100.0),
        (2, 10, "report_final.docx", ".docx", 300.0),
        (3, 10, "report_final_v2.docx", ".docx", 200.0),
        (4, 10, "report.pdf", ".pdf", 400.0),  # an export, not a version
        (5, 11, "report_v3.docx", ".docx", 500.0),  # other folder
        (6, 10, "chapter1.docx", ".docx", 1.0),
        (7, 10, "chapter2.docx", ".docx", 2.0),
    ]
    assert versions.groups_for(rows) == [[2, 3, 1]]


def test_search_shows_latest_and_older_one_walks_versions(store: FileStore, home: Path) -> None:
    now = time.time()
    write(home / "Work" / "proposal.docx", mtime=now - 3000)
    write(home / "Work" / "proposal_v2.docx", mtime=now - 2000)
    write(home / "Work" / "proposal_final.docx", mtime=now - 1000)
    write(home / "Work" / "notes.docx", mtime=now - 500)
    scan(store)
    assert versions.rebuild(store.conn) == 1
    res = search(store.conn, "proposal").results
    names = [r.name for r in res]
    assert names[0] == "proposal_final.docx" and "proposal.docx" not in names
    assert len(res[0].versions) == 2 and "latest of 3 versions" in res[0].why
    # "the older one" walks the versions of the focused result
    vitems = [Item(v, "", f"v{v}", "document", False, ts) for v, ts in
              ((2, now - 2000), (1, now - 3000))]  # fmt: skip
    top = Item(res[0].file_id, res[0].path, res[0].name, "document", False, now - 1000,
               versions=vitems)  # fmt: skip
    ctx = Context()
    ctx.set_results([top], "proposal", "results")
    assert ctx.older().name == "v2"  # type: ignore[union-attr]
    assert ctx.older().name == "v1"  # type: ignore[union-attr]
    assert ctx.older() is None
    assert ctx.newer().name == "v2"  # type: ignore[union-attr]
