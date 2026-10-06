"""The library as a whole: an index of every paper, the reader's own shelf
data (status, tags, course, collection), AI collections, and connections between
papers (search across all papers, related papers, citations within the library).

Files in the library folder:
  shelf.json      {paper id: {"status", "tags", "course", "collection"}}: the reader's edits
  collections.json {"collections": [{"name", "about"}], "assign": {id: name}, "at"}: the AI's grouping
Per paper, the index keeps the few fields the library page needs, cached in
memory by paper.json's modification time.
"""

import json
import os
import re
import threading
import time

import numpy as np

from . import ask, embed, library, llm

SHELF_FILE = library.LIBRARY / "shelf.json"
COLLECTIONS_FILE = library.LIBRARY / "collections.json"
STATUSES = ("to-read", "reading", "done")
_lock = threading.Lock()


def _read(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, data) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


# ---- the index

_cache: dict[str, tuple[float, dict]] = {}


def paper_ids() -> list[str]:
    if not library.PAPERS_DIR.exists():
        return []
    return [d.name for d in library.PAPERS_DIR.iterdir()
            if d.is_dir() and d.name.isalnum() and (d / "original.pdf").exists()]


def _year(paper: dict) -> str:
    y = (paper.get("summary") or {}).get("year")
    if y:
        return y
    arxiv = (paper.get("source") or {}).get("arxiv") or ""
    m = re.match(r"(\d{2})(\d{2})\.", arxiv)
    return f"20{m.group(1)}" if m else ""


def _entry(pid: str) -> dict | None:
    """The fields of one paper the library needs (cached until paper.json changes)."""
    path = library.json_path(pid)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    hit = _cache.get(pid)
    if hit and hit[0] == mtime:
        return hit[1]
    p = library.load_paper(pid)
    if not p:
        return None
    s = p.get("summary") or {}
    labels = p.get("ai_labels", {})
    e = {
        "id": pid,
        "title": p["meta"]["title"],
        "pages": p["meta"].get("pages"),
        "authors": s.get("authors", ""),
        "year": _year(p),
        "venue": s.get("venue", ""),
        "tldr": s.get("tldr", ""),
        "topics": s.get("topics", []),
        "has_summary": bool(s),
        "highlights": sum(1 for l in labels.values() if l.get("tier") is not None) if any("tier" in l for l in labels.values()) else len(labels),
        "notes": len(p.get("notes", [])),
        "questions": len((p.get("insights") or {}).get("questions", [])),
        "cards": len((p.get("cards") or {}).get("cards", [])),
        "classified": p.get("status", {}).get("classified", False),
        "source": p.get("source", {}),
        # For citation links between library papers (not sent to the page).
        "_refs": [(b["id"], b["text"]) for b in p["blocks"] if b["type"] == "references"],
        "_links": p.get("ref_links", {}),
    }
    _cache[pid] = (mtime, e)
    return e


def load_shelf() -> dict:
    return _read(SHELF_FILE, {})


def update_shelf(pid: str, patch: dict) -> dict:
    """Set a paper's status, tags, course or collection (None clears)."""
    with _lock:
        shelf = load_shelf()
        cur = shelf.setdefault(pid, {})
        if "status" in patch:
            if patch["status"] not in STATUSES + (None,):
                raise ValueError("bad status")
            cur["status"] = patch["status"]
        if "tags" in patch:
            tags = []
            for t in patch["tags"] or []:
                t = re.sub(r"\s+", " ", str(t)).strip()[:40]
                if t and t.lower() not in (x.lower() for x in tags):
                    tags.append(t)
            cur["tags"] = tags[:20]
        if "course" in patch:
            course = re.sub(r"\s+", " ", str(patch["course"] or "")).strip()[:40]
            # Same course, same spelling: "cse 517" files under an existing "CSE 517".
            known = {c.lower(): c for c in courses()}
            cur["course"] = known.get(course.lower(), course) or None
        if "collection" in patch:
            cur["collection"] = (str(patch["collection"]).strip()[:60] or None) if patch["collection"] else None
        cur = {k: v for k, v in cur.items() if v not in (None, [], "")}
        if cur:
            shelf[pid] = cur
        else:
            shelf.pop(pid, None)
        _write(SHELF_FILE, shelf)
        return cur


