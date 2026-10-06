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

import hashlib
import re
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
                           "big": big,
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
    """A big delimiter or operator (TeX's cmex glyphs) hangs far below the box
    the PDF gives it: follow its ink down from the box, in its own column,
    until a blank gap."""
    size = c["size"]
    clip = fitz.Rect(c["x0"], c["y0"], c["x1"], c["y0"] + 4 * size) & page.rect
    if clip.is_empty:
        return
    zoom = 4
    pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    rows = (img < 170).any(axis=1)
    last, blank = None, 0
    for i, v in enumerate(rows):
        if v:
            last, blank = i, 0
        elif last is not None:
            blank += 1
            if blank > 0.12 * size * zoom:
                break
    if last is not None:
        c["gy1"] = max(c["gy1"], clip.y0 + (last + 1) / zoom)


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
        # The glyphs of other words that reach into the crop, to white out;
        # the formula's own (small) glyphs, to keep even where those overlap.
        # Everything else in the crop stays: bars, radicals, big delimiters.
        mine = set(members)
        others = []
        for j in {j for c in cl for j in near(c)}:
            o = chars[j]
            if j in mine:
                continue
            box = fitz.Rect(o["x0"], o["oy"] - 0.72 * o["size"], o["x1"], o["oy"] + 0.22 * o["size"])
            if box.intersects(rect):
                others.append(tuple(box))
        own = [(c["x0"], c["gy0"], c["x1"], c["gy1"]) for c in cl if not c["big"] and not _hard(c["c"])]
        formulas.append({"rect": rect, "baseline": baseline, "size": cl[0]["main"], "lines": {c["line"] for c in cl},
                         "others": others, "own": own})

    lines = {ln for f in formulas for ln in f["lines"]}
    out_chars = [((c["x0"] + c["x1"]) / 2, (c["y0"] + c["y1"]) / 2, keep.get(c["cluster"]), c["c"])
                 for c in chars if c["line"] in lines]
    for f in formulas:
        del f["lines"]
    return formulas, out_chars


def crop(page: fitz.Page, f: dict, dpi: int) -> tuple[bytes, dict] | None:
    """Render a formula, trimmed to its ink. Returns (png, metrics in em of
    the surrounding text: width, height, depth below the baseline)."""
    rect = (f["rect"] + (-1, -1, 1, 1)) & page.rect
    pix = page.get_pixmap(clip=rect, dpi=dpi, colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    # Other words' glyphs (the descenders of the line above, a neighbouring
    # word) are whited out.
    px_pt = pix.width / rect.width
    mask = np.ones(img.shape, dtype=bool)

    def paint(boxes, value, pad):
        for x0, y0, x1, y1 in boxes:
            mask[max(0, int((y0 - pad - rect.y0) * px_pt)):max(0, int((y1 + pad - rect.y0) * px_pt) + 1),
                 max(0, int((x0 - pad - rect.x0) * px_pt)):max(0, int((x1 + pad - rect.x0) * px_pt) + 1)] = value
    paint(f["others"], False, 0.3)
    paint(f["own"], True, 0.2)
    ink = (img < 200) & mask
    rows, cols = np.where(ink.any(axis=1))[0], np.where(ink.any(axis=0))[0]
    if not len(rows) or not len(cols):
        return None
    scale = rect.width / pix.width  # pt per px
    pad = 1  # px of margin, so antialiased edges aren't cut
    r0, r1 = max(0, rows[0] - pad), min(pix.height, rows[-1] + 1 + pad)
    c0, c1 = max(0, cols[0] - pad), min(pix.width, cols[-1] + 1 + pad)
    tight = fitz.Rect(rect.x0 + c0 * scale, rect.y0 + r0 * scale, rect.x0 + c1 * scale, rect.y0 + r1 * scale)
    color = page.get_pixmap(clip=rect, dpi=dpi)  # same grid as the mask
    rgb = np.frombuffer(color.samples, dtype=np.uint8).reshape(color.height, color.width, color.n).copy()
    rgb[~mask] = 255
    rgb = np.ascontiguousarray(rgb[r0:r1, c0:c1, :3])
    out = fitz.Pixmap(fitz.csRGB, rgb.shape[1], rgb.shape[0], rgb.tobytes(), False)
    png = out.tobytes("png")
    size = f["size"]
    return png, {"w": round(tight.width / size, 3), "h": round(tight.height / size, 3),
                 "d": round((tight.y1 - f["baseline"]) / size, 3)}


def file_name(page_no: int, k: int, png: bytes) -> str:
    return f"inl-{page_no}-{k}-{hashlib.sha1(png).hexdigest()[:8]}.png"
