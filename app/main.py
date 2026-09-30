import hashlib
import json
import logging
import mimetypes
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import ask, cards, catalog, explain, export, fetch, jobs, library, llm, markdown, refs, translate
from .config import LIBRARY, WEB_DIR
from .highlight import CATEGORIES, VERSION as HIGHLIGHTER_VERSION

mimetypes.add_type("application/manifest+json", ".webmanifest")  # installable-app manifest

app = FastAPI(title="Papercut")


class _QuietHealth(logging.Filter):
    """The tray icon checks /api/health every 5 s; keep that out of the log."""

    def filter(self, record):
        return "/api/health" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_QuietHealth())
library.PAPERS_DIR.mkdir(parents=True, exist_ok=True)


def _existing(paper_id: str) -> Path:
    try:
        d = library.paper_dir(paper_id)
    except ValueError:
        raise HTTPException(400, "bad paper id")
    if not d.exists():
        raise HTTPException(404, "paper not found")
    return d


@app.post("/api/papers")
async def upload(file: UploadFile):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "expected a PDF")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
    try:
        paper_id = library.ingest(Path(tmp.name), file.filename)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    jobs.submit(paper_id)
    return {"id": paper_id}


@app.post("/api/papers/from-link")
def from_link(body: dict = Body(...)):
    """Download a paper from an arXiv ID/URL, DOI, OpenReview or PDF link, then process it."""
    try:
        info = fetch.resolve(str(body.get("link", "")))
        path = fetch.download(info["pdf_urls"])
    except fetch.FetchError as e:
        raise HTTPException(422, str(e))
    try:
        paper_id = library.ingest(path, fetch.filename_for(info), info)
    finally:
        path.unlink(missing_ok=True)
    jobs.submit(paper_id)
    return {"id": paper_id}


@app.post("/api/papers/{paper_id}/reprocess")
def reprocess(paper_id: str):
    _existing(paper_id)
    jobs.submit(paper_id, kind="full", force=True)
    return {"id": paper_id}


@app.post("/api/papers/{paper_id}/highlight")
def rehighlight(paper_id: str):
    _existing(paper_id)
    jobs.submit(paper_id, kind="highlight")
    return {"id": paper_id}


# ---- Notes: free-text notes attached to a sentence. Stored in paper.json on
# this PC, so every device using this server sees the same notes.

@app.get("/api/papers/{paper_id}/notes")
def list_notes(paper_id: str):
    _existing(paper_id)
    return (library.load_paper(paper_id) or {}).get("notes", [])


@app.post("/api/papers/{paper_id}/notes")
def add_note(paper_id: str, body: dict = Body(...)):
    _existing(paper_id)
    sid, text = str(body.get("sid", "")), str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "empty note")
    now = time.time()
    note = {"id": uuid.uuid4().hex[:12], "sid": sid, "text": text, "created": now, "updated": now}

    def apply(p):
        if sid not in p["sentences"]:
            raise HTTPException(404, "sentence not found")
        p.setdefault("notes", []).append(note)

    library.update_paper(paper_id, apply)
    return note


@app.put("/api/papers/{paper_id}/notes/{note_id}")
def edit_note(paper_id: str, note_id: str, body: dict = Body(...)):
    _existing(paper_id)
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "empty note")
    found = {}

    def apply(p):
        for n in p.get("notes", []):
            if n["id"] == note_id:
                n.update(text=text, updated=time.time())
                found.update(n)

    library.update_paper(paper_id, apply)
    if not found:
        raise HTTPException(404, "note not found")
    return found


@app.delete("/api/papers/{paper_id}/notes/{note_id}")
def delete_note(paper_id: str, note_id: str):
    _existing(paper_id)
    library.update_paper(paper_id, lambda p: p.update(notes=[n for n in p.get("notes", []) if n["id"] != note_id]))
    return {"ok": True}


@app.put("/api/papers/{paper_id}/edits")
def edit_highlights(paper_id: str, edits: dict = Body(...)):
    """{sentence_id: category | null | "reset"}. null clears a highlight;
    "reset" drops the edit so the AI's label shows again."""
    _existing(paper_id)
    for cat in edits.values():
        if cat not in CATEGORIES and cat not in (None, "reset"):
            raise HTTPException(400, f"unknown category {cat!r}")

    def apply(p):
        for sid, cat in edits.items():
            if sid not in p["sentences"]:
                continue
            if cat == "reset":
                p["user_edits"].pop(sid, None)
            else:
                p["user_edits"][sid] = {"category": cat}

    return library.update_paper(paper_id, apply)["user_edits"]


