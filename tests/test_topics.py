"""Phase 5: topics (clustering), sessions, learning from choices, and their intents
through the handler. Synthetic vectors stand in for the embedding model."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from tests.fakes import FakePlatform, write
from zachary.actions.safe_actions import SafeActions
from zachary.brain import sessions as sess
from zachary.brain.handler import Handler
from zachary.indexer import clustering
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.search import feedback
from zachary.search.hybrid import search

DIM = 256
NOW = time.time()


def _vec(center: np.ndarray, rng: np.random.Generator, noise: float = 0.25) -> bytes:
    v = center + rng.standard_normal(DIM) * noise / np.sqrt(DIM) * 4
    v /= np.linalg.norm(v)
    return bytes((v * 127).clip(-127, 127).astype(np.int8).tobytes())


def _put_vectors(store: FileStore, vectors: dict[str, bytes]) -> None:
    ids = dict(store.conn.execute("SELECT path, id FROM files").fetchall())
    with store.conn:
        for path, blob in vectors.items():
            store.conn.execute(
                "INSERT OR REPLACE INTO vec_ivf(cluster, file_id, embedding) VALUES (0, ?, ?)",
                (ids[path], blob),
            )


@pytest.fixture
def corpus(store: FileStore, home: Path) -> FileStore:
    """Three subjects of notes (one also with code), plus scattered files."""
    rng = np.random.default_rng(5)
    centers = {name: rng.standard_normal(DIM) for name in ("dsa", "ml", "fees", "far")}
    for c in centers.values():
        c /= np.linalg.norm(c)
    vectors: dict[str, bytes] = {}
    plan = [
        ("dsa", home / "College" / "DSA", "linked list trees graphs heap notes", 14, ".md"),
        ("ml", home / "Sem5" / "notes", "machine learning regression neural notes", 12, ".pdf"),
        ("fees", home / "Admin", "fee receipt payment semester", 10, ".pdf"),
    ]
    for key, folder, text, n, ext in plan:
        for i in range(n):
            p = write(folder / f"{key}_{text.split()[i % 4]}_{i}{ext}", text, mtime=NOW - i * 3600)
            vectors[str(p)] = _vec(centers[key], rng)
    # DSA code next to the notes (very close: should join), and unrelated code (shouldn't).
    for i in range(4):
        p = write(home / "code" / "dsa" / f"bst_{i}.cpp", "tree", mtime=NOW)
        vectors[str(p)] = _vec(centers["dsa"], rng, noise=0.1)
    for i in range(6):
        p = write(home / "code" / "web" / f"page_{i}.js", "react", mtime=NOW)
        vectors[str(p)] = _vec(centers["far"], rng)
    for i in range(10):  # scattered notes, no topic
        p = write(home / "misc" / f"note_{i}.txt", "misc", mtime=NOW)
        vectors[str(p)] = _vec(rng.standard_normal(DIM), rng, noise=0.0)
    scan(store)
    _put_vectors(store, vectors)
    return store


def _members(conn, label_part: str) -> list[str]:  # type: ignore[no-untyped-def]
    t = next(t for t in clustering.list_topics(conn) if label_part.lower() in t.label.lower())
    return [
        conn.execute("SELECT name FROM files WHERE id = ?", (f,)).fetchone()[0]
        for f in clustering.topic_files(conn, t.id)
    ]


def test_topics_found_and_labelled(corpus: FileStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clustering, "MIN_FILES", 10)
    st = clustering.build(corpus.conn, [str(corpus.config.index_roots()[0])])
    labels = sorted(t.label for t in clustering.list_topics(corpus.conn))
    assert st.topics == 3, labels
    assert "DSA" in labels and "Admin" in labels  # specific shared folders
    assert any("Machine" in lb or "Notes" in lb or "Sem5" in lb for lb in labels)
    dsa = _members(corpus.conn, "DSA")
    assert sum(n.startswith("dsa_") for n in dsa) == 14
    assert sum(n.startswith("bst_") for n in dsa) == 4  # close code joined the notes
    assert not any(n.startswith(("page_", "note_")) for t in clustering.list_topics(corpus.conn)
                   for n in _members(corpus.conn, t.label))  # fmt: skip


def test_topic_ids_stay_stable(corpus: FileStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clustering, "MIN_FILES", 10)
    clustering.build(corpus.conn)
    before = {t.label: t.id for t in clustering.list_topics(corpus.conn)}
    clustering.build(corpus.conn)
    after = {t.label: t.id for t in clustering.list_topics(corpus.conn)}
    assert before == after
    assert not clustering.due(corpus.conn)  # rebuilt just now: next one tomorrow


def test_too_few_files_makes_no_topics(store: FileStore, home: Path) -> None:
    write(home / "a.txt")
    scan(store)
    st = clustering.build(store.conn)
    assert st.topics == 0 and clustering.list_topics(store.conn) == []


# -- sessions --------------------------------------------------------------------------------
def _log(store: FileStore, path: Path, ts: float, source: str = "os_recent") -> None:
    fid = store.conn.execute("SELECT id FROM files WHERE path = ?", (str(path),)).fetchone()[0]
    store.conn.execute(
        "INSERT INTO access_log(file_id, ts, source) VALUES (?, ?, ?)", (fid, ts, source)
    )


@pytest.fixture
def worked(store: FileStore, home: Path) -> FileStore:
    a = write(home / "zach" / "pyproject.toml")
    b = write(home / "zach" / "pipeline.py")
    c = write(home / "zach" / "notes.md")
    d = write(home / "DSA" / "trees.pdf")
    e = write(home / ".bash_history")
    scan(store)
    from zachary.indexer import projects

    projects.apply_markers(store, scan(store).markers, scope=None)
    from datetime import date, datetime, timedelta

    day = date.today() - timedelta(days=2)
    t = datetime(day.year, day.month, day.day, 10).timestamp()  # two days ago, 10 am
    for i, p in enumerate([b, c, b, a, b]):  # one session, ~50 min
        _log(store, p, t + i * 600, "edit")
    _log(store, e, t + 100)  # shell history: noise
    _log(store, d, t + 5 * 3600)  # later: separate session
    _log(store, d, t + 5 * 3600 + 900)
    store.conn.commit()
    return store


def test_sessions_split_by_gaps_and_labelled(worked: FileStore) -> None:
    ss = sess.sessions(worked.conn, NOW - 3 * 86400, NOW)
    assert len(ss) == 2
    first, second = ss
    # Most worked on first, then most recent.
    assert [f.name for f in first.files] == ["pipeline.py", "pyproject.toml", "notes.md"]
    assert first.minutes == 40 and first.label == "zach"  # the project
    assert [f.name for f in second.files] == ["trees.pdf"] and second.label == "DSA"
    assert not any(f.name == ".bash_history" for s in ss for f in s.files)


def test_resume_picks_the_last_real_session(worked: FileStore) -> None:
    s = sess.last_session(worked.conn, NOW)
    assert s is not None and s.label == "DSA"
    # Just now: one quick look at something (e.g. via Zachary) -> resume the one before.
    write_ts = NOW - 60
    fid = worked.conn.execute("SELECT id FROM files WHERE name = 'notes.md'").fetchone()[0]
    worked.conn.execute("INSERT INTO access_log VALUES (NULL, ?, ?, 'zach')", (fid, write_ts))
    s2 = sess.last_session(worked.conn, NOW)
    assert s2 is not None and s2.label == "DSA"


# -- learning from choices -------------------------------------------------------------------
def test_ranking_adapts_to_repeated_choices(store: FileStore, home: Path) -> None:
    """Three equally good matches: choosing the third twice makes it first."""
    for i, name in enumerate(["report_alpha.docx", "report_beta.docx", "report_gamma.docx"]):
        write(home / "Work" / name, mtime=NOW - 86400 * (10 + i))
    scan(store)
    names = [r.name for r in search(store.conn, "report").results]
    assert names[0] == "report_alpha.docx"
    gamma = next(r for r in search(store.conn, "report").results if "gamma" in r.name)
    rank = names.index(gamma.name)
    feedback.record(store.conn, ["report"], gamma.file_id, rank)
    feedback.record(store.conn, ["report"], gamma.file_id, rank)
    res = search(store.conn, "report").results
    assert res[0].name == "report_gamma.docx" and "you chose this before" in res[0].why
    # Similar query (shares half the words): boosted too. Unrelated: untouched.
    assert search(store.conn, "work report").results[0].name == "report_gamma.docx"
    assert "you chose this before" not in search(store.conn, "alpha").results[0].why


def test_old_choices_fade(store: FileStore, home: Path) -> None:
    for name in ["notes_a.md", "notes_b.md"]:
        write(home / name, mtime=NOW - 86400)
    scan(store)
    b = next(r for r in search(store.conn, "notes").results if r.name == "notes_b.md")
    feedback.record(store.conn, ["notes"], b.file_id, 1, now=NOW - 200 * 86400)
    assert feedback.boosts(store.conn, ["notes"])[b.file_id] < 0.02  # ~6 half-lives ago


# -- through the handler -----------------------------------------------------------------------
def test_handler_topics_drill_in(corpus: FileStore, fake_platform: FakePlatform,
                                 monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr(clustering, "MIN_FILES", 10)
    clustering.build(corpus.conn)
    h = Handler(corpus.conn, SafeActions(corpus.conn, fake_platform), corpus.config)
    r = h.submit("show my topics")
    assert r.view == "topics" and len(r.items) == 3 and all(i.kind == "topic" for i in r.items)
    dsa_row = next(i for i, it in enumerate(r.items) if it.name == "DSA")
    r2 = h.act(dsa_row, "open")  # click / Enter on a topic shows its files
    assert r2.view == "results" and len(r2.items) == 18 and not fake_platform.opened
    assert r2.items[0].why == ["topic: DSA"]
    r3 = h.submit("topic dsa")
    assert len(r3.items) == 18


def test_handler_timeline_and_resume_ask_before_reopening(
    worked: FileStore, fake_platform: FakePlatform
) -> None:
    h = Handler(worked.conn, SafeActions(worked.conn, fake_platform), worked.config)
    r = h.submit("what was I working on 2 days ago")
    assert r.view == "timeline" and [i.name for i in r.items] == ["DSA", "zach"]
    assert "2 sessions" in r.say
    r2 = h.submit("open the second one")  # the "zach" session
    assert r2.say.endswith("Reopen them?") and not fake_platform.opened
    assert [i.name for i in r2.items] == ["pipeline.py", "pyproject.toml", "notes.md"]
    r3 = h.submit("yes")
    assert r3.say == "Reopened 3 files."
    assert sorted(p.name for p in fake_platform.opened) == ["notes.md", "pipeline.py",
                                                            "pyproject.toml"]  # fmt: skip
    # "resume where I left off" -> proposes; "no" -> nothing opens; "yes" alone -> nothing.
    fake_platform.opened.clear()
    r4 = h.submit("resume where I left off")
    assert "Your last session, DSA" in r4.say and r4.say.endswith("Reopen them?")
    assert h.submit("no").say == "Okay."
    assert h.submit("yes").say == "Nothing to confirm." and not fake_platform.opened
    # Typing (preview) never arms a proposal.
    h.preview("resume where I left off")
    assert h.submit("yes").say == "Nothing to confirm."
    # Spoken answers: "re-open them" reopens instead of searching for "reopen".
    h.submit("resume where I left off")
    assert h.preview("Re-open them.").say.startswith("Enter to reopen")
    r5 = h.submit("Re-open them.")
    assert r5.say.startswith("Reopened") and fake_platform.opened


def test_handler_records_choices(store: FileStore, home: Path,
                                 fake_platform: FakePlatform) -> None:  # fmt: skip
    for name in ["report_alpha.docx", "report_beta.docx", "report_gamma.docx"]:
        write(home / "Work" / name, mtime=NOW - 86400 * 20)
    scan(store)
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    first = h.submit("report")
    third = first.items[2].name
    h.submit("open the third one")
    h.submit("report")
    h.submit("open the third one" if h.context.items[2].name == third else "open the first one")
    assert h.submit("report").items[0].name == third
    n = store.conn.execute("SELECT count(*) FROM feedback WHERE terms = 'report'").fetchone()[0]
    assert n == 2
