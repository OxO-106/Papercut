"""Parse regressions, checked against your own processed library.

Papers are copyrighted, so they aren't in the repository: these tests look
up papers in the local library by title and skip any that aren't there. Each
case is a parsing bug that was fixed once and must stay fixed. They check the
stored paper.json, so reprocess a paper (or run scripts/process_corpus.py)
after changing the parser, then run:

    .venv\\Scripts\\python -m pytest tests
"""

import re

import pytest

from app import catalog, library

_papers = None


def paper(title_start: str) -> dict:
    global _papers
    if _papers is None:
        _papers = {}
        for pid in catalog.paper_ids():
            p = library.load_paper(pid)
            if p:
                _papers[p["meta"]["title"].lower()] = p
    for title, p in _papers.items():
        if title.startswith(title_start.lower()):
            return p
    pytest.skip(f"“{title_start}” isn't in the local library")


def floats(p: dict) -> list[tuple[str, str]]:
    """(type, caption) of every figure and table, in order."""
    S = p["sentences"]
    return [(b["type"], " ".join(S[s]["text"] for s in b.get("caption_sentences", [])))
            for b in p["blocks"] if b["type"] in ("figure", "table")]


def loose_captions(p: dict) -> list[str]:
    """Figure/table/listing captions left in the text, with no float."""
    S = p["sentences"]
    return [" ".join(S[s]["text"] for s in b["sentences"]) for b in p["blocks"] if b["type"] == "caption"]


def captioned(p: dict, label: str) -> list[tuple[str, str]]:
    return [f for f in floats(p) if re.match(rf"{label}\b", f[1])]


def test_react_figure_2_is_one_image():
    # Two plots side by side under one caption were split into two images.
    p = paper("ReAct: Synergizing")
    assert len(captioned(p, "Figure 2")) == 1


def test_attention_figure_2_is_one_image():
    assert len(captioned(paper("Attention Is All You Need"), "Figure 2")) == 1


def test_swebench_tables_2_and_3_are_separate():
    # Two tables side by side were once cropped as one.
    p = paper("SWE-bench: Can Language Models")
    assert len(captioned(p, "Table 2")) == 1 and len(captioned(p, "Table 3")) == 1


def test_deepseek_boxed_tables_and_listings_are_images():
    # Boxed text (no grid) was read as paragraphs; listings running over a
    # page break stayed text.
    p = paper("DeepSeek-R1: Incentivizing")
    for label in ["Table 1", "Table 2"] + [f"Listing {n}" for n in range(1, 8)]:
        assert len(captioned(p, label)) == 1, label
    assert not [c for c in loose_captions(p) if c.startswith("Listing")]


def test_opensage_listings_are_images():
    p = paper("OpenSage: Self-programming")
    assert len([f for f in floats(p) if f[1].startswith("Listing")]) >= 4


@pytest.mark.parametrize("pid", catalog.paper_ids() or ["none"])
def test_every_paper_is_sound(pid):
    if pid == "none":
        pytest.skip("empty library")
    p = library.load_paper(pid)
    if not p:
        pytest.skip("not processed yet")
    S = p["sentences"]
    ids = [s for b in p["blocks"] for s in b.get("sentences", []) + b.get("caption_sentences", [])]
    assert len(ids) == len(set(ids)), "a sentence belongs to two blocks"
    assert set(ids) <= set(S), "a block points at a missing sentence"
    for b in p["blocks"]:
        if b.get("image"):
            assert (library.paper_dir(pid) / b["image"]).exists(), f"missing crop {b['image']}"
    # Highlights and summary/cards point at real sentences.
    assert set(p.get("ai_labels", {})) <= set(S)
    for key in ("results", "contributions", "limitations"):
        for it in (p.get("summary") or {}).get(key, []):
            assert set(it["highlights"]) <= set(S)
