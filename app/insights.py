"""Questions: insightful questions about the highlighted parts, with answers.

1. Ask   - the local model (thinking on) looks at its own key highlights
           (tiers 1-2, with its margin notes and reading notes) and writes the
           questions a sharp reader would ask about them: why it works, what it
           assumes, how it compares, whether it generalizes, what it implies,
           what could undermine it, background a reader may lack. Each is tied
           to the highlights it's about and says where an answer most likely
           is: elsewhere in the paper, in other papers, or on the web.
2. Answer - each question goes through Ask's machinery (paper passages, plus
           paper and web search when the answer lies outside the paper), with a
           short answer and an honest status: answered, partly answered, open.

Stored in paper.json as "insights"; re-running replaces them.
"""

import json
import re
import time
from typing import Callable

from . import ask, library, llm

KINDS = ["mechanism", "assumption", "comparison", "generalization", "implication", "weakness", "background"]
LOOK = ["paper", "literature", "web"]

ASK_SYSTEM = """You are a sharp, curious researcher who has just read the paper "{title}" closely. Your reading notes:
---
{notes}
---
Below are the key sentences you highlighted, with your margin notes. Come up with the insightful questions a thoughtful reader would ask about them: questions that deepen understanding, test the claims, or connect the work to the wider field. Not questions the highlighted sentence itself already answers, and not trivia.
Mix these kinds:
- mechanism: why or how does it work?
- assumption: what must hold for this to be true or to work?
- comparison: how does it compare with alternatives or prior work?
- generalization: would it hold in other settings, scales, domains?
- implication: what follows from it; what does it enable or change?
- weakness: what could undermine the claim or the evidence?
- background: a concept or method the reader needs, which the paper doesn't explain well.
For each question give: q (self-contained: name the method, result or concept rather than saying "this"; never mention highlight numbers in the question), highlights (the numbers of the 1 or 2 highlights it is about), kind, and look: where an answer most likely is ("paper" = elsewhere in this paper, "literature" = other research papers, "web" = general background knowledge).
Write {n} questions, spread across the paper's main points; favour the ones most worth thinking about.
Reply with JSON only: {{"questions": [...]}}"""

ASK_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "highlights": {"type": "array", "items": {"type": "integer"}},
            "kind": {"type": "string", "enum": KINDS},
            "look": {"type": "string", "enum": LOOK},
        },
        "required": ["q", "highlights", "kind", "look"],
    }}},
    "required": ["questions"],
}

ANSWER_NOTE = """

(Answer this in at most about 150 words, citing your sources. Then, on its own last line, write exactly one of:
Status: answered
Status: partly answered
Status: open
Use "open" when neither the paper nor the sources you found settle the question; say briefly what is known.)"""

N_QUESTIONS = 10
# "(Highlight [21])", "(Highlights [8] and [11])": the model's own numbering, meaningless to the reader.
_HL_REF = re.compile(r"\s*\(?\b(?:see )?highlights?\s*(?:\[\d+\](?:\s*(?:,|and|&)\s*)?)+\)?", re.I)
_STATUS = re.compile(r"\n?\s*\**status:\s*(answered|partly answered|open)\**\s*\.?\s*$", re.I)


def key_highlights(paper: dict) -> list[str]:
    """Sentence ids of the AI's tier 1-2 highlights (plus the user's own), in reading order."""
    labels = paper.get("ai_labels", {})
    keep = {sid for sid, l in labels.items() if l.get("tier") in (1, 2)}
    if not keep:  # old-style labels: the most confident ones
        keep = {sid for sid, l in sorted(labels.items(), key=lambda kv: -kv[1]["confidence"])[:25]}
    for sid, e in paper.get("user_edits", {}).items():
        (keep.add if e.get("category") else keep.discard)(sid)
    order = {sid: i for i, sid in enumerate(paper["sentences"])}
    return sorted((s for s in keep if s in order), key=order.get)


def generate(paper: dict, progress: Callable[[str, float], None] = lambda s, f: None) -> dict:
    """Come up with questions about the key highlights and answer them."""
    S = paper["sentences"]
    hl = key_highlights(paper)
    if not hl:
        raise ValueError("This paper has no highlights yet; run the AI highlights first.")
    labels = paper.get("ai_labels", {})
    lines = []
    for i, sid in enumerate(hl):
        note = labels.get(sid, {}).get("note")
        lines.append(f"[{i + 1}] {S[sid]['text']}" + (f"\n     note: {note}" if note else ""))

    progress("Coming up with questions", 0.02)
    system = ASK_SYSTEM.format(title=paper["meta"]["title"], notes=paper.get("reading_notes") or "(none)", n=N_QUESTIONS)
    raw = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}],
                   schema=ASK_SCHEMA, temperature=0.4, max_tokens=10000, think=True)
    try:
        items = json.loads(raw).get("questions", [])
    except json.JSONDecodeError:
        items = []
    questions = []
    for it in items[:N_QUESTIONS + 2]:
        q = _HL_REF.sub("", str(it.get("q", ""))).strip()
        if not q:
            continue
        sids = [hl[i - 1] for i in it.get("highlights", []) if isinstance(i, int) and 1 <= i <= len(hl)]
        questions.append({"q": q, "kind": it.get("kind") if it.get("kind") in KINDS else "mechanism",
                          "look": it.get("look") if it.get("look") in LOOK else "paper", "highlights": sids[:2]})
    if not questions:
        raise ValueError("The model didn't come up with any questions; try again.")

    for k, item in enumerate(questions):
        progress(f"Answering question {k + 1} of {len(questions)}", 0.1 + 0.9 * k / len(questions))
        entry = None
        for ev in ask.answer(paper, item["q"], [], search=item["look"] != "paper", save=False, extra=ANSWER_NOTE):
            if ev["type"] == "final":
                entry = ev["entry"]
        text = (entry or {}).get("a", "")
        m = _STATUS.search(text)
        item["status"] = m.group(1).lower() if m else "partly answered"
        item["answer"] = _STATUS.sub("", text).strip()
        for field in ("sources", "web", "steps"):
            if entry and entry.get(field):
                item[field] = entry[field]
    return {"questions": questions, "at": time.time(), "model": llm.model_name()}
