from __future__ import annotations

from zachary.brain.context import CONTEXT_TTL_S, Context, Item


def _items() -> list[Item]:
    # name, ts
    return [
        Item(1, "/h/report_v3.docx", "report_v3.docx", "document", False, 300.0),
        Item(2, "/h/report_v1.docx", "report_v1.docx", "document", False, 100.0),
        Item(3, "/h/report_v2.docx", "report_v2.docx", "document", False, 200.0),
    ]


def test_ordinals_and_focus() -> None:
    c = Context()
    c.set_results(_items(), "report", "results", now=0)
    assert c.focused_item() is not None and c.focused_item().file_id == 1  # type: ignore[union-attr]
    assert c.by_ordinal(2).file_id == 2  # type: ignore[union-attr]
    assert c.focused_item().file_id == 2  # type: ignore[union-attr]
    assert c.by_ordinal(-1).file_id == 3  # type: ignore[union-attr]
    assert c.by_ordinal(9) is None


def test_older_and_newer_walk_by_time() -> None:
    c = Context()
    c.set_results(_items(), "report", "results", now=0)
    assert c.older().name == "report_v2.docx"  # type: ignore[union-attr]
    assert c.older().name == "report_v1.docx"  # type: ignore[union-attr]
    assert c.older() is None
    assert c.newer().name == "report_v2.docx"  # type: ignore[union-attr]


def test_expiry_and_empty() -> None:
    c = Context()
    assert not c.active()
    c.set_results(_items(), "q", "results", now=1000)
    assert c.active(now=1000 + CONTEXT_TTL_S - 1)
    assert not c.active(now=1000 + CONTEXT_TTL_S + 1)
    c.set_results([], "q", "results")
    assert c.focused_item() is None and c.older() is None
