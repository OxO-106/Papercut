"""One-page summary: a cheat sheet for a paper, written from the AI's own reading.

The model gets its reading notes, its key highlights (tiers 1-2, with margin
notes), the abstract, the section outline and the front matter, and writes:
TL;DR, problem, approach, key results, contributions, limitations and open
questions. Every bullet points back at the highlights it rests on, so the page
can jump to them. It also reads off the bibliographic details (authors, year,
venue) and a few topic tags, which the library uses to organise papers.

Stored in paper.json as "summary"; re-running replaces it.
"""

import json
import re
import time

from . import ask, library, llm
from .insights import key_highlights

SYSTEM = """You have just read the paper "{title}" closely and annotated it. Your reading notes:
---
{notes}
---
Write a one-page summary of the paper for someone who wants to remember it: a cheat sheet, not an abstract rewrite.
Be concrete: name the method, the datasets or benchmarks, and give the numbers that matter. Plain sentences, no marketing words.
Fields:
- tldr: one or two sentences: what the paper did and what it found.
- problem: the problem and why it matters (2-3 sentences).
- approach: the core idea and how it works (3-5 sentences).
- results: 3-5 key results, each with numbers where the paper gives them.
- contributions: 2-4 contributions, each in one sentence.
- limitations: 1-4 limitations or caveats (stated by the authors or evident).
- open_questions: 2-3 questions the paper leaves open.
For each item in results, contributions and limitations, "hl" lists the numbers of the highlights it rests on (may be empty).
- authors: the author names from the front matter (at most 6, then "et al."), or "" if not given.
- year: the publication year if stated or evident (e.g. from an arXiv id or venue), else "".
- venue: the conference or journal if stated, else "".
- topics: 3-6 short topic tags a researcher would file this paper under, most specific first (e.g. "LLM agents", "program repair", "benchmarks").
Reply with JSON only."""

_ITEMS = {"type": "array", "items": {"type": "object", "properties": {
    "text": {"type": "string"}, "hl": {"type": "array", "items": {"type": "integer"}}}, "required": ["text", "hl"]}}
SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"}, "problem": {"type": "string"}, "approach": {"type": "string"},
        "results": _ITEMS, "contributions": _ITEMS, "limitations": _ITEMS,
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "authors": {"type": "string"}, "year": {"type": "string"}, "venue": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tldr", "problem", "approach", "results", "contributions", "limitations", "open_questions",
                 "authors", "year", "venue", "topics"],
}


def _front_text(paper: dict, limit: int = 1500) -> str:
    """Title, authors and affiliations: the text before the abstract."""
    S = paper["sentences"]
    parts = []
    for b in paper["blocks"]:
        if b.get("region") != "front":
            break
        if b.get("sentences"):
            parts.append(" ".join(S[s]["text"] for s in b["sentences"]))
        elif b.get("text"):
            parts.append(b["text"])
    return "\n".join(parts)[:limit]


def _outline(paper: dict) -> str:
    heads = [b["text"] for b in paper["blocks"] if b["type"] == "heading" and b.get("region") == "body"]
    return "\n".join(heads[:40])


def _abstract(paper: dict) -> str:
    for p in ask.passages(paper):
        if re.match(r"\s*abstract", p["heading"], re.I):
            return p["text"][:2500]
    ps = ask.passages(paper)
    return ps[0]["text"][:2500] if ps else ""


def generate(paper: dict, progress=lambda s, f: None) -> dict:
    S = paper["sentences"]
    hl = key_highlights(paper)
    labels = paper.get("ai_labels", {})
    lines = []
    for i, sid in enumerate(hl):
        note = labels.get(sid, {}).get("note")
        lines.append(f"[{i + 1}] {S[sid]['text']}" + (f"\n     note: {note}" if note else ""))
    src = library.source_info(paper["id"])
    ident = ", ".join(f"{k}: {src[k]}" for k in ("arxiv", "doi") if src.get(k))
    user = (f"Front matter:\n{_front_text(paper)}\n\n" + (f"Identifiers: {ident}\n\n" if ident else "")
            + f"Abstract:\n{_abstract(paper)}\n\nSections:\n{_outline(paper)}\n\n"
            + "Your highlights:\n" + ("\n".join(lines) or "(none)"))
    system = SYSTEM.format(title=paper["meta"]["title"], notes=paper.get("reading_notes") or "(none)")
    progress("Writing the summary", 0.1)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    raw = llm.chat(messages, schema=SCHEMA, temperature=0.3, max_tokens=9000, think=True)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:  # thinking used up the budget: answer directly
        data = json.loads(llm.chat(messages, schema=SCHEMA, temperature=0.3, max_tokens=4000))

    def items(key):
        out = []
        for it in data.get(key) or []:
            text = str(it.get("text", "")).strip()
            if text:
                sids = [hl[n - 1] for n in it.get("hl", []) if isinstance(n, int) and 1 <= n <= len(hl)]
                out.append({"text": text, "highlights": list(dict.fromkeys(sids))[:3]})
        return out

    topics = []
    for t in data.get("topics") or []:
        t = re.sub(r"\s+", " ", str(t)).strip().strip(".")
        if t and t.lower() not in (x.lower() for x in topics):
            topics.append(t[:40])
    return {
        "tldr": str(data.get("tldr", "")).strip(),
        "problem": str(data.get("problem", "")).strip(),
        "approach": str(data.get("approach", "")).strip(),
        "results": items("results"),
        "contributions": items("contributions"),
        "limitations": items("limitations"),
        "open_questions": [str(q).strip() for q in data.get("open_questions") or [] if str(q).strip()][:4],
        "authors": str(data.get("authors", "")).strip()[:300],
        "year": (re.search(r"(19|20)\d{2}", str(data.get("year", ""))) or [""])[0],
        "venue": str(data.get("venue", "")).strip()[:120],
        "topics": topics[:6],
        "at": time.time(),
        "model": llm.model_name(),
    }
