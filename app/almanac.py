"""The reading for each course's next class, from Almanac's calendar (read-only).

Almanac plans a "Read <title>" task for each paper a class covers, due the
day before that class. A course's classes come from its recurring "… class"
event (weekdays, last day, skipped days). Each reading belongs to the first
class after its due day; a course's next class is the first class with
readings that hasn't ended yet. Papers are matched to the library by title.
Papercut only reads Almanac's database, never writes it.
"""

import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import ALMANAC_DB

TZ = ZoneInfo("America/Los_Angeles")  # Almanac's day boundary
_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _paper_title(task: str) -> str:
    """'Read P5. SWE-agent: … [NeurIPS 2024]' -> 'SWE-agent: …'."""
    t = re.sub(r"^Read\s+", "", task)
    t = re.sub(r"^P\d+\.\s*", "", t)
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", t).strip()


def _match(title: str, papers: list[dict]) -> dict | None:
    """The library paper with this title: the same words, else one title
    starting with the other (as Almanac's own papers.find)."""
    want = _norm(title)
    for test in (lambda t: t == want, lambda t: len(want) > 10 and (t.startswith(want) or want.startswith(t))):
        for p in papers:
            if p.get("title") and test(_norm(p["title"])):
                return p
    return None


def _sessions(start: str, end: str | None, repeat: str, until: str | None, skip: str | None) -> list[tuple[str, str, str]]:
    """Every class of a weekly event: [(day, start time, end time)]."""
    first = date.fromisoformat(start[:10])
    last = date.fromisoformat(until[:10]) if until else first + timedelta(days=200)
    days = {_DAYS[d] for d in repeat.split(",") if d.strip() in _DAYS}
    skipped = {s.strip() for s in (skip or "").split(",") if s.strip()}
    t0, t1 = start[11:16] or "00:00", (end or "")[11:16] or "23:59"
    out, d = [], first
    while d <= last:
        if d.weekday() in days and d.isoformat() not in skipped:
            out.append((d.isoformat(), t0, t1))
        d += timedelta(days=1)
    return out


def _label(number: str, instructor: str, shared: bool) -> str:
    """'239 Ding': the course number, and the instructor's surname when two
    courses share the number."""
    num = re.sub(r"^[A-Za-z]+\s*", "", number or "").strip() or number
    surname = (instructor or "").split()[-1] if instructor else ""
    return f"{num} {surname}".strip() if shared or not num else num


def next_classes(papers: list[dict], now: datetime | None = None) -> dict:
    """{"available": whether Almanac could be read, "classes": [{"label", "course", "instructor",
    "date", "start", "end", "papers": [paper ids, in Almanac's order], "missing": [titles not in
    the library]}]}, one per course with readings still to come, soonest first."""
    now = now or datetime.now(TZ)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    out = {"available": False, "classes": []}
    if not ALMANAC_DB.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{ALMANAC_DB.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            courses = con.execute("select id, number, instructor from courses").fetchall()
            events = con.execute("select course_id, start, end, repeat, until, skip from events "
                                 "where course_id is not null and repeat is not null and lower(title) like '%class%' "
                                 "order by id").fetchall()
            tasks = con.execute("select course_id, title, due, do_date from tasks where course_id is not null "
                                "and title like 'Read %' order by id").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return out
    out["available"] = True
    numbers = [n for _, n, _ in courses]
    for cid, number, instructor in courses:
        sessions = sorted(s for ev in events if ev[0] == cid for s in _sessions(*ev[1:]))
        if not sessions:
            continue
        # Each reading goes to the first class after its due day.
        by_class: dict[tuple, list[str]] = {}
        for _, title, due, do_date in (t for t in tasks if t[0] == cid):
            day = (due or do_date or "")[:10]
            cls = next((s for s in sessions if s[0] > day), None) if day else None
            if cls:
                by_class.setdefault(cls, []).append(_paper_title(title))
        upcoming = [s for s in sorted(by_class) if f"{s[0]}T{s[2]}" >= stamp]
        if not upcoming:
            continue
        day, t0, t1 = upcoming[0]
        ids, missing = [], []
        for name in by_class[upcoming[0]]:
            p = _match(name, papers)
            if p and p["id"] not in ids:
                ids.append(p["id"])
            elif not p and name not in missing:
                missing.append(name)
        out["classes"].append({"label": _label(number, instructor, numbers.count(number) > 1), "course": number,
                               "instructor": instructor, "date": day, "start": t0, "end": t1,
                               "papers": ids, "missing": missing})
    out["classes"].sort(key=lambda c: (c["date"], c["start"]))
    return out
