"""Each course's next class from Almanac's calendar: classes, which readings go to which, title matching."""
import sqlite3
from datetime import datetime

from app import almanac


def _db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        create table courses (id integer primary key, number text, instructor text);
        create table events (id integer primary key, title text, course_id integer, start text, end text,
                             repeat text, until text, skip text);
        create table tasks (id integer primary key, title text, course_id integer, due text, do_date text, status text);
        insert into courses values (15, 'CS 239', 'Robin Ding'), (16, 'CS 239', 'Miryung Kim'), (12, 'CS 201', 'Remy Wang');
        insert into events (title, course_id, start, end, repeat, until, skip) values
          ('CS 239 class', 15, '2026-09-28T16:00', '2026-09-28T17:50', 'MO,WE', '2026-12-04', '2026-10-14'),
          ('CS 239 office hours', 15, '2026-09-24T16:00', '2026-09-24T17:00', 'TH', '2026-12-04', null),
          ('CS 239 class', 16, '2026-09-24T10:00', '2026-09-24T11:50', 'TU,TH', '2026-12-04', null),
          ('CS 201 class', 12, '2026-09-24T12:00', '2026-09-24T13:50', 'TU,TH', '2026-12-04', null);
        insert into tasks (title, course_id, due, do_date, status) values
          ('Read Scaling Laws for Neural Language Models', 15, '2026-10-06', null, 'done'),
          ('Read On the naturalness of software', 15, '2026-10-06', null, 'open'),
          ('Read LoRA: Low-Rank Adaptation', 15, '2026-10-11', null, 'open'),
          ('Read Skipped Class Paper', 15, '2026-10-13', null, 'open'),
          ('Read P4. Tree of Thoughts [NeurIPS 2023]', 16, '2026-10-07', null, 'open'),
          ('Write the report', 16, '2026-10-07', null, 'open');
    """)
    con.commit()
    con.close()


PAPERS = [{"id": "a", "title": "Scaling Laws for Neural Language Models"},
          {"id": "b", "title": "On the Naturalness of Software"},
          {"id": "c", "title": "Tree of Thoughts: Deliberate Problem Solving with Large Language Models"}]


def _at(stamp):
    return datetime.fromisoformat(stamp).replace(tzinfo=almanac.TZ)


def test_next_class_per_course(tmp_path, monkeypatch):
    db = tmp_path / "almanac.db"
    _db(db)
    monkeypatch.setattr(almanac, "ALMANAC_DB", db)
    # Wednesday noon: Ding's class today covers both papers due yesterday (read or not).
    out = almanac.next_classes(PAPERS, _at("2026-10-07T12:00"))
    assert out["available"]
    ding, kim = out["classes"]
    assert (ding["label"], ding["date"], ding["start"], ding["end"]) == ("239 Ding", "2026-10-07", "16:00", "17:50")
    assert ding["papers"] == ["a", "b"] and ding["missing"] == []
    assert (kim["label"], kim["date"], kim["papers"]) == ("239 Kim", "2026-10-08", ["c"])
    # After Ding's class ends: Kim's is first; Ding's next is Monday's, with a paper not in the library.
    kim, ding = almanac.next_classes(PAPERS, _at("2026-10-07T18:00"))["classes"]
    assert (ding["date"], ding["papers"], ding["missing"]) == ("2026-10-12", [], ["LoRA: Low-Rank Adaptation"])
    # A skipped day isn't a class: its reading goes to the class after it.
    ding = almanac.next_classes(PAPERS, _at("2026-10-12T18:00"))["classes"][0]
    assert (ding["date"], ding["missing"]) == ("2026-10-19", ["Skipped Class Paper"])


def test_no_almanac(tmp_path, monkeypatch):
    monkeypatch.setattr(almanac, "ALMANAC_DB", tmp_path / "none.db")
    assert almanac.next_classes([]) == {"available": False, "classes": []}
