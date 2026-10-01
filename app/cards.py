"""Flashcards and spaced-repetition review.

Cards are written by the local model from what it already knows about the
paper: its summary, its key highlights (with margin notes) and the answered
Questions. Each card is a question on the front and a short answer on the
back, tied to the highlights it tests, so review can jump back to the paper.
The reader can also add, edit and delete cards.

Review uses a small SM-2 scheduler (the Anki family): each card has an ease,
an interval in days and a due time; grading it Again / Hard / Good / Easy sets
the next interval. Review state lives in the library's reviews.json, keyed
"<paper id>:<card id>", so cards from every paper can be reviewed together.
"""

import hashlib
import json
import math
import os
import threading
import time

from . import library, llm
from .insights import key_highlights

REVIEWS_FILE = library.LIBRARY / "reviews.json"
DAY = 86400.0
GRADES = ("again", "hard", "good", "easy")
_lock = threading.Lock()

SYSTEM = """You make flashcards for a researcher who read the paper "{title}" and wants to remember what matters in it.
Write {n} cards. Each tests one thing worth remembering: the core idea, a key design decision and why it was made, a key result (with its number), a definition, a limitation, or an insight that connects ideas. Mix the kinds; favour understanding ("why", "how", "what happens if") over trivia, but include the numbers that matter.
- front: a question that makes sense on its own, months later: name the method, benchmark or concept (never "this paper" alone, never "the authors" without saying who or what). At most 30 words.
- back: the answer, short and complete: 1-3 sentences, at most 60 words.
- kind: concept, method, result, definition, limitation or insight.
- hl: the numbers of the highlights the card rests on (may be empty).
Reply with JSON only: {{"cards": [...]}}"""

SCHEMA = {
    "type": "object",
    "properties": {"cards": {"type": "array", "items": {"type": "object", "properties": {
        "front": {"type": "string"}, "back": {"type": "string"},
        "kind": {"type": "string", "enum": ["concept", "method", "result", "definition", "limitation", "insight"]},
        "hl": {"type": "array", "items": {"type": "integer"}},
    }, "required": ["front", "back", "kind", "hl"]}}},
    "required": ["cards"],
}
N_CARDS = 15


def card_id(front: str) -> str:
    return hashlib.sha1(front.strip().lower().encode("utf-8")).hexdigest()[:10]


def generate(paper: dict, progress=lambda s, f: None) -> dict:
    S = paper["sentences"]
    hl = key_highlights(paper)
    labels = paper.get("ai_labels", {})
    lines = []
    for i, sid in enumerate(hl):
        note = labels.get(sid, {}).get("note")
        lines.append(f"[{i + 1}] {S[sid]['text']}" + (f"\n     note: {note}" if note else ""))
    parts = []
    s = paper.get("summary")
    if s:
        parts.append("Your summary:\n" + "\n".join(x for x in [
            f"TL;DR: {s['tldr']}", f"Problem: {s['problem']}", f"Approach: {s['approach']}",
            *("Result: " + r["text"] for r in s["results"]), *("Limitation: " + r["text"] for r in s["limitations"])] if x))
    elif paper.get("reading_notes"):
        parts.append("Your reading notes:\n" + paper["reading_notes"])
    qs = [q for q in (paper.get("insights") or {}).get("questions", []) if q.get("status") == "answered"]
    if qs:
        parts.append("Questions you answered about it:\n" + "\n".join(f"Q: {q['q']}\nA: {q['answer'][:500]}" for q in qs[:8]))
    parts.append("Your highlights:\n" + ("\n".join(lines) or "(none)"))
    if not hl and not s:
        raise ValueError("This paper has no highlights or summary yet to make cards from.")

    progress("Writing flashcards", 0.1)
    messages = [{"role": "system", "content": SYSTEM.format(title=paper["meta"]["title"], n=N_CARDS)},
                {"role": "user", "content": "\n\n".join(parts)}]
    raw = llm.chat(messages, schema=SCHEMA, temperature=0.4, max_tokens=9000, think=True)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(llm.chat(messages, schema=SCHEMA, temperature=0.4, max_tokens=4000))
    cards, seen = [], set()
    now = time.time()
    for c in data.get("cards", []):
        front, back = str(c.get("front", "")).strip(), str(c.get("back", "")).strip()
        if not front or not back or card_id(front) in seen:
            continue
        seen.add(card_id(front))
        sids = [hl[n - 1] for n in c.get("hl", []) if isinstance(n, int) and 1 <= n <= len(hl)]
        cards.append({"id": card_id(front), "front": front, "back": back, "kind": c.get("kind", "concept"),
                      "highlights": list(dict.fromkeys(sids))[:2], "by": "ai", "created": now})
    if not cards:
        raise ValueError("The model didn't write any cards; try again.")
    # Keep the reader's own cards when the AI's are rewritten.
    mine = [c for c in (paper.get("cards") or {}).get("cards", []) if c.get("by") == "you"]
    return {"cards": cards + [c for c in mine if c["id"] not in seen], "at": now, "model": llm.model_name()}


