"""Debug / power-user CLI: ``python -m zachary.cli <command>``.

Queries only read the database. ``scan`` runs a catch-up in this process
(useful when the daemon isn't running). Config commands (roots, exclude,
forget, pause) only change settings; the running indexer applies them within
a few seconds, and search hides newly excluded paths immediately.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from zachary.brain.timeparse import parse_time
from zachary.core.config import Config, ExcludeKind, default_db_path
from zachary.core.db import get_state, open_db
from zachary.indexer.projects import list_projects
from zachary.search.hybrid import recent, search


def _ago(ts: float) -> str:
    d = max(0.0, time.time() - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 86400 * 30:
        return f"{int(d // 86400)}d ago"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _short(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _open(args: argparse.Namespace) -> tuple[sqlite3.Connection, Config]:
    conn = open_db(args.db or default_db_path())
    return conn, Config(conn)


def cmd_search(args: argparse.Namespace) -> int:
    from zachary.search.semantic import local_semantic

    conn, cfg = _open(args)
    q = " ".join(args.query)
    sem = None if args.keywords_only else local_semantic(conn, cfg, in_memory=False)
    resp = search(conn, q, limit=args.limit, rules=cfg.path_rules(), semantic=sem)
    if args.json:
        print(
            json.dumps(
                {
                    "query": q,
                    "terms": resp.terms,
                    "time_range": resp.time_range.label if resp.time_range else None,
                    "range_matched": resp.range_matched,
                    "took_ms": round(resp.took_ms, 2),
                    "results": [
                        {"path": r.path, "kind": r.kind, "score": round(r.score, 5), "why": r.why}
                        for r in resp.results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    head = f"{len(resp.results)} result(s) in {resp.took_ms:.1f} ms"
    if resp.time_range:
        head += f"  [time: {resp.time_range.label}]"
    if resp.corrected:
        head += "  [did you mean: " + ", ".join(resp.corrected.values()) + "]"
    print(head)
    if resp.time_range and not resp.range_matched:
        print(f"  (nothing matched {resp.time_range.label}; showing best matches from any time)")
    for i, r in enumerate(resp.results, 1):
        icon = "/" if r.is_dir else ""
        print(f"{i:>2}. {r.name}{icon}")
        print(f"    {_short(os.path.dirname(r.path))}")
        print(f"    {'; '.join(r.why)}")
    return 0 if resp.results else 1


def cmd_projects(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    projects = list_projects(conn, limit=args.limit)
    if not projects:
        print("no projects found (has the indexer run? try: python -m zachary.cli scan)")
        return 1
    width = max(len(p.name) for p in projects)
    for i, p in enumerate(projects, 1):
        when = _ago(p.last_activity) if p.last_activity else "-"
        markers = ",".join(p.markers)
        print(f"{i:>3}. {p.name:<{width}}  {when:>10}  {_short(p.path)}  [{markers}]")
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    tr = parse_time(" ".join(args.when)).range if args.when else None
    items = recent(conn, limit=args.limit, time_range=tr)
    if tr:
        print(f"[time: {tr.label}]")
    for i, it in enumerate(items, 1):
        print(f"{i:>3}. {it.how:<6} {_ago(it.ts):>9}  {_short(it.path)}")
    return 0 if items else 1


def cmd_scan(args: argparse.Namespace) -> int:
    from zachary.indexer.catchup import catch_up
    from zachary.indexer.recent import ingest_recent
    from zachary.indexer.store import FileStore

    conn, cfg = _open(args)
    cfg.seed_defaults()
    store = FileStore(conn, cfg)
    store.ensure_fts()
    purged = store.purge_excluded()
    st = catch_up(store, cfg.platform, force_scan=args.full)
    n_recent = ingest_recent(store, cfg.platform)
    r = st.result
    total = conn.execute("SELECT count(*), sum(is_dir) FROM files").fetchone()
    print(
        f"scanned {', '.join(f'{_short(k)} ({v})' for k, v in st.method.items())} "
        f"in {st.seconds:.1f}s\n"
        f"  +{r.inserted} new, {r.updated} updated, {r.moved} moved, {r.deleted} deleted, "
        f"{purged} purged by excludes, {n_recent} recent-item accesses\n"
        f"  index: {total[0]} entries ({total[1] or 0} folders)"
    )
    n_dirs = int(total[1] or 0)
    limit = cfg.platform.check_watch_limit(n_dirs)
    if not limit.ok:
        print("\nwarning: " + limit.message)
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    """Index file contents now (in this process): extract, embed, vector index, relations."""
    from zachary.indexer import relations, versions
    from zachary.indexer.semantic import SemanticIndexer
    from zachary.indexer.semantic_worker import default_embedder
    from zachary.search import vectors

    conn, cfg = _open(args)
    db_path = Path(args.db or default_db_path())
    sem = SemanticIndexer(conn, cfg, default_embedder(cfg, db_path))
    total = sem.pending_count()
    if args.limit:
        total = min(total, args.limit)
    print(f"{total} files to embed (model: {cfg.get_setting('embed_model')})")
    done = 0
    t0 = time.perf_counter()
    agg = {"extracted": 0, "reused": 0, "name_only": 0, "chunks": 0, "bytes": 0}
    while done < total:
        st = sem.run(min(64, total - done))
        if not st.files:
            break
        done += st.files
        for k in agg:
            agg[k] += getattr(st, k)
        rate = done / max(time.perf_counter() - t0, 1e-9)
        print(f"\r  {done}/{total} files  ({rate:.0f} files/s)", end="", flush=True)
    secs = time.perf_counter() - t0
    print(
        f"\nembedded {done} files in {secs:.1f}s: {agg['extracted']} with text "
        f"({agg['bytes'] / 1e6:.1f} MB, {agg['chunks']} chunks), {agg['reused']} reused, "
        f"{agg['name_only']} name-only"
    )
    b = vectors.build(conn)
    print(f"vector index: {b.vectors} vectors in {b.clusters} clusters ({b.seconds:.1f}s)")
    r = relations.RelationBuilder(conn).compute(None)
    print(f"related files: {r.links} links for {r.files} files ({r.seconds:.1f}s)")
    print(f"version groups: {versions.rebuild(conn)}")
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    from zachary.brain.handler import REASONS
    from zachary.indexer.relations import related

    conn, cfg = _open(args)
    path = cfg.canonical(args.path)
    row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
    if row is None:
        print(f"not indexed: {path}")
        return 1
    t0 = time.perf_counter()
    rels = related(conn, int(row[0]))
    ms = (time.perf_counter() - t0) * 1e3
    print(f"{len(rels)} related ({ms:.2f} ms)")
    for i, r in enumerate(rels, 1):
        print(f"{i:>3}. {r.score:.2f}  {REASONS.get(r.reason, r.reason):<16} {_short(r.path)}")
    return 0 if rels else 1


def cmd_voice(args: argparse.Namespace) -> int:
    """Voice tools. Calls that use the paid Gnani API say so and are logged in the ledger."""
    from zachary.voice.audio import resample, seconds, wav_to_pcm
    from zachary.voice.factory import build_voice
    from zachary.voice.types import VoiceError

    conn, cfg = _open(args)
    data_dir = Path(args.db or default_db_path()).parent
    kit = build_voice(cfg, data_dir)
    if args.action == "usage":
        summary = kit.ledger.summary()
        if not summary:
            print(f"no Gnani calls recorded ({kit.ledger.path})")
            return 0
        print(f"Gnani calls ({kit.ledger.path}):")
        for ep, d in sorted(summary.items()):
            extra = f"{d['audio_s']:.1f}s audio" if d["audio_s"] else f"{int(d['chars'])} chars"
            print(f"  {ep:<11} {int(d['calls']):>4} calls  {extra:>14}  {int(d['errors'])} failed")
        return 0
    if args.action == "setup":
        print(f"Gnani key: {'found' if kit.has_key else 'missing (set GNANI_API_KEY or .env)'}")
        t = time.perf_counter()
        kit.offline_stt.transcribe(b"\0\0" * 16000)  # downloads the model once
        print(f"local speech model ready ({time.perf_counter() - t:.1f}s)")
        try:
            import sounddevice as sd

            print(f"microphone: {sd.query_devices(kind='input')['name']}")
        except Exception as e:
            print(f"microphone: unavailable ({e})")
        return 0
    if args.action == "transcribe":
        pcm, sr = wav_to_pcm(Path(args.text).read_bytes())
        pcm = resample(pcm, sr)
        stt = kit.offline_stt if args.offline or kit.rest_stt is None else kit.rest_stt
        if stt is not kit.offline_stt:
            print(f"(uses 1 Gnani STT call, {seconds(pcm):.1f}s of audio)")
        try:
            tr = stt.transcribe(pcm)
        except VoiceError as e:
            print(f"error ({e.kind}): {e}")
            return 1
        print(f"[{tr.provider}, {tr.latency_ms:.0f} ms] {tr.text}")
        return 0
    if args.action == "say":
        if kit.tts is None:
            print("no Gnani key: nothing to speak with")
            return 1
        from zachary.voice.audio import Player

        print("(uses 1 Gnani TTS call)")
        player = Player()
        t0 = time.perf_counter()
        first = None
        try:
            for chunk in kit.tts.stream(args.text):
                first = first or time.perf_counter()
                player.play(chunk.pcm, chunk.sample_rate)
        except VoiceError as e:
            print(f"error ({e.kind}): {e}")
            return 1
        player.end()
        player.wait()
        if first:
            print(f"first audio after {(first - t0) * 1e3:.0f} ms")
        return 0
    if args.action == "listen":
        return _voice_listen(args, kit, conn, cfg)
    if args.action == "mics":
        return _voice_mics(args, cfg)
    if args.action == "latency":
        return _voice_latency(data_dir / "latency.jsonl", int(args.text or 10))
    return 1


def _voice_latency(path: Path, n: int) -> int:
    """The last N voice commands, one row each, stage times in ms."""
    if not path.exists():
        print(f"no voice commands logged yet ({path})")
        return 0
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        with contextlib.suppress(ValueError):
            rows.append(json.loads(line))
    cols = ["mic_open", "stream_open", "speech_start", "speech_end", "stt", "intent_search",
            "tts_first_audio", "tts_done"]  # fmt: skip
    print("when      " + " ".join(f"{c[:11]:>11}" for c in cols) + "  heard / error")
    for r in rows:
        st = r.get("stages_ms", {})
        when = time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0)))
        cells = " ".join(f"{st[c]:>11.0f}" if c in st else f"{'-':>11}" for c in cols)
        note = r.get("error") or r.get("intent") or ""
        if r.get("text_after_speech_ms") is not None:
            note += f"  (text {r['text_after_speech_ms']} ms after you stopped)"
        print(f"{when}  {cells}  {note}")
    print("(each stage: ms since the previous one; speech_end -> tts_first_audio is the wait)")
    return 0


def _voice_mics(args: argparse.Namespace, cfg: Config) -> int:
    """List microphones with a live level, or pick one: ``voice mics NAME``."""
    import numpy as np

    from zachary.voice.audio import AudioUnavailable, MicStream, list_inputs, pcm_to_float

    if args.text:
        choice = "" if args.text.strip().strip("'\"").lower() in ("", "default") else args.text
        cfg.set_setting("mic_device", choice)
        shown = repr(choice) if choice else "the system default input"
        print(f"mic_device = {shown} (restart the assistant to use it)")
    current = str(cfg.get_setting("mic_device"))
    try:
        devices = list_inputs()
    except AudioUnavailable as e:
        print(e)
        return 1
    print("Speak while this runs (1.5 s per microphone):")
    for d in devices:
        m = MicStream(d.id)
        try:
            m.start()
            time.sleep(1.5)
            m.stop()
            fr = []
            while not m.frames.empty():
                fr.append(m.frames.get_nowait())
            x = pcm_to_float(b"".join(fr))
            level = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            meter = "silent (no signal)" if level == 0 else "#" * max(1, min(30, int(level * 300)))
        except Exception as e:
            meter = f"can't open ({type(e).__name__})"
        tags = (
            (" [system default]" if d.is_default else "")
            + (" [virtual]" if d.virtual else "")
            + (" [selected]" if current and current == d.id else "")
        )
        print(f"  {d.label}{tags}\n    id: {d.id}\n    level: {meter}")
    print("\nPick one:  python -m zachary.cli voice mics <id>   (default = the system's input)")
    return 0


def _voice_listen(args: argparse.Namespace, kit: Any, conn: Any, cfg: Config) -> int:
    """One spoken command: press Enter, speak, pause. Then search/open like the bar."""
    import threading

    from zachary.actions.safe_actions import SafeActions
    from zachary.brain.handler import Handler
    from zachary.search.semantic import local_semantic
    from zachary.voice.pipeline import Heard, MicSource, VoiceListener

    done = threading.Event()
    result: list[Heard] = []

    def got(h: Heard) -> None:
        result.append(h)
        done.set()

    listener = VoiceListener(
        MicSource(device=kit.mic_device),
        on_heard=got,
        on_state=lambda st: print(f"  [{st}]") if st != "idle" else None,
        rest_stt=None if args.offline else kit.rest_stt,
        stream_stt=None if args.offline else kit.stream_stt,
        offline_stt=kit.offline_stt,
        spotter=kit.spotter,
        wake_word=False,
    )
    listener.start()
    input("Press Enter, then speak your command (it stops when you pause)… ")
    listener.push_to_talk()
    done.wait(30)
    listener.stop()
    if not result or not result[0].text:
        print(f"nothing heard ({result[0].error if result and result[0].error else 'timeout'})")
        return 1
    h = result[0]
    print(f"heard ({h.transcript.provider if h.transcript else '?'}): {h.text!r}")
    from zachary.brain import llm

    provider = llm.Provider(cfg, _data_dir(args))
    handler = Handler(
        conn, SafeActions(conn, cfg.platform), cfg, lambda: local_semantic(conn, cfg), provider
    )
    reply = handler.submit(h.text)
    h.timeline.mark("intent_search")
    print(f"zach: {reply.say}")
    for i, it in enumerate(reply.items[:5], 1):
        print(f"  {i}. {it.name}  ({_short(os.path.dirname(it.path))})")
    if kit.tts is not None and not args.no_speak and reply.say:
        from zachary.voice.audio import Player

        player = Player()
        first = True
        for chunk in kit.tts.stream(reply.say):
            if first:
                h.timeline.mark("tts_first_audio")
                first = False
            player.play(chunk.pcm, chunk.sample_rate)
        player.end()
        player.wait()
        h.timeline.mark("tts_done")
    rec = h.timeline.finish(str(Path(args.db or default_db_path()).parent / "latency.jsonl"))
    stages = h.timeline.durations()
    print("latency: " + "  ".join(f"{k} {v:.0f} ms" for k, v in stages.items()))
    del rec
    return 0


def _data_dir(args: argparse.Namespace) -> Path:
    return Path(args.db or default_db_path()).parent


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer a question from your files (LLM if enabled, else the best passage)."""
    from zachary.actions.safe_actions import SafeActions
    from zachary.brain import llm
    from zachary.brain.handler import Handler
    from zachary.search.semantic import local_semantic

    conn, cfg = _open(args)
    provider = (lambda: None) if args.local else llm.Provider(cfg, _data_dir(args))
    handler = Handler(conn, SafeActions(conn, cfg.platform), cfg,
                      lambda: local_semantic(conn, cfg, in_memory=False), provider)  # fmt: skip
    question = " ".join(args.question)
    t0 = time.perf_counter()
    from zachary.brain.intents import parse

    explain = parse(question).kind == "explain"  # "explain my last project" stays itself
    reply = handler.submit(
        question if explain or question.startswith("ask ") else f"ask {question}"
    )
    ms = (time.perf_counter() - t0) * 1e3
    print(f"zach: {reply.say}")
    if reply.detail:
        print("\n" + "\n".join("  " + line for line in reply.detail.splitlines()))
    if reply.items:
        print("\nsources:")
        for i, it in enumerate(reply.items[:4], 1):
            print(f"  {i}. {it.name}  ({_short(os.path.dirname(it.path))})")
    print(f"\n{ms:.0f} ms" + ("" if provider() else "  (LLM off: passage only)"))
    return 0


