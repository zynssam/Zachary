"""Turns a typed/spoken request into a reply: what to show, what to say, and
(only on submit) which safe action to run. Qt-free so it is unit-testable; the UI
calls it from a worker thread.

Confidence rule (CLAUDE.md): "open" acts directly only when one result clearly
wins; otherwise the top results are shown and the user picks ("the second one").
``preview`` (as-you-type) never acts.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from zachary.brain import sessions as sess
from zachary.brain.context import Context, Item
from zachary.brain.intents import Intent, parse
from zachary.brain.timeparse import parse_time
from zachary.core.config import Config, PathRules
from zachary.indexer import clustering
from zachary.indexer.projects import list_projects
from zachary.indexer.relations import related as related_files
from zachary.search import feedback
from zachary.search.hybrid import SearchResult, recent, search, search_terms
from zachary.search.tokens import words
from zachary.search.vectors import SemanticSearch

RESULT_LIMIT = 8  # shown in the list
ASK_LIMIT = 5  # "which one?" shows the top 3-5
CLEAR_GAP = 0.12  # top score must beat the runner-up by 12% to act without asking


class Actions(Protocol):
    def open(self, item: Item) -> bool: ...

    def reveal(self, item: Item) -> bool: ...


@dataclass(slots=True)
class Reply:
    say: str  # one short line (spoken in Phase 4)
    view: str  # results | projects | recent | none
    items: list[Item]
    intent: Intent
    action: str | None = None  # open | reveal: performed (submit) or proposed (preview)
    target: Item | None = None
    selected: int | None = None  # index to highlight
    ms: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)


def clear_winner(results: list[SearchResult] | list[Item]) -> bool:
    """Act without asking only on a strong top match that clearly beats the rest."""
    if results and isinstance(results[0], SearchResult) and not results[0].strong:
        return False
    if len(results) == 1:
        return True
    if len(results) < 2:
        return False
    s1, s2 = results[0].score, results[1].score
    return s1 > 0 and (s1 - s2) / s1 >= CLEAR_GAP


def _ago(ts: float, now: float) -> str:
    d = max(0.0, now - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    days = int(d // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def _span(s: sess.Session) -> str:
    """ "Tue 9:10 pm" / "yesterday 9:10 pm" / "today 9:10 pm"."""
    t = time.localtime(s.start)
    d = date.fromtimestamp(s.start)
    today = date.today()
    day = (
        "today" if d == today
        else "yesterday" if d == today - timedelta(days=1)
        else time.strftime("%a %d %b", t)
    )  # fmt: skip
    return f"{day} {time.strftime('%I:%M %p', t).lstrip('0').lower()}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _minutes(m: int) -> str:
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def _item(r: SearchResult, conn: sqlite3.Connection | None = None) -> Item:
    it = Item(r.file_id, r.path, r.name, r.kind, r.is_dir, r.last_seen, list(r.why), r.score)
    if r.versions and conn is not None:
        marks = ",".join("?" * len(r.versions))
        rows = {
            row[0]: row
            for row in conn.execute(
                f"SELECT id, path, name, kind, mtime FROM files WHERE id IN ({marks})", r.versions
            )
        }
        it.versions = [
            Item(v, rows[v][1], rows[v][2], rows[v][3], False, float(rows[v][4]), ["older version"])
            for v in r.versions
            if v in rows
        ]
    return it


REASONS = {
    "content": "similar content",
    "name": "similar name",
    "location": "same folder",
    "coaccess": "opened together",
    "feedback": "you chose it before",
}
SEMANTIC_RETRY_S = 60.0


class Handler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        actions: Actions,
        config: Config,
        semantic: Callable[[], SemanticSearch | None] | None = None,
    ) -> None:
        self.conn = conn
        self.actions = actions
        self.config = config
        self.context = Context()
        self._rules: PathRules | None = None
        self._rules_rev = -1
        self._semantic_provider = semantic
        self._semantic: SemanticSearch | None = None
        self._semantic_tried = 0.0
        self._sessions: dict[int, sess.Session] = {}  # timeline rows -> sessions
        # A proposal waiting for "yes": ("reopen", files, what). Only from submit().
        self._pending: tuple[str, list[Item], str] | None = None

    @property
    def semantic(self) -> SemanticSearch | None:
        """Meaning search, once the model is on disk (retried every minute until then)."""
        if self._semantic is None and self._semantic_provider is not None:
            now = time.monotonic()
            if now - self._semantic_tried >= SEMANTIC_RETRY_S or not self._semantic_tried:
                self._semantic_tried = now
                self._semantic = self._semantic_provider()
        return self._semantic

    # -- helpers -----------------------------------------------------------------------
    def rules(self) -> PathRules:
        rev = self.config.config_rev()
        if self._rules is None or rev != self._rules_rev:
            self._rules = self.config.path_rules()
            self._rules_rev = rev
        return self._rules

    def _search(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        resp = search(self.conn, query, limit=limit, rules=self.rules(), semantic=self.semantic)
        note = ""
        if resp.time_range is not None and not resp.range_matched and resp.results:
            note = f"Nothing from {resp.time_range.label}; best matches from any time."
        elif resp.corrected:
            note = "Did you mean " + ", ".join(resp.corrected.values()) + "?"
        return resp.results, note

    def _projects(self, query: str = "") -> list[Item]:
        items = [
            Item(
                p.folder_id,
                p.path,
                p.name,
                "project",
                True,
                p.last_activity,
                [f"active {_ago(p.last_activity, time.time())}"] if p.last_activity else [],
                0.0,
                ", ".join(p.markers),
            )
            for p in list_projects(self.conn)
        ]
        want = [w for w in words(query) if w not in {"project", "projects", "repo", "repos"}]
        if want:
            items = [
                it
                for it in items
                if all(any(n.startswith(w) for n in words(it.name)) for w in want)
            ]
        return items

    def _recent(self, query: str) -> list[Item]:
        tr = parse_time(query).range
        now = time.time()
        return [
            Item(r.file_id, r.path, r.name, r.kind, False, r.ts, [f"{r.how} {_ago(r.ts, now)}"])
            for r in recent(self.conn, limit=RESULT_LIMIT * 2, time_range=tr)
            if os.path.basename(r.path) and not self.rules().is_excluded(r.path, False)
        ][:RESULT_LIMIT]

    # -- entry points ------------------------------------------------------------------
    def preview(self, text: str) -> Reply:
        """As-you-type: show what would happen. Never opens anything."""
        return self._handle(text, execute=False)

    def submit(self, text: str) -> Reply:
        """Enter / voice: may run a safe action per the confidence rule."""
        return self._handle(text, execute=True)

    def act(self, index: int, action: str) -> Reply:
        """A click / keyboard choice on a shown item."""
        intent = Intent("select", ordinal=index + 1, action=action)
        return self._followup(intent, execute=True)

    def _handle(self, text: str, *, execute: bool) -> Reply:
        t0 = time.perf_counter()
        intent = parse(text)
        if intent.kind == "related":  # before follow-ups: "related to the first one"
            reply = self._related(intent)
        elif intent.is_followup:
            reply = self._followup(intent, execute=execute)
        elif intent.kind == "none":
            items = self._recent("")
            self.context.set_results(items, "", "recent")
            reply = Reply(
                "Recent files." if items else "Type to search your files.", "recent", items, intent
            )
        elif intent.kind == "show_projects":
            items = self._projects()
            self.context.set_results(items, text, "projects")
            say = (
                f"{len(items)} projects. Most recent: {items[0].name}."
                if items
                else "No projects found yet."
            )
            reply = Reply(say, "projects", items, intent)
        elif intent.kind == "topic":
            reply = self._topics(intent, execute=execute)
        elif intent.kind == "resume":
            reply = self._resume(intent, execute=execute)
        elif intent.kind == "timeline":
            reply = self._timeline(intent)
        elif intent.kind in {"confirm", "cancel"}:
            reply = self._answer(intent, execute=execute)
        elif intent.kind == "recent":
            items = self._recent(intent.query)
            self.context.set_results(items, text, "recent")
            tr = parse_time(intent.query).range
            label = tr.label if tr else "recently"
            say = f"Files from {label}." if items else f"Nothing opened or edited {label}."
            reply = Reply(say, "recent", items, intent)
        else:
            reply = self._find_or_open(intent, execute=execute)
        reply.ms = (time.perf_counter() - t0) * 1e3
        return reply

    def _find_or_open(self, intent: Intent, *, execute: bool) -> Reply:
        items: list[Item]
        note = ""
        if intent.project_hint:
            items = self._projects(intent.query)
            if not items:
                results, note = self._search(intent.query, RESULT_LIMIT)
                items = [_item(r, self.conn) for r in results]
            view = "projects" if items and items[0].kind == "project" else "results"
            winner = len(items) == 1
        else:
            results, note = self._search(intent.query, RESULT_LIMIT)
            items = [_item(r, self.conn) for r in results]
            view = "results"
            # A note (typo corrected, nothing in the time range) means we guessed: ask.
            winner = clear_winner(results) and not note
        self.context.set_results(items, intent.query, view)
        q = intent.query.strip()
        if not items:
            return Reply(f"Nothing found for “{q}”." if q else "Nothing found.", view, [], intent)
        if intent.kind == "open":
            if winner:
                target = items[0]
                if execute:
                    ok = self.actions.open(target)
                    if ok and view == "results":
                        feedback.record(self.conn, search_terms(intent.query), target.file_id, 0)
                    say = f"Opening {target.name}." if ok else f"Couldn't open {target.name}."
                    return Reply(say, view, items, intent, "open" if ok else None, target, 0)
                return Reply(
                    f"Enter to open {target.name}.", view, items, intent, "open", target, 0
                )
            shown = items[:ASK_LIMIT]
            self.context.set_results(shown, intent.query, view)
            return Reply(note or f"Which one? I found {len(items)} matches.", view, shown, intent)
        say = note or (f"Found {len(items)}." if len(items) < RESULT_LIMIT else "Top matches.")
        return Reply(say, view, items, intent, selected=0)

    # -- Phase 5: topics, resume, timeline ---------------------------------------------
    def _file_items(self, file_ids: list[int], why: str) -> list[Item]:
        if not file_ids:
            return []
        marks = ",".join("?" * len(file_ids))
        rows = {
            r[0]: r
            for r in self.conn.execute(
                f"SELECT id, path, name, kind, is_dir, mtime FROM files WHERE id IN ({marks})",
                file_ids,
            )
        }
        rules = self.rules()
        return [
            Item(f, rows[f][1], rows[f][2], rows[f][3], bool(rows[f][4]), float(rows[f][5]), [why])
            for f in file_ids
            if f in rows and not rules.is_excluded(rows[f][1], bool(rows[f][4]))
        ]

    def _topics(self, intent: Intent, *, execute: bool) -> Reply:
        topics = clustering.list_topics(self.conn)
        if not topics:
            return Reply("No topics yet. They're built overnight once your files are indexed.",
                         "none", [], intent)  # fmt: skip
        if intent.query:
            want = set(words(intent.query))
            scored = sorted(
                ((len(want & set(words(t.label))) / max(1, len(want)), t) for t in topics),
                key=lambda x: -x[0],
            )
            if scored and scored[0][0] >= 0.5:
                return self._drill(self._topic_item(scored[0][1]), intent, execute=execute)
            # No topic by that name: an ordinary search for it.
            return self._find_or_open(Intent("find", query=intent.query), execute=execute)
        items = [self._topic_item(t) for t in topics]
        self.context.set_results(items, "", "topics")
        top = ", ".join(t.label for t in topics[:3])
        return Reply(f"{len(items)} topics. Most recent: {top}.", "topics", items, intent)

    @staticmethod
    def _topic_item(t: clustering.Topic) -> Item:
        return Item(t.id, "", t.label, "topic", False, t.last_ts, [f"{t.size} files"], 0.0,
                    "topic")  # fmt: skip

    def _drill(self, target: Item, intent: Intent, *, execute: bool) -> Reply:
        """Open a topic or a session: show what's in it."""
        if target.kind == "topic":
            items = self._file_items(clustering.topic_files(self.conn, target.file_id, 100),
                                     f"topic: {target.name}")  # fmt: skip
            self.context.set_results(items, target.name, "results")
            return Reply(f"{target.name}: {len(items)} files.", "results", items, intent,
                         selected=0)  # fmt: skip
        s = self._sessions.get(target.file_id)
        if s is None:
            return Reply("That session is gone; ask for the timeline again.", "none", [], intent)
        return self._session_reply(s, intent, execute=execute, lead=s.label)

    def _session_reply(self, s: sess.Session, intent: Intent, *, execute: bool, lead: str) -> Reply:
        files = sess.resume_files(s)
        items = self._file_items([f.file_id for f in files], f"{_span(s)}")
        items = [it for it in items if os.path.lexists(it.path)]
        self.context.set_results(items, "", "session")
        if not items:
            return Reply(f"{lead}: nothing left to reopen.", "session", [], intent)
        what = _count(len(items), "file")
        if execute:
            self._pending = ("reopen", items, what)
        return Reply(f"{lead} ({_span(s)}, {s.minutes} min): {what}. Reopen them?", "session",
                     items, intent, selected=0)  # fmt: skip

    def _resume(self, intent: Intent, *, execute: bool) -> Reply:
        s = sess.last_session(self.conn)
        if s is None:
            return Reply("I don't have a recent work session to resume yet.", "none", [], intent)
        return self._session_reply(s, intent, execute=execute,
                                   lead=f"Your last session, {s.label}")  # fmt: skip

    def _timeline(self, intent: Intent) -> Reply:
        now = time.time()
        tr = parse_time(intent.query).range
        start, end, label = (tr.start, tr.end, tr.label) if tr else (now - 86400, now, "today")
        found = sess.sessions(self.conn, start, end)
        self._sessions = dict(enumerate(found))
        items = [
            Item(i, "", s.label or "Session", "session", False, s.end,
                 [_span(s), f"{s.minutes} min", _count(len(s.files), "file")], 0.0, "session")
            for i, s in reversed(list(enumerate(found)))
        ]  # fmt: skip
        self.context.set_results(items, intent.query, "timeline")
        if not items:
            return Reply(f"Nothing worked on {label}.", "timeline", [], intent)
        longest = sorted(found, key=lambda s: -(s.end - s.start))[:3]
        parts = ", ".join(f"{s.label} {_minutes(s.minutes)}" for s in longest)
        return Reply(f"{label.capitalize()}: {_count(len(found), 'session')}. {parts}.",
                     "timeline", items, intent, selected=0)  # fmt: skip

    def _answer(self, intent: Intent, *, execute: bool) -> Reply:
        pending, self._pending = self._pending, None
        if intent.kind == "cancel" or pending is None:
            say = (
                "Okay." if pending is not None or intent.kind == "cancel" else "Nothing to confirm."
            )
            return Reply(say, self.context.view, self.context.items, intent)
        if not execute:
            self._pending = pending  # preview: don't consume it
            return Reply(f"Enter to reopen {pending[2]}.", "session", pending[1], intent)
        _kind, items, what = pending
        opened = sum(1 for it in items if self.actions.open(it))
        say = f"Reopened {opened} of {len(items)} files." if opened < len(items) else (
            f"Reopened {what}.")  # fmt: skip
        return Reply(say, "session", items, intent, "open" if opened else None)

    def _related(self, intent: Intent) -> Reply:
        """Precomputed related files of a shown item or of the best match for a query."""
        if intent.query:
            results, _ = self._search(intent.query, 1)
            if not results:
                return Reply(f"Nothing found for “{intent.query}”.", "none", [], intent)
            anchor = _item(results[0])
        else:
            ctx = self.context
            if not ctx.active():
                return Reply(
                    "Search for something first, then ask for related files.", "none", [], intent
                )
            found = ctx.by_ordinal(intent.ordinal) if intent.ordinal else ctx.focused_item()
            if found is None:
                return Reply(
                    "Which one? Say “related to the first one”.", ctx.view, ctx.items, intent
                )
            anchor = found
        rels = related_files(self.conn, anchor.file_id)
        rules = self.rules()
        items = [
            Item(
                r.file_id,
                r.path,
                r.name,
                r.kind,
                False,
                r.mtime,
                [REASONS.get(r.reason, "")],
                r.score,
            )
            for r in rels
            if not rules.is_excluded(r.path, False)
        ]
        self.context.set_results(items, f"related:{anchor.file_id}", "related")
        if not items:
            return Reply(f"Nothing related to {anchor.name} yet.", "related", [], intent)
        return Reply(
            f"{len(items)} files related to {anchor.name}.", "related", items, intent, selected=0
        )

    def _followup(self, intent: Intent, *, execute: bool) -> Reply:
        ctx = self.context
        if not ctx.active():
            return Reply("Search for something first, then say which one.", "none", [], intent)
        if intent.kind == "select":
            target = (
                ctx.by_ordinal(intent.ordinal)
                if intent.ordinal is not None
                else (ctx.focused_item())
            )
            if target is None:
                n = len(ctx.items)
                return Reply(
                    f"There are only {n} results.",
                    ctx.view,
                    ctx.items,
                    intent,
                    selected=ctx.focused,
                )
        elif intent.kind in {"older", "newer"}:
            target = ctx.older() if intent.kind == "older" else ctx.newer()
            if target is None:
                word = "older" if intent.kind == "older" else "newer"
                return Reply(
                    f"There's no {word} one.", ctx.view, ctx.items, intent, selected=ctx.focused
                )
        else:  # reveal
            target = ctx.focused_item()
            if target is None:
                return Reply("Nothing selected.", ctx.view, ctx.items, intent)
        if target.kind in {"topic", "session"}:
            return self._drill(target, intent, execute=execute)
        action = "reveal" if intent.kind == "reveal" or intent.action == "reveal" else "open"
        idx = ctx.focused
        if not execute:
            verb = "show in folder" if action == "reveal" else "open"
            return Reply(
                f"Enter to {verb}: {target.name}.", ctx.view, ctx.items, intent, action, target, idx
            )
        ok = self.actions.reveal(target) if action == "reveal" else self.actions.open(target)
        if ok and ctx.view == "results" and idx is not None:
            feedback.record(self.conn, search_terms(ctx.query), target.file_id, idx)
        if action == "reveal":
            say = f"Showing {target.name} in its folder." if ok else f"Couldn't show {target.name}."
        else:
            say = f"Opening {target.name}." if ok else f"Couldn't open {target.name}."
        return Reply(say, ctx.view, ctx.items, intent, action if ok else None, target, idx)
