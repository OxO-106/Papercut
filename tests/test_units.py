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


def test_automatic_organize_only_files_new_papers(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "COLLECTIONS_FILE", tmp_path / "collections.json")
    (tmp_path / "collections.json").write_text(json.dumps(
        {"collections": [{"name": "Agents", "about": ""}], "assign": {"old": "Agents"}}))
    entries = {"old": {"id": "old", "title": "Old", "tldr": "", "topics": []},
               "new": {"id": "new", "title": "New", "tldr": "", "topics": []}}
    monkeypatch.setattr(catalog, "paper_ids", lambda: list(entries))
    monkeypatch.setattr(catalog, "_entry", entries.get)
    # The model tries to move the old paper and puts the new one in a new collection.
    reply = {"collections": [{"name": "Agents", "about": ""}, {"name": "Vision", "about": ""}],
             "assign": [{"id": 1, "collection": "Vision"}, {"id": 2, "collection": "Vision"}]}
    monkeypatch.setattr(catalog.llm, "chat", lambda *a, **k: json.dumps(reply))
    monkeypatch.setattr(catalog.llm, "model_name", lambda: "test")
    r = catalog.organize()
    assert r["assign"] == {"old": "Agents", "new": "Vision"}
    assert [c["name"] for c in r["collections"]] == ["Agents", "Vision"]
    assert catalog.organize(fresh=True)["assign"] == {"old": "Vision", "new": "Vision"}


def test_script_ranges_bridge_symbols_not_spaces():
    from app.segment import _script_ranges
    s = "ot-1, at+1 x"
    assert _script_ranges(s, [1, 3, 8, 9]) == [[1, 4], [8, 10]]  # "t-1", "t+1"
    assert _script_ranges("a b", [0, 2]) == [[0, 1], [2, 3]]


def test_markdown_lists_underlines():
    p = json.loads(json.dumps(PAPER))
    p["underlines"] = [{"id": "u1", "sid": "s2", "a": 0, "b": 7}]
    md = markdown.render(p, ["underlines"])
    assert "## Underlined" in md and "- X beats (p. 3)" in md


def test_math_ranges_bridge_operators_and_close_brackets():
    from app.segment import _math_ranges
    s = "policy π(at|ct), where"
    #     0123456789012345678
    assert _math_ranges(s, [7, 9, 10, 12, 13]) == [[7, 15]]  # "π(at|ct)", not the comma
    assert _math_ranges("ot ∈ O and x", [0, 1, 5, 11]) == [[0, 6], [11, 12]]



def test_tidy_headings_and_glued_symbols():
    from app.parse import _tidy_text
    raw = [
        {"type": "heading", "text": "2 REAC T: S YNERGIZING REASONING +ACTING"},
        {"type": "heading", "text": "R E F E R E N C E S"},
        {"type": "heading", "text": "E.1 Ethics &Broader Impacts"},
        {"type": "heading", "text": "APPENDIX B"},
        {"type": "heading", "text": "F IN-DEPTH ANALYSIS"},
        {"type": "paragraph", "text": "We propose ReAct, synergizing x +y and Chain-of-Thought +Reflexion."},
    ]
    out = [b["text"] for b in _tidy_text(raw)]
    assert out[:5] == ["2 REACT: SYNERGIZING REASONING + ACTING", "REFERENCES", "E.1 Ethics & Broader Impacts",
                       "APPENDIX B", "F IN-DEPTH ANALYSIS"]
    assert out[5] == "We propose ReAct, synergizing x +y and Chain-of-Thought + Reflexion."


def test_carry_over_matches_slightly_changed_text():
    old = {"sentences": {"s1": {"text": "Chain-of-Thought +Reflexion works."}}, "blocks": [], "status": {},
           "ai_labels": {"s1": {"category": "result", "confidence": .9, "tier": 1}},
           "underlines": [{"id": "u", "sid": "s1", "a": 0, "b": 5}]}
    new = {"sentences": {"t1": {"text": "Chain-of-Thought + Reflexion works."}}, "blocks": [], "status": {}}
    carry_over(old, new)
    assert new["ai_labels"] == {"t1": old["ai_labels"]["s1"]}
    assert new["underlines"] == []  # offsets may have moved: dropped rather than misplaced


def test_glued_caption_split_from_next_page():
    from types import SimpleNamespace as NS
    from app.parse import _split_glued_captions
    text = "These Table 2 | Performance comparison between our NSA and baselines."
    cut = text.index("Table")
    page = NS(size=NS(height=800))
    bbox = NS(to_top_left_origin=lambda page_height: NS(l=0, t=0, r=1, b=1))
    item = NS(text=text, prov=[NS(page_no=10, charspan=(0, cut - 1), bbox=bbox),
                               NS(page_no=11, charspan=(cut, len(text)), bbox=bbox)])
    doc = NS(pages={10: page, 11: page})
    parts = _split_glued_captions(item, doc)
    assert [p[0] for p in parts] == ["These", text[cut:]]
    assert [p[1][0][0] for p in parts] == [10, 11]
    one_page = NS(text="Plain paragraph.", prov=[NS(page_no=3, charspan=(0, 16), bbox=bbox)])
    assert _split_glued_captions(one_page, doc) is None