def cmd_llm(args: argparse.Namespace) -> int:
    """LLM status and usage (no API call)."""
    from zachary.brain import llm
    from zachary.core.env import get_secret

    _, cfg = _open(args)
    s = cfg.load()
    if args.action == "status":
        name = llm.provider_name(cfg)
        info = llm.PROVIDERS[name]
        model = s.llm_model or info.model
        print(f"enabled: {s.llm_enabled}   provider: {name}   model: {model}   "
              f"effort: {s.llm_effort}")  # fmt: skip
        found = bool(get_secret(info.key))
        print(f"{info.key}: {'found' if found else f'missing (free key: {info.signup})'}")
        if not s.llm_enabled:
            print("turn on: python -m zachary.cli set llm_enabled true")
        print("providers: " + ", ".join(llm.PROVIDERS) + "  (cli set llm_provider gemini)")
        return 0
    entries = llm.usage_ledger(_data_dir(args)).entries()
    if not entries:
        print("no LLM calls yet")
        return 0
    by: dict[str, dict[str, float]] = {}
    for e in entries:
        d = by.setdefault(e["endpoint"], {"calls": 0, "in": 0, "out": 0, "errors": 0, "ms": 0})
        d["calls"] += 1
        d["in"] += int(e.get("input_tokens", 0))
        d["out"] += int(e.get("output_tokens", 0))
        d["errors"] += int(e.get("status") != "ok")
        d["ms"] += float(e.get("ms", 0))
    for k, d in by.items():
        print(f"  {k:14} {int(d['calls']):4} calls  {int(d['in']):7} in / {int(d['out']):6} out "
              f"tokens  {int(d['errors'])} errors  avg {d['ms'] / d['calls']:.0f} ms")  # fmt: skip
    return 0


