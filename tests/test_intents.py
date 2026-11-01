"""Intent parser: every intent with English, Hinglish/Kannada, and typo variants."""

from __future__ import annotations

import pytest

from zachary.brain.intents import parse


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("open the fee receipt", "the fee receipt"),
        ("please open fee receipt", "fee receipt"),
        ("hey zach open my resume", "my resume"),
        ("fee receipt kholo", "fee receipt"),  # Hinglish
        ("fee receipt khol do", "fee receipt do"),
        ("resume tere", "resume"),  # Kannada
        ("opne fee receipt", "fee receipt"),  # typo
        ("oepn resume", "resume"),  # typo
    ],
)
def test_open(text: str, query: str) -> None:
    i = parse(text)
    assert i.kind == "open" and i.query == query


@pytest.mark.parametrize(
    "text",
    [
        "find my DSA notes",
        "search linked list code",
        "show me everything on data structures",
        "DSA notes dhundo",  # Hinglish
        "linked list code dikhao",
        "DSA notes hudukku",  # Kannada
        "serch linked list",  # typo
        "fnid dsa notes",  # typo (transposition)
    ],
)
def test_find(text: str) -> None:
    assert parse(text).kind == "find"


def test_bare_text_is_a_low_confidence_find() -> None:
    i = parse("fees yesterday")
    assert i.kind == "find" and i.query == "fees yesterday" and i.confidence < 0.7


@pytest.mark.parametrize(
    "text",
    [
        "show me all my project files across my storage",
        "my projects",
        "list projects",
        "mere projects dikhao",  # Hinglish
        "saare projects dikhao",
        "nanna projects torisu",  # Kannada
        "shwo my porjects",  # typos
        "show my projetcs",
    ],
)
def test_show_projects(text: str) -> None:
    assert parse(text).kind == "show_projects"


def test_named_project_is_an_open_with_project_hint() -> None:
    i = parse("open the zachary project")
    assert i.kind == "open" and i.project_hint and "zachary" in i.query


@pytest.mark.parametrize(
    "text",
    [
        "recent files",
        "what did I open yesterday",
        "what was I working on last tuesday",
        "recently opened",
        "kal kya khola tha",  # Hinglish
        "haal hi mein kholi files",
        "ninne open madida files",  # Kannada
        "recnet files",  # typo
        "what did i opne yesterday",  # typo in a non-command position stays harmless
    ],
)
def test_recent(text: str) -> None:
    assert parse(text).kind == "recent"


@pytest.mark.parametrize(
    ("text", "ordinal"),
    [
        ("open the second one", 2),
        ("the first one", 1),
        ("open number 3", 3),
        ("#4", 4),
        ("open 2", 2),
        ("the last one", -1),
        ("2nd one", 2),
        ("doosra wala kholo", 2),  # Hinglish
        ("pehla wala", 1),
        ("aakhri wala", -1),
        ("eradane", 2),  # Kannada
        ("secnd one", 2),  # typo
        ("open the thrid one", 3),  # typo
    ],
)
def test_select_followups(text: str, ordinal: int) -> None:
    i = parse(text)
    assert i.kind == "select" and i.ordinal == ordinal and i.action == "open"


@pytest.mark.parametrize(
    "text",
    [
        "show its folder",
        "open its folder",
        "where is it",
        "show in folder",
        "iska folder kholo",  # Hinglish
        "kahan hai",
        "folder elli",  # Kannada
        "show its foldr",  # typo
    ],
)
def test_reveal(text: str) -> None:
    i = parse(text)
    assert i.kind == "reveal" and i.action == "reveal"


def test_select_with_reveal() -> None:
    i = parse("show the second one's folder")
    assert i.kind == "select" and i.ordinal == 2 and i.action == "reveal"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("the older one", "older"),
        ("open the old one", "older"),
        ("purana wala", "older"),  # Hinglish
        ("haleya", "older"),  # Kannada
        ("the newer one", "newer"),
        ("the latest one", "newer"),
        ("naya wala", "newer"),
        ("the oldr one", "older"),  # typo
    ],
)
def test_older_newer(text: str, kind: str) -> None:
    assert parse(text).kind == kind


@pytest.mark.parametrize("text", ["open it", "open that", "isko kholo"])
def test_open_it(text: str) -> None:
    i = parse(text)
    assert i.kind == "select" and i.ordinal is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("shop receipts", "find"),  # "shop" is not "show"
        ("report", "find"),  # not "repo"
        ("sem 5 notes", "find"),  # "5" here is not an ordinal
        ("mere notes", "find"),  # Hindi "mere" is not Kannada "tere"
        ("the second report", "find"),  # an ordinal plus content is a search
        ("open file 2024 budget", "open"),
    ],
)
def test_no_false_commands(text: str, kind: str) -> None:
    assert parse(text).kind == kind


def test_long_sentence_from_the_spec() -> None:
    i = parse("Hey Zach, I opened a document about my fees yesterday, I forgot the name, open it.")
    assert i.kind == "open"
    assert "fees" in i.query and "yesterday" in i.query


def test_empty() -> None:
    assert parse("").kind == "none"
    assert parse("   ").kind == "none"
