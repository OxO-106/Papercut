"""AI highlights: the local model reads the paper, then marks it up.

Three passes, like a careful human reader:
  1. Read   - the model reads the whole main text (thinking on) and writes
              reading notes: problem, core idea, contributions, key results,
              surprises, limitations, key terms.
  2. Mark   - it re-reads section by section, paragraph structure visible,
              with its notes and the highlights it already made, and marks the
              sentences that carry the essence of the paper, each with a
              category, an importance and a short margin note.
  3. Choose - it looks at all its marks together (thinking on), merges marks
              that make the same point, drops routine ones and sorts the rest
              into tiers: 1 = the essence, 2 = key support, 3 = worth noting.
The tier and importance become the confidence the density slider ranks by;
marks dropped in pass 3 are kept at the bottom of the ranking, so only a high
density shows them.

The model never writes text we show as the paper: highlights are always the
parser's own sentences. Unknown numbers or categories are dropped.
"""

import json
from typing import Callable

from . import llm

CATEGORIES = ["objective", "novelty", "method", "result", "limitation", "definition"]
CHUNK = 45  # sentences per marking request, in whole paragraphs
VERSION = 2  # stored in status; 1 = the old one-pass classifier

READ_SYSTEM = """You are an expert reader of research papers. Read the paper below carefully and think about what it really says.
Then write your reading notes: the notes a thoughtful researcher would keep after a close reading.
Cover, concisely: the problem and why it matters; the core idea; the specific contributions; the key results (with the numbers that matter); what is surprising or non-obvious; the limitations; key terms the paper defines.
Plain text, at most 350 words."""

MARK_SYSTEM = """You are re-reading the paper "{title}" with a highlighter, after a first full read. Your reading notes on the whole paper:
---
{notes}
---
Now you see one section, split into paragraphs with numbered sentences. Highlight the sentences a reader must stop and think about: the ones that carry the essence of the paper. A sentence earns a highlight if it states the problem or goal, the core idea, a real contribution, a key design decision, a key result (especially with numbers), a surprising or non-obvious insight, an important limitation, or a key definition.
Be selective, like a careful reader with a pen:
- Most paragraphs get zero or one highlight; two only when both are clearly essential. Highlighting a whole paragraph is almost never right.
- Background, related work, setup detail and routine description usually get nothing.
- Don't highlight a sentence that repeats a point already highlighted (listed below); highlight a repetition only if it adds a number or a sharper claim.
For each highlight give:
- id: the sentence number
- category: objective (goal/problem), novelty (what is new), method (key design decision), result (finding or outcome), limitation (caveat, failure, future work), definition (key term)
- importance: 5 = the essence of the paper, belongs in a one-paragraph summary; 4 = key evidence or step behind a main claim; 3 = notable, worth remembering
- note: your margin note, at most 15 words: why this sentence matters (don't restate it)
Reply with JSON only: {{"highlights": [...]}} (an empty list is fine)."""

MARK_SCHEMA = {
    "type": "object",
    "properties": {"highlights": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "category": {"type": "string", "enum": CATEGORIES},
            "importance": {"type": "integer", "enum": [3, 4, 5]},
            "note": {"type": "string"},
        },
        "required": ["id", "category", "importance", "note"],
    }}},
    "required": ["highlights"],
}

CHOOSE_SYSTEM = """You are finishing your annotation of the paper "{title}". Your reading notes:
---
{notes}
---
While re-reading section by section you marked the candidate highlights below (in reading order, with your margin notes).
Now look at them all together and decide what stays, as a careful reader would:
1. Find candidates that make the same point (e.g. the same headline result stated in the abstract, introduction and results). Keep only the single best one of each group: the most specific and informative (numbers beat vague wording). Drop the rest.
2. Drop candidates that are routine description rather than essence.
3. Keep every other candidate that is genuinely meaningful, insightful or important. There is no quota: a long or dense paper may keep many, a short one few. What matters is that each kept sentence earns its place.
4. Give every kept candidate a tier:
   1 = the essence: if a reader read only these, they would know what the paper did, found and why it matters.
   2 = key supporting points: important evidence, design decisions, insights and limitations.
   3 = worth noting.
Reply with JSON only: {{"keep": [{{"id": <number>, "tier": 1|2|3}}]}}. Candidates not listed are dropped."""

CHOOSE_SCHEMA = {
    "type": "object",
    "properties": {"keep": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "tier": {"type": "integer", "enum": [1, 2, 3]}},
        "required": ["id", "tier"],
    }}},
    "required": ["keep"],
}

# Confidence the density slider ranks by: tier first, then the pass-2 importance.
TIER_BASE = {1: 0.9, 2: 0.75, 3: 0.6, None: 0.3}  # None = dropped in pass 3


def sections(paper: dict) -> list[tuple[str, list[tuple[str, list[str]]]]]:
    """(heading, [(kind, sentence ids) per paragraph]) for the main text,
    including figure/table captions. Front matter, footnotes, references and
    appendices are skipped."""
    out: list[tuple[str, list]] = []
    heading = "Abstract"
    for b in paper["blocks"]:
        if b.get("region") != "body":
            continue
        if b["type"] == "heading":
            heading = b["text"]
            out.append((heading, []))
            continue
        if b["type"] in ("paragraph", "list_item"):
            kind, ids = "paragraph", b["sentences"]
        elif b["type"] == "caption":
            kind, ids = "caption", b["sentences"]
        elif b["type"] in ("figure", "table"):
            kind, ids = f"{b['type']} caption", b.get("caption_sentences", [])
        else:
            continue
        if not ids:
            continue
        if not out:
            out.append((heading, []))
        out[-1][1].append((kind, ids))
    return [(h, paras) for h, paras in out if paras]