# ---- review state

def load_reviews() -> dict:
    try:
        return json.loads(REVIEWS_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_reviews(r: dict) -> None:
    tmp = REVIEWS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(r), "utf-8")
    os.replace(tmp, REVIEWS_FILE)


def schedule(state: dict | None, grade: str, now: float | None = None) -> dict:
    """SM-2 with Anki-style grades, in days. Again: see it again in a minute
    (this session) and start over. A new card: Hard or Good = 1 day, Easy = 4
    days; the second Good = 3 days. After that the interval grows by x1.2
    (Hard), x ease (Good) or x ease x1.3 (Easy); Hard and Easy also nudge the
    ease down or up."""
    now = time.time() if now is None else now
    s = dict(state or {"ease": 2.5, "interval": 0.0, "reps": 0, "lapses": 0})
    ease, interval, reps = s["ease"], s["interval"], s["reps"]
    if grade == "again":
        s.update(ease=max(1.3, ease - 0.2), interval=0.0, reps=0, lapses=s["lapses"] + 1, due=now + 60)
    else:
        if reps == 0:
            interval = {"hard": 1.0, "good": 1.0, "easy": 4.0}[grade]
        elif reps == 1 and grade == "good":
            interval = 3.0
        else:
            interval = max(interval + 1, interval * {"hard": 1.2, "good": ease, "easy": ease * 1.3}[grade])
        ease = max(1.3, ease + {"hard": -0.15, "good": 0.0, "easy": 0.15}[grade])
        s.update(ease=round(ease, 2), interval=round(interval, 2), reps=reps + 1, due=now + interval * DAY)
    s["last"] = now
    s.setdefault("first", now)  # when the card was first seen (the daily limit on new cards)
    return s


def preview(state: dict | None) -> dict[str, float]:
    """Seconds until the card would be due again, for each grade (button labels)."""
    now = time.time()
    return {g: schedule(state, g, now)["due"] - now for g in GRADES}


def grade(paper_id: str, cid: str, g: str) -> dict:
    if g not in GRADES:
        raise ValueError("bad grade")
    with _lock:
        r = load_reviews()
        key = f"{paper_id}:{cid}"
        r[key] = schedule(r.get(key), g)
        _save_reviews(r)
        return r[key]


def forget(paper_id: str, cid: str | None = None) -> None:
    """Drop review state for one card (or every card of a paper)."""
    with _lock:
        r = load_reviews()
        prefix = f"{paper_id}:"
        r = {k: v for k, v in r.items() if not (k == f"{paper_id}:{cid}" if cid else k.startswith(prefix))}
        _save_reviews(r)


NEW_PER_DAY = 20


def due(papers: list[dict], limit: int = 200) -> dict:
    """Cards due now across the given papers (each {"id", "title", "cards"}):
    cards seen before whose due time has passed, then new cards (at most 20
    new a day, so a fresh library isn't a wall). Returns the queue and counts."""
    now = time.time()
    r = load_reviews()
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    started_today = sum(1 for st in r.values() if st.get("first", 0) >= midnight)
    old, new, total, next_due = [], [], 0, None
    for p in papers:
        for c in p["cards"]:
            total += 1
            st = r.get(f"{p['id']}:{c['id']}")
            item = {**c, "paper": p["id"], "title": p["title"], "state": st, "next": preview(st)}
            if st is None:
                new.append(item)
            elif st["due"] <= now:
                old.append(item)
            else:
                next_due = min(next_due or math.inf, st["due"])
    old.sort(key=lambda c: c["state"]["due"])
    queue = (old + new[:max(0, NEW_PER_DAY - started_today)])[:limit]
    return {"queue": queue, "due": len(old), "new": len(new), "total": total, "next_due": next_due}
