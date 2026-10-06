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


def _align(tnorm: str, words: list[tuple], formula_of: dict | None = None) -> tuple[dict[int, int], str, dict[int, int]]:
    """Map normalized text positions to word indices, and to the code of
    special characters (1 subscript, 2 superscript, plus 3 in a math font).
    Positions in an inline formula shown as a crop are listed in `formula_of`
    (text position -> formula id), filled when given."""
    wchars, wmap, wkind, wform = [], [], [], []
    for wi, w in enumerate(words):
        n, idx = _normalize(w[5])
        wchars.append(n)
        wmap.extend([wi] * len(n))
        scripts = w[8] if len(w) > 8 else None
        if scripts:
            # scripts has one entry per non-space character of the word's text
            nonspace = [i for i, c in enumerate(w[5]) if not c.isspace()]
            pos = {ci: k for k, ci in enumerate(nonspace)}
            wkind.extend(int(scripts[pos[i]]) if pos.get(i, len(scripts)) < len(scripts) else 0 for i in idx)
        else:
            wkind.extend([0] * len(n))
        forms = w[9] if len(w) > 9 else None
        if forms:
            nonspace = [i for i, c in enumerate(w[5]) if not c.isspace()]
            pos = {ci: k for k, ci in enumerate(nonspace)}
            wform.extend(forms[pos[i]] if pos.get(i, len(forms)) < len(forms) else None for i in idx)
        else:
            wform.extend([None] * len(n))
    wnorm = "".join(wchars)
    t2w: dict[int, int] = {}
    t2k: dict[int, int] = {}
    if tnorm and wnorm:
        sm = SequenceMatcher(None, tnorm, wnorm, autojunk=False)
        for a, b, size in sm.get_matching_blocks():
            for k in range(size):
                t2w[a + k] = wmap[b + k]
                if wkind[b + k]:
                    t2k[a + k] = wkind[b + k]
                if formula_of is not None and wform[b + k]:
                    formula_of[a + k] = wform[b + k]
    return t2w, wnorm, t2k


def restore_spaces(text: str, words: list[tuple]) -> str:
    """Some PDFs position words without space characters, and Docling glues
    them ("helpshapetheirfuture"). PyMuPDF infers word breaks from glyph gaps.
    Wherever one Docling token covers several PyMuPDF words, insert a space."""
    if not words:
        return text
    tnorm, tidx = _normalize(text)
    t2w = _align(tnorm, words)[0]
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
    styles: {"bold": [[start, end], ...], "italic", "sub", "sup", "math"}:
    character ranges (relative to the sentence) set in bold or italic in the
    PDF, e.g. run-in headings like "Evaluation metrics." or "Contributions.",
    subscripts/superscripts (the t of o_t, the t-1 of a_{t-1}), and text set
    in a math font ("o_t ∈ O").
    """
    tnorm, tidx = _normalize(text)
    t2f: dict[int, str] = {}
    t2w, _, t2k = _align(tnorm, words, t2f)  # text-normalized position -> word index, script kind, formula

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
            page, x0, y0, x1, y1, _, line_key = words[wi][:7]
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
            "sub": _script_ranges(sentence, sorted(tidx[p] - start for p in range(first, pos) if t2k.get(p, 0) % 3 == 1)),
            "sup": _script_ranges(sentence, sorted(tidx[p] - start for p in range(first, pos) if t2k.get(p, 0) % 3 == 2)),
            "math": _math_ranges(sentence, sorted(tidx[p] - start for p in range(first, pos) if t2k.get(p, 0) >= 3)),
            "formula": _formula_ranges(sentence, [(tidx[p] - start, t2f[p]) for p in range(first, pos) if p in t2f],
                                       {tidx[p] - start for p in range(first, pos) if p in t2w and p not in t2f}),
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


def _script_ranges(sentence: str, chars: list[int]) -> list[list[int]]:
    """Merge subscript characters into ranges, bridging only symbols between
    them ("t-1", "t+1"), never spaces."""
    out: list[list[int]] = []
    for c in chars:
        gap = sentence[out[-1][1]:c] if out else None
        if out and all(not ch.isalnum() and not ch.isspace() for ch in gap) and len(gap) <= 2:
            out[-1][1] = c + 1
        else:
            out.append([c, c + 1])
    return out


def _math_ranges(sentence: str, chars: list[int]) -> list[list[int]]:
    """Merge math-font characters into formula stretches, bridging the spaces,
    operators and brackets between them ("o_t ∈ O", "π(a_t|c_t)"), which the
    character alignment can't see."""
    out: list[list[int]] = []
    for c in chars:
        gap = sentence[out[-1][1]:c] if out else None
        if out and len(gap) <= 4 and all(not ch.isalnum() for ch in gap):
            out[-1][1] = c + 1
        else:
            out.append([c, c + 1])
    for r in out:  # take in brackets that close the formula: "π(a|c)" not "π(a|c"
        while r[1] < len(sentence) and sentence[r[1]] in ")]}|'′":
            r[1] += 1
    return out


