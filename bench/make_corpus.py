"""A realistic content corpus for Phase 3 throughput: real text in every format.

    python bench/make_corpus.py ~/.cache/zachary-bench/corpus --files 2000

Files mix PDF (multi-page), DOCX, PPTX, XLSX, Markdown, text and code, built
from topic paragraphs (data structures, OS, networks, finance, travel, cooking,
ML, biology) so sizes and vocabulary resemble real documents.
"""

from __future__ import annotations

import argparse
import random
import shutil
import time
from pathlib import Path

TOPICS = {
    "dsa": "A linked list is a sequence of nodes. Binary search trees keep keys ordered. "
    "Hash tables map keys to buckets. Heaps implement priority queues. Graph traversal "
    "uses breadth first search and depth first search. Dynamic programming stores subproblems.",
    "os": "The scheduler picks the next process. Virtual memory maps pages to frames. "
    "Deadlock needs mutual exclusion and circular wait. File systems use inodes and journals.",
    "networks": "TCP provides reliable delivery with acknowledgements. Routers forward packets "
    "using routing tables. DNS resolves names to addresses. TLS encrypts the connection.",
    "finance": "The tuition fee is due at the start of the semester. Keep the receipt for the "
    "payment. The budget lists rent, groceries, electricity and the phone bill.",
    "travel": "Flight tickets and hotel booking for the holiday. The itinerary covers the beach, "
    "a boat ride and the old fort. Pack sunscreen and a camera.",
    "cooking": "Heat oil, add cumin and onions, then tomatoes and spices. Simmer the gravy, add "
    "paneer and cream. Serve hot with rice or naan.",
    "ml": "The model is trained with gradient descent. Overfitting is reduced with dropout and "
    "regularization. Evaluate with precision, recall and a held out test set.",
    "biology": "Cells contain DNA in the nucleus. Mitochondria produce energy. Enzymes speed up "
    "reactions. Photosynthesis converts light into chemical energy.",
}
CODE = {
    "dsa": "class Node:\n    def __init__(self, value):\n        self.value = value\n        "
    "self.next = None\n\ndef reverse(head):\n    prev = None\n    while head:\n        "
    "head.next, prev, head = prev, head, head.next\n    return prev\n",
    "ml": "import numpy as np\n\ndef train(X, y, lr=0.1, epochs=100):\n    w = np.zeros(X.shape[1])"
    "\n    for _ in range(epochs):\n        grad = X.T @ (X @ w - y) / len(y)"
    "\n        w -= lr * grad\n    return w\n",
}


def paragraph(rng: random.Random, topic: str) -> str:
    sents = TOPICS[topic].split(". ")
    rng.shuffle(sents)
    return ". ".join(sents[: rng.randint(3, len(sents))]).strip(". ") + "."


def text_for(rng: random.Random, topic: str, paras: int) -> str:
    return "\n\n".join(paragraph(rng, topic) for _ in range(paras))


def build(root: Path, n: int, seed: int = 1) -> dict[str, int]:
    import docx
    import openpyxl
    import pymupdf
    from pptx import Presentation

    rng = random.Random(seed)
    counts: dict[str, int] = {}
    topics = list(TOPICS)
    for i in range(n):
        topic = rng.choice(topics)
        folder = root / topic / f"set{i % 20}"
        folder.mkdir(parents=True, exist_ok=True)
        kind = rng.choices(["pdf", "docx", "pptx", "xlsx", "md", "txt", "py"],
                           weights=[25, 20, 10, 8, 15, 12, 10])[0]  # fmt: skip
        name = folder / f"{topic}_{kind}_{i}.{kind}"
        if kind == "pdf":
            doc = pymupdf.open()
            for _ in range(rng.randint(1, 6)):
                page = doc.new_page()
                page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text_for(rng, topic, 6))
            doc.save(name)
        elif kind == "docx":
            d = docx.Document()
            for _ in range(rng.randint(3, 20)):
                d.add_paragraph(paragraph(rng, topic))
            d.save(str(name))
        elif kind == "pptx":
            prs = Presentation()
            for _ in range(rng.randint(2, 8)):
                s = prs.slides.add_slide(prs.slide_layouts[1])
                s.shapes.title.text = topic.upper()
                s.placeholders[1].text = paragraph(rng, topic)
            prs.save(str(name))
        elif kind == "xlsx":
            wb = openpyxl.Workbook()
            ws = wb.active
            for r in range(rng.randint(10, 200)):
                ws.append([f"{topic} item {r}", rng.randint(1, 10000), paragraph(rng, topic)[:60]])
            wb.save(str(name))
        elif kind == "py":
            name.write_text(CODE.get(topic, CODE["dsa"]) * rng.randint(1, 10))
        else:
            name.write_text(text_for(rng, topic, rng.randint(2, 40)))
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--files", type=int, default=2000)
    args = ap.parse_args()
    shutil.rmtree(args.root, ignore_errors=True)
    t0 = time.perf_counter()
    counts = build(args.root, args.files)
    size = sum(p.stat().st_size for p in args.root.rglob("*") if p.is_file())
    print(f"built {sum(counts.values())} files {counts}, {size / 1e6:.1f} MB "
          f"in {time.perf_counter() - t0:.1f}s at {args.root}")  # fmt: skip


if __name__ == "__main__":
    main()
