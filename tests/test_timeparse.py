from __future__ import annotations

from datetime import datetime

import pytest

from zachary.brain.timeparse import parse_time

# Wednesday 4 Nov 2026, 15:00 local time.
NOW = datetime(2026, 11, 4, 15, 0).astimezone()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %m-%d %H:%M")


@pytest.mark.parametrize(
    ("query", "label", "start", "end", "rest"),
    [
        # English
        ("fees yesterday", "yesterday", "Tue 11-03 00:00", "Wed 11-04 00:00", "fees"),
        ("notes from today", "today", "Wed 11-04 00:00", "Thu 11-05 00:00", "notes"),
        (
            "the day before yesterday",
            "day before yesterday",
            "Mon 11-02 00:00",
            "Tue 11-03 00:00",
            "",
        ),
        ("report from last week", "last week", "Mon 10-26 00:00", "Mon 11-02 00:00", "report"),
        ("slides this week", "this week", "Mon 11-02 00:00", "Thu 11-05 00:00", "slides"),
        ("bills last month", "last month", "Thu 10-01 00:00", "Sun 11-01 00:00", "bills"),
        ("last tuesday notes", "last Tuesday", "Tue 11-03 00:00", "Wed 11-04 00:00", "notes"),
        ("on wednesday", "last Wednesday", "Wed 10-28 00:00", "Thu 10-29 00:00", ""),
        ("on fri", "last Friday", "Fri 10-30 00:00", "Sat 10-31 00:00", ""),
        ("pdf 3 days ago", "3 days ago", "Sun 11-01 00:00", "Mon 11-02 00:00", "pdf"),
        ("a couple of days ago", "2 days ago", "Mon 11-02 00:00", "Tue 11-03 00:00", ""),
        ("past 5 days", "past 5 days", "Fri 10-30 15:00", "Wed 11-04 15:00", ""),
        ("this morning", "this morning", "Wed 11-04 00:00", "Wed 11-04 12:00", ""),
        ("pdf last night", "last night", "Tue 11-03 18:00", "Wed 11-04 04:00", "pdf"),
        ("recently edited", "recently", "Wed 10-28 15:00", "Wed 11-04 15:00", "edited"),
        # Hinglish / Hindi
        ("kal wali fee file", "yesterday", "Tue 11-03 00:00", "Wed 11-04 00:00", "fee file"),
        ("parso ka pdf", "day before yesterday", "Mon 11-02 00:00", "Tue 11-03 00:00", "pdf"),
        ("pichhle hafte ka report", "last week", "Mon 10-26 00:00", "Mon 11-02 00:00", "report"),
        (
            "mangalvar ko kholi thi",
            "last Tuesday",
            "Tue 11-03 00:00",
            "Wed 11-04 00:00",
            "kholi thi",
        ),
        ("do din pehle", "2 days ago", "Mon 11-02 00:00", "Tue 11-03 00:00", ""),
        ("kal raat ka pdf", "last night", "Tue 11-03 18:00", "Wed 11-04 04:00", "pdf"),
        ("aaj ki notes", "today", "Wed 11-04 00:00", "Thu 11-05 00:00", "notes"),
        # Kannada
        (
            "ninne open madida file",
            "yesterday",
            "Tue 11-03 00:00",
            "Wed 11-04 00:00",
            "open madida file",
        ),
        ("monne", "day before yesterday", "Mon 11-02 00:00", "Tue 11-03 00:00", ""),
        ("kaleda vaara report", "last week", "Mon 10-26 00:00", "Mon 11-02 00:00", "report"),
        # Typos
        ("fees yesterdy", "yesterday", "Tue 11-03 00:00", "Wed 11-04 00:00", "fees"),
        ("yestarday notes", "yesterday", "Tue 11-03 00:00", "Wed 11-04 00:00", "notes"),
        ("last tusday", "last Tuesday", "Tue 11-03 00:00", "Wed 11-04 00:00", ""),
        ("wensday slides", "last Wednesday", "Wed 10-28 00:00", "Thu 10-29 00:00", "slides"),
    ],
)
def test_time_phrases(query: str, label: str, start: str, end: str, rest: str) -> None:
    p = parse_time(query, NOW)
    assert p.range is not None, query
    assert p.range.label == label
    assert _day(p.range.start) == start
    assert _day(p.range.end) == end
    assert p.rest == rest


@pytest.mark.parametrize(
    "query",
    [
        "sun.png",
        "sundar photo",
        "resume",
        "mon repo",
        "linked list code",
        "sat exam",
        "kalman filter",
    ],
)
def test_no_false_positives(query: str) -> None:
    assert parse_time(query, NOW).range is None


def test_rest_keeps_unmatched_words() -> None:
    p = parse_time("open the fee doc I opened yesterday", NOW)
    assert p.rest == "open the fee doc i opened"
    assert p.matched == "yesterday"


def test_tuesday_on_tuesday_means_a_week_ago() -> None:
    tue = datetime(2026, 11, 3, 10, 0).astimezone()
    p = parse_time("last tuesday", tue)
    assert p.range is not None
    assert _day(p.range.start) == "Tue 10-27 00:00"


def test_default_now_is_used() -> None:
    assert parse_time("yesterday").range is not None