@app.post("/api/papers/{paper_id}/export")
def export_highlighted(paper_id: str, body: dict = Body(...)):
    """The original PDF with the given highlights ({labels: {sid: category}}) as annotations."""
    _existing(paper_id)
    paper = library.load_paper(paper_id)
    if not paper:
        raise HTTPException(409, "not processed yet")
    labels = body.get("labels") or {}
    if not isinstance(labels, dict):
        raise HTTPException(400, "labels must be an object")
    data, stats = export.export_pdf(paper_id, labels)
    name = export.filename(paper)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"highlighted.pdf\"; filename*=UTF-8''{quote(name)}",
        "X-Highlights": str(stats["highlights"]),
        "X-Skipped": str(stats["skipped"]),
        "X-Notes": str(stats["notes"]),
        "Access-Control-Expose-Headers": "Content-Disposition, X-Highlights, X-Skipped, X-Notes",
    })


def _ndjson(events):
    """Stream model events as NDJSON lines; failures become an error event."""
    try:
        for ev in events:
            yield json.dumps(ev) + "\n"
    except httpx.ConnectError:
        yield json.dumps({"type": "error", "message": "Ollama isn't running. Start it and try again."}) + "\n"
    except Exception as e:
        yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n"


@app.post("/api/papers/{paper_id}/ask")
def ask_question(paper_id: str, body: dict = Body(...)):
    """Stream the answer as NDJSON events: sources, tokens, done (or error)."""
    _existing(paper_id)
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "empty question")
    paper = library.load_paper(paper_id)
    if not paper:
        raise HTTPException(409, "not processed yet")
    search = bool(body.get("search"))  # the Search button: look beyond the paper first
    return StreamingResponse(_ndjson(ask.answer(paper, question, paper.get("chat", []), search)), media_type="application/x-ndjson")


@app.post("/api/papers/{paper_id}/explain")
def explain_sentence(paper_id: str, body: dict = Body(...)):
    """Stream a plain-language explanation of one sentence as NDJSON."""
    _existing(paper_id)
    paper = library.load_paper(paper_id)
    sid = str(body.get("sid", ""))
    if not paper or sid not in paper["sentences"]:
        raise HTTPException(404, "sentence not found")
    return StreamingResponse(_ndjson(explain.explain(paper, sid)), media_type="application/x-ndjson")


@app.post("/api/papers/{paper_id}/translate")
def translate_text(paper_id: str, body: dict = Body(...)):
    """Stream a translation of a word, sentence or passage as NDJSON.
    {text, lang, context?}: context is the sentence around a single word."""
    _existing(paper_id)
    paper = library.load_paper(paper_id)
    if not paper:
        raise HTTPException(409, "not processed yet")
    text = str(body.get("text", "")).strip()
    lang = str(body.get("lang", "")).strip()[:40] or "Simplified Chinese"
    if not text:
        raise HTTPException(400, "nothing to translate")
    context = str(body.get("context", ""))[:2000]
    return StreamingResponse(_ndjson(translate.translate(paper, text, lang, context)), media_type="application/x-ndjson")


@app.post("/api/papers/{paper_id}/explain-float")
def explain_figure(paper_id: str, body: dict = Body(...)):
    """Stream an explanation of a figure or table, read from its image, as NDJSON."""
    _existing(paper_id)
    paper = library.load_paper(paper_id)
    block = str(body.get("block", ""))
    b = next((x for x in (paper or {}).get("blocks", []) if x["id"] == block), None)
    if not b or b["type"] not in ("figure", "table") or not b.get("image"):
        raise HTTPException(404, "figure or table not found")
    return StreamingResponse(_ndjson(explain.explain_float(paper, block)), media_type="application/x-ndjson")


@app.get("/api/papers/{paper_id}/insights")
def get_insights(paper_id: str):
    """The questions about this paper's highlights (or null), and the job's state."""
    _existing(paper_id)
    paper = library.load_paper(paper_id) or {}
    return {"insights": paper.get("insights"), "status": jobs.status(jobs.insights_key(paper_id))}


