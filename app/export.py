"""Export the original PDF with the reader's highlights as real PDF annotations.

The page content is untouched. Each highlighted sentence becomes one highlight
annotation per page it spans, placed on the word boxes saved at parse time
(one rectangle per text line), coloured by category, with the category as its
comment. A sticky note on page 1 explains the colours. Recipients can view,
edit or delete the annotations in any PDF reader.
"""

import re

import pymupdf

from . import library
from .highlight import CATEGORIES

# Highlighter colours (RGB 0-1); PDF viewers blend highlights with the page.
COLORS = {
    "objective": (0.55, 0.87, 0.55),
    "novelty": (0.55, 0.76, 1.00),
    "method": (0.80, 0.66, 1.00),
    "result": (1.00, 0.62, 0.62),
    "limitation": (1.00, 0.74, 0.45),
    "definition": (1.00, 0.92, 0.35),
}
NAMES = {
    "objective": "Objective: what the work sets out to do",
    "novelty": "Novelty: what is new",
    "method": "Method: key design decisions",
    "result": "Result: findings and outcomes",
    "limitation": "Limitation: caveats and future work",
    "definition": "Definition: key terms",
}
COLOR_WORDS = {
    "objective": "Green", "novelty": "Blue", "method": "Purple",
    "result": "Red", "limitation": "Orange", "definition": "Yellow",
}
AUTHOR = "Papercut"


def export_pdf(paper_id: str, labels: dict[str, str]) -> tuple[bytes, dict]:
    """labels: {sentence_id: category} as shown in the reader.
    Returns (pdf bytes, stats)."""
    paper = library.load_paper(paper_id)
    doc = pymupdf.open(library.pdf_path(paper_id))
    stats = {"highlights": 0, "skipped": 0}
    used = set()

    for sid, cat in labels.items():
        s = paper["sentences"].get(sid)
        if cat not in CATEGORIES or not s:
            continue
        if not s.get("rects"):
            stats["skipped"] += 1  # couldn't be located on the page at parse time
            continue
        by_page: dict[int, list] = {}
        for r in s["rects"]:
            by_page.setdefault(r["page"], []).append(pymupdf.Rect(r["bbox"]))
        for page_no, rects in by_page.items():
            page = doc[page_no - 1]
            annot = page.add_highlight_annot(rects)
            annot.set_colors(stroke=COLORS[cat])
            annot.set_info(title=AUTHOR, subject=cat.capitalize(), content=cat.capitalize())
            annot.update()
        stats["highlights"] += 1
        used.add(cat)

    # Notes become PDF comments (sticky notes) beside their sentence's first line.
    stats["notes"] = 0
    for note in paper.get("notes", []):
        rects = paper["sentences"].get(note.get("sid"), {}).get("rects")
        if not rects:
            continue
        first = rects[0]
        page = doc[first["page"] - 1]
        x0, y0 = first["bbox"][0], first["bbox"][1]
        # Put the icon in the margin left of the line (the gutter, for a right
        # column): the column's left edge is where other lines spanning x0 start.
        col_left = min((r["bbox"][0] for s in paper["sentences"].values() for r in s.get("rects", [])
                        if r["page"] == first["page"] and r["bbox"][0] <= x0 <= r["bbox"][2]), default=x0)
        at = pymupdf.Point(max(page.rect.x0 + 2, col_left - 19), y0)
        a = page.add_text_annot(at, note["text"], icon="Comment")
        a.set_colors(stroke=(1.0, 0.8, 0.2))
        a.set_info(title="Note", subject="Note")
        a.update()
        stats["notes"] += 1

    if used or stats["notes"]:
        _legend(doc[0], used, stats["notes"])
    data = doc.tobytes(garbage=1, deflate=True)
    doc.close()
    return data, stats


def _legend(page: pymupdf.Page, used: set[str], notes: int = 0) -> None:
    lines = ["Annotations added with Papercut:"]
    if used:
        lines += ["", "Highlight colours:"] + [f"{COLOR_WORDS[c]} = {NAMES[c]}" for c in CATEGORIES if c in used]
    if notes:
        lines += ["", f"Speech-bubble icons are the reader's notes ({notes})."]
    lines += ["", "You can edit or delete these annotations in any PDF reader."]
    note = page.add_text_annot(pymupdf.Point(page.rect.x0 + 18, page.rect.y0 + 18), "\n".join(lines), icon="Note")
    note.set_colors(stroke=(1.0, 0.85, 0.3))
    note.set_info(title=AUTHOR, subject="Highlight colours")
    note.update()


def filename(paper: dict) -> str:
    title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", paper["meta"]["title"]).strip()
    title = re.sub(r"\s+", " ", title)[:90].rstrip(" .") or "paper"
    return f"{title} (highlighted).pdf"