def courses() -> list[str]:
    """Every course a paper is filed under, in alphabetical order."""
    return sorted({v["course"] for v in load_shelf().values() if v.get("course")}, key=str.lower)


def forget(pid: str) -> None:
    """Drop a removed paper from the shelf, the collections and the caches."""
    with _lock:
        shelf = load_shelf()
        if shelf.pop(pid, None) is not None:
            _write(SHELF_FILE, shelf)
        cols = load_collections()
        if cols.get("assign", {}).pop(pid, None) is not None:
            _write(COLLECTIONS_FILE, cols)
    _cache.pop(pid, None)
    _vec_cache.pop(pid, None)


def load_collections() -> dict:
    return _read(COLLECTIONS_FILE, {"collections": [], "assign": {}})


def entries() -> list[dict]:
    """Every processed paper with shelf data, collection, progress and when it
    was added and last opened."""
    shelf = load_shelf()
    cols = load_collections()
    opened = {r["id"]: r.get("opened") for r in library.load_recent()}
    out = []
    for pid in paper_ids():
        e = _entry(pid)
        if not e:  # still processing: list it by file name
            src = library.source_info(pid)
            e = {"id": pid, "title": src.get("filename", pid), "processing": True, "topics": []}
        e = {k: v for k, v in e.items() if not k.startswith("_")}
        mine = shelf.get(pid, {})
        e["tags"] = mine.get("tags", [])
        e["course"] = mine.get("course")
        e["collection"] = mine.get("collection") or cols["assign"].get(pid)
        e["collection_set_by"] = "you" if mine.get("collection") else ("ai" if cols["assign"].get(pid) else None)
        e["percent"] = library.load_position(pid).get("percent")
        e["opened"] = opened.get(pid)
        try:
            e["added"] = library.pdf_path(pid).stat().st_mtime
        except FileNotFoundError:
            e["added"] = None
        # Status: the reader's choice, else from progress.
        pct = e["percent"] or 0
        e["status"] = mine.get("status") or ("done" if pct >= 95 else "reading" if e["opened"] and pct >= 1 else "to-read")
        e["status_set"] = bool(mine.get("status"))
        out.append(e)
    return out


# ---- AI collections

ORGANIZE_SYSTEM = """You organise a researcher's personal library of academic papers into collections (like folders on a shelf).
Below is every paper: its title, one-line summary and topic tags.
Define a small set of collections that group the papers the way the researcher would look for them: 2 to 8 collections, each with a short name (1-4 words, e.g. "LLM agents for code", "Reasoning & RL") and a one-sentence description. Prefer meaningful research themes over generic ones ("Machine learning") and avoid a collection of one paper unless it really fits nowhere else.
{keep}Then put every paper in exactly one collection (its best fit).
Reply with JSON only: {{"collections": [{{"name", "about"}}], "assign": [{{"id": <paper number>, "collection": <name>}}]}}"""
KEEP_LINE = "The library already has these collections; keep them (you may add new ones if papers don't fit): {names}.\n"

ORGANIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "collections": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "about": {"type": "string"}}, "required": ["name", "about"]}},
        "assign": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "collection": {"type": "string"}}, "required": ["id", "collection"]}},
    },
    "required": ["collections", "assign"],
}


