"""Names that speech recognition mishears ("Zakari" -> Zachary), the wake word vs. the
Zachary project, and general questions going to the LLM."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes import FakePlatform, write
from zachary.actions.safe_actions import SafeActions
from zachary.brain import qa
from zachary.brain.handler import Handler, about_my_files
from zachary.brain.llm import ClaudeLLM
from zachary.brain.vocab import Corrector, sound_key
from zachary.indexer import projects
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.search.hybrid import search
from zachary.voice.gnani_stt import keyterms_value

VOCAB = ["Zachary", "Durdrsti", "Spotify", "leetcode solutions", "Hackfest SVIT"]
KNOWN = {"open", "the", "fee", "receipt", "show", "my", "movie", "scary", "new", "file", "in"}


def corrector() -> Corrector:
    return Corrector(VOCAB, lambda w: w in KNOWN)


def test_sound_key() -> None:
    assert sound_key("zachary") == sound_key("zakari") == sound_key("zackery")
    assert sound_key("spotify") == sound_key("spotfy")


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [("open zakari in vscode", "open zachary in vscode"),
     ("open zachery", "open zachary"), ("open zack ary", "open zachary"),
     ("open sachary project", "open zachary project"), ("open spotfy", "open spotify"),
     ("open durdristi", "open durdrsti"), ("open spot ify", "open spotify"),
     ("open zakre in vs code", "open zachary in vs code")],  # Gnani, real (no keyterms)
)  # fmt: skip
def test_misheard_names_are_fixed(heard: str, fixed: str) -> None:
    assert corrector().correct(heard)[0] == fixed


@pytest.mark.parametrize(
    "text", ["show my scary movie", "open the fee receipt", "open new file", "open zachary"]
)
def test_real_words_are_left_alone(text: str) -> None:
    assert corrector().correct(text) == (text, {})


def test_zachary_is_searchable(store: FileStore, home: Path) -> None:
    """ "zachary" used to be dropped as the wake word: the project was unfindable."""
    write(home / "code" / "Zachary" / "pyproject.toml")
    scan(store)
    assert [r.name for r in search(store.conn, "zachary").results][:1] == ["Zachary"]


@pytest.fixture
def h(store: FileStore, home: Path, fake_platform: FakePlatform) -> Handler:
    write(home / "code" / "Zachary" / "pyproject.toml")
    write(home / "code" / "Zachary" / "zachary" / "__init__.py")
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    return Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)


def test_open_misheard_project_in_vscode(h: Handler, fake_platform: FakePlatform, home: Path
                                         ) -> None:  # fmt: skip
    assert h.submit("open zakari in vs code").say == "Opening Zachary in Visual Studio Code."
    assert fake_platform.launched == [("code.desktop", home / "code" / "Zachary")]


def test_open_project_by_exact_name(h: Handler, fake_platform: FakePlatform, home: Path) -> None:
    assert h.submit("open zachary").say == "Opening Zachary."
    assert fake_platform.opened == [home / "code" / "Zachary"]  # not the inner package
    assert h.submit("hey zachary open the readme").intent.query != "zachary"


def test_keyterms_header_value() -> None:
    v = json.loads(keyterms_value(lambda: ["Zachary", "ಕನ್ನಡ", "bad <|x|>"] + ["n"] * 300))
    assert v[:2] == ["Zachary", "ಕನ್ನಡ"] and len(v) == 200 and "bad <|x|>" not in v
    assert keyterms_value(lambda: ["ಕನ್ನಡ"]).isascii()  # (a WebSocket header)
    assert keyterms_value(None) == ""


# -- general questions ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("q", "mine"),
    [("how many planets are there", False), ("how do i reverse a string in python", False),
     ("when is my fee due", True), ("mera result kab aayega", True),
     ("what does the assignment say", True)],
)  # fmt: skip
def test_about_my_files(q: str, mine: bool) -> None:
    assert about_my_files(q) is mine


class Msgs:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies, self.calls = replies, []  # type: ignore[var-annotated]

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", usage=None, content=[
            SimpleNamespace(type="text", text=json.dumps(self.replies.pop(0)))])  # fmt: skip


def test_general_question_sends_only_the_question(
    h: Handler, store: FileStore, fake_platform: FakePlatform
) -> None:
    msgs = Msgs([{"answer": "Eight."}])
    model = ClaudeLLM("k", client=SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    h._llm_provider = lambda: model
    r = h.submit("how many planets are there")
    assert r.say == "Eight." and "not from your files" in r.detail
    sent = msgs.calls[0]["messages"][0]["content"]
    assert "how many planets are there" in sent and "<excerpt" not in sent


def test_general_question_without_llm(h: Handler) -> None:
    assert "turn on the LLM" in h.submit("how many planets are there").say


def test_impersonal_question_named_like_a_file_is_answered_from_it(
    store: FileStore, home: Path
) -> None:
    (home / "hackfest").mkdir()
    (home / "hackfest" / "hackfest pitch notes.txt").write_text(
        "The hackfest pitch is on Friday at 10 am in the main hall."
    )
    scan(store)
    from zachary.core.config import Config
    from zachary.indexer.embedder import HashEmbedder
    from zachary.indexer.semantic import SemanticIndexer
    from zachary.search import vectors

    SemanticIndexer(store.conn, Config(store.conn, store.config.platform), HashEmbedder()).run(8)
    vectors.build(store.conn)
    ps = qa.retrieve(store.conn, "when was the hackfest pitch")
    assert ps and qa.about_files("when was the hackfest pitch", ps, personal=False)
    assert not qa.about_files("how many planets are there",
                              qa.retrieve(store.conn, "how many planets are there"), False)  # fmt: skip


def test_short_common_words_are_never_names() -> None:
    """ "how many files" once became "how main files" (a folder called main)."""
    c = Corrector(["main", "backend", "Zachary", "Manya"], lambda w: w in {"how", "files"})
    assert c.correct("how many files do i have") == ("how many files do i have", {})


# -- "how many ... do I have": counted in the index, never the LLM -------------------------------
@pytest.mark.parametrize(
    "text",
    ["How many files do I have?", "how many pdfs do i have", "how many projects do i have",
     "how much space do my videos take", "how many files in downloads", "kitni files hain",
     "downloads mein kitni files hain", "how many python files", "how many folders"],
)  # fmt: skip
def test_stats_intent(text: str) -> None:
    from zachary.brain.intents import parse

    assert parse(text).kind == "stats"


def test_stats_answers(store: FileStore, home: Path, fake_platform: FakePlatform) -> None:
    def put(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    for n in range(3):
        put(home / "Downloads" / f"r{n}.pdf", b"x" * 1000)
    put(home / "Videos" / "trip.mp4", b"v" * 5000)
    put(home / "Documents" / "clip.mp4", b"v" * 3000)
    put(home / "code" / "app" / "pyproject.toml", b"[project]")
    put(home / "code" / "app" / "main.py", b"print(1)")
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    asked: list[str] = []

    def no_llm() -> None:
        asked.append("llm")

    h._llm_provider = no_llm
    assert h.submit("how many pdfs do i have").say == "You have 3 PDF files, 3 KB in total."
    assert h.submit("how many files in downloads").say.startswith("You have 3 files in Downloads")
    assert h.submit("how much space do my videos take").say == "Your videos take 8 KB (2 files)."
    assert h.submit("how many projects do i have").say == "You have 1 project."
    r = h.submit("How many files do I have?")
    assert r.say.startswith("You have 7 files") and "Videos" in r.detail
    assert not asked  # counted locally, no LLM


# -- questions about a project go to the LLM, from that project's files ------------------------
@pytest.mark.parametrize(
    ("text", "kind"),
    [("what is the zachary project about", "ask"), ("what does this project do", "ask"),
     ("how do i run the zachary project", "ask"), ("what is the durdrsti project", "ask"),
     ("how many projects do i have", "stats"), ("what are my projects", "show_projects"),
     ("show my projects", "show_projects"), ("open the zachary project", "open")],
)  # fmt: skip
def test_project_questions_parse(text: str, kind: str) -> None:
    from zachary.brain.intents import parse

    i = parse(text)
    assert i.kind == kind and (kind != "ask" or i.project_hint)


def test_project_question_answered_from_its_own_files(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    from zachary.core.config import Config
    from zachary.indexer.embedder import HashEmbedder
    from zachary.indexer.semantic import SemanticIndexer

    app = home / "code" / "Hydra"
    (app / "hydra.egg-info").mkdir(parents=True)
    (app / "pyproject.toml").write_text("[project]\nname='hydra'\n")
    (app / "README.md").write_text("# Hydra\nTracks hostel water use from meter photos.\n")
    (app / "hydra.egg-info" / "SOURCES.txt").write_text("hydra/__init__.py hydra hydra hydra")
    (home / "Other").mkdir()
    (home / "Other" / "notes.md").write_text("Hydra is also a Greek monster.")  # elsewhere
    (home / "Durdrsti").mkdir()
    (home / "Durdrsti" / "plan.md").write_text("Durdrsti forecasts port congestion.")
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    SemanticIndexer(store.conn, Config(store.conn, store.config.platform), HashEmbedder()).run(16)
    msgs = Msgs([{"answer": "It tracks water use.", "found": True, "sources": [1]},
                 {"answer": "It forecasts congestion.", "found": True, "sources": [1]}])  # fmt: skip
    model = ClaudeLLM("k", client=SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config, None,
                lambda: model)  # fmt: skip
    r = h.submit("what is the hydra project about")
    assert r.say == "It tracks water use." and r.items[0].name == "Hydra"
    sent = msgs.calls[0]["messages"][0]["content"]
    assert "meter photos" in sent and "Greek monster" not in sent  # only Hydra's files
    assert "SOURCES.txt" not in sent  # build output skipped
    r2 = h.submit("what is the durdrsti project")  # a folder, not a code project
    assert (
        r2.items[0].name == "Durdrsti"
        and "port congestion" in msgs.calls[1]["messages"][0]["content"]
    )
