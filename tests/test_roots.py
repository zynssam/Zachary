from __future__ import annotations

import sqlite3
from pathlib import Path

from zachary.core.config import Config, collapse_roots
from zachary.core.platform.linux import LinuxPlatform, parse_mounts, parse_user_dirs
from zachary.core.platform.windows import DRIVE_FIXED, DRIVE_REMOVABLE, select_drives

HOME = Path("/home/u")

MOUNTS = r"""/dev/nvme0n1p3 / btrfs rw,subvol=/root 0 0
/dev/nvme0n1p3 /home btrfs rw,subvol=/home 0 0
proc /proc proc rw 0 0
tmpfs /tmp tmpfs rw 0 0
/dev/nvme0n1p2 /boot ext4 rw 0 0
/dev/nvme0n1p1 /boot/efi vfat rw 0 0
/dev/loop1 /var/lib/snapd/snap/core22/2437 squashfs ro 0 0
/dev/sda1 /run/media/u/My\040USB fuseblk rw 0 0
/dev/sdb1 /mnt/data ext4 rw 0 0
/dev/sdb1 /mnt/data ext4 rw 0 0
nas:/share /mnt/nas nfs4 rw 0 0
//srv/x /mnt/smb cifs rw 0 0
u@h:/ /home/u/remote fuse.sshfs rw 0 0
/dev/sdc1 /media/u/CAMERA exfat rw 0 0
"""


def test_parse_mounts_keeps_disks_only() -> None:
    assert parse_mounts(MOUNTS, HOME) == [
        Path("/run/media/u/My USB"),
        Path("/mnt/data"),
        Path("/media/u/CAMERA"),
    ]


def test_parse_user_dirs() -> None:
    text = """# comment
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOCUMENTS_DIR="/data/Docs"
XDG_DOWNLOAD_DIR="$HOME"
XDG_MUSIC_DIR="$HOME/Music"
"""
    # Downloads set to $HOME means "disabled" in XDG; Music isn't wanted.
    assert parse_user_dirs(text, HOME) == [HOME / "Desktop", Path("/data/Docs")]


def test_select_drives_skips_system_network_and_cdrom() -> None:
    mask = 0b1111100  # C D E F G
    types = {"C": DRIVE_FIXED, "D": DRIVE_FIXED, "E": DRIVE_REMOVABLE, "F": 4, "G": 5}
    assert select_drives(mask, types, "C:") == [Path("D:\\"), Path("E:\\")]


def test_collapse_roots() -> None:
    roots = [
        "/home/u",
        "/home/u/Documents",
        "/home/u/Downloads",
        "/data/Docs",
        "/mnt/data",
        "/mnt/data",
        "/home/user2",
    ]
    assert collapse_roots(roots) == ["/data/Docs", "/home/u", "/home/user2", "/mnt/data"]


def test_linux_default_roots_on_this_machine() -> None:
    roots = LinuxPlatform().default_index_roots()
    assert roots[0] == Path.home()
    assert len(roots) >= 4  # home + desktop/documents/downloads (+ any disks)


def test_root_beats_ancestor_exclude(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cfg = Config(conn, LinuxPlatform())
    cfg.add_exclude(tmp_path / "run")
    usb = tmp_path / "run" / "media" / "u" / "USB"
    cfg.set_index_roots([usb])
    m = cfg.exclude_matcher()
    assert not m.is_excluded(usb / "notes.txt", is_dir=False)
    # Without that root the ancestor exclude applies.
    assert cfg.exclude_matcher(roots=[tmp_path]).is_excluded(usb / "notes.txt", is_dir=False)


def test_exclude_inside_or_equal_root_still_applies(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cfg = Config(conn, LinuxPlatform())
    cfg.set_index_roots([tmp_path / "home", tmp_path / "Downloads"])
    cfg.add_exclude(tmp_path / "home" / "Private")
    cfg.add_exclude(tmp_path / "Downloads")
    m = cfg.exclude_matcher()
    assert m.is_excluded(tmp_path / "home" / "Private" / "a.txt", is_dir=False)
    assert m.is_excluded(tmp_path / "Downloads" / "a.exe", is_dir=False)


def test_config_index_roots_collapses_defaults(conn: sqlite3.Connection) -> None:
    roots = Config(conn, LinuxPlatform()).index_roots()
    home = Path.home()
    assert home in roots
    assert all(r == home or home not in r.parents for r in roots)