def organize(fresh: bool = False, progress=lambda s, f: None) -> dict:
    """Let the model group the library into collections. By default (the
    automatic run after a new paper's summary) the existing collections and
    every paper already in one stay as they are: only papers without a
    collection are filed, into an existing collection or a new one if none
    fits. fresh=True starts over and regroups everything."""
    papers = [e for e in (_entry(pid) for pid in paper_ids()) if e]
    if not papers:
        raise ValueError("The library is empty.")
    old = load_collections()
    lines = []
    for i, e in enumerate(papers):
        lines.append(f"[{i + 1}] {e['title']}\n    {e['tldr'] or '(no summary yet)'}\n    topics: {', '.join(e['topics']) or '-'}")
    keep = "" if fresh or not old["collections"] else KEEP_LINE.format(names=", ".join(c["name"] for c in old["collections"]))
    progress("Grouping papers into collections", 0.2)
    messages = [{"role": "system", "content": ORGANIZE_SYSTEM.format(keep=keep)},
                {"role": "user", "content": "\n".join(lines)}]
    raw = llm.chat(messages, schema=ORGANIZE_SCHEMA, temperature=0.2, max_tokens=8000, think=True)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(llm.chat(messages, schema=ORGANIZE_SCHEMA, temperature=0.2, max_tokens=3000))
    keep_old = not fresh and bool(old["collections"])
    cols, seen = [], set()
    if keep_old:
        for c in old["collections"]:
            seen.add(c["name"].lower())
            cols.append(c)
    for c in data.get("collections", []):
        name = re.sub(r"\s+", " ", str(c.get("name", ""))).strip()[:60]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            cols.append({"name": name, "about": str(c.get("about", "")).strip()[:200]})
    by_lower = {c["name"].lower(): c["name"] for c in cols}
    ids = {e["id"] for e in papers}
    assign = {pid: c for pid, c in old["assign"].items() if pid in ids} if keep_old else {}
    for a in data.get("assign", []):
        i, name = a.get("id"), str(a.get("collection", "")).strip().lower()
        if isinstance(i, int) and 1 <= i <= len(papers) and name in by_lower:
            assign.setdefault(papers[i - 1]["id"], by_lower[name])  # papers already filed stay put
    used = set(assign.values())
    result = {"collections": [c for c in cols if c["name"] in used], "assign": assign, "at": time.time(),
              "model": llm.model_name()}
    with _lock:
        _write(COLLECTIONS_FILE, result)
    return result


def unassigned() -> list[str]:
    """Papers with a summary but no collection yet (the AI hasn't seen them)."""
    assign = load_collections()["assign"]
    shelf = load_shelf()
    return [e["id"] for e in (_entry(pid) for pid in paper_ids())
            if e and e["has_summary"] and e["id"] not in assign and not shelf.get(e["id"], {}).get("collection")]


# ---- search across the library

_vec_cache: dict[str, tuple[float, list[dict], np.ndarray | None]] = {}


def _passages(pid: str):
    """(passages, unit vectors or None) for one paper, cached by paper.json's mtime."""
    try:
        mtime = library.json_path(pid).stat().st_mtime
    except FileNotFoundError:  # removed while searching
        return [], None
    hit = _vec_cache.get(pid)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    paper = library.load_paper(pid)
    ps = ask.passages(paper)
    vecs = None
    if ps:
        try:
            vecs = ask.embed_passages(paper, ps)
        except Exception:
            vecs = None  # embedding model unavailable: keyword search only
    _vec_cache[pid] = (mtime, ps, vecs)
    return ps, vecs


