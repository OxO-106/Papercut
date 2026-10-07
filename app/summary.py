"""The summary: a paper explained to someone who hasn't read it.

Written in two passes, both by the local model:
  1. Core    - from the paper's main text, the AI's reading notes and key
               highlights: in brief, background with key terms, objective,
               what's new, method, key results, limitations; plus the
               bibliographic details (authors, year, venue) and topic tags,
               which the library uses to organise papers.
  2. Context - from the core, the paper's related-work text and reference
               list, the library papers connected to it, and later papers
               that cite it (OpenAlex; arXiv when OpenAlex has nothing):
               related work, future research, open questions with proposed
               answers, a note on how each library paper relates, and which
               later papers matter and why.
Bullets point back at the highlights (and related work at the reference
entries) they rest on, so the page can jump to them.

Stored in paper.json as "summary" (version 2); re-running replaces it.
"""

import json
import re
import time

import httpx

from . import ask, catalog, library, llm, research
from .highlight import sections
from .insights import key_highlights

VERSION = 2
MAX_TEXT = 50000  # characters of main text given to the core pass (~12k tokens)

LATEX = "Write any formula or symbol in LaTeX between dollar signs, e.g. $L \\propto N^{{-0.076}}$, $\\alpha = 0.5$, $x_{{t-1}}$."

CORE_SYSTEM = """You have just read the paper "{title}" closely and annotated it. Your reading notes:
---
{notes}
---
Write a detailed summary of the paper for a reader who has NOT read it and may be new to the field. After reading only your summary they should understand what the paper did, how, what it found, and why it matters, well enough to explain it to someone else, without opening the paper. This is a full explanation, not a list of headlines: every point is a few complete sentences that give the specifics and explain them. Aim for 1000-1500 words in total across the fields below; a short summary fails the reader.
Be concrete: name the method and its parts, the models and their sizes, the datasets or benchmarks and their sizes, the baselines, the hyperparameters that matter, and give the numbers. Explain jargon when it first appears. Plain sentences, no marketing words, and don't repeat a point across fields.
""" + LATEX + """
Fields:
- in_brief: three or four sentences a newcomer understands: the problem, what the paper did, what it found, and why that matters.
- background: what a newcomer needs to know first, explained step by step: the area, how things worked before this paper, the ideas and methods it builds on, and what was missing (6-10 sentences).
- key_terms: the terms, symbols and acronyms the paper relies on (usually 5-10), each with a plain definition of one or two sentences that a newcomer understands.
- objective: the question or gap the paper addresses, why it matters, and what the authors set out to show (3-5 sentences).
- novelty: what is new in this paper (usually 3-5). Each point is 2-3 sentences: what is new, what earlier work did instead, and why the difference matters.
- method: how the work was done, as ordered steps a newcomer can follow (usually 4-8). Each step is 2-4 sentences with the specifics: what is done, how it works, the models, sizes, data, training details, baselines, and how results are measured.
- results: the key findings (usually 4-8). Each is 2-3 sentences: the finding with its numbers, what it is compared against, and what it means. Interpret numbers exactly as the paper does (an exponent is not a percentage).
- limitations: limitations and caveats (usually 3-5). Each is 1-3 sentences: the limitation, whether the authors state it or it is your own observation, and why it matters.
For each item in novelty, method, results and limitations, "hl" lists the numbers of the highlights it rests on (may be empty).
- authors: the author names from the front matter (at most 6, then "et al."), or "" if not given.
- year: the publication year if stated or evident (e.g. from an arXiv id or venue), else "".
- venue: the conference or journal if stated, else "".
- topics: 3-6 short topic tags a researcher would file this paper under, most specific first (e.g. "LLM agents", "program repair", "benchmarks").
Reply with JSON only."""

