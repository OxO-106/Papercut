"""Sentence splitting, and mapping each sentence back to word boxes on the PDF page.

Docling gives us each text element's text and its box on the page. PyMuPDF gives
us every word with its own box. We align the element's text against the words
inside its box, character by character on a normalized form (NFKC, lowercase,
alphanumerics only), so hyphenation, ligatures and spacing differences don't
break the match. Each sentence then gets the boxes of the words it covers,
merged per text line.
"""

import re
import unicodedata
from difflib import SequenceMatcher

import pysbd

_segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)

# pysbd is conservative; these fix-ups catch common academic false splits.
_NO_SPLIT_AFTER = re.compile(r"(\b(?:et al|e\.g|i\.e|Fig|Figs|Eq|Eqs|Sec|Tab|Ref|vs|cf|approx|resp)\.\s*)$", re.I)


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Return (start, end) character spans of sentences, trimmed of whitespace."""
    spans = []
    for s in _segmenter.segment(text):
        start, end = s.start, s.end
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            spans.append([start, end])
    merged: list[list[int]] = []
    for span in spans:
        if merged:
            prev_text = text[merged[-1][0]:merged[-1][1]]
            next_text = text[span[0]:span[1]]
            # Join if the previous piece ends in a known abbreviation, or the next
            # starts lowercase / with a digit (e.g. "Fig. 3 shows").
            if _NO_SPLIT_AFTER.search(prev_text + " ") or next_text[:1].islower() or next_text[:1].isdigit():
                merged[-1][1] = span[1]
                continue
        merged.append(span)
    return [(a, b) for a, b in merged]


def _normalize(s: str) -> tuple[str, list[int]]:
    """Normalized string plus, for each normalized char, its index in the input."""
    out, idx = [], []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if c.isalnum():
                out.append(c)
                idx.append(i)
    return "".join(out), idx


def _align(tnorm: str, words: list[tuple]) -> tuple[dict[int, int], str]:
    """Map normalized text positions to word indices."""
    wchars, wmap = [], []
    for wi, w in enumerate(words):
        n, _ = _normalize(w[5])
        wchars.append(n)
        wmap.extend([wi] * len(n))
    wnorm = "".join(wchars)
    t2w: dict[int, int] = {}
    if tnorm and wnorm:
        sm = SequenceMatcher(None, tnorm, wnorm, autojunk=False)
        for a, b, size in sm.get_matching_blocks():
            for k in range(size):
                t2w[a + k] = wmap[b + k]
    return t2w, wnorm


def restore_spaces(text: str, words: list[tuple]) -> str:
    """Some PDFs position words without space characters, and Docling glues
    them ("helpshapetheirfuture"). PyMuPDF infers word breaks from glyph gaps.
    Wherever one Docling token covers several PyMuPDF words, insert a space."""
    if not words:
        return text
    tnorm, tidx = _normalize(text)
    t2w, _ = _align(tnorm, words)
    inserts = []
    for p in range(1, len(tnorm)):
        if p not in t2w or p - 1 not in t2w or t2w[p] == t2w[p - 1]:
            continue
        a, b = tidx[p - 1], tidx[p]
        if any(c.isspace() for c in text[a + 1:b]):
            continue  # already separated
        prev = words[t2w[p - 1]]
        if prev[5].endswith("-"):
            continue  # line-end hyphenation: "fine-" + "grained"
        # Keep the previous word's trailing punctuation with it: "al.," + "2023"
        trailing = len(prev[5]) - len(prev[5].rstrip(".,;:!?)]}'\"”’"))
        inserts.append(min(a + 1 + trailing, b))
    for pos in reversed(inserts):
        text = text[:pos] + " " + text[pos:]
    return text


def map_sentences(text: str, spans: list[tuple[int, int]], words: list[tuple]) -> list[tuple[list[dict], float, dict]]:
    """For each sentence span, return (rects, coverage, styles).

    words: (page_no, x0, y0, x1, y1, text, line_key, style) in reading order;
    style has bit 1 for bold and bit 2 for italic.
    rects: [{"page": n, "bbox": [x0, y0, x1, y1]}], one per text line touched.
    coverage: fraction of the sentence's normalized characters found on the page.
    styles: {"bold": [[start, end], ...], "italic": [...]}: character ranges
    (relative to the sentence) set in bold or italic in the PDF, e.g. run-in
    headings like "Evaluation metrics." or "Contributions."
    """
    tnorm, tidx = _normalize(text)
    t2w, _ = _align(tnorm, words)  # text-normalized position -> word index

    results = []
    pos = 0
    for start, end in spans:
        # normalized positions whose source char lies in [start, end)
        while pos < len(tidx) and tidx[pos] < start:
            pos += 1
        first = pos
        while pos < len(tidx) and tidx[pos] < end:
            pos += 1
        total = pos - first
        hit = [t2w[p] for p in range(first, pos) if p in t2w]
        coverage = len(hit) / total if total else 1.0

        lines: dict[tuple, list[float]] = {}
        order = []
        for wi in sorted(set(hit)):
            page, x0, y0, x1, y1, _, line_key, _ = words[wi]
            key = (page, line_key)
            if key not in lines:
                lines[key] = [x0, y0, x1, y1]
                order.append(key)
            else:
                r = lines[key]
                r[0], r[1], r[2], r[3] = min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)
        rects = [{"page": k[0], "bbox": [round(v, 2) for v in lines[k]]} for k in order]
        styled = [(tidx[p] - start, words[t2w[p]][7]) for p in range(first, pos) if p in t2w]
        sentence = text[start:end]
        results.append((rects, round(coverage, 3), {
            "bold": _ranges(sentence, [c for c, st in styled if st & 1]),
            "italic": _ranges(sentence, [c for c, st in styled if st & 2]),
        }))
    return results


def _ranges(sentence: str, chars: list[int]) -> list[list[int]]:
    """Merge bold character positions into ranges, bridging the spaces and
    punctuation between bold words and keeping trailing punctuation ("metrics.")."""
    out: list[list[int]] = []
    for c in chars:
        if out and all(not ch.isalnum() for ch in sentence[out[-1][1]:c]):
            out[-1][1] = c + 1
        else:
            out.append([c, c + 1])
    for r in out:
        while r[0] > 0 and not sentence[r[0] - 1].isalnum() and not sentence[r[0] - 1].isspace():
            r[0] -= 1  # opening quote or bracket: "'Oracle' retrieval."
        while r[1] < len(sentence) and not sentence[r[1]].isalnum() and not sentence[r[1]].isspace():
            r[1] += 1
    return out
