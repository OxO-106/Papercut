"""Fast tests of pure logic: no model, no network, no library files."""

import json

import pytest

from app import cards, catalog, markdown, refs
from app.citations import Linker
from app.library import carry_over

DAY = cards.DAY


# ---- spaced repetition

def test_new_card_intervals():
    now = 1_000_000.0
    assert cards.schedule(None, "again", now)["due"] == now + 60
    assert cards.schedule(None, "hard", now)["interval"] == 1.0
    assert cards.schedule(None, "good", now)["interval"] == 1.0
    assert cards.schedule(None, "easy", now)["interval"] == 4.0


def test_intervals_grow_and_again_resets():
    now = 0.0
    s = cards.schedule(None, "good", now)
    s = cards.schedule(s, "good", now)
    assert s["interval"] == 3.0
    s = cards.schedule(s, "good", now)
    assert s["interval"] == pytest.approx(7.5)  # 3 x ease 2.5
    s = cards.schedule(s, "again", now)
    assert s["interval"] == 0 and s["reps"] == 0 and s["lapses"] == 1 and s["ease"] == 2.3


def test_ease_has_a_floor():
    s = None
    for _ in range(20):
        s = cards.schedule(s, "again", 0)
    assert s["ease"] == 1.3


def test_session_has_every_card_weakest_first(monkeypatch):
    now = 10 * DAY
    reviews = {"p:a": {"due": now - 5, "interval": 1, "ease": 2.5, "reps": 1, "lapses": 0, "last": now - DAY},
               "p:b": {"due": now + DAY, "interval": 1, "ease": 2.5, "reps": 1, "lapses": 0, "last": now - 2 * DAY}}
    monkeypatch.setattr(cards, "load_reviews", lambda: reviews)
    monkeypatch.setattr(cards.time, "time", lambda: now)
    deck = [{"id": x, "front": x, "back": x} for x in "bca"]
    s = cards.session("p", "T", deck)
    assert [c["id"] for c in s["queue"]] == ["a", "c", "b"]  # run out, never seen, the rest
    assert cards.deck_info("p", deck) == {"cards": 3, "reviewed": 2, "last": now - DAY}


def test_card_id_ignores_case_and_spaces():
    assert cards.card_id(" What is X? ") == cards.card_id("what is x?")


# ---- Markdown

PAPER = {
    "meta": {"title": "A Paper"},
    "source": {"arxiv": "2310.06770"},
    "blocks": [
        {"id": "b1", "type": "heading", "text": "1 Intro"},
        {"id": "b2", "type": "paragraph", "sentences": ["s1", "s2"]},
    ],
    "sentences": {
        "s1": {"text": "We propose X.", "rects": [{"page": 2, "bbox": [0, 0, 1, 1]}]},
        "s2": {"text": "X beats Y by 5 points.", "rects": [{"page": 3, "bbox": [0, 0, 1, 1]}]},
    },
    "ai_labels": {"s1": {"category": "novelty", "confidence": .9, "tier": 1, "note": "the core idea"},
                  "s2": {"category": "result", "confidence": .3, "tier": None}},
    "user_edits": {},
    "notes": [{"id": "n1", "sid": "s1", "text": "check this"}],
    "insights": {"questions": [{"q": "Why?", "status": "answered", "highlights": ["s1"],
                                "answer": "Because [1] and [S1].", "sources": [{"n": 2, "sid": "s1"}, {"n": 1, "sid": "s2"}],
                                "web": [{"n": "S1", "title": "Blog", "url": "https://example.org"}]}]},
}


def test_markdown_highlights_notes_and_questions():
    md = markdown.render(PAPER)
    assert md.startswith("# A Paper")
    assert "<https://arxiv.org/abs/2310.06770>" in md
    assert "**Novelty** We propose X. (p. 2)" in md
    assert "*the core idea*" in md
    assert "My note: check this" in md
    assert "X beats Y" not in md.split("## Questions")[0]  # dropped by the AI: not a highlight
    assert "Because (p. 3) and [S1](https://example.org)." in md


