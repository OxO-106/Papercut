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
  3. Visuals - headline numbers, the paper's key figures, a results table
               and a chart; every number is checked against the paper's text.
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
    """Later papers to place this one against: those citing it (Semantic
    Scholar, else OpenAlex), else recent arXiv papers on the same subject.
    Returns (works, how found)."""
    progress("Finding later work that cites it", 0.45)
    for source in (research.s2_citing_works, research.citing_works):
        try:
            works = source(paper)
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
    return _context(paper, out, progress)


CONTEXT_KEYS = ("related_work", "future_research", "open_questions", "library_notes", "later_work", "later_how")


def refresh_context(paper: dict, progress=lambda s, f: None) -> dict:
    """Rewrite only the second pass (related work, what's next, connections)
    of a version-2 summary, keeping the first part as it is."""
    old = paper.get("summary") or {}
    if old.get("version") != VERSION:
        raise ValueError("This paper's summary is from before version 2: write it again instead.")
    return _context(paper, {k: v for k, v in old.items() if k not in CONTEXT_KEYS}, progress)


def _context(paper: dict, out: dict, progress) -> dict:
    """Pass 2, on top of the first part (`out`): related work, what's next,
    connections in the library and later work."""
    title, year = paper["meta"]["title"], out.get("year", "")
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
    locate_open_questions(paper, out)
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
    return _visuals(paper, out, progress)


def locate_open_questions(paper: dict, summary: dict) -> dict:
    """Point each open question at the sentence of the paper that raises it
    ("at", a sentence id), so the summary can link back to that place."""
    hl = key_highlights(paper)
    for q in summary.get("open_questions") or []:
        if isinstance(q, dict) and q.get("q"):
            sid = ask.raised_at(paper, q["q"], hl, q.get("answer", "")[:600])
            q["at"] = [sid] if sid else []
    return summary


def refresh_visuals(paper: dict, progress=lambda s, f: None) -> dict:
    """Add (or redo) only the third pass, the visuals, of a version-2 summary."""
    old = paper.get("summary") or {}
    if old.get("version") != VERSION:
        raise ValueError("This paper's summary is from before version 2: write it again instead.")
    return _visuals(paper, {k: v for k, v in old.items() if k != "visuals"}, progress)


VISUALS_SYSTEM = """You are adding visuals to a summary of the paper "{title}" for a reader who has not read it. The summary so far:
---
{core}
---
From the paper's text and its list of figures and tables (given below), pick and build what helps a reader see the results at a glance. Use ONLY numbers that are written in the text below, copied exactly as written (same digits and decimals); never compute, round, estimate or invent a number. Leave a field empty rather than guess.
Fields:
- key_numbers: the 3-4 headline numbers of the paper (a score, a speed-up, a size, an exponent), each with "value" (short, as written, with its unit, e.g. "12.5%", "0.076", "175B") and "label" (a few words saying what it measures, e.g. "SWE-bench Lite resolved, GPT-4").
- figures: the 1-2 figures or tables from the list that best show the main idea or result, each with "label" copied exactly from the list (e.g. "Figure 3", "Table 2") and "why": two sentences on what that one shows and how to read it (axes, what to compare).
- table: the paper's main quantitative comparison as a small table a reader would want side by side (methods vs baselines, settings, datasets): "title", "columns" (2-6 headers; the first names the rows), "rows" (2-8 rows, each one cell per column, numbers as written), "note" (one sentence: what the numbers are and which way is better). Empty columns and rows if the text doesn't give such numbers.
- chart: one chart of numbers that compare: "kind" "bar" (values across methods or settings) or "line" (a value as something grows, e.g. model size); "title"; "x_label"; "y_label"; "series": one per line or group, each "name" and "points" [{{"x": label, "y": number}}] with at least 3 points in total; "ours": the x label (bar) or series name (line) that is this paper's own method, or "". Empty series if the text has no such numbers.
Reply with JSON only."""

VISUALS_SCHEMA = {
    "type": "object",
    "properties": {
        "key_numbers": {"type": "array", "items": {"type": "object", "properties": {
            "value": {"type": "string"}, "label": {"type": "string"}}, "required": ["value", "label"]}},
        "figures": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "why": {"type": "string"}}, "required": ["label", "why"]}},
        "table": {"type": "object", "properties": {
            "title": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}, "note": {"type": "string"}},
            "required": ["title", "columns", "rows", "note"]},
        "chart": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["bar", "line"]}, "title": {"type": "string"},
            "x_label": {"type": "string"}, "y_label": {"type": "string"}, "ours": {"type": "string"},
            "series": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "points": {"type": "array", "items": {"type": "object", "properties": {
                    "x": {"type": "string"}, "y": {"type": "number"}}, "required": ["x", "y"]}}}, "required": ["name", "points"]}}},
            "required": ["kind", "title", "x_label", "y_label", "ours", "series"]},
    },
    "required": ["key_numbers", "figures", "table", "chart"],
}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    """The numbers written in a text, without thousands separators or trailing
    decimal zeros ("1,024" -> "1024", "0.050" -> "0.05")."""
    out = set()
    for n in _NUMBER.findall(text):
        n = n.replace(",", "")
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def _plain(y: float) -> str:
    """A number as the paper would write it: 12.0 -> "12", 0.0760 -> "0.076"."""
    return f"{y:.6f}".rstrip("0").rstrip(".") if isinstance(y, float) else str(y)