def search(query: str, k: int = 30) -> dict:
    """Passages across every paper that answer `query` (meaning, via the
    embedding model, plus keywords), and papers whose title, tags or summary
    match. Returns {"papers": [...], "passages": [...]}."""
    query = query.strip()
    if not query:
        return {"papers": [], "passages": []}
    try:
        qv = embed.query_vector(query)
    except Exception:
        qv = None
    qtok = set(ask._tokens(query))
    hits = []
    titles = {}
    for pid in paper_ids():
        e = _entry(pid)
        if not e:
            continue
        titles[pid] = e["title"]
        ps, vecs = _passages(pid)
        if not ps:
            continue
        sem = (vecs @ qv) if (vecs is not None and qv is not None) else np.zeros(len(ps))
        for i, p in enumerate(ps):
            toks = set(ask._tokens(p["heading"] + " " + p["text"]))
            kw = len(qtok & toks) / max(1, len(qtok))
            hits.append((float(sem[i]) + 0.25 * kw, kw, pid, p))
    hits.sort(key=lambda h: -h[0])
    passages, per_paper = [], {}
    for score, kw, pid, p in hits:
        if len(passages) >= k:
            break
        if qv is None and kw == 0:
            break
        if per_paper.get(pid, 0) >= 4:  # don't let one paper fill the page
            continue
        per_paper[pid] = per_paper.get(pid, 0) + 1
        passages.append({"paper": pid, "title": titles[pid], "sid": p["sid"], "heading": p["heading"],
                         "text": p["text"][:400], "score": round(score, 3)})
    # Papers: their best passage score, plus a boost for matches in title/tags/summary.
    shelf = load_shelf()
    best: dict[str, float] = {}
    for score, kw, pid, p in hits:
        best[pid] = max(best.get(pid, -1), score)
    papers = []
    nq = norm(query)
    for pid, title in titles.items():
        e = _entry(pid)
        meta = norm(" ".join([title, e["authors"], e["tldr"], " ".join(e["topics"]),
                              " ".join(shelf.get(pid, {}).get("tags", []))]))
        direct = nq in meta or (qtok and qtok <= set(ask._tokens(meta)))
        papers.append({"id": pid, "title": title, "score": round(best.get(pid, 0) + (0.5 if direct else 0), 3),
                       "match": "title" if nq and nq in norm(title) else ("details" if direct else "content")})
    papers.sort(key=lambda x: -x["score"])
    return {"papers": papers[:10], "passages": passages}


def _paper_vector(pid: str) -> np.ndarray | None:
    ps, vecs = _passages(pid)
    if vecs is None or not len(vecs):
        return None
    v = vecs.mean(axis=0)
    return v / max(np.linalg.norm(v), 1e-9)


def related(pid: str, k: int = 5) -> list[dict]:
    """Other papers in the library closest in content (mean passage embedding),
    with the topic tags they share."""
    me = _paper_vector(pid)
    if me is None:
        return []
    mine = {t.lower() for t in (_entry(pid) or {}).get("topics", [])}
    out = []
    for other in paper_ids():
        if other == pid:
            continue
        v = _paper_vector(other) if library.json_path(other).exists() else None
        if v is None:
            continue
        e = _entry(other)
        out.append({"id": other, "title": e["title"], "score": round(float(me @ v), 3),
                    "shared": [t for t in e["topics"] if t.lower() in mine]})
    out.sort(key=lambda x: -x["score"])
    return out[:k]


def _references(paper: dict) -> list[dict]:
    return [b for b in paper["blocks"] if b["type"] == "references"]


def _title_in(title: str, ref_text: str) -> bool:
    t = norm(title)
    return len(t.split()) >= 3 and t in norm(ref_text)


def connections(pid: str) -> dict:
    """How this paper connects to the rest of the library:
    cites     - its reference entries that are papers in the library
    cited_by  - library papers whose reference lists include this paper
    related   - the closest papers in content."""
    paper = library.load_paper(pid)
    if not paper:
        return {"cites": [], "cited_by": [], "related": []}
    links = paper.get("ref_links", {})
    others = {o: _entry(o) for o in paper_ids() if o != pid}
    others = {o: e for o, e in others.items() if e}
    cites = []
    for n, b in enumerate(_references(paper), 1):
        target = links.get(b["id"])
        if not (target and target in others):
            target = next((o for o, e in others.items() if _title_in(e["title"], b["text"])), None)
        if target:
            cites.append({"block": b["id"], "n": n, "id": target, "title": others[target]["title"]})
    cited_by = []
    me = _entry(pid)
    for o, e in others.items():
        hit = next((n for n, (bid, text) in enumerate(e["_refs"], 1)
                    if e["_links"].get(bid) == pid or _title_in(me["title"], text)), None)
        if hit:
            cited_by.append({"id": o, "title": e["title"], "n": hit})
    return {"cites": cites, "cited_by": cited_by, "related": related(pid)}
