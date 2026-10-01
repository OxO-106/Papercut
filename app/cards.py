"""Flashcards and spaced-repetition review.

Cards are written by the local model from what it already knows about the
paper, as many as are worth having (no fixed number): its summary, its key highlights (with margin notes) and the answered
Questions. Each card is a question on the front and a short answer on the
back, tied to the highlights it tests, so review can jump back to the paper.
The reader can also add, edit and delete cards.

Review is on demand, one paper at a time: nothing is scheduled or pushed.
Grades still matter: a small SM-2 scheduler (the Anki family) keeps an ease
and an interval per card, and a review session puts the weakest cards first
(those whose interval has run out, then new ones, then the rest). Review
state lives in the library's reviews.json, keyed "<paper id>:<card id>".
"""

import hashlib
import json
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
Write a card for everything in the paper that is genuinely worth remembering, and nothing else. There is no target number: a rich paper may deserve many cards, a thin one only a few. Never pad with trivia or near-duplicates to reach a count; every card must be meaningful and insightful on its own.
Each card tests one thing worth remembering: the core idea, a key design decision and why it was made, a key result (with its number), a definition, a limitation, or an insight that connects ideas. Mix the kinds; favour understanding ("why", "how", "what happens if") over trivia, but include the numbers that matter.
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
    messages = [{"role": "system", "content": SYSTEM.format(title=paper["meta"]["title"])},
                {"role": "user", "content": "\n\n".join(parts)}]
    raw = llm.chat(messages, schema=SCHEMA, temperature=0.4, max_tokens=14000, think=True)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(llm.chat(messages, schema=SCHEMA, temperature=0.4, max_tokens=7000))
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


def session(paper_id: str, title: str, deck: list[dict]) -> dict:
    """Every card of one paper, for a review whenever the reader wants one
    (nothing is scheduled or pushed). Weakest first: cards whose interval has
    run out (most overdue first), then cards never reviewed, then the rest
    (the soonest due first)."""
    now = time.time()
    r = load_reviews()
    lapsed, new, later = [], [], []
    for c in deck:
        st = r.get(f"{paper_id}:{c['id']}")
        item = {**c, "paper": paper_id, "title": title, "state": st}
        (new if st is None else lapsed if st["due"] <= now else later).append(item)
    lapsed.sort(key=lambda c: c["state"]["due"])
    later.sort(key=lambda c: c["state"]["due"])
    return {"queue": lapsed + new + later, "total": len(deck)}


def deck_info(paper_id: str, deck: list[dict]) -> dict:
    """For the list of decks: how many cards, how many reviewed, and when."""
    r = load_reviews()
    states = [r[k] for k in (f"{paper_id}:{c['id']}" for c in deck) if k in r]
    return {"cards": len(deck), "reviewed": len(states),
            "last": max((st.get("last", 0) for st in states), default=None) or None}
