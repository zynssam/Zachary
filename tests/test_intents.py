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


@pytest.mark.parametrize(
    ("text", "kind", "query"),
    [
        ("show my topics", "topic", ""),
        ("my subjects", "topic", ""),
        ("topic machine learning", "topic", "machine learning"),
        ("resume where I left off", "resume", ""),
        ("pick up where i left off", "resume", ""),
        ("reopen my last session", "resume", ""),
        ("resume my work", "resume", ""),
        ("jahan chhoda tha wahi se", "resume", ""),
        ("what was I working on last tuesday", "timeline", "what was i working on last tuesday"),
        ("what did I do yesterday", "timeline", "what did i do yesterday"),
        ("kal kya kaam kiya", "timeline", "kal kya kaam kiya"),
        ("show my sessions", "timeline", "show my sessions"),
        ("yes", "confirm", ""),
        ("yes open them", "confirm", ""),
        ("haan", "confirm", ""),
        ("reopen them", "confirm", ""),
        ("Re-open them.", "confirm", ""),
        ("re open those", "confirm", ""),
        ("reopen these files", "confirm", ""),
        ("reopen those 8 files", "confirm", ""),
        ("yes open all of them", "confirm", ""),
        ("open them all", "confirm", ""),
        ("haan sab kholo", "confirm", ""),
        ("no", "cancel", ""),
        ("nahi", "cancel", ""),
    ],
)
def test_phase5_intents(text: str, kind: str, query: str) -> None:
    i = parse(text)
    assert (i.kind, i.query) == (kind, query)


@pytest.mark.parametrize("text", ["open it", "open that file", "open the first one"])
def test_confirm_words_dont_steal_followups(text: str) -> None:
    assert parse(text).kind == "select"


def test_all_files_stays_a_search() -> None:
    assert parse("all files").kind == "find"


@pytest.mark.parametrize(
    "text",
    [
        "files I worked on yesterday",
        "what did I open yesterday",
        "open fee receipt",
        "open my resume",
        "resume",
    ],
)
def test_phase5_intents_dont_steal_file_queries(text: str) -> None:
    assert parse(text).kind in {"recent", "open", "find"}


@pytest.mark.parametrize(
    "text",
    [
        "when is my fee due?",
        "when is my fee due",
        "whn is my fee due",  # typo
        "how much fee did I pay last year",
        "what is my receipt number",
        "who is my mentor",
        "fee kab bharna hai",  # Hinglish
        "meri fee kitni hai",
        "ask hostel fee amount",
        "is the hostel fee refundable?",
    ],
)
def test_ask_intent(text: str) -> None:
    assert parse(text).kind == "ask"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("where is my resume", "ask"),  # a locate question: the LLM picks, then it opens
        ("which file has my marks", "ask"),
        ("dsa notes?", "find"),
        ("fee receipt", "find"),
        ("open fee receipt", "open"),
        ("what are my projects", "show_projects"),
        ("what was I working on yesterday", "timeline"),
        ("what did I open yesterday", "recent"),
        ("show my topics", "topic"),
        ("kya maine kal kuch khola", "find"),
    ],
)
def test_not_a_question(text: str, kind: str) -> None:
    assert parse(text).kind == kind


@pytest.mark.parametrize(
    ("text", "query", "ordinal", "project"),
    [
        ("Explain me the last project I was working on.", "last project working", None, True),
        ("tell me about the zachary project", "zachary project", None, True),
        ("summarize my dsa notes", "dsa notes", None, False),
        ("summarise my dsa notes", "dsa notes", None, False),  # British spelling
        ("explain the second one", "", 2, False),
        ("explain the last one", "", -1, False),
        ("explain it", "", None, False),
        ("dsa notes samjhao", "dsa notes", None, False),  # Hinglish
        ("explian my latest project", "latest project", None, True),  # typo
    ],
)
def test_explain_intent(text: str, query: str, ordinal: int | None, project: bool) -> None:
    i = parse(text)
    assert (i.kind, i.query, i.ordinal, i.project_hint) == ("explain", query, ordinal, project)


@pytest.mark.parametrize(
    ("text", "kind", "query"),
    [
        ("clean up my downloads", "clean", "downloads"),
        ("find duplicates", "clean", "duplicates"),
        ("free up space", "clean", ""),
        ("remove old installers", "clean", "old installers"),
        ("delete empty folders", "clean", "empty"),
        ("downloads saaf karo", "clean", "downloads"),  # Hinglish
        ("claen my downloads", "clean", "downloads"),  # typo
        ("organize my dsa stuff", "organize", "dsa"),
        ("organise my machine learning notes", "organize", "machine learning notes"),
        ("orgnize my dsa notes", "organize", "dsa notes"),  # typo
        ("undo", "undo", ""),
        ("undo the last cleanup", "undo", ""),
        # Not destructive: stay searches.
        ("open the cleanup report", "open", "the cleanup report"),
        ("delete my resume", "find", "delete my resume"),
        ("restore my files", "find", "restore my files"),
    ],
)
def test_clean_organize_undo_intents(text: str, kind: str, query: str) -> None:
    i = parse(text)
    assert (i.kind, i.query) == (kind, query)
