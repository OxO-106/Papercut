"""Ask questions about a paper: a small retrieval-augmented setup.

Retrieval: each paragraph/caption is a passage; BM25 ranks them against the
question (plus the previous question, so follow-ups work). Passages holding
highlighted sentences of the kind the question asks about ("limitations",
"results", ...) get a boost. The abstract is always included for context.

Generation: the local model answers from the numbered passages and cites
them as [n]; the reader turns citations into links to the paper. When the
question needs more than the paper (related or later work, what a cited
paper did, a concept the paper doesn't explain), the model can call the
research tools in research.py; their results are cited as [S1], [S2], ...
"""

import math
import re
import time
from collections import Counter
from typing import Iterator

from . import embed, library, llm, research

TOP_K = 8
HISTORY_TURNS = 3

_STOP = set("""a an and are as at be been being but by can could did do does for from had has have how i if in into is it its
its of on or our should so such than that the their them then there these they this those to was we were what when where
which while who why will with would you your about also between both each more most other over same some very""".split())

_INTENT = {
    "limitation": r"limit|weak|drawback|fail|shortcoming|caveat|future|threat|problem with",
    "result": r"result|perform|accura|score|find|found|outperform|improv|better|worse|number|metric|evaluat",
    "novelty": r"novel|contribut|new|differ|unique|innovat",
    "method": r"method|approach|how does|how do|work|architect|design|pipeline|algorithm|implement|train",
    "objective": r"goal|aim|objective|purpose|motivat|why|problem|question",
    "definition": r"defin|what is|what are|what does .* mean|meaning|term",
}

SYSTEM = """You answer questions about the academic paper "{title}".
Each question comes with numbered passages from the paper; cite them after each claim, like [2] or [2][5].
You can also look beyond the paper with tools:
- search_papers: find papers on a topic (related work, alternatives, background); citing_papers: later work that cites this paper; get_reference: what the paper's reference [n] is.
{web}
For later or related work, combine tools: citing_papers, then search_papers with specific terms (e.g. the paper's benchmark or method name plus what the question asks about), and compare what you find with this paper. Several searches are fine.
Use the passages first. Use tools when the question needs more: related or later work, what a cited paper did, or a concept, method or tool the paper uses but doesn't explain well. Don't use tools when the passages answer the question.
Tool results are numbered [S1], [S2], ...; cite them the same way. Never write [S1] or any [S..] citation unless a tool returned that source in this conversation.
If neither the passages nor the tools answer the question, say so plainly and don't guess.
Length: by default a short paragraph or a few bullet points starting with "- ". When asked to explain a concept in depth, give a complete, well-structured explanation (several paragraphs are fine). No headings."""
WEB_LINE = "- web_search: search the web for explanations of concepts, methods and tools (encyclopedias, documentation, tutorials); web_fetch: read one page."
NO_WEB_LINE = "- (General web search is not set up, so explain concepts from the papers you can find and from the paper itself.)"
MAX_TOOL_ROUNDS = 4

# Questions that need outside sources: the model is told to search first
# (without this nudge it often answers concept questions from memory).
_NEEDS_SEARCH = re.compile(
    r"explain\b.*\b(thorough|in depth|in detail|detailed|fully|from scratch)|(thorough|in-depth|in depth|detailed) explanation"
    r"|related work|related papers|similar (work|papers|approaches)|other (papers|work|approaches)|prior work|earlier work"
    r"|later work|follow[- ]?up|since (then|this paper)|built on|cite[sd]? (this|the paper)|state of the art now|latest"
    r"|what (did|does) (ref(erence)?|\[)\s*\d+|reference \[?\d+\]?|\[\d+\] (find|show|do|propose)"
    r"|search (the )?(web|online|internet)|look (it|this|that) up|look up",
    re.I,
)
SEARCH_NUDGE = ("\n\n(This question needs sources beyond the paper: use your tools to search before answering, "
                "and base the answer on what you find.)")


def _stem(w: str) -> str:
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)] + rep
    return w


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1]


def passages(paper: dict) -> list[dict]:
    """[{"sid", "heading", "text", "sentences"}] in reading order: main text,
    captions and appendix; not front matter or references."""
    out, heading = [], ""
    S = paper["sentences"]
    for b in paper["blocks"]:
        if b.get("region") not in ("body", "appendix"):
            continue
        if b["type"] == "heading":
            heading = b["text"]
            continue
        ids = b.get("sentences") if b["type"] in ("paragraph", "list_item", "caption", "footnote") else b.get("caption_sentences")
        if ids:
            out.append({"sid": ids[0], "heading": heading, "sentences": ids, "text": " ".join(S[i]["text"] for i in ids)})
    return out


def _labels(paper: dict) -> dict[str, str]:
    out = {sid: l["category"] for sid, l in paper.get("ai_labels", {}).items() if _labels_kept(l)}
    for sid, e in paper.get("user_edits", {}).items():
        if e.get("category"):
            out[sid] = e["category"]
        else:
            out.pop(sid, None)
    return out


def _bm25(ps: list[dict], query: str) -> list[float]:
    docs = [_tokens(p["heading"] + " " + p["text"]) for p in ps]
    avg = sum(map(len, docs)) / len(docs)
    df = Counter(t for d in docs for t in set(d))
    n = len(docs)
    q = _tokens(query)
    scores = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scores.append(s)
    return scores


def embed_passages(paper: dict, ps: list[dict] | None = None):
    """Passage vectors (computed once, then cached in the paper's folder)."""
    ps = passages(paper) if ps is None else ps
    return embed.passage_vectors(paper["id"], [p["heading"] + "\n" + p["text"] for p in ps])