def test_markdown_underline_offsets_count_utf16():
    from app.markdown import _utf16_slice
    text = "token q𝑡 computes"
    # The page counts 𝑡 as two units: "computes" starts at 10, not 9.
    assert _utf16_slice(text, 10, 18) == "computes"
    assert _utf16_slice(text, 6, 9) == "q𝑡"


def test_formula_ranges_take_in_unmatched_accents_and_brackets():
    from app.segment import _formula_ranges
    s = "keys ˜ cmp 𝑡 and ⌊ 𝑠-𝑙 𝑑 ⌋ tokens"
    c = s.index("mp")  # only "mp 𝑡" matched to the formula's characters
    chars = [(c, "f1"), (c + 1, "f1"), (s.index("𝑡"), "f1")]
    plain = set(range(0, 4)) | {s.index("and")}
    (a, b, fid), = _formula_ranges(s, chars, plain)
    assert s[a:b] == "˜ cmp 𝑡" and fid == "f1"
    f = s.index("𝑠")
    (a, b, _), = _formula_ranges(s, [(f, "f2"), (f + 2, "f2"), (s.index("𝑑"), "f2")], plain)
    assert s[a:b] == "⌊ 𝑠-𝑙 𝑑 ⌋"


def test_inline_formula_seeds():
    from app.inline_math import _hard, _mathy
    assert _hard("\x04") and _hard("�") and _hard("⌊") and _hard("˜")
    assert _mathy("𝑡") and _mathy("α") and _mathy("∈") and not _mathy("a")


def test_figure_mentions_any_case_and_dotted_labels():
    from app.xrefs import _CAPTION_LABEL, _key, find_mentions
    labels = {"figure:1": "b1", "table:A3": "b2", "table:4": "b3"}
    hits = find_mentions("For example, in figure 1, see Table A.3 and TABLE 4.", labels)
    assert [ids for _, _, ids in hits] == [["b1"], ["b2"], ["b3"]]
    m = _CAPTION_LABEL.match("9 Figure 6: New module")  # a page number glued in front
    assert _key("figure", m[2]) == "figure:6"


def test_caption_with_an_unmapped_bar():
    from app.parse import _caption_kind
    assert _caption_kind("Table 4 � Chinchilla architecture details.") == "table"
    assert _caption_kind("Table A.3: Performance comparison") == "table"


def _para(text):
    return {"type": "paragraph", "region": "body", "text": text, "provs": []}


def test_paragraph_ending_on_et_al_continues_after_a_float():
    from app.parse import _merge_split_paragraphs
    raw = [_para("Yoran et al. [31] and Nair et al."), {"type": "table", "region": "body"},
           _para("[16] use decider models to reason over several generations.")]
    out = _merge_split_paragraphs(raw)
    assert out[0]["text"] == "Yoran et al. [31] and Nair et al. [16] use decider models to reason over several generations."
    # A finished sentence stays apart from a next paragraph that starts lowercase (a command).
    raw = [_para("Let's look at the file."), _para("ls -a")]
    assert len(_merge_split_paragraphs(raw)) == 2


def _glyph(c, x, oy, size=10.0, font="CMMI10"):
    return {"c": c, "x0": x, "x1": x + 0.5 * size, "y0": oy - 0.75 * size, "y1": oy + 0.25 * size,
            "gy0": oy - 0.7 * size, "gy1": oy + 0.2 * size, "oy": oy, "size": size, "main": 10.0,
            "row_main": 10.0, "row_base": 100.0, "baseline": 100.0, "big": False, "font": font, "bold": False}


def test_inline_formula_as_mathml():
    from app.inline_math import to_mathml
    # a′ᵢ: the prime beside the letter, the i as a subscript.
    cl = [_glyph("a", 10, 100), _glyph("′", 15, 97, 7, "CMSY7"), _glyph("i", 15.5, 102, 7, "CMMI7")]
    assert to_mathml(cl) == '<math><msub><mrow><mi>a</mi><mo lspace="0" rspace="0">′</mo></mrow><mi>i</mi></msub></math>'
    # TeX's big operator without Unicode ("Q" in cmex) with its limits beside it.
    cl = [_glyph("Q", 10, 100, 10, "CMEX10"), _glyph("n", 17, 96, 7, "CMMI7"), _glyph("i", 17, 103, 7, "CMMI7")]
    assert to_mathml(cl) == "<math><msubsup><mo>∏</mo><mi>i</mi><mi>n</mi></msubsup></math>"
    # An unmatched bracket the text shows outside the formula is left to the text.
    cl = [_glyph("x", 10, 100), _glyph("[", 15, 100, 10, "CMR10")]
    assert to_mathml(cl, text="x") == "<math><mi>x</mi></math>"


def test_formula_range_takes_in_its_own_symbol_beside_it():
    from app.parse import _widen_formula
    s = "and □r [t] = x"
    a = s.index("r [t]")
    assert _widen_formula(s, a, a + 5, ["□", "[", "t", "]", "r"]) == (s.index("□"), a + 5)