@app.post("/api/papers/{paper_id}/insights")
def make_insights(paper_id: str):
    """Write (or rewrite) questions about the highlights and answer them, in the background."""
    _existing(paper_id)
    jobs.submit(paper_id, kind="insights")
    return {"status": jobs.status(jobs.insights_key(paper_id))}


# ---- Summary, flashcards: side jobs like Questions (GET = stored result + job state).

def _side_get(paper_id: str, kind: str):
    _existing(paper_id)
    paper = library.load_paper(paper_id) or {}
    return {kind: paper.get(kind), "status": jobs.status(jobs.side_key(paper_id, kind))}


def _side_post(paper_id: str, kind: str):
    _existing(paper_id)
    if not library.load_paper(paper_id):
        raise HTTPException(409, "not processed yet")
    jobs.submit(paper_id, kind=kind)
    return {"status": jobs.status(jobs.side_key(paper_id, kind))}


@app.get("/api/papers/{paper_id}/summary")
def get_summary(paper_id: str):
    return _side_get(paper_id, "summary")


@app.post("/api/papers/{paper_id}/summary")
def make_summary(paper_id: str):
    """(Re)write the one-page summary in the background."""
    return _side_post(paper_id, "summary")


@app.get("/api/papers/{paper_id}/connections")
def get_connections(paper_id: str):
    """Library papers this one cites, is cited by, and is closest to in content."""
    _existing(paper_id)
    return catalog.connections(paper_id)


@app.get("/api/papers/{paper_id}/cards")
def get_cards(paper_id: str):
    out = _side_get(paper_id, "cards")
    deck = (out["cards"] or {}).get("cards", [])
    review = cards.due([{"id": paper_id, "title": "", "cards": deck}])
    review.pop("queue")
    out["review"] = review
    return out


@app.post("/api/papers/{paper_id}/cards")
def make_cards(paper_id: str):
    """(Re)write the AI's flashcards in the background (your own cards stay)."""
    return _side_post(paper_id, "cards")


@app.post("/api/papers/{paper_id}/cards/new")
def add_card(paper_id: str, body: dict = Body(...)):
    """A card of your own: {front, back, sid?}."""
    _existing(paper_id)
    front, back = str(body.get("front", "")).strip(), str(body.get("back", "")).strip()
    if not front or not back:
        raise HTTPException(400, "a card needs a front and a back")
    sid = str(body.get("sid") or "")
    card = {"id": cards.card_id(front), "front": front[:500], "back": back[:2000], "kind": "concept",
            "highlights": [sid] if sid else [], "by": "you", "created": time.time()}

    def apply(p):
        deck = p.setdefault("cards", {"cards": [], "at": time.time()})
        if any(c["id"] == card["id"] for c in deck["cards"]):
            raise HTTPException(409, "there is already a card with this front")
        deck["cards"].append(card)

    library.update_paper(paper_id, apply)
    return card


@app.put("/api/papers/{paper_id}/cards/{card_id}")
def edit_card(paper_id: str, card_id: str, body: dict = Body(...)):
    """Edit a card's back (its front is its identity, so review history stays)."""
    _existing(paper_id)
    found = {}

    def apply(p):
        for c in (p.get("cards") or {}).get("cards", []):
            if c["id"] == card_id:
                if str(body.get("back", "")).strip():
                    c["back"] = str(body["back"]).strip()[:2000]
                found.update(c)

    library.update_paper(paper_id, apply)
    if not found:
        raise HTTPException(404, "card not found")
    return found


@app.delete("/api/papers/{paper_id}/cards/{card_id}")
def delete_card(paper_id: str, card_id: str):
    _existing(paper_id)

    def apply(p):
        deck = p.get("cards") or {}
        deck["cards"] = [c for c in deck.get("cards", []) if c["id"] != card_id]

    library.update_paper(paper_id, apply)
    cards.forget(paper_id, card_id)
    return {"ok": True}


# ---- Review: flashcards due across the library (or one paper).

