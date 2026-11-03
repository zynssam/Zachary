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
