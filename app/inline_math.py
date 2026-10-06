"""Inline formulas the PDF's text layer can't carry.

A formula in running text often survives text extraction badly: stacked
scripts come out in a row ("p cmp t" for p_t^cmp), a fraction is flattened,
an accent loses its letter ("˜ cmp" for K̃^cmp), and some fonts have no
Unicode for their brackets ("�"). Such formulas are shown as small crops of
the PDF, set on the text's baseline; the text stays underneath for search,
copying and the AI. Simple ones (o_t, α_{t,i}) stay text, styled from the
sub/sup/math ranges.

A formula is found on the page as a cluster of characters: seeds (set in a
math font, raised or lowered, Greek or math symbols) and the characters that
touch them without a space (the p of p^cmp, the brackets), but not running
words. A cluster is cropped when it is complex: characters stacked over each
other (a subscript under a superscript, a fraction, an accent), or brackets
and glyphs text can't show.
"""

import re
import unicodedata
from collections import defaultdict

import numpy as np
import pymupdf as fitz

# Shown badly as text: no Unicode, floor/ceiling brackets, roots and big
# operators, spacing and combining accents.
_HARD = set("�⌊⌋⌈⌉√∑∏∫˜ˆ¯˙¨ˇ")


_ACCENTS = set("˜ˆ¯˙¨ˇ")
_BRACKETS = set("()[]{}|‖⟨⟩")


def _hard(c: str) -> bool:
    cp = ord(c)
    # (glyphs without Unicode come out as control or private-use characters)
    return c in _HARD or 0x300 <= cp <= 0x36F or 0x20D0 <= cp <= 0x20FF or cp < 0x20 or 0xE000 <= cp <= 0xF8FF


def _mathy(c: str) -> bool:
    cp = ord(c)
    return (_hard(c) or 0x1D400 <= cp <= 0x1D7FF or 0x370 <= cp <= 0x3FF
            or 0x2200 <= cp <= 0x22FF or 0x2190 <= cp <= 0x21FF or 0x27E8 <= cp <= 0x27EF)