def _semantic(paper: dict, ps: list[dict], query: str) -> list[float] | None:
    """Cosine similarity from the embedding model; None if it's unavailable."""
    try:
        return (embed_passages(paper, ps) @ embed.query_vector(query)).tolist()
    except Exception:
        return None  # e.g. model not pulled: keyword search still works


def retrieve(paper: dict, query: str, k: int = TOP_K) -> list[dict]:
    """Hybrid search: BM25 and embedding rankings merged by reciprocal rank
    fusion, plus a boost for passages holding highlights of the kind the
    question asks about. The abstract is always included."""
    ps = passages(paper)
    if not ps:
        return []
    n = len(ps)
    lexical = _bm25(ps, query)
    rankings = [lexical]
    semantic = _semantic(paper, ps, query)
    if semantic is not None:
        rankings.append(semantic)

    scores = [0.0] * n
    for ranking in rankings:
        order = sorted(range(n), key=lambda i: -ranking[i])
        for rank, i in enumerate(order):
            scores[i] += 1 / (60 + rank)

    wanted = {cat for cat, pat in _INTENT.items() if re.search(pat, query, re.I)}
    if wanted:
        labels = _labels(paper)
        for i, p in enumerate(ps):
            if any(labels.get(sid) in wanted for sid in p["sentences"]):
                scores[i] += 0.5 / 60  # about half a top-ranked vote

    ranked = sorted(range(n), key=lambda i: -scores[i])
    relevant = [i for i in ranked if lexical[i] > 0 or semantic is not None]
    chosen = set(relevant[:k])
    abstract = next((i for i, p in enumerate(ps) if re.match(r"\s*abstract", p["heading"], re.I)), 0)
    chosen.add(abstract)  # always give the model the paper's summary
    return [ps[i] for i in sorted(chosen)]  # reading order reads better than score order


def _labels_kept(l: dict) -> bool:
    return l.get("tier") is not None if "tier" in l else l["confidence"] >= 0.7


def answer(paper: dict, question: str, history: list[dict], search: bool = False,
           save: bool = True, extra: str = "") -> Iterator[dict]:
    """Yield {"type": "sources"} (paper passages), then any number of
    {"type": "step"} (a tool being used), {"type": "web"} (outside sources so
    far), {"type": "token"} and {"type": "discard"} (drop the text streamed so
    far: the model went on to use a tool), then {"type": "done"}.
    search=True (the page's Search button) makes the model search first, as
    does a question that plainly asks for outside sources. `extra` adds
    instructions after the question; save=False leaves the chat alone (the
    Questions feature uses both). The last event before "done" is
    {"type": "final", "entry": ...}: the answer as it would be saved."""
    prev = [h for h in history if h.get("q") and h.get("a")][-HISTORY_TURNS:]
    query = question + " " + (prev[-1]["q"] if prev else "")
    found = retrieve(paper, query)
    sources = [{"n": i + 1, "sid": p["sid"], "heading": p["heading"], "text": p["text"][:240]} for i, p in enumerate(found)]
    yield {"type": "sources", "sources": sources}

    box = research.Toolbox(paper)
    listing = "\n\n".join(f"[{i + 1}] ({p['heading'] or 'Untitled section'}) {p['text']}" for i, p in enumerate(found))
    system = SYSTEM.format(title=paper["meta"]["title"], web=WEB_LINE if box.web else NO_WEB_LINE)
    messages = [{"role": "system", "content": system}]
    for h in prev:
        messages += [{"role": "user", "content": h["q"]}, {"role": "assistant", "content": h["a"]}]
    wants_search = search or bool(_NEEDS_SEARCH.search(question))
    nudge = SEARCH_NUDGE if wants_search else ""
    messages.append({"role": "user", "content": f"Passages from the paper:\n\n{listing}\n\nQuestion: {question}{nudge}{extra}"})

    steps: list[str] = []
    text: list[str] = []
    for round_ in range(MAX_TOOL_ROUNDS + 1):
        tools = box.tools() if round_ < MAX_TOOL_ROUNDS else []  # last round: answer with what it has
        text, calls = [], None
        # Think (a) on the first turn when a search is wanted: without it the
        # model often skips the tools and answers from memory; (b) once outside
        # sources are in: otherwise it garbles details it half-remembers
        # (tested on BM25's k1).
        think = bool(box.sources) or (wants_search and round_ == 0)
        for kind, value in llm.stream_turn(messages, tools, max_tokens=6000 if think else 2500, think=think):
            if kind == "thinking":
                continue
            if kind == "token":
                text.append(value)
                yield {"type": "token", "text": value}
            else:
                calls = value
        if not calls:
            break
        if text:
            yield {"type": "discard"}
        messages.append({"role": "assistant", "content": "".join(text),
                         "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]})
        for c in calls:
            label = box.describe(c["name"], c["arguments"])
            steps.append(label)
            yield {"type": "step", "text": label}
            messages.append({"role": "tool", "tool_name": c["name"], "content": box.run(c["name"], c["arguments"])})
        yield {"type": "web", "sources": box.sources}

    # Drop [S..] citations of sources that don't exist (the model sometimes invents them).
    real = {s["n"] for s in box.sources}
    final = re.sub(r"\s*\[(S\d+)\]", lambda m: m.group(0) if m.group(1) in real else "", "".join(text)).strip()
    entry = {"q": question, "a": final, "sources": sources, "at": time.time()}
    if box.sources:
        entry["web"] = box.sources
    if steps:
        entry["steps"] = steps

    def save_entry(p):
        p.setdefault("chat", []).append(entry)
        p["chat"] = p["chat"][-50:]

    if save:
        library.update_paper(paper["id"], save_entry)
    yield {"type": "final", "entry": entry}
    yield {"type": "done"}