def _json(raw: str, key: str) -> list:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data.get(key, []) if isinstance(data, dict) else []
    return items if isinstance(items, list) else []


def read(paper: dict, secs) -> str:
    """Pass 1: reading notes on the whole main text."""
    S = paper["sentences"]
    text = "\n\n".join(f"## {h}\n" + "\n".join(" ".join(S[s]["text"] for s in ids) for _, ids in paras)
                       for h, paras in secs)
    return llm.chat(
        [{"role": "system", "content": READ_SYSTEM},
         {"role": "user", "content": f"Paper: {paper['meta']['title']}\n\n{text}"}],
        temperature=0.3, max_tokens=8000, think=True,
    ).strip()


def mark(paper: dict, secs, notes: str, progress) -> dict:
    """Pass 2: candidate highlights, section by section."""
    S = paper["sentences"]
    system = MARK_SYSTEM.format(title=paper["meta"]["title"], notes=notes or "(no notes)")
    chunks = []
    for heading, paras in secs:
        cur: list = []
        for para in paras:
            if cur and sum(len(ids) for _, ids in cur) + len(para[1]) > CHUNK:
                chunks.append((heading, cur))
                cur = []
            cur.append(para)
        if cur:
            chunks.append((heading, cur))

    marks: dict[str, dict] = {}
    order: list[str] = []
    for k, (heading, paras) in enumerate(chunks):
        progress(f"Highlighting ({k}/{len(chunks)} parts)", k / len(chunks))
        ids, lines = [], []
        for kind, pids in paras:
            lines.append(f"¶ ({kind})")
            for sid in pids:
                ids.append(sid)
                lines.append(f"[{len(ids)}] {S[sid]['text']}")
            lines.append("")
        done = "\n".join(f"- {S[s]['text'][:160]}" for s in order[-25:]) or "(none yet)"
        user = f"Section: {heading}\n\nAlready highlighted earlier:\n{done}\n\n" + "\n".join(lines)
        raw = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                       schema=MARK_SCHEMA, temperature=0.2, max_tokens=1500)
        for item in _json(raw, "highlights"):
            try:
                i, cat, imp = int(item["id"]), str(item["category"]).lower(), int(item["importance"])
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= i <= len(ids) and cat in CATEGORIES:
                sid = ids[i - 1]
                marks[sid] = {"category": cat, "importance": max(3, min(5, imp)),
                              "note": str(item.get("note", "")).strip()[:200]}
                order.append(sid)
    return marks


def choose(paper: dict, marks: dict, notes: str) -> dict[str, int]:
    """Pass 3: {sentence id: tier} for the marks that stay."""
    S = paper["sentences"]
    position = {sid: i for i, sid in enumerate(S)}
    heading, section_of = "Abstract", {}
    for b in paper["blocks"]:
        if b["type"] == "heading":
            heading = b["text"]
        for sid in b.get("sentences", []) + b.get("caption_sentences", []):
            section_of[sid] = heading
    cands = sorted(marks, key=lambda s: position.get(s, 0))
    lines = [f"[{i + 1}] ({section_of.get(s, '')[:40]}; {marks[s]['category']}) {S[s]['text']}\n     note: {marks[s]['note']}"
             for i, s in enumerate(cands)]
    messages = [{"role": "system", "content": CHOOSE_SYSTEM.format(title=paper["meta"]["title"], notes=notes or "(no notes)")},
                {"role": "user", "content": "\n".join(lines)}]
    # Think first when there's room. With many candidates (e.g. 204 in a long
    # paper) the reasoning can use up the whole budget before any answer is
    # written; then decide again without thinking (the answer itself is short).
    budget = llm.NUM_CTX - len("\n".join(lines) + CHOOSE_SYSTEM + (notes or "")) // 3 - 1000
    raw = llm.chat(messages, schema=CHOOSE_SCHEMA, temperature=0.2, max_tokens=max(4000, min(20000, budget)), think=True)
    if not _json(raw, "keep"):
        raw = llm.chat(messages, schema=CHOOSE_SCHEMA, temperature=0.2, max_tokens=6000)
    tiers = {}
    for item in _json(raw, "keep"):
        try:
            i, tier = int(item["id"]), int(item["tier"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= i <= len(cands) and tier in (1, 2, 3):
            tiers[cands[i - 1]] = tier
    return tiers


def classify_paper(paper: dict, progress: Callable[[str, float], None] = lambda s, f: None) -> tuple[dict, str]:
    """Return ({sentence_id: {category, confidence, tier, note}}, reading notes)."""
    secs = sections(paper)
    progress("Reading the paper", 0.0)
    notes = read(paper, secs)

    marks = mark(paper, secs, notes, lambda s, f: progress(s, 0.2 + 0.55 * f))

    progress("Choosing what matters most", 0.8)
    try:
        tiers = choose(paper, marks, notes) if marks else {}
    except Exception:
        tiers = {}
    if marks and not tiers:
        # Pass 3 failed twice: keep only what pass 2 rated important (5 -> tier 1,
        # 4 -> tier 2); importance-3 marks were never judged, so they're dropped.
        tiers = {sid: {5: 1, 4: 2}[m["importance"]] for sid, m in marks.items() if m["importance"] >= 4}

    labels = {}
    for sid, m in marks.items():
        tier = tiers.get(sid)
        conf = TIER_BASE[tier] + 0.02 * (m["importance"] - 3)
        labels[sid] = {"category": m["category"], "confidence": round(conf, 3), "tier": tier, "note": m["note"]}
    return labels, notes
