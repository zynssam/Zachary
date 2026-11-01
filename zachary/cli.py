"""Debug / power-user CLI: ``python -m zachary.cli <command>``.

Queries only read the database. ``scan`` runs a catch-up in this process
(useful when the daemon isn't running). Config commands (roots, exclude,
forget, pause) only change settings; the running indexer applies them within
a few seconds, and search hides newly excluded paths immediately.
"""

from __future__ import annotations

import argparse
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
    return 1


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
        MicSource(),
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
    handler = Handler(conn, SafeActions(conn, cfg.platform), cfg, lambda: local_semantic(conn, cfg))
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

    s = sub.add_parser("voice", help="voice: usage | setup | transcribe WAV | say TEXT | listen")
    s.add_argument("action", choices=["usage", "setup", "transcribe", "say", "listen"])
    s.add_argument("text", nargs="?", default="", help="WAV path (transcribe) or text (say)")
    s.add_argument("--offline", action="store_true", help="local speech recognition only")
    s.add_argument("--no-speak", action="store_true", help="don't speak the reply (listen)")
    s.set_defaults(fn=cmd_voice)

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