def _print_plan(plan: Any) -> None:
    from zachary.actions import planner

    print(f"plan {plan.id} [{plan.status}]: {plan.title}")
    print(f"  {plan.note}")
    cat = ""
    for it in plan.items:
        if it.category != cat:
            cat = it.category
            print(f"\n  {planner.LABELS[cat]}")
        mark = "x" if it.selected else " "
        size = f"{planner.human(it.size):>9}" if it.size else " " * 9
        dst = f"  ->  {planner._short(it.dst)}" if it.dst else ""
        res = f"   [{it.result}]" if it.result else ""
        print(f"  [{mark}] {it.id:>4}  {size}  {planner._short(it.src)}{dst}   ({it.reason}){res}")


def _handler(args: argparse.Namespace) -> Any:
    from zachary.actions.safe_actions import SafeActions
    from zachary.brain.handler import Handler

    conn, cfg = _open(args)
    return Handler(conn, SafeActions(conn, cfg.platform), cfg, None, None, cfg.platform)


def cmd_clean(args: argparse.Namespace) -> int:
    """Make a clean-up plan (nothing changes): ``clean``, ``clean downloads duplicates``."""
    h = _handler(args)
    t0 = time.perf_counter()
    reply = h.submit("clean " + " ".join(args.what))
    if reply.plan is None:
        print(reply.say)
        return 0
    _print_plan(reply.plan)
    print(f"\n{(time.perf_counter() - t0):.1f}s. Nothing has changed. To apply the ticked items:")
    print(f"  python -m zachary.cli plan approve {reply.plan.id}"
          "   [--only 1,2,5] [--skip 3,4]")  # fmt: skip
    return 0