CONTEXT_SYSTEM = """You are finishing a summary of the paper "{title}" for a reader who has not read it. The first part, already written:
---
{core}
---
Now place the paper in its field, using what is given below: the paper's own related-work text, its reference list (R1, R2, ...), papers in the reader's library connected to it (L1, L2, ...), and later papers that cite it or follow on from it (W1, W2, ...).
""" + LATEX + """
Fields:
Each point is a few complete sentences that a reader who hasn't read the paper can follow.
- related_work: the main lines of earlier work the paper builds on or argues against (usually 3-6). Each is 2-4 sentences: what that work did, and how this paper builds on it or differs from it. Name the work by its authors or method, never by citation keys or numbers like "[BB01]" or "[12]": "refs" carries those, as the numbers of the reference entries it is about (R3 -> 3).
- future_research: concrete research someone could do next (usually 3-5), each 2-3 sentences: the direction, why the results or limitations open it up (or which later work has started it), and how one might begin.
- open_questions: questions the paper leaves unresolved (usually 3-4), each with a proposed answer of 3-5 sentences: your best reasoned answer or hypothesis and why, how one could test it, and what later work says if it bears on it.
- library: for every library paper listed (L1, L2, ...), "n" (L3 -> 3), its "title" copied exactly, and one or two sentences on how it relates to this paper (builds on it, competes with it, applies it, shares a method, ...), based on its own summary given there. Empty if none are listed.
- later_work: for every later paper listed (W1, W2, ...), "n" (W2 -> 2), "title" copied exactly, "keep" (true only if it builds directly on this paper, tests or extends its findings, or challenges them; false for papers that merely cite it in passing), and one sentence on what it does in relation to this paper. Empty if none are listed.
Reply with JSON only."""

_ITEMS = {"type": "array", "items": {"type": "object", "properties": {
    "text": {"type": "string"}, "hl": {"type": "array", "items": {"type": "integer"}}}, "required": ["text", "hl"]}}
CORE_SCHEMA = {
    "type": "object",
    "properties": {
        "in_brief": {"type": "string"}, "background": {"type": "string"},
        "key_terms": {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"}, "definition": {"type": "string"}}, "required": ["term", "definition"]}},
        "objective": {"type": "string"},
        "novelty": _ITEMS, "method": _ITEMS, "results": _ITEMS, "limitations": _ITEMS,
        "authors": {"type": "string"}, "year": {"type": "string"}, "venue": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["in_brief", "background", "key_terms", "objective", "novelty", "method", "results", "limitations",
                 "authors", "year", "venue", "topics"],
}
_NOTES = {"type": "array", "items": {"type": "object", "properties": {
    "n": {"type": "integer"}, "title": {"type": "string"}, "note": {"type": "string"}}, "required": ["n", "title", "note"]}}
CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "related_work": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "refs": {"type": "array", "items": {"type": "integer"}}}, "required": ["text", "refs"]}},
        "future_research": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "answer": {"type": "string"}}, "required": ["question", "answer"]}},
        "library": _NOTES,
        "later_work": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"}, "title": {"type": "string"}, "keep": {"type": "boolean"}, "note": {"type": "string"}},
            "required": ["n", "title", "keep", "note"]}},
    },
    "required": ["related_work", "future_research", "open_questions", "library", "later_work"],
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


def _main_text(paper: dict, limit: int = MAX_TEXT) -> str:
    """The main text with its section headings (no appendix), cut at `limit`."""
    S = paper["sentences"]
    text = "\n\n".join(f"## {h}\n" + "\n".join(" ".join(S[s]["text"] for s in ids) for _, ids in paras)
                       for h, paras in sections(paper))
    return text if len(text) <= limit else text[:limit] + "\n[... the rest of the paper is cut ...]"


_RELATED = re.compile(r"related|prior work|previous work|background|preliminar", re.I)


def _related_text(paper: dict, limit: int = 7000) -> str:
    """The paper's related-work (or background) sections; else its introduction."""
    ps = ask.passages(paper)
    picked = [p for p in ps if _RELATED.search(p["heading"])]
    if not picked:
        picked = [p for p in ps if re.search(r"introduction", p["heading"], re.I)]
    return "\n".join(p["text"] for p in picked)[:limit]


def _references(paper: dict) -> list[dict]:
    return [b for b in paper["blocks"] if b["type"] == "references"]


# LaTeX commands whose backslash JSON reads as an escape (\t \b \f \n \r):
# "\to" would arrive as a tab and "o". Doubled before parsing.
_TEX_ESCAPES = re.compile(r"(?<!\\)\\(?=(?:t(?:o|imes|au|heta|ext\w*|ilde|op|riangle)|b(?:eta|ar|f|ig\w*|oldsymbol|inom)"
                          r"|f(?:rac|orall)|n(?:abla|u|eq|e|ot|eg)|r(?:ho|ight\w*|angle|m))(?![A-Za-z]))")