def _float_list(paper: dict) -> list[tuple[str, str]]:
    """(label, caption) of the main text's figures and tables, in order."""
    S, by = paper["sentences"], {b["id"]: b for b in paper["blocks"]}
    out = []
    for b in paper["blocks"]:
        if b["type"] not in ("figure", "table") or not b.get("image") or b.get("region") == "appendix":
            continue
        ids = b.get("caption_sentences") or by.get(b.get("caption_block"), {}).get("sentences", [])
        cap = " ".join(S[s]["text"] for s in ids)
        m = re.match(r"\s*(Figure|Fig\.?|Table)\s*(\d+)", cap, re.I)
        if m:
            out.append((("Table " if m[1].lower().startswith("t") else "Figure ") + m[2], cap))
    return out


def _visuals(paper: dict, out: dict, progress) -> dict:
    """Pass 3: headline numbers, the key figures, a results table and a chart.
    Every number must be written in the paper: anything the model can't point
    to there is dropped, so a table or chart never shows an invented value."""
    progress("Adding tables and charts", 0.96)
    S = paper["sentences"]
    floats = _float_list(paper)
    known = _numbers(" ".join(s["text"] for s in S.values()))
    ok = lambda text: _numbers(str(text)) <= known
    core_text = "\n".join(x for x in [
        f"In brief: {out.get('tldr', '')}", *("Result: " + x["text"] for x in out.get("results", []))] if x)
    user = (f"The paper:\n{_main_text(paper, 40000)}\n\nIts figures and tables:\n"
            + ("\n".join(f"{label}: {cap[:300]}" for label, cap in floats) or "(none)"))
    system = VISUALS_SYSTEM.format(title=paper["meta"]["title"], core=core_text)
    try:
        v = _ask([{"role": "system", "content": system}, {"role": "user", "content": user}], VISUALS_SCHEMA)
    except (json.JSONDecodeError, httpx.HTTPError):
        return out  # the summary stands without visuals

    vis = {}
    vis["key_numbers"] = [{"value": str(k["value"]).strip()[:24], "label": str(k.get("label", "")).strip()[:80]}
                          for k in v.get("key_numbers") or []
                          if _numbers(str(k.get("value", ""))) and ok(k["value"]) and str(k.get("label", "")).strip()][:4]
    figs, labels = [], {label.lower(): label for label, _ in floats}
    for f in v.get("figures") or []:
        said = re.sub(r"^fig\.?\s*(?:ure)?\s*", "figure ", str(f.get("label", "")).strip().lower())
        label = labels.get(re.sub(r"^tab\.?\s*(?:le)?\s*", "table ", said))
        if label and str(f.get("why", "")).strip() and label not in (x["label"] for x in figs):
            figs.append({"label": label, "why": str(f["why"]).strip()})
    vis["figures"] = figs[:2]

    t = v.get("table") or {}
    cols = [str(c).strip() for c in t.get("columns") or []][:6]
    rows = [[str(c).strip() for c in r][:len(cols)] for r in t.get("rows") or [] if isinstance(r, list) and len(r) >= len(cols)]
    # A column with a number the paper doesn't state goes; so does a row whose name does.
    keep = [0] + [i for i in range(1, len(cols)) if all(ok(r[i]) for r in rows)]
    cols = [cols[i] for i in keep]
    rows = [[r[i] for i in keep] for r in rows if ok(r[0])]
    if len(cols) >= 2 and len(rows) >= 2 and sum(bool(_numbers(c)) for r in rows for c in r[1:]) >= 2:
        vis["table"] = {"title": str(t.get("title", "")).strip(), "columns": cols, "rows": rows[:8],
                        "note": str(t.get("note", "")).strip()}

    c = v.get("chart") or {}
    series = []
    for sr in c.get("series") or []:
        pts = [{"x": str(p.get("x", "")).strip()[:40], "y": p["y"]} for p in sr.get("points") or []
               if isinstance(p.get("y"), (int, float)) and str(p.get("x", "")).strip() and ok(_plain(p["y"]))]
        if pts:
            series.append({"name": str(sr.get("name", "")).strip()[:60], "points": pts[:12]})
    series = series[:3]
    if c.get("kind") in ("bar", "line") and sum(len(sr["points"]) for sr in series) >= 3 \
            and (c["kind"] == "bar" or any(len(sr["points"]) >= 3 for sr in series)):
        vis["chart"] = {k: str(c.get(k, "")).strip() for k in ("kind", "title", "x_label", "y_label", "ours")} | {"series": series}
    out["visuals"] = vis
    return out
