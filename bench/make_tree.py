"""Create a deterministic synthetic home folder for benchmarks.

~200k indexable files in ~12k folders (documents, college notes, projects,
downloads, photos, music) plus ~30k files inside excluded folders
(node_modules, .git, __pycache__) that the scanner must skip. mtimes spread
over three years. Files are empty: Phase 1 is metadata only.

    python bench/make_tree.py ~/.cache/zachary-bench/home --files 200000
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import time
from pathlib import Path

WORDS = (
    "report notes assignment lab final draft summary budget invoice receipt fee fees "
    "hostel exam timetable syllabus lecture tutorial project proposal resume letter "
    "statement bank tax insurance rent electricity phone bill ticket booking travel "
    "linked list tree graph heap stack queue sorting dynamic programming recursion "
    "network database operating system compiler algorithm physics chemistry maths "
    "calculus statistics economics marketing design sketch photo screenshot scan "
    "meeting minutes agenda plan roadmap spec architecture api server client mobile "
    "android web react python java rust go notebook dataset model training results"
).split()
EXTS_DOC = [".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".odt", ".csv"]
EXTS_CODE = [".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".go", ".rs", ".json", ".md"]
EXTS_MEDIA = [".jpg", ".png", ".mp4", ".mp3", ".heic"]
MARKERS = ["package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod",
           "pom.xml", "CMakeLists.txt", "Makefile"]  # fmt: skip


def _name(rng: random.Random, exts: list[str]) -> str:
    n = rng.randint(1, 4)
    words = rng.sample(WORDS, n)
    style = rng.random()
    if style < 0.3:
        base = "_".join(words)
    elif style < 0.55:
        base = " ".join(w.capitalize() for w in words)
    elif style < 0.75:
        base = "".join(w.capitalize() for w in words)
    else:
        base = "-".join(words) + f"-{rng.randint(1, 99)}"
    if rng.random() < 0.08:
        base += rng.choice(["_v2", "_final", "_final_v3", " (1)", "_old"])
    return base + rng.choice(exts)


class Builder:
    def __init__(self, root: Path, seed: int) -> None:
        self.root = root
        self.rng = random.Random(seed)
        self.files = 0
        self.excluded = 0
        self.dirs = 0
        self.now = time.time()

    def _mtime(self) -> float:
        # Skewed toward recent: most files are old, some are fresh.
        age = self.rng.expovariate(1 / (86400 * 200))
        return self.now - min(age, 86400 * 3 * 365)

    def mkdir(self, p: Path) -> Path:
        p.mkdir(parents=True, exist_ok=True)
        self.dirs += 1
        return p

    def touch(self, p: Path, *, excluded: bool = False) -> None:
        if p.exists():
            return
        with open(p, "wb"):
            pass
        t = self._mtime()
        os.utime(p, (t, t))
        if excluded:
            self.excluded += 1
        else:
            self.files += 1

    def fill(self, d: Path, n: int, exts: list[str]) -> None:
        for _ in range(n):
            self.touch(d / _name(self.rng, exts))


def build(root: Path, total: int, seed: int = 42) -> Builder:
    b = Builder(root, seed)
    rng = b.rng
    share = {
        "college": 0.20, "documents": 0.15, "projects": 0.30, "downloads": 0.06,
        "pictures": 0.17, "music": 0.07, "misc": 0.05,
    }  # fmt: skip
    # College: semesters x subjects x units
    target = int(total * share["college"])
    col = b.mkdir(root / "College")
    per = max(1, target // (8 * 12 * 4))
    for sem in range(1, 9):
        for subj in rng.sample(WORDS, 12):
            for unit in range(1, 5):
                d = b.mkdir(col / f"Sem{sem}" / subj.capitalize() / f"Unit {unit}")
                b.fill(d, per, EXTS_DOC)
    fees = b.mkdir(col / "Sem5" / "Fees")
    for name in ["Fee Receipt Sem 5.pdf", "fee_structure_2024.pdf", "Hostel Fees.xlsx",
                 "Exam Fee Challan.pdf", "fee-refund-request.docx"]:  # fmt: skip
        b.touch(fees / name)
    # Documents: nested personal folders
    target = int(total * share["documents"])
    docs = b.mkdir(root / "Documents")
    made = 0
    while made < target:
        depth = rng.randint(1, 4)
        d = docs.joinpath(*[rng.choice(WORDS).capitalize() for _ in range(depth)])
        b.mkdir(d)
        n = rng.randint(5, 40)
        b.fill(d, n, EXTS_DOC)
        made += n
    # Projects: markers, src trees, excluded junk
    target = int(total * share["projects"])
    proj_root = b.mkdir(root / "Projects")
    made = 0
    i = 0
    while made < target:
        i += 1
        p = b.mkdir(proj_root / f"{rng.choice(WORDS)}-{rng.choice(WORDS)}-{i}")
        b.touch(p / rng.choice(MARKERS))
        git = b.mkdir(p / ".git" / "objects")
        for _ in range(rng.randint(5, 30)):
            b.touch(git / f"{rng.getrandbits(64):016x}", excluded=True)
        if rng.random() < 0.4:
            nm = b.mkdir(p / "node_modules" / rng.choice(WORDS))
            for _ in range(rng.randint(20, 120)):
                b.touch(nm / f"{rng.choice(WORDS)}.js", excluded=True)
        for _ in range(rng.randint(2, 8)):
            d = b.mkdir(p.joinpath("src", *rng.sample(WORDS, rng.randint(1, 3))))
            n = rng.randint(3, 25)
            b.fill(d, n, EXTS_CODE)
            made += n
            if rng.random() < 0.3:
                pc = b.mkdir(d / "__pycache__")
                for _ in range(n):
                    b.touch(pc / f"{rng.choice(WORDS)}.cpython-312.pyc", excluded=True)
    dsa = b.mkdir(proj_root / "dsa-practice")
    b.touch(dsa / "Makefile")
    for n in ["linked_list.cpp", "LinkedListNotes.md", "binary_tree.cpp", "graph_bfs.py"]:
        b.touch(dsa / n)
    # Downloads: flat and big
    dl = b.mkdir(root / "Downloads")
    b.fill(dl, int(total * share["downloads"]), [*EXTS_DOC, ".zip", ".exe", ".msi"])
    # Pictures / Music: date folders
    for key, top, exts in (("pictures", "Pictures", EXTS_MEDIA), ("music", "Music", [".mp3"])):
        target = int(total * share[key])
        n_dirs = max(1, target // 60)
        for k in range(n_dirs):
            d = b.mkdir(root / top / str(2023 + k % 4) / f"{(k % 12) + 1:02d}" / f"set{k}")
            b.fill(d, 60, exts)
    # Misc: deep chains to exercise depth
    misc = b.mkdir(root / "Misc")
    while b.files < total:
        d = misc.joinpath(*[rng.choice(WORDS) for _ in range(rng.randint(3, 9))])
        b.mkdir(d)
        b.fill(d, min(20, total - b.files), EXTS_DOC + EXTS_CODE)
    return b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--files", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true", help="delete an existing tree first")
    args = ap.parse_args()
    if args.root.exists():
        if not args.force:
            raise SystemExit(f"{args.root} exists (use --force to rebuild)")
        shutil.rmtree(args.root)
    t0 = time.perf_counter()
    b = build(args.root, args.files, args.seed)
    print(
        f"built {b.files:,} indexable files + {b.excluded:,} in excluded folders, "
        f"~{b.dirs:,} folders in {time.perf_counter() - t0:.1f}s at {args.root}"
    )


if __name__ == "__main__":
    main()