def _decks(paper_id: str | None = None) -> list[dict]:
    out = []
    for pid in [paper_id] if paper_id else catalog.paper_ids():
        p = library.load_paper(pid)
        if p and (p.get("cards") or {}).get("cards"):
            out.append({"id": pid, "title": p["meta"]["title"], "cards": p["cards"]["cards"]})
    return out


@app.get("/api/review")
def review_queue(paper: str | None = None):
    """Cards due now (and up to 20 new ones), oldest due first."""
    if paper:
        _existing(paper)
    return cards.due(_decks(paper))


@app.post("/api/review/{paper_id}/{card_id}")
def review_card(paper_id: str, card_id: str, body: dict = Body(...)):
    _existing(paper_id)
    try:
        return cards.grade(paper_id, card_id, str(body.get("grade", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---- Markdown export

@app.get("/api/papers/{paper_id}/markdown")
def export_markdown(paper_id: str, parts: str = ",".join(markdown.PARTS), hidden: str = ""):
    """The paper's reading as a Markdown file. parts: any of summary, highlights,
    notes, questions, cards, chat; hidden: highlight categories to leave out."""
    _existing(paper_id)
    paper = library.load_paper(paper_id)
    if not paper:
        raise HTTPException(409, "not processed yet")
    want = [x for x in parts.split(",") if x in markdown.PARTS] or list(markdown.PARTS)
    text = markdown.render(paper, want, {h for h in hidden.split(",") if h})
    name = markdown.filename(paper, " - questions" if want == ["questions"] else "")
    return Response(text.encode("utf-8"), media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"notes.md\"; filename*=UTF-8''{quote(name)}",
        "Access-Control-Expose-Headers": "Content-Disposition",
    })


# ---- References: add a cited paper to the library

@app.post("/api/papers/{paper_id}/references/{block_id}/add")
def add_reference(paper_id: str, block_id: str):
    """Find the paper a reference entry cites, download it and queue it.
    Returns {"id", "title", "existing"}; the link is remembered on this paper."""
    _existing(paper_id)
    paper = library.load_paper(paper_id)
    ref = next((b for b in (paper or {}).get("blocks", []) if b["id"] == block_id and b["type"] == "references"), None)
    if not ref:
        raise HTTPException(404, "reference not found")
    linked = (paper.get("ref_links") or {}).get(block_id)
    if linked and library.pdf_path(linked).exists():
        e = library.load_paper(linked)
        return {"id": linked, "title": e["meta"]["title"] if e else None, "existing": True}
    try:
        info, title = refs.resolve(ref["text"])
        path = fetch.download(info["pdf_urls"])
    except fetch.FetchError as e:
        raise HTTPException(422, str(e))
    try:
        new_id = library.ingest(path, fetch.filename_for(info), info)
    finally:
        path.unlink(missing_ok=True)
    existing = library.load_paper(new_id) is not None
    jobs.submit(new_id)
    library.update_paper(paper_id, lambda p: p.setdefault("ref_links", {}).update({block_id: new_id}))
    known = library.load_paper(new_id)
    return {"id": new_id, "title": known["meta"]["title"] if known else title, "existing": existing}


# ---- The library

@app.get("/api/library")
def get_library():
    """Every paper with its shelf data, plus the collections."""
    cols = catalog.load_collections()
    return {"papers": catalog.entries(), "collections": cols["collections"], "organized": cols.get("at"),
            "organize": jobs.status(jobs.side_key(jobs.LIBRARY_ID, "organize"))}


@app.put("/api/library/{paper_id}")
def put_shelf(paper_id: str, body: dict = Body(...)):
    """Set a paper's status (to-read/reading/done/null), tags or collection."""
    _existing(paper_id)
    try:
        return catalog.update_shelf(paper_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/library/organize")
def organize_library(body: dict = Body(default={})):
    """Let the AI group the library into collections, in the background.
    {"fresh": true} starts over instead of keeping the current collections."""
    jobs.submit(jobs.LIBRARY_ID, kind="organize-fresh" if body.get("fresh") else "organize")
    return {"status": jobs.status(jobs.side_key(jobs.LIBRARY_ID, "organize"))}


@app.get("/api/search")
def search_library(q: str = ""):
    """Search across every paper: matching papers and passages."""
    return catalog.search(q[:300])


@app.delete("/api/papers/{paper_id}/chat")
def clear_chat(paper_id: str):
    _existing(paper_id)
    library.update_paper(paper_id, lambda p: p.update(chat=[]))
    return {"ok": True}


@app.get("/api/health")
def health():
    """Cheap liveness check for the launch scripts (doesn't touch Ollama)."""
    return {"ok": True}


@app.get("/api/ai")
def ai_status():
    """The local model and whether Ollama is serving it."""
    return llm.status()


@app.get("/api/papers/{paper_id}/status")
def paper_status(paper_id: str):
    _existing(paper_id)
    return jobs.status(paper_id)


@app.get("/api/papers/{paper_id}")
def get_paper(paper_id: str):
    _existing(paper_id)
    paper = library.load_paper(paper_id)
    if not paper:
        jobs.submit(paper_id)  # e.g. the server restarted mid-processing
        raise HTTPException(409, "not processed yet")
    library.touch_recent(paper_id, paper["meta"]["title"])
    paper["highlighter_version"] = HIGHLIGHTER_VERSION  # older highlights get an "Update" notice
    # Image sizes let the page reserve each figure's height before it loads,
    # so jumps and restored positions don't drift as images arrive.
    for b in paper["blocks"]:
        if b.get("image"):
            b["px"] = _png_size(library.paper_dir(paper_id) / b["image"])
    return paper


def _png_size(path: Path) -> list[int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if head[:8] != bytes.fromhex("89504e470d0a1a0a"):  # PNG signature
        return None
    return [int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")]


@app.get("/api/papers/{paper_id}/pdf")
def get_pdf(paper_id: str):
    _existing(paper_id)
    return FileResponse(library.pdf_path(paper_id), media_type="application/pdf")


@app.get("/api/papers/{paper_id}/assets/{name}")
def get_asset(paper_id: str, name: str):
    d = _existing(paper_id)
    p = (library.assets_dir(paper_id) / name).resolve()
    if p.parent != (d / "assets").resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/papers/{paper_id}/position")
def get_position(paper_id: str):
    _existing(paper_id)
    return library.load_position(paper_id)


@app.put("/api/papers/{paper_id}/position")
def put_position(paper_id: str, body: dict = Body(...)):
    """Remember the sentence at the top of the screen, so the paper reopens
    there on any device. Last write wins, by the client's timestamp."""
    _existing(paper_id)
    try:
        pos = {
            "sid": str(body.get("sid", ""))[:32],
            "offset": float(body.get("offset", 0)),
            "percent": max(0.0, min(100.0, float(body.get("percent", 0)))),
            "updated": float(body.get("updated") or time.time()),
        }
    except (TypeError, ValueError):
        raise HTTPException(400, "bad position")
    if pos["updated"] >= library.load_position(paper_id).get("updated", 0):
        library.save_position(paper_id, pos)
    return pos


@app.get("/api/recent")
def recent():
    items = library.load_recent()
    for it in items:
        try:
            it["percent"] = library.load_position(it["id"]).get("percent")
        except ValueError:
            pass
    return items


@app.get("/api/settings")
def get_settings():
    return library.load_settings()


@app.put("/api/settings")
def put_settings(settings: dict = Body(...)):
    library.save_settings(settings)
    return settings


@app.get("/api/info")
def info():
    return {"library": str(LIBRARY)}


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Make browsers check for a newer app.js/style.css/etc. on every load.
    Without a Cache-Control header, Edge reused stale copies after updates.
    Unchanged files are answered with a cheap 304 via their ETag."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


_ASSET_REF = re.compile(r'(href|src)="((?:icons/)?[\w.-]+\.(?:js|css|svg|ico|png|webmanifest))"')


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index():
    """The page, with ?v=<content hash> on every local file it references:
    when app.js or style.css changes, its URL changes, so no browser can keep
    running an old copy."""
    html = (WEB_DIR / "index.html").read_text("utf-8")

    def versioned(m):
        path = WEB_DIR / m.group(2)
        if not path.exists():
            return m.group(0)
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
        return f'{m.group(1)}="{m.group(2)}?v={digest}"'

    return HTMLResponse(_ASSET_REF.sub(versioned, html), headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Browsers ask for /favicon.ico regardless of the <link> tags.
    return FileResponse(WEB_DIR / "icons" / "favicon.ico")


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