def cmd_organize(args: argparse.Namespace) -> int:
    h = _handler(args)
    reply = h.submit("organize " + " ".join(args.what))
    if reply.plan is None:
        print(reply.say)
        return 0
    _print_plan(reply.plan)
    print(f"\nNothing has changed. Apply: python -m zachary.cli plan approve {reply.plan.id}"
          "   [--only ...] [--skip ...]")  # fmt: skip
    return 0


def _ids(text: str | None) -> set[int]:
    return {int(x) for x in (text or "").replace(" ", "").split(",") if x}


def cmd_plan(args: argparse.Namespace) -> int:
    from zachary.actions import planner

    h = _handler(args)
    if args.id is None:
        rows = h.conn.execute(
            "SELECT id, kind, status, created_at FROM action_plans ORDER BY id DESC LIMIT 10"
        ).fetchall()
        for r in rows:
            when = datetime.fromtimestamp(r[3]).strftime("%d %b %H:%M")
            print(f"  {r[0]:>4}  {when}  {r[1]:9} {r[2]}")
        return 0
    plan = planner.load(h.conn, args.id)
    if plan is None:
        print(f"no plan {args.id}")
        return 1
    if args.action == "show":
        _print_plan(plan)
        return 0
    if args.action == "reject":
        print(h.reject_plan(plan.id).say)
        return 0
    only, skip = _ids(args.only), _ids(args.skip)
    select = {i.id: (i.id in only if only else i.selected) and i.id not in skip
              for i in plan.items}  # fmt: skip
    n = sum(select.values())
    if not args.yes:
        ans = input(f"Apply {n} item(s) of plan {plan.id}? Files go to the trash / are moved. "
                    "[y/N] ")  # fmt: skip
        if ans.strip().lower() not in {"y", "yes"}:
            print("Nothing changed.")
            return 0
    reply = h.approve_plan(plan.id, select)
    print(reply.say)
    if reply.detail:
        print(reply.detail)
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    from zachary.actions import planner

    h = _handler(args)
    plan = (planner.load(h.conn, args.id) if args.id
            else planner.latest(h.conn, ["executed", "failed"]))  # fmt: skip
    if plan is None:
        print("There's nothing to undo.")
        return 0
    reply = h.undo_plan(plan.id)
    print(f"plan {plan.id} ({plan.title}): {reply.say}")
    if reply.detail:
        print(reply.detail)
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    """Folder rules: never_touch (Zachary never proposes changes there), auto_tidy."""
    conn, cfg = _open(args)
    if args.action in {"add", "remove"}:
        if not args.path or args.rule not in {"never_touch", "auto_tidy", "name_only"}:
            print("usage: rules add|remove PATH never_touch|auto_tidy|name_only")
            return 1
        fn = cfg.add_folder_rule if args.action == "add" else cfg.remove_folder_rule
        fn(Path(args.path).expanduser(), args.rule)
    for path, rule in conn.execute("SELECT path, rule FROM folder_rules ORDER BY path"):
        print(f"  {rule:12} {_short(path)}")
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    """List topics (virtual collections); --rebuild clusters now instead of overnight."""
    from zachary.indexer import clustering

    conn, cfg = _open(args)
    if args.rebuild:
        st = clustering.build(conn, [str(r) for r in cfg.index_roots()])
        print(f"{st.topics} topics from {st.files} files ({st.noise} unassigned), "
              f"{st.seconds:.1f}s")  # fmt: skip
    topics = clustering.list_topics(conn, args.limit)
    if not topics:
        print("no topics yet (built overnight by the indexer, or run: topics --rebuild)")
        return 0
    for t in topics:
        when = time.strftime("%d %b", time.localtime(t.last_ts)) if t.last_ts else ""
        print(f"  {t.size:4} files  {when:>6}  {t.label}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn, cfg = _open(args)
    files, dirs = conn.execute("SELECT count(*), coalesce(sum(is_dir), 0) FROM files").fetchone()
    n_proj = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
    n_acc = conn.execute("SELECT count(*) FROM access_log").fetchone()[0]
    hb = get_state(conn, "indexer.heartbeat")
    print(f"db:        {args.db or default_db_path()}")
    print(f"roots:     {', '.join(_short(str(r)) for r in cfg.index_roots())}")
    print(f"indexed:   {files - dirs} files, {dirs} folders, {n_proj} projects, {n_acc} accesses")
    print(f"paused:    {cfg.get_setting('indexing_paused')}")
    alive = hb is not None and time.time() - float(hb) < 15
    print(
        f"indexer:   {'running' if alive else 'not running'}"
        + (f" (heartbeat {_ago(float(hb))})" if hb else "")
    )
    watching = get_state(conn, "indexer.watching")
    if watching is not None:
        print(f"watching:  {watching}")
    n_emb, n_files = conn.execute(
        "SELECT coalesce(sum(embedded_at IS NOT NULL), 0), count(*) FROM files WHERE is_dir = 0"
    ).fetchone()
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    n_rel = conn.execute("SELECT count(DISTINCT src_id) FROM relations").fetchone()[0]
    print(f"semantic:  {n_emb}/{n_files} files embedded, {n_chunks} passages, "
          f"{n_rel} files with related links")  # fmt: skip
    waiting = get_state(conn, "semantic.waiting")
    if waiting:
        print(f"           waiting: {waiting}")
    err = get_state(conn, "semantic.error")
    if err:
        print(f"           last error: {err}")
    msg = get_state(conn, "indexer.watch_limit_message")
    if msg:
        print("\n" + str(msg))
    return 0


def cmd_roots(args: argparse.Namespace) -> int:
    _, cfg = _open(args)
    stored: list[str] = cfg.get_setting("index_roots")
    if args.action == "list":
        src = "configured" if stored else "defaults"
        print(f"({src})")
        for r in cfg.index_roots():
            print(f"  {r}")
        return 0
    if args.action == "reset":
        cfg.set_setting("index_roots", [])
    elif args.action == "add":
        cfg.set_index_roots([*(stored or map(str, cfg.index_roots())), *args.paths])
    elif args.action == "remove":
        drop = {cfg.canonical(p) for p in args.paths}
        cfg.set_index_roots([r for r in (stored or map(str, cfg.index_roots())) if r not in drop])
    for r in cfg.index_roots():
        print(f"  {r}")
    return 0


def cmd_exclude(args: argparse.Namespace) -> int:
    _, cfg = _open(args)
    if args.action == "list":
        for pattern, rule_kind, source, enabled in cfg.list_excludes():
            flag = "" if enabled else "  (disabled)"
            print(f"  {rule_kind:<7} {source:<7} {pattern}{flag}")
        return 0
    for p in args.paths:
        kind: ExcludeKind = "glob" if any(c in p for c in "*?[") else "path"
        if args.action == "add":
            cfg.add_exclude(p, kind)
        elif kind == "path" and not os.path.isabs(p) and "/" not in p and "\\" not in p:
            cfg.set_exclude_enabled(p, "dirname", False)  # e.g. "build": disable a default
        else:
            cfg.remove_exclude(p, kind)
            cfg.set_exclude_enabled(p, kind, False)
        print(f"{args.action}: {p}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Exclude a file/folder; the indexer purges it and all derived data."""
    _, cfg = _open(args)
    for p in args.paths:
        cfg.add_exclude(p, "path")
        print(f"forgetting {cfg.canonical(p)} (hidden from search now; purged by the indexer)")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """Show or change a setting, e.g. ``set index_hidden_folders true``."""
    _, cfg = _open(args)
    if args.key is None:
        for k, v in vars_of(cfg).items():
            print(f"  {k} = {json.dumps(v)}")
        return 0
    if args.value is None:
        print(json.dumps(cfg.get_setting(args.key)))
        return 0
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    cfg.set_setting(args.key, value)
    print(f"{args.key} = {json.dumps(value)}")
    return 0


def vars_of(cfg: Config) -> dict[str, object]:
    s = cfg.load()
    return {k: getattr(s, k) for k in s.__dataclass_fields__}


def cmd_pause(args: argparse.Namespace, paused: bool) -> int:
    _, cfg = _open(args)
    cfg.set_setting("indexing_paused", paused)
    print("indexing paused" if paused else "indexing resumed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="zachary.cli", description="Zachary command line")
    ap.add_argument("--db", default=None, help="database path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search files")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.add_argument("--keywords-only", action="store_true", help="skip meaning search")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("projects", help="projects by latest activity")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.set_defaults(fn=cmd_projects)

    s = sub.add_parser("recent", help='recently opened/edited, e.g. recent "last week"')
    s.add_argument("when", nargs="*")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(fn=cmd_recent)

    s = sub.add_parser("scan", help="catch up now (in this process)")
    s.add_argument("--full", action="store_true", help="ignore journals; full re-scan")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("embed", help="index file contents now (downloads the model once)")
    s.add_argument("--limit", type=int, default=0, help="at most this many files")
    s.set_defaults(fn=cmd_embed)

    s = sub.add_parser("related", help="files related to a file")
    s.add_argument("path")
    s.set_defaults(fn=cmd_related)

    s = sub.add_parser(
        "voice",
        help="voice: usage | setup | mics [ID] | latency [N] | transcribe WAV | say TEXT | listen",
    )
    s.add_argument(
        "action", choices=["usage", "setup", "mics", "latency", "transcribe", "say", "listen"]
    )
    s.add_argument("text", nargs="?", default=None, help="WAV path, text to say, or a mic id")
    s.add_argument("--offline", action="store_true", help="local speech recognition only")
    s.add_argument("--no-speak", action="store_true", help="don't speak the reply (listen)")
    s.set_defaults(fn=cmd_voice)

    s = sub.add_parser("topics", help="topics (virtual collections of related files)")
    s.add_argument("--rebuild", action="store_true", help="cluster now (otherwise nightly)")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_topics)

    s = sub.add_parser("clean", help="plan a clean-up (nothing changes), e.g. clean downloads")
    s.add_argument("what", nargs="*", help="duplicates | installers | junk | empty | large; "
                   "downloads | desktop | documents ...")  # fmt: skip
    s.set_defaults(fn=cmd_clean)

    s = sub.add_parser("organize", help="plan moving a topic's files into one folder")
    s.add_argument("what", nargs="+")
    s.set_defaults(fn=cmd_organize)

    s = sub.add_parser("plan", help="list plans | plan show|approve|reject ID")
    s.add_argument("action", nargs="?", choices=["show", "approve", "reject"], default="show")
    s.add_argument("id", nargs="?", type=int, default=None)
    s.add_argument("--only", help="approve only these item numbers, e.g. 1,2,5")
    s.add_argument("--skip", help="leave these item numbers alone")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("undo", help="undo the last applied plan (or plan ID)")
    s.add_argument("id", nargs="?", type=int, default=None)
    s.set_defaults(fn=cmd_undo)

    s = sub.add_parser("rules", help="folder rules: rules | rules add|remove PATH RULE")
    s.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    s.add_argument("path", nargs="?")
    s.add_argument("rule", nargs="?")
    s.set_defaults(fn=cmd_rules)

    s = sub.add_parser("ask", help='answer from your files, e.g. ask "when is my fee due"')
    s.add_argument("question", nargs="+")
    s.add_argument("--local", action="store_true", help="never call the LLM (passage only)")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("llm", help="optional LLM: status | usage")
    s.add_argument("action", choices=["status", "usage"])
    s.set_defaults(fn=cmd_llm)

    s = sub.add_parser("status", help="index and indexer status")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("roots", help="list/add/remove/reset index roots")
    s.add_argument("action", choices=["list", "add", "remove", "reset"], nargs="?", default="list")
    s.add_argument("paths", nargs="*")
    s.set_defaults(fn=cmd_roots)

    s = sub.add_parser("exclude", help="list/add/remove exclude rules")
    s.add_argument("action", choices=["list", "add", "remove"], nargs="?", default="list")
    s.add_argument("paths", nargs="*")
    s.set_defaults(fn=cmd_exclude)

    s = sub.add_parser("forget", help="forget files/folders and all derived data")
    s.add_argument("paths", nargs="+")
    s.set_defaults(fn=cmd_forget)

    s = sub.add_parser("set", help="show/change settings, e.g. set index_hidden_folders true")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("pause", help="pause indexing")
    s.set_defaults(fn=lambda a: cmd_pause(a, True))
    s = sub.add_parser("resume", help="resume indexing")
    s.set_defaults(fn=lambda a: cmd_pause(a, False))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