def test_markdown_parts_and_hidden_categories():
    md = markdown.render(PAPER, ["highlights"], {"novelty"})
    assert "We propose X" not in md and "## Questions" not in md


# ---- references

def test_reference_with_arxiv_id_needs_no_search():
    info, title = refs.resolve("Yao, S. et al. ReAct. arXiv preprint arXiv:2210.03629, 2022.")
    assert info["arxiv"] == "2210.03629" and title is None


def test_reference_with_arxiv_doi():
    info, _ = refs.resolve("Some paper. doi: 10.48550/arXiv.2310.06770")
    assert info["arxiv"] == "2310.06770"


def test_title_match_is_strict():
    entry = "Vaswani, A. et al. Attention is all you need. NeurIPS 2017."
    assert refs._title_matches("Attention Is All You Need", entry)
    assert not refs._title_matches("Attention", entry)  # too short to trust
    assert not refs._title_matches("Attention is not all you need", entry)


# ---- citations

def test_linker_numeric_and_author_year():
    entries = [{"id": "r1", "text": "Smith, J. A study. 2020."}, {"id": "r2", "text": "Lee, K. Other work. 2021."}]
    lk = Linker(entries)
    assert lk.find("as shown [1, 2]") == [[9, 15, ["r1", "r2"]]]
    assert lk.find("see (Lee et al., 2021)")[0][2] == ["r2"]


# ---- library

def test_carry_over_remaps_by_text():
    old = json.loads(json.dumps(PAPER))
    old["summary"] = {"results": [{"text": "r", "highlights": ["s2"]}], "contributions": [], "limitations": []}
    old["cards"] = {"cards": [{"id": "c", "front": "f", "back": "b", "highlights": ["s1"]}]}
    old["blocks"].append({"id": "b9", "type": "references", "text": "Ref A"})
    old["ref_links"] = {"b9": "abc"}
    old["status"] = {}
    new = {"sentences": {"t1": {"text": "X beats Y by 5 points."}, "t2": {"text": "We propose X."}},
           "blocks": [{"id": "b3", "type": "references", "text": "Ref A"}], "status": {}}
    carry_over(old, new)
    assert new["ai_labels"]["t2"]["tier"] == 1
    assert new["summary"]["results"][0]["highlights"] == ["t1"]
    assert new["cards"]["cards"][0]["highlights"] == ["t2"]
    assert new["ref_links"] == {"b3": "abc"}
    assert new["notes"][0]["sid"] == "t2"


def test_shelf_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "SHELF_FILE", tmp_path / "shelf.json")
    assert catalog.update_shelf("p1", {"status": "done", "tags": ["RL", "rl", " agents "]}) == \
        {"status": "done", "tags": ["RL", "agents"]}
    with pytest.raises(ValueError):
        catalog.update_shelf("p1", {"status": "someday"})
    assert catalog.update_shelf("p1", {"status": None, "tags": []}) == {}
    assert catalog.load_shelf() == {}


def test_title_guess_and_first_author():
    e = ("Sainbayar Sukhbaatar, Arthur Szlam, Jason Weston, and Rob Fergus. End-to-end memory networks. "
         "In Advances in Neural Information Processing Systems 28, 2015.")
    assert refs._title_guesses(e)[0] == "End-to-end memory networks"
    assert refs._first_author(e) == "Sukhbaatar"
    assert refs._first_author("T. Liu, C. Xu, and J. McAuley. Repobench: x. 2024.") == "Liu"
    assert refs._title_guesses("Ł. Kaiser and S. Bengio. Can active memory replace attention? In NIPS, 2016.")[0] \
        == "Can active memory replace attention?"


def test_near_identical_titles_need_the_same_author():
    assert refs._same_title("RepoBench: Auto-Completion", "Repobench: autocompletion", "", "")
    assert refs._same_title("Software Testing with Large Language Models", "Software testing with large language model",
                            "Junjie Wang", "Wang")
    assert not refs._same_title("Software Testing with Large Language Models", "Software testing with large language model",
                                "Someone Else", "Wang")
