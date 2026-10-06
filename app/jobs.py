"""Background processing queue. One worker thread: the GPU runs one paper at a time.

A "full" job parses the PDF and then highlights it; a "highlight" job re-runs
only the AI. Side jobs add to a processed paper, and their status is kept
apart under "<paper id>:<kind>", so they never look like the paper itself is
being processed:
  insights - write and answer questions about the highlights
  summary  - the one-page summary (queued after every highlighting)
  cards    - flashcards
One library-wide job, "organize" (status key "library:organize"), groups the
papers into collections; it is queued when a summary finishes for a paper that
has no collection yet. If highlighting fails (e.g. Ollama isn't running) the
paper is still readable; the error is kept in paper.json's status.
"""

import json
import os
import queue
import threading
import traceback

import httpx

from . import ask, cards, catalog, insights, library, llm, summary
from .highlight import VERSION as highlight_version, classify_paper
from .parse import parse_paper

_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
_status: dict[str, dict] = {}
_lock = threading.Lock()

# Unfinished jobs are written here, so a server restart resumes them instead
# of silently dropping a long highlighting batch.
PENDING_FILE = library.LIBRARY / "pending-jobs.json"
_pending: list[list[str]] = []


def _save_pending() -> None:
    try:
        PENDING_FILE.write_text(json.dumps(_pending), "utf-8")
    except OSError:
        pass


def _set(paper_id: str, **kw) -> None:
    with _lock:
        _status.setdefault(paper_id, {}).update(kw)


def status(paper_id: str) -> dict:
    with _lock:
        if paper_id in _status:
            return dict(_status[paper_id])
    if ":" in paper_id:  # e.g. "<id>:insights" with nothing queued or running
        return {"state": "idle"}
    if library.load_paper(paper_id):
        return {"state": "done", "stage": "Ready", "progress": 1.0}
    return {"state": "unknown"}


SIDE_KINDS = ("insights", "summary", "cards", "organize", "organize-fresh")
LIBRARY_ID = "library"  # the paper id of library-wide jobs


def insights_key(paper_id: str) -> str:
    return f"{paper_id}:insights"


def side_key(paper_id: str, kind: str) -> str:
    return f"{paper_id}:{kind.removesuffix('-fresh')}"


def submit(paper_id: str, kind: str = "full", force: bool = False) -> None:
    if kind == "full" and not force and library.load_paper(paper_id):
        return
    key = side_key(paper_id, kind) if kind in SIDE_KINDS else paper_id
    with _lock:
        if _status.get(key, {}).get("state") in ("queued", "running"):
            return
    _set(key, state="queued", stage="Waiting in queue", progress=0.0, error=None)
    with _lock:
        _pending.append([paper_id, kind])
        _save_pending()
    _queue.put((paper_id, kind))


def busy(paper_id: str) -> bool:
    """Whether the AI is working on this paper right now."""
    with _lock:
        return any(v.get("state") == "running" for k, v in _status.items()
                   if k == paper_id or k.startswith(f"{paper_id}:"))


def forget(paper_id: str) -> None:
    """A removed paper: drop its job states and queued jobs (the worker skips
    any still in the queue)."""
    with _lock:
        for k in [k for k in _status if k == paper_id or k.startswith(f"{paper_id}:")]:
            del _status[k]
        _pending[:] = [x for x in _pending if x[0] != paper_id]
        _save_pending()


def _finished(paper_id: str, kind: str) -> None:
    with _lock:
        if [paper_id, kind] in _pending:
            _pending.remove([paper_id, kind])
        _save_pending()


def _side(paper_id: str, kind: str) -> None:
    """Run one side job and store its result in paper.json."""
    key = side_key(paper_id, kind)
    progress = lambda stage, frac: _set(key, stage=stage, progress=frac)
    if kind.startswith("organize"):
        catalog.organize(fresh=kind == "organize-fresh", progress=progress)
        return
    make = {"insights": insights.generate, "summary": summary.generate, "cards": cards.generate}[kind]
    result = make(library.load_paper(paper_id), progress)
    library.update_paper(paper_id, lambda p: p.update({kind: result}))
    if kind == "summary" and paper_id in catalog.unassigned():
        submit(LIBRARY_ID, kind="organize")  # file the new paper into a collection


