"""Background processing queue. One worker thread: the GPU runs one paper at a time.

A "full" job parses the PDF and then highlights it; a "highlight" job re-runs
only the AI; an "insights" job writes and answers questions about the
highlights (its status is kept apart, under "<paper id>:insights", so it
never looks like the paper itself is being processed). If highlighting fails (e.g. Ollama isn't running) the paper is
still readable; the error is kept in paper.json's status.
"""

import json
import queue
import threading
import traceback

import httpx

from . import ask, insights, library, llm
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


def insights_key(paper_id: str) -> str:
    return f"{paper_id}:insights"


def submit(paper_id: str, kind: str = "full", force: bool = False) -> None:
    if kind == "full" and not force and library.load_paper(paper_id):
        return
    key = insights_key(paper_id) if kind == "insights" else paper_id
    with _lock:
        if _status.get(key, {}).get("state") in ("queued", "running"):
            return
    _set(key, state="queued", stage="Waiting in queue", progress=0.0, error=None)
    with _lock:
        _pending.append([paper_id, kind])
        _save_pending()
    _queue.put((paper_id, kind))


def _finished(paper_id: str, kind: str) -> None:
    with _lock:
        if [paper_id, kind] in _pending:
            _pending.remove([paper_id, kind])
        _save_pending()


def _insights(paper_id: str) -> None:
    key = insights_key(paper_id)
    result = insights.generate(library.load_paper(paper_id), lambda stage, frac: _set(key, stage=stage, progress=frac))
    library.update_paper(paper_id, lambda p: p.update(insights=result))


def _explain(e: Exception) -> str:
    """Turn common AI failures into something the reader can act on."""
    cfg = llm.settings()
    if isinstance(e, httpx.ConnectError):
        return f"Highlights unavailable: Ollama isn't running at {cfg['url']}. Start Ollama, then retry."
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
        return f"Highlights unavailable: the model {cfg['model']} isn't downloaded. Run: ollama pull {cfg['model']}"
    if isinstance(e, httpx.TimeoutException):
        return "Highlights unavailable: the AI took too long to answer. Retry, or pick a smaller model."
    return f"Highlights unavailable: {type(e).__name__}: {e}"


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

    # Embed passages for Q&A now, so the first question is quick.
    try:
        _set(paper_id, stage="Preparing Q&A search", progress=1.0)
        ask.embed_passages(library.load_paper(paper_id))
    except Exception:
        traceback.print_exc()  # Q&A falls back to keyword search


def _worker() -> None:
    while True:
        paper_id, kind = _queue.get()
        if kind == "insights":
            key = insights_key(paper_id)
            try:
                _set(key, state="running")
                _insights(paper_id)
                _set(key, state="done", stage="Ready", progress=1.0)
            except Exception as e:
                traceback.print_exc()
                _set(key, state="error", stage="Failed", error=str(e) if isinstance(e, ValueError) else _explain(e).replace("Highlights", "Questions"))
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
        if library.paper_dir(paper_id).exists():
            submit(paper_id, kind=kind, force=True)


threading.Thread(target=_worker, daemon=True, name="paper-worker").start()
_resume()
