"""Small realistic corpus with real text in every supported format (tests + bench)."""

from __future__ import annotations

import os
import time
from pathlib import Path

DAY = 86400.0

DSA_NOTES = """Data Structures - Unit 2 notes
A linked list stores elements in nodes; each node points to the next node.
Stacks are LIFO and queues are FIFO. A binary search tree keeps smaller keys on the
left subtree and larger keys on the right. Heaps support priority queues.
Graphs are traversed with BFS and DFS. Hash tables give average O(1) lookups."""

LINKED_LIST_CODE = """// singly linked list
struct Node { int value; Node* next; };

Node* reverse(Node* head) {
    Node* prev = nullptr;
    while (head) { Node* nxt = head->next; head->next = prev; prev = head; head = nxt; }
    return prev;
}
"""

FEE_TEXT = """Fee Receipt - Semester 5
Tuition fee: Rs 50,000. Hostel fee: Rs 30,000.
Payment received. The next installment is due on 15 June 2026.
Late fee applies after the due date."""

OS_TEXT = """Operating Systems lecture 4: process scheduling.
Round robin, shortest job first and priority scheduling. Deadlocks need mutual
exclusion, hold and wait, no preemption and circular wait."""

TRAVEL_TEXT = """Goa trip itinerary: beach day at Baga, boat ride, seafood dinner.
Flight tickets and hotel booking confirmation for the holiday."""

RECIPE_TEXT = """Paneer butter masala recipe. Ingredients: paneer, butter, tomato, cream,
garam masala. Cook the tomato gravy, add cream and paneer, simmer for ten minutes."""


def _pdf(path: Path, text: str, title: str) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text)
    doc.set_metadata({"title": title})
    doc.save(path)


def _docx(path: Path, text: str) -> None:
    import docx

    d = docx.Document()
    for line in text.splitlines():
        d.add_paragraph(line)
    d.save(str(path))


def _pptx(path: Path, title: str, body: str) -> None:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = title
    slide.placeholders[1].text = body
    prs.save(str(path))


def _xlsx(path: Path, rows: list[list[object]]) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(str(path))


def build_corpus(home: Path, now: float | None = None) -> dict[str, Path]:
    """Create the corpus; returns name -> path. DSA notes are old, the code is recent."""
    now = now or time.time()
    files: dict[str, Path] = {}

    def put(rel: str, mtime: float) -> Path:
        p = home / rel
        os.utime(p, (mtime, mtime))
        files[p.name] = p
        return p

    (home / "College" / "Sem3" / "DSA").mkdir(parents=True)
    (home / "College" / "Sem5" / "Fees").mkdir(parents=True)
    (home / "College" / "Sem4" / "OS").mkdir(parents=True)
    (home / "code" / "practice").mkdir(parents=True)
    (home / "Personal").mkdir(parents=True)
    _pdf(home / "College/Sem3/DSA/Unit2 notes.pdf", DSA_NOTES, "Data Structures Unit 2")
    put("College/Sem3/DSA/Unit2 notes.pdf", now - 600 * DAY)
    _docx(home / "College/Sem3/DSA/tree assignment.docx",
          "Assignment: implement insertion and deletion in a binary search tree.")  # fmt: skip
    put("College/Sem3/DSA/tree assignment.docx", now - 590 * DAY)
    (home / "code/practice/ll.cpp").write_text(LINKED_LIST_CODE)
    put("code/practice/ll.cpp", now - 2 * DAY)
    _pdf(home / "College/Sem5/Fees/receipt_sem5.pdf", FEE_TEXT, "Fee Receipt Semester 5")
    put("College/Sem5/Fees/receipt_sem5.pdf", now - 40 * DAY)
    _pptx(home / "College/Sem4/OS/lecture4.pptx", "Process scheduling", OS_TEXT)
    put("College/Sem4/OS/lecture4.pptx", now - 300 * DAY)
    _xlsx(home / "Personal/expenses.xlsx",
          [["Item", "Amount"], ["Tuition fee", 50000], ["Groceries", 4000]])  # fmt: skip
    put("Personal/expenses.xlsx", now - 10 * DAY)
    (home / "Personal/goa trip.md").write_text(TRAVEL_TEXT)
    put("Personal/goa trip.md", now - 100 * DAY)
    (home / "Personal/paneer.txt").write_text(RECIPE_TEXT)
    put("Personal/paneer.txt", now - 200 * DAY)
    (home / "Personal/IMG_2041.jpg").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    put("Personal/IMG_2041.jpg", now - 5 * DAY)
    return files
