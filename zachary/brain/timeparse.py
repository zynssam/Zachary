"""Parse time phrases in queries into a local-time range.

Handles English plus common Hindi/Hinglish and Kannada words:
"yesterday", "kal", "ninne", "day before yesterday", "parso", "last week",
"pichhle hafte", "last Tuesday", "mangalvar ko", "3 days ago", "2 din pehle",
"past 5 days", "this morning", "last night", "kal raat", "recently", ...

Pure functions: pass ``now`` for deterministic results. The matched phrase
(plus filler like "on"/"ko"/"wala") is removed from the returned ``rest``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: float  # unix seconds, inclusive
    end: float  # unix seconds, exclusive
    label: str


@dataclass(frozen=True, slots=True)
class TimeParse:
    range: TimeRange | None
    rest: str
    matched: str = ""


def _sod(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _som(d: datetime) -> datetime:
    return _sod(d).replace(day=1)


def _prev_month(d: datetime) -> datetime:
    first = _som(d)
    return _som(first - DAY)


def _sow(d: datetime) -> datetime:
    return _sod(d) - timedelta(days=d.weekday())


Resolver = Callable[[datetime], tuple[datetime, datetime]]


def _today(n: datetime) -> tuple[datetime, datetime]:
    return _sod(n), _sod(n) + DAY


def _yesterday(n: datetime) -> tuple[datetime, datetime]:
    return _sod(n) - DAY, _sod(n)


def _day_before(n: datetime) -> tuple[datetime, datetime]:
    return _sod(n) - 2 * DAY, _sod(n) - DAY


def _last_night(n: datetime) -> tuple[datetime, datetime]:
    return _sod(n) - timedelta(hours=6), _sod(n) + timedelta(hours=4)


def _this_morning(n: datetime) -> tuple[datetime, datetime]:
    return _sod(n), _sod(n) + timedelta(hours=12)


def _tonight(n: datetime) -> tuple[datetime, datetime]:
    return _sod(n) + timedelta(hours=17), _sod(n) + DAY


def _this_week(n: datetime) -> tuple[datetime, datetime]:
    return _sow(n), _sod(n) + DAY


def _last_week(n: datetime) -> tuple[datetime, datetime]:
    return _sow(n) - 7 * DAY, _sow(n)


def _past_week(n: datetime) -> tuple[datetime, datetime]:
    return n - 7 * DAY, n + timedelta(seconds=1)


def _this_month(n: datetime) -> tuple[datetime, datetime]:
    return _som(n), _sod(n) + DAY


def _last_month(n: datetime) -> tuple[datetime, datetime]:
    return _prev_month(n), _som(n)


def _this_year(n: datetime) -> tuple[datetime, datetime]:
    return _sod(n).replace(month=1, day=1), _sod(n) + DAY


def _last_year(n: datetime) -> tuple[datetime, datetime]:
    start = _sod(n).replace(year=n.year - 1, month=1, day=1)
    return start, start.replace(year=n.year)


# (phrase tokens, resolver, label). Longest phrases are tried first.
_PHRASES: list[tuple[tuple[str, ...], Resolver, str]] = []


def _add(label: str, fn: Resolver, *phrases: str) -> None:
    for p in phrases:
        _PHRASES.append((tuple(p.split()), fn, label))


_add("today", _today, "today", "aaj", "aj", "ivattu", "indu", "earlier today", "aaj hi")
_add("yesterday", _yesterday, "yesterday", "kal", "kall", "ninne", "yday", "yest")
_add(
    "day before yesterday",
    _day_before,
    "day before yesterday",
    "the day before yesterday",
    "parso",
    "parson",
    "monne",
)
_add(
    "last night",
    _last_night,
    "last night",
    "kal raat",
    "kal rat",
    "kal raat ko",
    "ninne raatri",
    "ninne rathri",
)
_add(
    "this morning",
    _this_morning,
    "this morning",
    "aaj subah",
    "aaj subha",
    "ivattu beligge",
)
_add("tonight", _tonight, "tonight", "this evening", "aaj shaam", "aaj sham")
_add(
    "this week",
    _this_week,
    "this week",
    "is hafte",
    "iss hafte",
    "is week",
    "iss week",
    "ee vaara",
)
_add(
    "last week",
    _last_week,
    "last week",
    "last wk",
    "previous week",
    "pichhle hafte",
    "pichle hafte",
    "pichhle week",
    "pichle week",
    "kaleda vaara",
    "hinde vaara",
)
_add("past week", _past_week, "past week", "past 7 days", "last 7 days")
_add("this month", _this_month, "this month", "is mahine", "iss mahine", "ee tingalu")
_add(
    "last month",
    _last_month,
    "last month",
    "previous month",
    "pichhle mahine",
    "pichle mahine",
    "kaleda tingalu",
)
_add("this year", _this_year, "this year", "is saal", "iss saal", "ee varsha")
_add("last year", _last_year, "last year", "pichhle saal", "pichle saal", "kaleda varsha")
_add(
    "recently",
    _past_week,
    "recently",
    "recent",
    "lately",
    "haal hi mein",
    "haal hi me",
    "abhi abhi",
    "ittichege",
)
_PHRASES.sort(key=lambda x: -len(x[0]))

WEEKDAYS: dict[str, int] = {}
for _i, _names in enumerate(
    [
        ("monday", "somvar", "somvaar", "somavara"),
        ("tuesday", "mangalvar", "mangalvaar", "mangalavara"),
        ("wednesday", "budhvar", "budhvaar", "budhavara"),
        ("thursday", "guruvar", "guruvaar", "brihaspativar", "guruvara"),
        ("friday", "shukravar", "shukravaar", "shukravara"),
        ("saturday", "shanivar", "shanivaar", "shanivara"),
        ("sunday", "ravivar", "ravivaar", "bhanuvara", "ravivara", "itvaar", "itwar"),
    ]
):
    for _n in _names:
        WEEKDAYS[_n] = _i
# Abbreviations only count after a prefix ("last tue", "on fri"): "sun"/"mon"/"sat" are words.
WEEKDAY_ABBR: dict[str, int] = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}  # fmt: skip
_WEEKDAY_PREFIX = frozenset({"last", "on", "this", "past", "pichhle", "pichle", "kaleda"})

NUMBERS: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "ondu": 1, "eradu": 2, "mooru": 3, "nalku": 4, "aidu": 5,
}  # fmt: skip
_UNITS: dict[str, str] = {
    "day": "d", "days": "d", "din": "d", "dino": "d", "dina": "d",
    "week": "w", "weeks": "w", "hafte": "w", "hafta": "w", "haftay": "w", "vaara": "w",
    "month": "m", "months": "m", "mahine": "m", "mahina": "m", "tingalu": "m",
}  # fmt: skip
_AGO = frozenset({"ago", "back", "pehle", "pahle", "pehele", "phele", "hinde", "munche"})
_PRE_FILLER = frozenset({"on", "in", "during", "from", "since", "of"})
_POST_FILLER = frozenset(
    {"ko", "ka", "ki", "ke", "wala", "wali", "waala", "waali", "wale", "mein", "me", "se"}
)

_FUZZY_VOCAB = (
    "yesterday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "morning", "evening", "tonight", "recently", "pichhle",
)  # fmt: skip
# Same Indic ranges as search.tokens: \w alone splits words at vowel signs.
_TOKEN = re.compile(r"[\w'\u0900-\u0963\u0966-\u0dff]+", re.UNICODE)


def _fix_typo(tok: str) -> str:
    """Correct misspelled long time words ("yesterdy", "tusday"). Short words never change."""
    if len(tok) < 6 or tok in _FUZZY_VOCAB or tok in WEEKDAYS:
        return tok
    hit = difflib.get_close_matches(tok, _FUZZY_VOCAB, n=1, cutoff=0.85)
    return hit[0] if hit else tok


def _weekday_range(n: datetime, wd: int) -> tuple[datetime, datetime]:
    """The most recent past ``wd`` (strictly before today)."""
    delta = (n.weekday() - wd) % 7 or 7
    start = _sod(n) - timedelta(days=delta)
    return start, start + DAY


def _ago_range(n: datetime, qty: int, unit: str) -> tuple[datetime, datetime]:
    if unit == "d":
        start = _sod(n) - timedelta(days=qty)
        return start, start + DAY
    if unit == "w":
        center = _sod(n) - timedelta(days=7 * qty)
        return center - 3 * DAY, center + 4 * DAY
    center = _sod(n) - timedelta(days=30 * qty)
    return center - 15 * DAY, center + 15 * DAY


def _number(tok: str) -> int | None:
    if tok.isdigit():
        v = int(tok)
        return v if 0 < v <= 3650 else None
    return NUMBERS.get(tok)


def _match_at(
    toks: list[str], i: int, now: datetime
) -> tuple[int, tuple[datetime, datetime], str] | None:
    """Try to match a time phrase starting at token ``i``: (length, range, label)."""
    for phrase, fn, label in _PHRASES:
        k = len(phrase)
        if tuple(toks[i : i + k]) == phrase:
            return k, fn(now), label
    t = toks[i]
    nxt = toks[i + 1] if i + 1 < len(toks) else ""
    # Weekdays: "tuesday", "last tuesday", "on tue", "mangalvar ko"
    if t in WEEKDAYS:
        return 1, _weekday_range(now, WEEKDAYS[t]), f"last {_label_day(WEEKDAYS[t])}"
    if t in _WEEKDAY_PREFIX:
        wd = WEEKDAYS.get(nxt, WEEKDAY_ABBR.get(nxt))
        if wd is not None:
            return 2, _weekday_range(now, wd), f"last {_label_day(wd)}"
    # "3 days ago", "do din pehle", "a week ago", "a couple of days ago"
    qty = _number(t)
    if qty is not None:
        j = i + 1
        if j < len(toks) and toks[j] == "couple":
            qty, j = 2, j + 1
        if j < len(toks) and toks[j] == "of":
            j += 1
        if j < len(toks) and toks[j] in _UNITS and j + 1 < len(toks) and toks[j + 1] in _AGO:
            unit = _UNITS[toks[j]]
            word = {"d": "day", "w": "week", "m": "month"}[unit]
            return (j + 2 - i), _ago_range(now, qty, unit), f"{qty} {word}{'s' * (qty > 1)} ago"
    # "past 5 days", "last 3 weeks"
    if t in ("past", "last", "pichhle", "pichle") and i + 2 < len(toks):
        q = _number(toks[i + 1])
        unit_tok = toks[i + 2]
        if q is not None and unit_tok in _UNITS:
            unit = _UNITS[unit_tok]
            days = {"d": 1, "w": 7, "m": 30}[unit] * q
            word = {"d": "day", "w": "week", "m": "month"}[unit]
            return (
                3,
                (now - timedelta(days=days), now + timedelta(seconds=1)),
                (f"past {q} {word}{'s' * (q > 1)}"),
            )
    return None


def _label_day(wd: int) -> str:
    return ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[wd]


def parse_time(text: str, now: datetime | None = None) -> TimeParse:
    """Find the first time phrase in ``text``. ``rest`` is the text without it."""
    now = now or datetime.now().astimezone()
    raw = [m.group(0).lower() for m in _TOKEN.finditer(text)]
    toks = [_fix_typo(t) for t in raw]
    for i in range(len(toks)):
        m = _match_at(toks, i, now)
        if m is None:
            continue
        k, (start, end), label = m
        lo, hi = i, i + k
        if lo > 0 and toks[lo - 1] in _PRE_FILLER:
            lo -= 1
        if hi < len(toks) and toks[hi] in _POST_FILLER:
            hi += 1
        rest = " ".join(raw[:lo] + raw[hi:])
        return TimeParse(
            TimeRange(start.timestamp(), end.timestamp(), label), rest, " ".join(raw[i : i + k])
        )
    return TimeParse(None, " ".join(raw))