def _page_chars(page: fitz.Page, math_font: re.Pattern) -> list[dict]:
    """Every visible character with its box, baseline and line, and whether
    it is a seed of a formula."""
    chars = []
    for block in page.get_text("rawdict")["blocks"]:
        for li, line in enumerate(block.get("lines", [])):
            spans = [sp for sp in line["spans"] if sp.get("chars")]
            if not spans:
                continue
            weight: dict[float, int] = defaultdict(int)
            for sp in spans:
                weight[round(sp["size"], 1)] += len(sp["chars"])
            main = max(weight, key=weight.get)
            base = sorted(sp["origin"][1] for sp in spans if abs(sp["size"] - main) < 0.3)
            baseline = base[len(base) // 2] if base else spans[0]["origin"][1]
            line_id = (id(block), li)
            run: list[dict] = []  # letters in a row, for telling words from symbols
            for sp in spans:
                dy = sp["origin"][1] - baseline
                small = sp["size"] <= 0.85 * main
                script = small and (dy > 0.08 * main or dy < -0.2 * main)
                in_math = bool(math_font.search(sp["font"].split("+")[-1]))
                for ch in sp["chars"]:
                    c = ch["c"]
                    if not c.strip():
                        run = []
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    oy = ch["origin"][1]
                    # Big operators and delimiters (∏, ∑, a matrix's parentheses)
                    # reach far above and below the line: use their whole box.
                    big = sp["size"] >= 1.2 * main or "cmex" in sp["font"].lower() or c in "∏∑∫"
                    full = big or _hard(c) and c not in _ACCENTS or c in _BRACKETS
                    # The glyph's own extent, roughly: the PDF's boxes span the
                    # font's whole ascent and descent and reach into the lines
                    # above and below.
                    rec = {"c": c, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "oy": oy,
                           "gy0": y0 if full else max(y0, oy - (1.1 if c in _ACCENTS else 0.8) * sp["size"]),
                           "gy1": y1 if full else (oy + 0.1 * sp["size"]) if c in _ACCENTS else min(y1, oy + 0.42 * sp["size"]),
                           "big": big, "cmex": "cmex" in sp["font"].lower(),
                           "font": sp["font"].split("+")[-1], "bold": bool(sp["flags"] & 16),
                           # Real math, not just an accented letter in a name ("Koč").
                           "strong": in_math or script or (_mathy(c) and c not in _ACCENTS and not 0x300 <= ord(c) <= 0x36F),
                           "size": sp["size"], "main": main, "baseline": baseline, "line": line_id,
                           "seed": in_math or script or _mathy(c), "cluster": None}
                    chars.append(rec)
                    if c.isalpha() and not rec["seed"]:
                        run.append(rec)
                        for r in run:
                            r["run"] = len(run)
                    else:
                        run = []
                        rec["run"] = 0
    _assign_rows(chars)
    for c in chars:
        if c["big"]:
            _measure_big(page, c)
    return chars


def _measure_big(page: fitz.Page, c: dict) -> None:
    """A big delimiter or operator (TeX's cmex glyphs) doesn't fit the box the
    PDF gives it: follow its ink down in its own column until a blank gap.
    CMEX boxes are the font's whole ascent and descent (about 3.7 em, far more
    than the ink, and reaching into the text around): they hang from their
    origin, so only the ink below it counts. Other big glyphs keep their box
    and may grow past it."""
    size = c["size"]
    top = c["oy"] - 0.15 * size if c["cmex"] else c["y0"]
    clip = fitz.Rect(c["x0"], top, c["x1"], top + 4 * size) & page.rect
    if clip.is_empty:
        return
    zoom = 4
    pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    rows = (img < 170).any(axis=1)
    first, last, blank = None, None, 0
    for i, v in enumerate(rows):
        if v:
            first = i if first is None else first
            last, blank = i, 0
        elif last is not None:
            blank += 1
            if blank > 0.12 * size * zoom:
                break
    if last is None:
        return
    bottom = clip.y0 + (last + 1) / zoom
    if c["cmex"]:
        c["gy0"], c["gy1"] = clip.y0 + first / zoom, min(c["y1"], bottom)
    else:
        c["gy1"] = max(c["gy1"], bottom)


def _assign_rows(chars: list[dict]) -> None:
    """The row of text each character belongs to: the nearest line of
    body-size text above or below which it sits as a script would (raised up
    to 0.8 of a line's text size, lowered up to 0.6). A script the PDF keeps
    as a line of its own thus joins its row, and a formula never reaches into
    the next row."""
    sizes: dict[float, int] = defaultdict(int)
    for c in chars:
        sizes[round(c["size"], 1)] += 1
    if not sizes:
        return
    body = max(sizes, key=sizes.get)
    # Rows by baseline: the PDF often breaks a row into pieces around a formula.
    lines: dict[int, list] = {}
    for c in chars:
        if c["main"] >= 0.85 * body:
            r = lines.setdefault(round(c["baseline"]), [c["baseline"], c["x0"], c["x1"], c["main"]])
            r[1], r[2] = min(r[1], c["x0"]), max(r[2], c["x1"])
    # Each line of the PDF's text goes to one row as a whole (its pieces of
    # a script, "[t]", stay together), by its baseline and extent.
    by_line: dict[tuple, list[dict]] = defaultdict(list)
    for c in chars:
        by_line[c["line"]].append(c)
    for key, members in by_line.items():
        oy = sorted(c["oy"] for c in members)[len(members) // 2]
        lx0, lx1 = min(c["x0"] for c in members), max(c["x1"] for c in members)
        best, row, rb, rm = None, key, members[0]["baseline"], members[0]["main"]
        for rkey, (base, x0, x1, m) in lines.items():
            d = oy - base
            cost = d if d >= 0 else -0.75 * d  # scripts and numerators rise further than they drop
            if x0 - 2 <= lx1 and lx0 <= x1 + 2 and abs(d) <= 1.2 * m and (best is None or cost < best):
                best, row, rb, rm = cost, rkey, base, m
        for c in members:
            c["row"], c["row_base"], c["row_main"] = row, rb, rm
    for c in chars:
        rb, rm = c["row_base"], c["row_main"]
        # A script the PDF set as a line of its own ("cmp" in K̃^cmp): small
        # and raised or lowered against its row, not just its own line.
        dy = c["oy"] - rb
        if not c["seed"] and c["size"] <= 0.85 * rm and (dy > 0.08 * rm or dy < -0.2 * rm):
            c["seed"] = c["strong"] = True


def _touching(a: dict, b: dict) -> bool:
    gap = max(a["x0"], b["x0"]) - min(a["x1"], b["x1"])  # < 0: they overlap across
    if gap > 0.22 * max(a["main"], b["main"]):
        return False
    if a["row"] != b["row"]:
        # Never into the next row of text, but a big bracket (whose origin sits
        # oddly) joins what it encloses, side by side.
        hard = _hard(a["c"]) or _hard(b["c"]) or a["big"] or b["big"]
        return hard and a["seed"] and b["seed"] and min(a["gy1"], b["gy1"]) > max(a["gy0"], b["gy0"])
    if a["line"] == b["line"]:
        return min(a["y1"], b["y1"]) > max(a["y0"], b["y0"])
    # A script the PDF set as a line of its own: only math joins it, right against it.
    vgap = max(a["gy0"], b["gy0"]) - min(a["gy1"], b["gy1"])
    return a["seed"] and b["seed"] and vgap < 0.15 * a["main"]


def _joinable(c: dict) -> bool:
    # Non-math characters join a formula unless they are part of a word.
    return c["seed"] or not c["c"].isalpha() or c.get("run", 0) <= 2


def _complex(cl: list[dict]) -> bool:
    if not any(c["strong"] for c in cl):
        return False
    if any(_hard(c["c"]) for c in cl):
        return True
    for i, a in enumerate(cl):
        for b in cl[i + 1:]:
            over = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
            narrow = min(a["x1"] - a["x0"], b["x1"] - b["x0"])
            if narrow > 0 and over > 0.4 * narrow and abs(a["oy"] - b["oy"]) > 0.3 * a["main"]:
                return True  # one above the other
    return False


def page_formulas(page: fitz.Page, math_font: re.Pattern, display: list = ()) -> tuple[list[dict], list[tuple]]:
    """The complex inline formulas on a page and the characters that matter
    for mapping them to words. `display`: boxes of the page's display
    equations, whose characters are left out.

    Returns (formulas, chars): formulas [{"rect", "baseline", "size"}];
    chars [(cx, cy, index in formulas or None, character)] for every character on the
    lines a formula touches."""
    chars = _page_chars(page, math_font)
    # Characters of display equations (shown as crops of their own) never
    # join an inline formula.
    shown = [fitz.Rect(b) for b in display]
    chars = [c for c in chars
             if not any(r.contains(fitz.Point((c["x0"] + c["x1"]) / 2, (c["y0"] + c["y1"]) / 2)) for r in shown)]
    grid: dict[tuple, list[int]] = defaultdict(list)
    for i, c in enumerate(chars):
        for gx in range(int(c["x0"] // 12), int(c["x1"] // 12) + 1):
            for gy in range(int(c["y0"] // 12), int(c["y1"] // 12) + 1):
                grid[gx, gy].append(i)

    def near(c):
        seen = set()
        for gx in range(int(c["x0"] // 12) - 1, int(c["x1"] // 12) + 2):
            for gy in range(int(c["y0"] // 12) - 1, int(c["y1"] // 12) + 2):
                for j in grid.get((gx, gy), ()):
                    if j not in seen:
                        seen.add(j)
                        yield j

    clusters: list[list[int]] = []
    for i, c in enumerate(chars):
        if not c["seed"] or c["cluster"] is not None:
            continue
        k = len(clusters)
        c["cluster"] = k
        todo, members = [i], [i]
        while todo:
            a = chars[todo.pop()]
            for j in near(a):
                b = chars[j]
                # Text joins only right beside math, not beside other text
                # (the p of p^cmp, a bracket; not "e.g.," before a formula).
                if b["cluster"] is None and _joinable(b) and (a["seed"] or b["seed"]) and _touching(a, b):
                    b["cluster"] = k
                    todo.append(j)
                    members.append(j)
        clusters.append(members)

    # Punctuation and unpaired brackets at a formula's edges stay with the
    # text ("values(K̃", "K̃_t,"): the text shows them already.
    for members in clusters:
        while len(members) > 1:
            members.sort(key=lambda i: chars[i]["x0"])
            texts = "".join(chars[i]["c"] for i in members)
            first, last = chars[members[0]], chars[members[-1]]
            if not first["seed"] and (first["c"] in ",.;:" or first["c"] in "([{" and not any(
                    ch in ")]}" for ch in texts)):
                first["cluster"] = None
                members.pop(0)
            elif not last["seed"] and (last["c"] in ",.;:" or last["c"] in ")]}" and not any(
                    ch in "([{" for ch in texts)):
                last["cluster"] = None
                members.pop()
            else:
                break

    # Columns of a matrix, or a big bracket and what it holds, can come apart
    # at the space between them: merge tall clusters side by side on a row.
    def extent(members):
        cl = [chars[i] for i in members]
        rows = defaultdict(int)
        for c in cl:
            rows[c["row"]] += 1
        small = all(c["size"] <= 0.85 * c["row_main"] for c in cl)
        return (min(c["x0"] for c in cl), min(c["gy0"] for c in cl), max(c["x1"] for c in cl),
                max(c["gy1"] for c in cl), max(rows, key=rows.get), max(c["main"] for c in cl), small)
    merged = True
    while merged:
        merged = False
        ext = [extent(m) if m else None for m in clusters]
        for i in range(len(clusters)):
            for j in range(len(clusters)):
                if i == j or not clusters[i] or not clusters[j]:
                    continue
                (ax0, ay0, ax1, ay1, ar, am, asm), (bx0, by0, bx1, by1, br, bm, bsm) = ext[i], ext[j]
                tall = ay1 - ay0 > 1.6 * am and by1 - by0 > 1.6 * am
                side = tall and ar == br and 0 <= bx0 - ax1 < 1.2 * am and min(ay1, by1) - max(ay0, by0) > 0.5 * am
                # A numerator right over its denominator: both in script size
                # (rows of prose are full size), most of their widths shared.
                over = min(ax1, bx1) - max(ax0, bx0)
                stacked = (asm and bsm and over > 0.5 * min(ax1 - ax0, bx1 - bx0)
                           and -2 <= by0 - ay1 < 0.5 * am)
                if side or stacked:
                    for k in clusters[j]:
                        chars[k]["cluster"] = i
                    clusters[i] += clusters[j]
                    clusters[j] = []
                    merged = True
                    break
            if merged:
                break

    formulas, keep = [], {}
    for k, members in enumerate(clusters):
        cl = [chars[i] for i in members]
        if len(cl) < 2 or not _complex(cl):
            continue
        rect = fitz.Rect(cl[0]["x0"], cl[0]["gy0"], cl[0]["x1"], cl[0]["gy1"])
        for c in cl[1:]:
            rect |= (c["x0"], c["gy0"], c["x1"], c["gy1"])
        body = [c for c in cl if abs(c["size"] - c["main"]) < 0.3]
        baseline = sorted(c["oy"] for c in body)[len(body) // 2] if body else cl[0]["baseline"]
        keep[k] = len(formulas)
        formulas.append({"rect": rect, "baseline": baseline, "size": cl[0]["main"], "lines": {c["line"] for c in cl},
                         "chars": cl})

    lines = {ln for f in formulas for ln in f["lines"]}
    out_chars = [((c["x0"] + c["x1"]) / 2, (c["y0"] + c["y1"]) / 2, keep.get(c["cluster"]), c["c"])
                 for c in chars if c["line"] in lines]
    for f in formulas:
        del f["lines"]
    return formulas, out_chars


# ---------- Formulas as MathML ----------
# A formula is rebuilt from its characters' sizes and positions: scripts
# (raised or lowered, smaller), stacked scripts (a′ᵢ), fractions (a
# script-size numerator over its denominator), accents over a letter, and big
# operators with their limits. The browser typesets the MathML in the page's
# math font, like the rest of the paper's math.

# TeX's cmex font has no Unicode in many PDFs: its glyphs come out as their
# codes (control characters, or letters: "X" for a display ∑, "Q" for ∏).
_CMEX = {
    0x00: "(", 0x01: ")", 0x02: "[", 0x03: "]", 0x04: "⌊", 0x05: "⌋", 0x06: "⌈", 0x07: "⌉",
    0x08: "{", 0x09: "}", 0x0A: "⟨", 0x0B: "⟩", 0x0C: "|", 0x0D: "‖", 0x0E: "/", 0x0F: "\\",
    0x10: "(", 0x11: ")", 0x12: "(", 0x13: ")", 0x14: "[", 0x15: "]", 0x16: "⌊", 0x17: "⌋",
    0x18: "⌈", 0x19: "⌉", 0x1A: "{", 0x1B: "}", 0x1C: "⟨", 0x1D: "⟩", 0x1E: "/", 0x1F: "\\",
    0x20: "(", 0x21: ")", 0x22: "[", 0x23: "]", 0x24: "⌊", 0x25: "⌋", 0x26: "⌈", 0x27: "⌉",
    0x28: "{", 0x29: "}", 0x2A: "⟨", 0x2B: "⟩", 0x2C: "/", 0x2D: "\\", 0x2E: "/", 0x2F: "\\",
    0x30: "(", 0x31: ")", 0x32: "[", 0x33: "]", 0x34: "[", 0x35: "]", 0x36: "[", 0x37: "]",
    0x38: "{", 0x39: "}", 0x3A: "{", 0x3B: "}", 0x3C: "{", 0x3D: "}", 0x3E: "|",
    0x40: "(", 0x41: ")", 0x42: "(", 0x43: ")", 0x44: "⟨", 0x45: "⟩", 0x46: "⊔", 0x47: "⊔",
    0x48: "∮", 0x49: "∮", 0x4A: "⊙", 0x4B: "⊙", 0x4C: "⊕", 0x4D: "⊕", 0x4E: "⊗", 0x4F: "⊗",
    0x50: "∑", 0x51: "∏", 0x52: "∫", 0x53: "⋃", 0x54: "⋂", 0x55: "⨄", 0x56: "⋀", 0x57: "⋁",
    0x58: "∑", 0x59: "∏", 0x5A: "∫", 0x5B: "⋃", 0x5C: "⋂", 0x5D: "⨄", 0x5E: "⋀", 0x5F: "⋁",
    0x60: "∐", 0x61: "∐", 0x62: "^", 0x63: "^", 0x64: "^", 0x65: "~", 0x66: "~", 0x67: "~",
    0x68: "[", 0x69: "]", 0x6A: "⌊", 0x6B: "⌋", 0x6C: "⌈", 0x6D: "⌉", 0x6E: "{", 0x6F: "}",
    0x70: "√", 0x71: "√", 0x72: "√", 0x73: "√", 0x74: "√",
}
# The pieces TeX builds the tallest brackets from (top, middle, bottom).
_CMEX_PIECES = set(range(0x30, 0x40)) | {0x42, 0x43}
# Adobe Symbol's pieces of tall brackets (private use): top, extension, bottom.
_SYMBOL_PIECES = {
    0xF8EB: "(", 0xF8EC: "(", 0xF8ED: "(", 0xF8EE: "[", 0xF8EF: "[", 0xF8F0: "[",
    0xF8F1: "{", 0xF8F2: "{", 0xF8F3: "{", 0xF8F4: "{", 0xF8F5: "∫",
    0xF8F6: ")", 0xF8F7: ")", 0xF8F8: ")", 0xF8F9: "]", 0xF8FA: "]", 0xF8FB: "]",
    0xF8FC: "}", 0xF8FD: "}", 0xF8FE: "}",
}
# Accents, spacing or combining, as MathML shows them over a letter.
_ACCENT_MARK = {"˜": "~", "ˆ": "^", "¯": "¯", "˙": "˙", "¨": "¨", "ˇ": "ˇ",
                "̃": "~", "̂": "^", "̄": "¯", "̅": "¯", "̇": "˙", "̈": "¨",
                "̌": "ˇ", "́": "´", "̀": "`", "⃗": "→"}
_ITALIC_FONT = re.compile(r"(?i:ital|oblique|cmmi|cmti|mmi\d|txmi|lmmi|-it$)|(?<=[a-z])(T|R|B|TB|RB)?I$")
_FENCES = set("()[]{}|‖⌊⌋⌈⌉⟨⟩")
# TeX's extension fonts and their clones (txfonts, pxfonts, Latin Modern) share cmex's codes.
_EXTENSION_FONT = re.compile(r"(?i)cmex|txex|pxex|lmex|euex|mathex")
_LIMIT_OPERATORS = set("∑∏⋃⋂⋀⋁∐")
_PRIMES = set("′″‴")


def _symbol(c: dict) -> str | None:
    """The character a glyph shows; None for one that shows nothing (or
    nothing known)."""
    ch, cp = c["c"], ord(c["c"])
    if _EXTENSION_FONT.search(c["font"]) and cp < 0x80:
        return _CMEX.get(cp)
    if cp in _SYMBOL_PIECES:
        return _SYMBOL_PIECES[cp]
    if 0xFE00 <= cp <= 0xFE0F or not ch.strip():
        return None
    if cp < 0x20 or 0xE000 <= cp <= 0xF8FF or ch == "�" or 0xD800 <= cp <= 0xDFFF:
        return None
    return ch


def _letter(ch: str) -> tuple[str, bool | None, bool]:
    """A math letter (𝑡, 𝛼, 𝐪) as the plain letter, whether its block is
    italic (None: not a math letter), and whether it is bold."""
    if 0x1D400 <= ord(ch) <= 0x1D7FF:
        name = unicodedata.name(ch, "")
        plain = unicodedata.normalize("NFKC", ch)
        if len(plain) == 1:
            return plain, "ITALIC" in name, "BOLD" in name
    if ch == "ℎ":  # Planck's h stands in for the missing italic h
        return "h", True, False
    return ch, None, False


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _glyphs(cl: list[dict]) -> list[dict]:
    """The formula's characters as MathML sees them: mapped to Unicode, the
    pieces of a tall bracket made one, accents tied to the letter under them."""
    out = []
    for c in sorted(cl, key=lambda c: (c["x0"], c["oy"])):
        sym = _symbol(c)
        if sym is None:
            continue
        g = dict(c, s=sym)
        ext = bool(_EXTENSION_FONT.search(c["font"]))
        piece = ord(c["c"]) in _SYMBOL_PIECES or ext and ord(c["c"]) in _CMEX_PIECES
        prev = next((p for p in reversed(out[-3:]) if p["s"] == sym and p.get("piece")
                     and min(p["x1"], g["x1"]) - max(p["x0"], g["x0"]) > 0), None) if piece else None
        if prev is not None:
            prev["gy0"], prev["gy1"] = min(prev["gy0"], g["gy0"]), max(prev["gy1"], g["gy1"])
            continue
        # Big: set apart from the text's levels (a tall bracket or operator).
        # The page's own flag also marks full-size letters on a line of scripts.
        g["piece"], g["big"] = piece, ext or piece or sym in "∑∏∫∮"
        out.append(g)
    letters = [g for g in out if g["s"] not in _ACCENT_MARK]
    for g in [g for g in out if g["s"] in _ACCENT_MARK]:
        mid = (g["x0"] + g["x1"]) / 2
        under = [b for b in letters if b["x0"] - 1 <= mid <= b["x1"] + 1 and not b.get("accent")
                 and b["oy"] >= g["oy"] - 0.2 * b["size"]]
        if under:
            base = min(under, key=lambda b: abs((b["x0"] + b["x1"]) / 2 - mid))
            base["accent"] = _ACCENT_MARK[g["s"]]
            out.remove(g)
    return out


def _italic(g: dict) -> tuple[str, bool, bool]:
    ch, italic, bold = _letter(g["s"])
    if italic is None:
        italic = ch.isalpha() and bool(_ITALIC_FONT.search(g["font"]))
    return ch, italic, bold or g.get("bold", False)


def _token(run: list[dict]) -> list[str]:
    """MathML for characters side by side on one level, without scripts."""
    parts, i = [], 0
    while i < len(run):
        g = run[i]
        ch, italic, bold = _italic(g)
        if ch.isdigit():
            j = i + 1
            while j < len(run) and (run[j]["s"].isdigit() or run[j]["s"] == "." and j + 1 < len(run)
                                    and run[j + 1]["s"].isdigit()) and not run[j - 1].get("accent"):
                j += 1
            node = f"<mn>{''.join(r['s'] for r in run[i:j])}</mn>"
            i = j
        elif ch.isalpha() and not italic:
            # Upright letters in a row are a word: a function name or a label ("prompt").
            j = i + 1
            while j < len(run) and not run[j - 1].get("accent"):
                nch, nit, _ = _italic(run[j])
                if not nch.isalpha() or nit:
                    break
                j += 1
            word = "".join(_italic(r)[0] for r in run[i:j])
            node = f'<mi mathvariant="normal">{_esc(word)}</mi>'
            i = j
        elif ch.isalpha():
            node = f"<mi>{_esc(ch)}</mi>"
            i += 1
        else:
            stretch = ' stretchy="false"' if ch in _FENCES and not g["big"] else ""
            node = f'<mo lspace="0" rspace="0">{_esc(ch)}</mo>' if ch in _PRIMES else f"<mo{stretch}>{_esc(ch)}</mo>"
            i += 1
        if bold and node.startswith(("<mi", "<mn")):
            node = node[:3] + ' class="b"' + node[3:]
        accent = run[i - 1].get("accent")
        if accent:
            node = f'<mover accent="true">{node}<mo>{_esc(accent)}</mo></mover>'
        parts.append(node)
    return parts


def _row(nodes: list[str]) -> str:
    return nodes[0] if len(nodes) == 1 else f"<mrow>{''.join(nodes)}</mrow>"


_page = None  # the page being read, to look for fraction bars


def _ink_rows(clip: fitz.Rect):
    """Each pixel row of a region of the page (4 px per pt): the share of it inked."""
    clip = clip & _page.rect
    if clip.is_empty or clip.width < 1:
        return None
    pix = _page.get_pixmap(clip=clip, matrix=fitz.Matrix(4, 4), colorspace=fitz.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 160


def _bar(up: list[dict], down: list[dict]) -> bool | None:
    """Whether a rule runs between two stacked groups (a fraction's bar);
    None without the page to look at."""
    if _page is None:
        return None
    x0 = max(min(g["x0"] for g in up), min(g["x0"] for g in down))
    x1 = min(max(g["x1"] for g in up), max(g["x1"] for g in down))
    y0 = max(g["oy"] for g in up)
    y1 = min(g["oy"] - 0.6 * g["size"] for g in down)
    ink = _ink_rows(fitz.Rect(x0, min(y0, y1), x1, max(y0, y1) + 0.5))
    return ink is not None and bool((ink.mean(axis=1) > 0.8).any())


def _frac(up: list[dict], down: list[dict], depth: int, bar: bool = True) -> str:
    thickness = "" if bar else ' linethickness="0"'  # a binomial's stack
    return f"<mfrac{thickness}>{_layout(up, depth + 1)}{_layout(down, depth + 1)}</mfrac>"


def _radicand_end(root: dict) -> float:
    """Where a radical's bar ends on the right (the radicand is under it)."""
    if _page is None:
        return root["x1"] + root["size"]
    ink = _ink_rows(fitz.Rect(root["x1"] - 0.5, root["y0"] - 1, root["x1"] + 30 * root["size"], root["y0"] + 0.4 * root["size"]))
    if ink is None:
        return root["x1"]
    best = 0
    for row in ink:
        n = 0
        while n < len(row) and row[n]:
            n += 1
        best = max(best, n)
    return root["x1"] - 0.5 + best / 4


def _split(scripts: list[dict], y: float, size: float) -> tuple[list, list]:
    """Scripts above and below a line at `y` (a baseline)."""
    up = [g for g in scripts if g["oy"] < y - 0.08 * size]
    return up, [g for g in scripts if g not in up]


def _stack(gs: list[dict]) -> tuple[list, list] | None:
    """Two groups one over the other (a fraction's or a binomial's halves),
    split at the widest vertical gap between characters; None if they
    aren't stacked."""
    ys = sorted(g["oy"] for g in gs)
    if len(ys) < 2:
        return None
    gap, at = max((b - a, i) for i, (a, b) in enumerate(zip(ys, ys[1:])))
    if gap < 0.45 * max(g["size"] for g in gs):
        return None
    up = [g for g in gs if g["oy"] <= ys[at]]
    down = [g for g in gs if g["oy"] > ys[at]]
    return (up, down) if _overlap(up, down) > 0.5 else None


def _overlap(a: list[dict], b: list[dict]) -> float:
    """How much of the narrower of two groups the other spans across."""
    if not a or not b:
        return 0
    ax0, ax1 = min(g["x0"] for g in a), max(g["x1"] for g in a)
    bx0, bx1 = min(g["x0"] for g in b), max(g["x1"] for g in b)
    return max(0, min(ax1, bx1) - max(ax0, bx0)) / max(1e-6, min(ax1 - ax0, bx1 - bx0))


def _layout(gs: list[dict], depth: int = 0, row: tuple | None = None) -> str:
    """MathML for one level of a formula (the whole, or a script, numerator
    or denominator) from its glyphs, left to right. `row`: the level's text
    size and baseline (the text row's, for the whole formula)."""
    if not gs:
        return "<mrow></mrow>"
    gs = sorted(gs, key=lambda g: g["x0"])
    if row:
        main, base_y = row
    else:
        sizes = sorted(g["size"] for g in gs if not g["big"]) or [gs[0]["size"]]
        main = sizes[-1]
        on_level = [g for g in gs if g["size"] >= 0.88 * main and not g["big"]]
        base_y = sorted(g["oy"] for g in on_level)[len(on_level) // 2] if on_level else gs[0]["oy"]

    def level(g):  # on this level: full size here, or a big operator, bracket or radical
        return g["big"] or g["s"] == "√" or (g["size"] >= 0.88 * main and abs(g["oy"] - base_y) < 0.3 * main)

    if depth > 4 or all(level(g) and g["s"] != "√" for g in gs):
        return _tight(_row(_token(gs)), depth)
    nodes, i = [], 0
    while i < len(gs):
        k = i
        if gs[i]["s"] == "√":
            end = _radicand_end(gs[i])
            j = i + 1
            while j < len(gs) and (gs[j]["x0"] < end - 0.5 or j == i + 1):
                j += 1
            nodes.append(f"<msqrt>{_layout(gs[i + 1:j], depth + 1) if j > i + 1 else '<mrow></mrow>'}</msqrt>")
        elif level(gs[i]):
            while k + 1 < len(gs) and level(gs[k + 1]) and gs[k + 1]["s"] != "√":
                k += 1
            base = gs[k]
            if k > i:
                nodes.extend(_token(gs[i:k]))
            j = k + 1
            while j < len(gs) and not level(gs[j]):
                j += 1
            if j > k + 1:
                nodes.append(_scripted(base, gs[k + 1:j], depth, base_y if base["big"] else base["oy"]))
            else:
                nodes.extend(_token([base]))
        else:  # script-size characters with nothing before them
            j = i
            while j < len(gs) and not level(gs[j]):
                j += 1
            nodes.append(_loose(gs[i:j], base_y, main, depth))
        i = j
    return _tight(_row(nodes), depth)


def _tight(xml: str, depth: int) -> str:
    """Below the top level (scripts, fractions, roots), operators get no
    space around them, as TeX sets them ("i+1", not "i + 1")."""
    if not depth:
        return xml
    return re.sub(r"<mo(?![^>]*lspace)", '<mo lspace="0" rspace="0"', xml)


def _loose(gs: list[dict], base_y: float, main: float, depth: int) -> str:
    """Script-size characters with no base: a numerator over its denominator,
    or a raised or lowered bit on its own."""
    stack = _stack(gs)
    if stack:
        return _frac(*stack, depth, _bar(*stack) is not False)
    up, down = _split(gs, base_y, main)
    if up and not down:
        return f"<msup><mrow></mrow>{_layout(up, depth + 1)}</msup>"
    if down and not up:
        return f"<msub><mrow></mrow>{_layout(down, depth + 1)}</msub>"
    return _row(_token(gs))


def _scripted(base: dict, scripts: list[dict], depth: int, y: float) -> str:
    """A base with its scripts (split at `y`, its baseline): sub/superscripts
    on the right, limits under and over a big operator; or what follows it
    is a fraction (a bar between), or a binomial's stack after a bracket."""
    b = _row(_token([base]))
    if base["s"] in "([{⟨":  # a bracket around a fraction or a binomial
        stack = _stack(scripts)
        if stack:
            return b + _frac(*stack, depth, _bar(*stack) is not False)
    up, down = _split(scripts, y, base["size"])
    if up and down and _overlap(up, down) > 0.5 and _bar(up, down):
        return b + _frac(up, down, depth)
    primes = [g for g in up if g["s"] in _PRIMES]
    if primes:  # a prime glyph already sits high: set it beside the letter, not raised again
        b = f"<mrow>{b}{''.join(_token(primes))}</mrow>"
        up = [g for g in up if g not in primes]
    sup = _layout(up, depth + 1) if up else None
    sub = _layout(down, depth + 1) if down else None
    limits = base["s"] in _LIMIT_OPERATORS and all(
        base["x0"] - 2 <= (g["x0"] + g["x1"]) / 2 <= base["x1"] + 2 for g in scripts)
    if limits:
        if sup and sub:
            return f"<munderover>{b}{sub}{sup}</munderover>"
        return f"<mover>{b}{sup}</mover>" if sup else f"<munder>{b}{sub}</munder>"
    if sup and sub:
        return f"<msubsup>{b}{sub}{sup}</msubsup>"
    return f"<msup>{b}{sup}</msup>" if sup else f"<msub>{b}{sub}</msub>"


def symbols(cl: list[dict]) -> list[str]:
    """The characters a formula shows, left to right."""
    return [g["s"] for g in _glyphs(cl)]


_OPENS, _CLOSES = "([{⟨⌊⌈", ")]}⟩⌋⌉"


def _trim_edges(gs: list[dict], text: str) -> list[dict]:
    """Leave out a bracket without its partner, or punctuation, at the
    formula's edges when the sentence's text shows it beside the formula
    rather than in it ("x[", "[z", "y,")."""
    text = text.strip()

    def unmatched(i, step):
        depth = 0
        rng = range(i, len(gs)) if step > 0 else range(i, -1, -1)
        opener, closer = (_OPENS, _CLOSES) if step > 0 else (_CLOSES, _OPENS)
        for j in rng:
            ch = gs[j]["s"]
            if ch in opener:
                depth += 1
            elif ch in closer:
                depth -= 1
                if depth == 0:
                    return False
        return True

    while gs and not text.startswith(gs[0]["s"]) and (
            gs[0]["s"] in ",.;:" or gs[0]["s"] in _OPENS and unmatched(0, 1) or gs[0]["s"] in _CLOSES):
        gs = gs[1:]
    while gs and not text.endswith(gs[-1]["s"]) and (
            gs[-1]["s"] in ",.;:" or gs[-1]["s"] in _CLOSES and unmatched(len(gs) - 1, -1) or gs[-1]["s"] in _OPENS):
        gs = gs[:-1]
    return gs


def to_mathml(cl: list[dict], page: fitz.Page | None = None, text: str | None = None) -> str:
    """The formula a cluster of characters shows, as inline MathML ("" if
    none of its glyphs can be shown). `page`, to tell fractions from
    binomials; `text`, the formula's stretch of the sentence, whose edges
    decide on brackets and punctuation there."""
    global _page
    gs = _glyphs(cl)
    if text is not None:
        gs = _trim_edges(gs, text)
    if not gs:
        return ""
    # The text row: its size, and the baseline of the formula's glyphs of
    # that size (a tall bracket's line can lend the row a baseline of its own).
    small = [g for g in gs if not g["big"]] or gs
    main = sorted(g["row_main"] for g in small)[len(small) // 2]
    level = [g["oy"] for g in small if g["size"] >= 0.88 * main]
    base = sorted(level)[len(level) // 2] if level else sorted(g["row_base"] for g in small)[len(small) // 2]
    _page = page
    try:
        return f"<math>{_layout(gs, row=(main, base))}</math>"
    finally:
        _page = None
