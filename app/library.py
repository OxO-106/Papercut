"""Library folder: one directory per paper, keyed by a prefix of the PDF's SHA-256."""

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from .config import LIBRARY, PAPERS_DIR

RECENT_FILE = LIBRARY / "recent.json"
SETTINGS_FILE = LIBRARY / "settings.json"
RECENT_MAX = 30


def paper_dir(paper_id: str) -> Path:
    if not paper_id.isalnum():
        raise ValueError("bad paper id")
    return PAPERS_DIR / paper_id


def pdf_path(paper_id: str) -> Path:
    return paper_dir(paper_id) / "original.pdf"


def json_path(paper_id: str) -> Path:
    return paper_dir(paper_id) / "paper.json"


def assets_dir(paper_id: str) -> Path:
    return paper_dir(paper_id) / "assets"


def ingest(src: Path, filename: str, origin: dict | None = None) -> str:
    """Copy a PDF into the library. Returns its id; re-ingesting the same file is a no-op.
    origin: where it came from, e.g. {"arxiv": "2310.06770", "url": "..."}."""
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    paper_id = digest[:16]
    d = paper_dir(paper_id)
    if not pdf_path(paper_id).exists():
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, pdf_path(paper_id))
        info = {"filename": filename, "sha256": digest}
        info.update({k: v for k, v in (origin or {}).items() if k in ("arxiv", "doi", "url") and v})
        (d / "source.json").write_text(json.dumps(info), "utf-8")
    return paper_id


def source_info(paper_id: str) -> dict:
    p = paper_dir(paper_id) / "source.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def load_paper(paper_id: str) -> dict | None:
    p = json_path(paper_id)
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def save_paper(paper_id: str, paper: dict) -> None:
    # Write-then-rename so a cloud-sync client never sees a half-written file.
    p = json_path(paper_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(paper, ensure_ascii=False), "utf-8")
    os.replace(tmp, p)


_paper_lock = threading.Lock()


def update_paper(paper_id: str, fn) -> dict:
    """Read-modify-write paper.json under a lock (the AI worker and user edits
    both write to it)."""
    with _paper_lock:
        paper = load_paper(paper_id)
        if paper is None:
            raise FileNotFoundError(paper_id)
        fn(paper)
        save_paper(paper_id, paper)
        return paper


def carry_over(old: dict, new: dict) -> None:
    """Keep AI labels and user edits across a re-parse. Sentence ids are
    renumbered when parsing changes, so match sentences by their text."""
    by_text = {}
    for sid, s in new["sentences"].items():
        by_text.setdefault(s["text"], sid)

    def remap(labels: dict) -> dict:
        out = {}
        for sid, v in labels.items():
            text = old["sentences"].get(sid, {}).get("text")
            if text in by_text:
                out[by_text[text]] = v
        return out

    new["ai_labels"] = remap(old.get("ai_labels", {}))
    # Q&A history: re-point each cited passage at its new first-sentence id.
    new["chat"] = old.get("chat", [])
    for entry in new["chat"]:
        for src in entry.get("sources", []):
            text = old["sentences"].get(src.get("sid"), {}).get("text")
            src["sid"] = by_text.get(text)
    new["user_edits"] = remap(old.get("user_edits", {}))
    new["explanations"] = remap(old.get("explanations", {}))
    new["float_explanations"] = old.get("float_explanations", {})  # keyed by image file
    new["translations"] = old.get("translations", {})  # keyed by text, not sentence id
    # Notes follow their sentence; a note whose sentence vanished keeps its
    # text (sid None) rather than being lost.
    new["notes"] = old.get("notes", [])
    for note in new["notes"]:
        note["sid"] = by_text.get(old["sentences"].get(note.get("sid"), {}).get("text"))
    new["reading_notes"] = old.get("reading_notes", "")
    # Questions: re-point their highlights and cited passages at the new ids.
    if old.get("insights"):
        new["insights"] = old["insights"]
        for q in new["insights"].get("questions", []):
            q["highlights"] = [x for x in (by_text.get(old["sentences"].get(h, {}).get("text")) for h in q.get("highlights", [])) if x]
            for src in q.get("sources", []):
                src["sid"] = by_text.get(old["sentences"].get(src.get("sid"), {}).get("text"))
    if old.get("status", {}).get("classified"):
        new["status"].update(classified=True, model=old["status"].get("model"),
                             highlight_version=old["status"].get("highlight_version", 1))


def load_recent() -> list[dict]:
    try:
        return json.loads(RECENT_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def touch_recent(paper_id: str, title: str) -> None:
    items = [r for r in load_recent() if r["id"] != paper_id]
    items.insert(0, {"id": paper_id, "title": title, "opened": time.time()})
    LIBRARY.mkdir(parents=True, exist_ok=True)
    RECENT_FILE.write_text(json.dumps(items[:RECENT_MAX], ensure_ascii=False), "utf-8")


def load_position(paper_id: str) -> dict:
    """Where the reader last was: {sid, offset, percent, updated}. Kept in its
    own small file so saving it while scrolling never rewrites paper.json."""
    try:
        return json.loads((paper_dir(paper_id) / "position.json").read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_position(paper_id: str, pos: dict) -> None:
    p = paper_dir(paper_id) / "position.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pos), "utf-8")
    os.replace(tmp, p)


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict) -> None:
    LIBRARY.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2), "utf-8")
    os.replace(tmp, SETTINGS_FILE)