_FORMULA_EDGE = set("\ufffd˜ˆ¯˙¨ˇ⌊⌋⌈⌉")


def _formula_ranges(sentence: str, chars: list[tuple[int, str]], plain: set[int] = frozenset()) -> list[list]:
    """[start, end, formula id] for each inline formula shown as a crop: from
    its first to its last character found in the text, widened over the
    characters beside it that match nothing else on the page (the rest of a
    token, "˜ c" before "mp", accents and brackets the PDF has no match for),
    which the crop shows. `plain`: positions matched to text outside any
    formula. Overlapping stretches keep the first."""
    span: dict[str, list[int]] = {}
    for c, fid in chars:
        r = span.setdefault(fid, [c, c + 1])
        r[0], r[1] = min(r[0], c), max(r[1], c + 1)
    out: list[list] = []
    def loose(i):  # a character the crop can stand for
        return i not in plain and not sentence[i].isspace()

    def token(i, j):  # the stretch of non-spaces around [i, j)
        while i > 0 and not sentence[i - 1].isspace():
            i -= 1
        while j < len(sentence) and not sentence[j].isspace():
            j += 1
        return i, j

    for fid, (a, b) in sorted(span.items(), key=lambda x: x[1][0]):
        # The rest of a short token ("cmp" when only "mp" matched).
        ta, tb = token(a, a + 1)
        if (tb - ta <= 4 and sentence[ta:a].isalnum()) or all(loose(i) and sentence[i].isalnum() for i in range(ta, a)):
            a = ta
        else:  # only letters and digits: an unmatched "=" or "(" before is text
            while a > 0 and loose(a - 1) and sentence[a - 1].isalnum():
                a -= 1
        # An accented letter just before, which the crop shows: "˜𝑉 cmp", "˜ cmp".
        while a > 1 and sentence[a - 1] == " ":
            if sentence[a - 2] in _FORMULA_EDGE:  # "and˜ cmp": just the accent
                a -= 2
            elif a > 2 and sentence[a - 2].isalpha() and sentence[a - 3] in _FORMULA_EDGE:  # "˜𝑉 cmp"
                a -= 3
            else:
                break
        while a > 0 and sentence[a - 1] in _FORMULA_EDGE:
            a -= 1
        ta, tb = token(b - 1, b)
        if tb - ta <= 4 or all(loose(i) for i in range(b, tb)):
            b = tb
        while b < len(sentence) and sentence[b] in _FORMULA_EDGE:
            b += 1
        # A closing bracket the PDF has no match for, after a space: "… ⌋", "… �".
        while b + 1 < len(sentence) and sentence[b] == " " and sentence[b + 1] in _FORMULA_EDGE                 and (b + 2 == len(sentence) or not sentence[b + 2].isalnum()):
            b += 2
        if out and a < out[-1][1]:
            continue
        out.append([a, b, fid])
    return out