def _explain(e: Exception, what: str = "Highlights") -> str:
    """Turn common AI failures into something the reader can act on."""
    cfg = llm.settings()
    if isinstance(e, httpx.ConnectError):
        return f"{what} unavailable: Ollama isn't running at {cfg['url']}. Start Ollama, then retry."
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
        return f"{what} unavailable: the model {cfg['model']} isn't downloaded. Run: ollama pull {cfg['model']}"
    if isinstance(e, httpx.TimeoutException):
        return f"{what} unavailable: the AI took too long to answer. Retry, or pick a smaller model."
    return f"{what} unavailable: {type(e).__name__}: {e}"


def _highlight(paper_id: str, paper: dict, lo: float) -> None:
    """Run the AI and store its labels; progress runs from `lo` to 1."""
    def progress(stage, frac):
        _set(paper_id, stage=stage, progress=lo + (1 - lo) * frac)

    try:
        labels, notes = classify_paper(paper, progress)
        model, error = llm.model_name(), None
    except Exception as e:
        traceback.print_exc()
        labels, notes, model, error = None, None, None, _explain(e)

    def apply(p):
        if labels is not None:
            p["ai_labels"] = labels
            p["reading_notes"] = notes
            p["status"]["highlight_version"] = highlight_version
        p["status"].update(classified=labels is not None or p["status"].get("classified", False),
                           model=model or p["status"].get("model"), error=error)

    library.update_paper(paper_id, apply)
    if labels is not None:
        submit(paper_id, kind="summary")  # the summary is written from the new highlights

    # Embed passages for Q&A now, so the first question is quick.
    try:
        _set(paper_id, stage="Preparing Q&A search", progress=1.0)
        ask.embed_passages(library.load_paper(paper_id))
    except Exception:
        traceback.print_exc()  # Q&A falls back to keyword search


def _worker() -> None:
    while True:
        paper_id, kind = _queue.get()
        if paper_id != LIBRARY_ID and not library.pdf_path(paper_id).exists():  # removed meanwhile
            _finished(paper_id, kind)
            _queue.task_done()
            continue
        if kind in SIDE_KINDS:
            key = side_key(paper_id, kind)
            what = {"insights": "Questions", "summary": "Summary", "cards": "Flashcards"}.get(kind, "Collections")
            try:
                _set(key, state="running")
                _side(paper_id, kind)
                _set(key, state="done", stage="Ready", progress=1.0)
            except Exception as e:
                traceback.print_exc()
                _set(key, state="error", stage="Failed",
                     error=str(e) if isinstance(e, ValueError) else _explain(e, what))
            finally:
                _finished(paper_id, kind)
                _queue.task_done()
            continue
        try:
            _set(paper_id, state="running")
            if kind == "full":
                old = library.load_paper(paper_id)
                paper = parse_paper(paper_id, lambda stage, frac: _set(paper_id, stage=stage, progress=frac * 0.3))
                if old:
                    library.carry_over(old, paper)
                library.save_paper(paper_id, paper)
                # Only call the AI for papers it has never seen; "Re-run AI highlights" forces it.
                if not paper["status"]["classified"]:
                    _highlight(paper_id, paper, 0.3)
            else:
                _highlight(paper_id, library.load_paper(paper_id), 0.0)
            _set(paper_id, state="done", stage="Ready", progress=1.0)
        except Exception as e:  # keep the worker alive; report to the UI
            traceback.print_exc()
            _set(paper_id, state="error", stage="Failed", error=f"{type(e).__name__}: {e}")
        finally:
            _finished(paper_id, kind)
            _queue.task_done()


def _resume() -> None:
    """Re-queue the jobs a previous server left unfinished."""
    try:
        left = json.loads(PENDING_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for paper_id, kind in left:
        if paper_id == LIBRARY_ID or library.paper_dir(paper_id).exists():
            submit(paper_id, kind=kind, force=True)


def _backfill() -> None:
    """Papers highlighted before summaries existed get one (once)."""
    queued = {tuple(x) for x in _pending}
    for pid in catalog.paper_ids():
        p = library.load_paper(pid)
        if p and p["status"].get("classified") and not p.get("summary") and (pid, "summary") not in queued:
            submit(pid, kind="summary")


# PAPERCUT_WORKER=0 runs a server without the worker (a second copy for
# testing against the same library): nothing is processed or resumed.
if os.environ.get("PAPERCUT_WORKER", "1") != "0":
    threading.Thread(target=_worker, daemon=True, name="paper-worker").start()
    _resume()
    _backfill()