# Citation keys or numbers the model copied into related work anyway
# ("[BB01, Goo01]", "[12, 14]"): the chips carry them.
_CITE_KEYS = re.compile(r"\s*\[(?:[A-Z][A-Za-z+]*\d{2}[a-z]?|R?\d{1,3})(?:\s*[,;]\s*(?:[A-Z][A-Za-z+]*\d{2}[a-z]?|R?\d{1,3}))*\]")


def _loads(raw: str) -> dict:
    return json.loads(_TEX_ESCAPES.sub(r"\\\\", raw))


def _ask(messages: list[dict], schema: dict) -> dict:
    raw = llm.chat(messages, schema=schema, temperature=0.3, max_tokens=14000, think=True)
    try:
        return _loads(raw)
    except json.JSONDecodeError:  # thinking used up the budget: answer directly
        return _loads(llm.chat(messages, schema=schema, temperature=0.3, max_tokens=8000))


def _which(item: dict, titles: list[str]) -> int | None:
    """The listed paper a note is about: by the title the model copied (its
    numbers get mixed up between lists), else by its number if that agrees."""
    said = research._norm(str(item.get("title", "")))
    normed = [research._norm(t) for t in titles]
    n = item.get("n")
    if isinstance(n, int) and 1 <= n <= len(titles) and (not said or said == normed[n - 1]
                                                          or said in normed[n - 1] or normed[n - 1] in said):
        return n - 1
    if said:
        hits = [i for i, t in enumerate(normed) if t == said or (len(said) > 15 and (said in t or t in said))]
        if len(hits) == 1:
            return hits[0]
    return None


def _later_work(paper: dict, title: str, year: str, progress) -> tuple[list[dict], str]:
    """Later papers to place this one against: those citing it (OpenAlex),
    else recent arXiv papers on the same subject. Returns (works, how found)."""
    progress("Finding later work that cites it", 0.45)
    try:
        works = research.citing_works(paper)
        if works:
            return works, "cites"
    except httpx.HTTPError:
        pass
    try:
        found = research.search_works(title, 8)
    except httpx.HTTPError:
        return [], ""
    me = research._norm(title)
    works = [w for w in found if research._norm(w["title"]) != me and (not year or w["year"] >= year)]
    return works[:6], "related"


