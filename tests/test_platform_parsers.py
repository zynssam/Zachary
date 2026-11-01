"""Pure parsers for OS data. They run on every OS (no OS calls)."""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from pathlib import Path

from zachary.core.platform.linux import check_watch_limit_values, parse_xbel
from zachary.core.platform.windows import parse_lnk_target, parse_usn_records

XBEL = """<?xml version="1.0" encoding="UTF-8"?>
<xbel version="1.0" xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"
      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info">
  <bookmark href="file:///home/u/College/Fee%20Receipt%20Sem%205.pdf"
            added="2026-10-01T10:00:00Z" modified="2026-10-01T10:00:00Z"
            visited="1970-01-01T00:00:00Z">
    <info><metadata owner="http://freedesktop.org">
      <mime:mime-type type="application/pdf"/>
      <bookmark:applications>
        <bookmark:application name="Evince" exec="&apos;evince %u&apos;"
            modified="2026-11-03T09:30:00.123456Z" count="3"/>
      </bookmark:applications>
    </metadata></info>
  </bookmark>
  <bookmark href="https://example.com/x" modified="2026-11-03T09:30:00Z"/>
  <bookmark href="file:///home/u/notes.md" added="2026-11-02T08:00:00Z"/>
</xbel>
"""


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


def test_parse_xbel_latest_app_time_and_local_files_only() -> None:
    items = parse_xbel(XBEL)
    assert [str(i.path) for i in items] == [
        str(Path("/home/u/College/Fee Receipt Sem 5.pdf")),
        str(Path("/home/u/notes.md")),
    ]
    assert abs(items[0].ts - _ts("2026-11-03T09:30:00.123456")) < 1e-3
    assert items[1].ts == _ts("2026-11-02T08:00:00")


def test_parse_xbel_garbage() -> None:
    assert parse_xbel("not xml") == []
    assert parse_xbel("<xbel/>") == []


def test_watch_limit_values() -> None:
    assert check_watch_limit_values(None, 10**6).ok
    assert check_watch_limit_values(65536, 10_000).ok
    lim = check_watch_limit_values(46173, 40_000)
    assert not lim.ok and lim.recommended == 524288
    assert "sudo sysctl fs.inotify.max_user_watches=524288" in lim.message
    assert "never needs root" in lim.message
    big = check_watch_limit_values(8192, 1_000_000)
    assert big.recommended is not None and big.recommended >= 1_500_000


def _lnk(base: str, suffix: str = "", *, unicode: bool, id_list: bool) -> bytes:
    flags = 0x2 | (0x1 if id_list else 0)
    header = struct.pack("<I16sI", 0x4C, b"\x01\x14\x02" + b"\0" * 13, flags).ljust(0x4C, b"\0")
    body = b""
    if id_list:
        ids = b"\x04\x00\xaa\xbb" + b"\0\0"  # one 4-byte item + terminator
        body += struct.pack("<H", len(ids)) + ids
    volume = b"\x10\0\0\0" + b"\0" * 12
    if unicode:
        hdr = 0x24
        ansi_base = b"\0"
        ansi_suffix = b"\0"
        u_base = base.encode("utf-16-le") + b"\0\0"
        u_suffix = suffix.encode("utf-16-le") + b"\0\0"
        off_vol = hdr
        off_lbp = off_vol + len(volume)
        off_cps = off_lbp + len(ansi_base)
        off_lbp_u = off_cps + len(ansi_suffix)
        off_cps_u = off_lbp_u + len(u_base)
        tail = volume + ansi_base + ansi_suffix + u_base + u_suffix
        size = hdr + len(tail)
        info = (
            struct.pack("<9I", size, hdr, 1, off_vol, off_lbp, 0, off_cps, off_lbp_u, off_cps_u)
            + tail
        )
    else:
        hdr = 0x1C
        a_base = base.encode("cp1252") + b"\0"
        a_suffix = suffix.encode("cp1252") + b"\0"
        off_vol = hdr
        off_lbp = off_vol + len(volume)
        off_cps = off_lbp + len(a_base)
        tail = volume + a_base + a_suffix
        info = struct.pack("<7I", hdr + len(tail), hdr, 1, off_vol, off_lbp, 0, off_cps) + tail
    return header + body + info


def test_parse_lnk_ansi_and_unicode() -> None:
    p = "C:\\Users\\u\\Documents\\Fee Receipt.pdf"
    assert parse_lnk_target(_lnk(p, unicode=False, id_list=True)) == p
    assert parse_lnk_target(_lnk(p, unicode=False, id_list=False)) == p
    q = "C:\\Users\\u\\Documents\\फीस रसीद.pdf"
    assert parse_lnk_target(_lnk(q, unicode=True, id_list=True)) == q
    assert parse_lnk_target(_lnk("C:\\Users", "u\\x.txt", unicode=False, id_list=False)) == (
        "C:\\Users\\u\\x.txt"
    )


def test_parse_lnk_rejects_garbage() -> None:
    assert parse_lnk_target(b"") is None
    assert parse_lnk_target(b"\x4c\0\0\0" + b"\0" * 100) is None  # no LinkInfo
    broken = _lnk("C:\\x", unicode=True, id_list=False)[:-6]  # cut the terminator off
    assert parse_lnk_target(broken) is None


def _usn_record(frn: int, parent: int, name: str, reason: int) -> bytes:
    raw = name.encode("utf-16-le")
    length = (60 + len(raw) + 7) // 8 * 8
    rec = struct.pack("<IHHQQqqIIIIHH", length, 2, 0, frn, parent, 1, 0, reason, 0, 0, 0,
                      len(raw), 60) + raw  # fmt: skip
    return rec.ljust(length, b"\0")


def test_parse_usn_records() -> None:
    buf = (
        struct.pack("<q", 4242)
        + _usn_record(10, 5, "Fee.pdf", 0x100)
        + _usn_record(11, 5, "नोट्स.txt", 0x2000)
    )
    nxt, recs = parse_usn_records(buf, len(buf))
    assert nxt == 4242
    assert [(r.frn, r.parent_frn, r.name, r.reason) for r in recs] == [
        (10, 5, "Fee.pdf", 0x100),
        (11, 5, "नोट्स.txt", 0x2000),
    ]
    assert parse_usn_records(buf[:8], 8) == (4242, [])


def _usn_record_v3(frn: int, parent: int, name: str, reason: int) -> bytes:
    raw = name.encode("utf-16-le")
    length = (76 + len(raw) + 7) // 8 * 8
    rec = struct.pack(
        "<IHH16s16sqqIIIIHH",
        length,
        3,
        0,
        frn.to_bytes(16, "little"),
        parent.to_bytes(16, "little"),
        1,
        0,
        reason,
        0,
        0,
        0,
        len(raw),
        76,
    )
    return (rec + raw).ljust(length, b"\0")  # fmt: skip


def test_parse_usn_records_v3_and_unknown_versions() -> None:
    big = (7 << 64) | 99
    unknown = struct.pack("<IHH", 16, 4, 0).ljust(16, b"\0")  # skipped by its length
    buf = struct.pack("<q", 1) + _usn_record_v3(big, 5, "x.txt", 0x100) + unknown
    _nxt, recs = parse_usn_records(buf, len(buf))
    assert [(r.frn, r.parent_frn, r.name) for r in recs] == [(big, 5, "x.txt")]