def generate(paper: dict, progress=lambda s, f: None) -> dict:
    S = paper["sentences"]
    title = paper["meta"]["title"]
    hl = key_highlights(paper)
    labels = paper.get("ai_labels", {})
    lines = []
    for i, sid in enumerate(hl):
        note = labels.get(sid, {}).get("note")
        lines.append(f"[{i + 1}] {S[sid]['text']}" + (f"\n     note: {note}" if note else ""))
    src = library.source_info(paper["id"])
    ident = ", ".join(f"{k}: {src[k]}" for k in ("arxiv", "doi") if src.get(k))

    # ---- 1. core
    progress("Writing the summary", 0.05)
    user = (f"Front matter:\n{_front_text(paper)}\n\n" + (f"Identifiers: {ident}\n\n" if ident else "")
            + f"The paper:\n{_main_text(paper)}\n\n"
            + "Your highlights:\n" + ("\n".join(lines) or "(none)"))
    system = CORE_SYSTEM.format(title=title, notes=paper.get("reading_notes") or "(none)")
    core = _ask([{"role": "system", "content": system}, {"role": "user", "content": user}], CORE_SCHEMA)

    def items(data, key, cap=8):
        out = []
        for it in data.get(key) or []:
            text = str(it.get("text", "")).strip()
            if text:
                sids = [hl[n - 1] for n in it.get("hl", []) if isinstance(n, int) and 1 <= n <= len(hl)]
                out.append({"text": text, "highlights": list(dict.fromkeys(sids))[:3]})
        return out[:cap]

    topics = []
    for t in core.get("topics") or []:
        t = re.sub(r"\s+", " ", str(t)).strip().strip(".")
        if t and t.lower() not in (x.lower() for x in topics):
            topics.append(t[:40])
    year = (re.search(r"(19|20)\d{2}", str(core.get("year", ""))) or [""])[0]
    out = {
        "version": VERSION,
        "tldr": str(core.get("in_brief", "")).strip(),
        "background": str(core.get("background", "")).strip(),
        "key_terms": [{"term": str(k.get("term", "")).strip(), "definition": str(k.get("definition", "")).strip()}
                      for k in core.get("key_terms") or [] if str(k.get("term", "")).strip()][:8],
        "objective": str(core.get("objective", "")).strip(),
        "novelty": items(core, "novelty"),
        "method": items(core, "method", 10),
        "results": items(core, "results"),
        "limitations": items(core, "limitations"),
        "authors": str(core.get("authors", "")).strip()[:300],
        "year": year,
        "venue": str(core.get("venue", "")).strip()[:120],
        "topics": topics[:6],
    }

    # ---- 2. context: related work, what's next, connections
    refs = _references(paper)
    conn = catalog.connections(paper["id"])
    linked = {}
    for group in (conn["cites"], conn["cited_by"], [x for x in conn["related"] if x["score"] >= 0.6]):
        for x in group:
            linked.setdefault(x["id"], x["title"])
    lib = list(linked.items())
    later, found_how = _later_work(paper, title, year, progress)

    progress("Placing it among other work", 0.55)
    core_text = "\n".join(x for x in [
        f"In brief: {out['tldr']}", f"Objective: {out['objective']}",
        *("New: " + x["text"] for x in out["novelty"]), *("Result: " + x["text"] for x in out["results"]),
        *("Limitation: " + x["text"] for x in out["limitations"])] if x)

    def tldr_of(pid):
        e = catalog._entry(pid)
        return (e or {}).get("tldr") or ""

    parts = [f"The paper's related-work text:\n{_related_text(paper) or '(none)'}",
             "Reference list:\n" + ("\n".join(f"R{i}: {b['text'][:220]}" for i, b in enumerate(refs[:150], 1)) or "(none)"),
             "Library papers connected to it:\n" + ("\n".join(
                 f"L{i}: {t}" + (f"\n    {tldr_of(pid)[:400]}" if tldr_of(pid) else "") for i, (pid, t) in enumerate(lib, 1))
                 or "(none)"),
             ("Later papers that cite it" if found_how == "cites" else "Later papers on the same subject") + ":\n" + ("\n".join(
                 f"W{i}: {w['title']} ({', '.join(x for x in (w.get('authors'), w.get('year')) if x)}"
                 + (f"; cited by {w['cited_by']}" if w.get("cited_by") is not None else "") + ")"
                 + (f"\n    {w['abstract'][:600]}" if w.get("abstract") else "") for i, w in enumerate(later, 1))
                 or "(none)")]
    system = CONTEXT_SYSTEM.format(title=title, core=core_text)
    ctx = _ask([{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}], CONTEXT_SCHEMA)
    progress("Finishing", 0.95)

    out["related_work"] = []
    for it in ctx.get("related_work") or []:
        text = _CITE_KEYS.sub("", str(it.get("text", ""))).strip()
        if text:
            blocks = [refs[n - 1]["id"] for n in it.get("refs", []) if isinstance(n, int) and 1 <= n <= len(refs)]
            out["related_work"].append({"text": text, "refs": list(dict.fromkeys(blocks))[:4]})
    out["related_work"] = out["related_work"][:7]
    out["future_research"] = [str(x).strip() for x in ctx.get("future_research") or [] if str(x).strip()][:6]
    out["open_questions"] = [{"q": str(x.get("question", "")).strip(), "answer": str(x.get("answer", "")).strip()}
                             for x in ctx.get("open_questions") or [] if str(x.get("question", "")).strip()][:5]
    out["library_notes"] = {}
    for x in ctx.get("library") or []:
        i = _which(x, [t for _, t in lib])
        if i is not None and str(x.get("note", "")).strip():
            out["library_notes"][lib[i][0]] = str(x["note"]).strip()
    notes = {}
    for x in ctx.get("later_work") or []:
        i = _which(x, [w["title"] for w in later])
        if i is not None:
            notes[i + 1] = x
    out["later_work"] = [
        {k: w.get(k) for k in ("title", "year", "authors", "cited_by", "url")} | {"note": str(notes[i]["note"]).strip()}
        for i, w in enumerate(later, 1) if i in notes and notes[i].get("keep") and str(notes[i].get("note", "")).strip()]
    out["later_how"] = found_how
    out["at"] = time.time()
    out["model"] = llm.model_name()
    return out
