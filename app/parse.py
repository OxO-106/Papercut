"""PDF -> paper.json: Docling for structure, PyMuPDF for word boxes and crops."""

import hashlib
import os
import re
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from . import library
from .citations import Linker, clean_reference_entries
from .config import CROP_DPI, SCHEMA_VERSION
from .segment import _normalize, map_sentences, restore_spaces, split_sentences
from .xrefs import find_mentions, label_floats

Progress = Callable[[str, float], None]

_converter = None


def _get_converter():
    """Docling loads its layout models once; reuse the converter across papers."""
    global _converter
    if _converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = False  # born-digital PDFs only
        opts.do_table_structure = False  # tables are shown as image crops
        _converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
        try:
            _converter.initialize_pipeline(InputFormat.PDF)
        except Exception:
            # Models download on first run; afterwards, don't let a flaky
            # connection to Hugging Face stop us from using the cached copies.
            os.environ["HF_HUB_OFFLINE"] = "1"
            _converter.initialize_pipeline(InputFormat.PDF)
    return _converter


_REFERENCES = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography|literature cited)\s*$", re.I)
_APPENDIX = re.compile(r"^\s*(appendix|appendices|supplementary|[A-H](\.\d+)*[.\s]\s*\S)", re.I)
_CAPTION_START = re.compile(r"^\s*(Figure|Fig\.|Table|Tab\.)\s*[A-Z]?\d+\s*[:.|]", re.I)
_SENT_END = re.compile(r"[.!?:;][\"')\]]*\s*$")

TEXT_TYPES = {"paragraph", "list_item", "caption", "footnote", "references"}
FLOAT_TYPES = {"figure", "table", "footnote", "caption"}


def _provs(item, doc) -> list[tuple[int, tuple[float, float, float, float]]]:
    out = []
    for p in item.prov:
        h = doc.pages[p.page_no].size.height
        bb = p.bbox.to_top_left_origin(page_height=h)
        out.append((p.page_no, (bb.l, bb.t, bb.r, bb.b)))
    return out


def _crop(pdf: fitz.Document, paper_id: str, name: str, prov) -> dict | None:
    """Render a region of the page to a PNG. None if it would be empty
    (a speck, or a blank area Docling mistook for a picture)."""
    page_no, (x0, y0, x1, y1) = prov
    page = pdf[page_no - 1]
    rect = fitz.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2) & page.rect
    if rect.width < 12 or rect.height < 8:
        return None
    pix = page.get_pixmap(clip=rect, dpi=CROP_DPI)
    if pix.is_unicolor:
        return None
    # The file name carries a hash of the image, so a crop that changes on
    # re-parse gets a new URL: browsers can't keep showing a cached old image.
    png = pix.tobytes("png")
    fname = f"{name}-{hashlib.sha1(png).hexdigest()[:8]}.png"
    (library.assets_dir(paper_id) / fname).write_bytes(png)
    return {"image": f"assets/{fname}", "width_pt": round(rect.width, 1), "page": page_no}


def _label(item) -> str:
    return str(getattr(item.label, "value", item.label))


def parse_paper(paper_id: str, progress: Progress = lambda s, f: None) -> dict:
    pdf_file = library.pdf_path(paper_id)
    assets = library.assets_dir(paper_id)
    assets.mkdir(parents=True, exist_ok=True)
    for old in assets.glob("*.png"):  # crops from a previous parse
        old.unlink()

    progress("Parsing layout", 0.05)
    doc = _get_converter().convert(str(pdf_file)).document

    progress("Extracting figures and text", 0.6)
    pdf = fitz.open(pdf_file)

    # Captions attached to a figure/table are rendered with it, not in the text flow.
    owned_captions: dict[str, str] = {}
    for fl in list(doc.pictures) + list(doc.tables):
        for ref in fl.captions:
            owned_captions[ref.cref] = fl.self_ref

    raw: list[dict] = []  # blocks before sentence segmentation
    region = "front"  # front -> body -> references -> appendix
    n_float = 0

    def emit(block, item):
        if item.prov:
            page_no, bbox = _provs(item, doc)[0]
            block["_pos"] = (page_no, *bbox)
        raw.append(block)

    for item, _level in doc.iterate_items():
        ref = item.self_ref
        parent = getattr(getattr(item, "parent", None), "cref", "") or ""
        label = _label(item)

        if ref in owned_captions:
            continue
        if parent.startswith(("#/pictures", "#/tables")):
            continue  # text inside a figure (axis labels etc.)
        if label in ("page_header", "page_footer"):
            continue

        if label in ("picture", "chart", "table"):
            if not item.prov:
                continue
            n_float += 1
            kind = "table" if label == "table" else "figure"
            crop = _crop(pdf, paper_id, f"{kind}-{n_float}", _provs(item, doc)[0])
            if crop is None:
                continue
            block = {"type": kind, **crop}
            caps = []
            for cref in item.captions:
                cap = cref.resolve(doc)
                if cap.text.strip():
                    caps.append({"text": cap.text.strip(), "provs": _provs(cap, doc)})
            block["_captions"] = caps
            block["region"] = region
            emit(block, item)
            continue

        if label == "formula":
            if not item.prov:
                continue
            n_float += 1
            crop = _crop(pdf, paper_id, f"eq-{n_float}", _provs(item, doc)[0])
            if crop:
                emit({"type": "equation", **crop, "region": region}, item)
            continue

        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        if label == "title":
            emit({"type": "title", "text": text, "provs": _provs(item, doc), "region": "front"}, item)
            continue

        if label == "section_header":
            if _REFERENCES.match(text) or _REFERENCES.match(text.replace(" ", "")):  # "R E F E R E N C E S"
                region = "references"
            elif region == "references" and _starts_appendix(text):
                region = "appendix"
            elif re.match(r"^\s*(appendix|appendices)\b", text, re.I):
                region = "appendix"
            elif region == "front":
                region = "body"
            emit({"type": "heading", "level": getattr(item, "level", 1) or 1, "text": text,
                  "provs": _provs(item, doc), "region": region}, item)
            continue

        if label == "code":
            emit({"type": "code", "text": text, "region": region}, item)
            continue

        kind = {
            "list_item": "list_item",
            "footnote": "footnote",
            "caption": "caption",
        }.get(label, "paragraph")
        if region == "references":
            kind = "references"
        emit({"type": kind, "text": text, "provs": _provs(item, doc), "region": region}, item)

    words_by_page = _page_words(pdf)
    for b in raw:
        if b.get("provs") and b.get("text"):
            b["text"] = restore_spaces(b["text"], _words_in(words_by_page, b["provs"]))

    raw = _drop_text_inside_floats(raw)
    raw, title = _fix_front_matter(raw, pdf)

    # Captions Docling read as body text ("Table 3: BM25 recall ..."). Must run
    # before table detection (a caption like "Table 5: ... GPT-3.5" is number-
    # heavy) and before paragraphs are rejoined (or it gets glued onto body text).
    for b in raw:
        if b["type"] == "paragraph" and b["region"] != "front" and _CAPTION_START.match(b["text"]):
            b["type"] = "caption"
        # "Listing 3 | ..." read as a paragraph or as code (it sits on top of code).
        elif b["type"] in ("paragraph", "code") and b["region"] != "front" and _LISTING.match(b["text"]) \
                and len(b["text"]) < 300 and "\n" not in b["text"].strip():
            b["type"] = "caption"
    raw = _join_caption_lines(raw)
    raw, n_float = _tables_from_rules(raw, pdf, paper_id, n_float)

    # Tables Docling didn't recognise arrive as number-heavy text: show them as
    # crops. A header row next to such a table has fewer numbers, so it only
    # needs to look table-ish once its neighbour has been converted.
    def to_table(b):
        nonlocal n_float
        n_float += 1
        crop = _crop(pdf, paper_id, f"table-{n_float}", b["provs"][0])
        if crop:  # otherwise leave it as text
            b.update(type="table", **crop, _captions=[])
            del b["text"]

    candidates = lambda b: b["type"] == "paragraph" and b["region"] != "front" and b.get("provs")
    for b in raw:
        if candidates(b) and _looks_tabular(b["text"]):
            to_table(b)
    for i, b in enumerate(raw):
        near_table = any(0 <= j < len(raw) and raw[j]["type"] == "table" for j in (i - 1, i + 1))
        if candidates(b) and near_table and _looks_tabular(b["text"], loose=True):
            to_table(b)

    raw, n_float = _attach_captions(raw, pdf, paper_id, n_float)
    raw, n_float = _merge_split_figures(raw, pdf, paper_id, n_float)
    raw = _merge_split_paragraphs(raw)
    raw = _place_footnotes(raw)

    progress("Mapping sentences to the page", 0.8)
    blocks, sentences = [], {}
    counter = iter(range(1, 10**7))

    def add_sentences(text, provs):
        ids = []
        words = _words_in(words_by_page, provs)
        text = restore_spaces(text, words)  # captions haven't been through the pass above
        spans = split_sentences(text)
        for (start, end), (rects, cov, styles) in zip(spans, map_sentences(text, spans, words)):
            sid = f"s{next(counter)}"
            sentences[sid] = {"text": text[start:end], "rects": rects, "coverage": cov}
            sentences[sid].update({k: v for k, v in styles.items() if v})  # "bold" / "italic" ranges
            ids.append(sid)
        return ids

    for i, b in enumerate(raw):
        b["id"] = f"b{i + 1}"
        b.pop("_pos", None)
        if b["type"] in TEXT_TYPES and b["type"] != "references":
            b["sentences"] = add_sentences(b.pop("text"), b.pop("provs"))
        else:
            b.pop("provs", None)
        if "_captions" in b:
            b["caption_sentences"] = [sid for cap in b["_captions"] for sid in add_sentences(cap["text"], cap["provs"])]
            del b["_captions"]
        blocks.append(b)

    # Link in-text citations to reference-list entries.
    entries = clean_reference_entries([b for b in blocks if b["type"] == "references"])
    blocks = [b for b in blocks if not b.get("_merged")]
    linker = Linker(entries)
    for s in sentences.values():
        cites = linker.find(s["text"])
        if cites:
            s["cites"] = cites

    # Link "Figure 3" / "Table 2" mentions to their blocks, except a caption
    # mentioning its own figure.
    labels = label_floats(blocks, sentences)
    owner = {}
    for b in blocks:
        for sid in b.get("sentences", []) + b.get("caption_sentences", []):
            owner[sid] = b["id"]
    for b in blocks:
        if b.get("caption_block"):
            cap = next(c for c in blocks if c["id"] == b["caption_block"])
            for sid in cap.get("sentences", []):
                owner[sid] = b["id"]
    for sid, s in sentences.items():
        taken = [(a, e) for a, e, _ in s.get("cites", [])]
        refs = [x for x in find_mentions(s["text"], labels)
                if owner.get(sid) not in x[2] and not any(a < x[1] and x[0] < e for a, e in taken)]
        refs = [[a, e, [i for i in ids if i != owner.get(sid)]] for a, e, ids in refs]
        refs = [r for r in refs if r[2]]
        if refs:
            s["xrefs"] = refs

    pdf.close()
    src = library.source_info(paper_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": paper_id,
        "source": {"filename": src.get("filename"), "arxiv": src.get("arxiv"), "doi": src.get("doi"), "url": src.get("url")},
        "meta": {"title": title or Path(src.get("filename", "Untitled")).stem, "pages": len(doc.pages)},
        "status": {"parsed": True, "classified": False, "model": None, "error": None},
        "blocks": blocks,
        "sentences": sentences,
        "ai_labels": {},
        "user_edits": {},
    }


def _pdf_title(pdf: fitz.Document) -> str | None:
    """The title is the first run of the most prominent (largest, then bold)
    horizontal lines in the top half of page 1."""
    page = pdf[0]
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if line["dir"] != (1.0, 0.0) or line["bbox"][1] > page.rect.height / 2:
                continue  # skip rotated text (arXiv stamps) and the lower half
            spans = [s for s in line["spans"] if any(c.isalnum() for c in s["text"])]
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not spans or sum(c.isalnum() for c in text) < 4:
                continue
            lines.append((round(max(s["size"] for s in spans)), line["bbox"][1], line["bbox"][3], text))
    if not lines:
        return None
    lines.sort(key=lambda l: l[1])

    def first_run(best):
        run, last_bottom = [], None
        for size, top, bottom, text in lines:
            if best is not None and size != best:
                if run:
                    break
                continue
            if last_bottom is not None and top - last_bottom > size * 1.2:
                break  # a gap: the next line belongs to something else
            run.append(text)
            last_bottom = bottom
        return run

    run = first_run(max(size for size, *_ in lines))
    if len(run) > 4 or len(" ".join(run)) > 300:
        run = first_run(None)  # nothing stands out (e.g. a manuscript): take the top lines
    title = " ".join(run)
    return title if 3 <= len(title) <= 300 else None


def _fix_front_matter(raw: list[dict], pdf: fitz.Document) -> tuple[list[dict], str | None]:
    """Put title, authors and affiliations first, in page order.

    Docling sometimes reads page 1 of a two-column layout abstract-first, labels
    author names as headings, or merges the title into the author line. Anything
    on page 1 that sits entirely above the abstract is front matter.
    """
    anchor = next((b for b in raw if b.get("_pos", (0,))[0] == 1 and b["type"] == "heading"
                   and re.match(r"^\s*abstract\b", b["text"], re.I)), None)
    if anchor is None:
        # No "Abstract" heading: the first real prose paragraph on page 1 starts the body.
        anchor = next((b for b in raw if b.get("_pos", (0,))[0] == 1 and b["type"] == "paragraph"
                       and len(b["text"]) > 250 and _is_prose(b["text"])), None)

    textual = {"title", "heading", "paragraph", "list_item", "footnote"}
    raw = [b for b in raw if "text" not in b or any(c.isalnum() for c in b["text"])]
    if anchor is not None:
        top = anchor["_pos"][2]
        front = [b for b in raw if b is not anchor and b["type"] in textual
                 and b.get("_pos", (0,))[0] == 1 and b["_pos"][4] < top]
        front_ids = {id(b) for b in front}
        # Also keep anything Docling already put before the anchor.
        before = []
        for b in raw:
            if b is anchor:
                break
            if id(b) not in front_ids and b["region"] == "front":
                before.append(b)
        front = _grid_order(front) + before
        front_ids = {id(b) for b in front}
        raw = front + [b for b in raw if id(b) not in front_ids]
    else:
        front = [b for b in raw if b["region"] == "front"]

    for b in front:
        b["region"] = "front"
        if b["type"] in ("title", "heading"):
            b["type"] = "paragraph"  # author names, "Affiliations", ...
            b.setdefault("provs", [])

    title = _pdf_title(pdf)
    if not title:
        # Fonts don't reveal it: trust the first short text block on page 1.
        first = next((b for b in front if b.get("text")), None)
        if first is None or len(first["text"]) > 250:
            return raw, None
        title = first["text"]
    want, _ = _normalize(title)
    for b in front:
        have, idx = _normalize(b.get("text", ""))
        if not want or not have.startswith(want):
            continue
        cut = idx[len(want) - 1] + 1
        rest = b["text"][cut:].strip()
        if not any(c.isalnum() for c in rest):
            rest = ""  # e.g. the "?" ending a question title
        title = b["text"][:cut].strip() if rest else b["text"].strip()
        pos = raw.index(b)
        if rest:
            b["text"] = rest
            raw.insert(pos, {"type": "title", "text": title, "region": "front"})
        else:
            b["type"] = "title"
        break
    else:
        raw.insert(0, {"type": "title", "text": title, "region": "front"})
    # The title always leads.
    t = next(b for b in raw if b["type"] == "title")
    raw.remove(t)
    return [t] + raw, title


def _join_caption_lines(raw: list[dict]) -> list[dict]:
    """Docling sometimes splits a caption at a line break ("Table 4: New tasks
    with" / "zero-shot ToT and GPT-4."). Rejoin a caption that ends mid-sentence
    with the text block directly below it in the same column."""
    out: list[dict] = []
    for b in raw:
        prev = out[-1] if out else None
        if (prev and prev["type"] == "caption" and b["type"] == "paragraph" and prev.get("_pos") and b.get("_pos")
                and not _SENT_END.search(prev["text"])):
            p, px0, py0, px1, py1 = prev["_pos"]
            q, bx0, by0, bx1, by1 = b["_pos"]
            if p == q and 0 <= by0 - py1 <= 6 and min(px1, bx1) - max(px0, bx0) > 0.5 * min(px1 - px0, bx1 - bx0):
                prev["text"] += " " + b["text"]
                prev["provs"] = prev.get("provs", []) + b.get("provs", [])
                prev["_pos"] = (p, min(px0, bx0), py0, max(px1, bx1), by1)
                continue
        out.append(b)
    return out


def _caption_kind(text: str) -> str | None:
    m = _CAPTION_START.match(text)
    return None if not m else ("table" if m.group(1).lower().startswith("tab") else "figure")


def _ink_columns(page: fitz.Page, rect: fitz.Rect) -> list[bool]:
    """For each 1pt column of `rect`, whether anything is drawn there."""
    pix = page.get_pixmap(clip=rect, dpi=72, colorspace=fitz.csGRAY)
    data, w, h, stride = pix.samples, pix.width, pix.height, pix.stride
    return [any(data[y * stride + x] < 200 for y in range(h)) for x in range(w)]


def _trim(page: fitz.Page, rect: fitz.Rect, pad: float = 3) -> fitz.Rect:
    """Shrink `rect` to the drawn content inside it."""
    pix = page.get_pixmap(clip=rect, dpi=72, colorspace=fitz.csGRAY)
    data, w, h, stride = pix.samples, pix.width, pix.height, pix.stride
    xs = [x for x in range(w) if any(data[y * stride + x] < 200 for y in range(h))]
    ys = [y for y in range(h) if any(data[y * stride + x] < 200 for x in range(w))]
    if not xs or not ys:
        return rect
    sx, sy = rect.width / w, rect.height / h
    return fitz.Rect(rect.x0 + xs[0] * sx - pad, rect.y0 + ys[0] * sy - pad,
                     rect.x0 + (xs[-1] + 1) * sx + pad, rect.y0 + (ys[-1] + 1) * sy + pad) & rect


def _attach_captions(raw: list[dict], pdf: fitz.Document, paper_id: str, n_float: int) -> tuple[list[dict], int]:
    """Give uncaptioned figures/tables the loose captions Docling left beside them.

    One caption next to the image: it becomes the image's caption (rendered
    below it, as in the paper). Several captions side by side under one image
    (Docling merged e.g. Tables 2 and 3, printed next to each other): cut the
    image at the blank strips between the caption columns, trim each part, and
    give each its own caption.
    """
    loose = [i for i, b in enumerate(raw) if b["type"] == "caption" and b.get("_pos") and _caption_kind(b["text"])]
    used: set[int] = set()
    replace: dict[int, list[dict]] = {}

    for fi, f in enumerate(raw):
        if f["type"] not in ("figure", "table") or f.get("_captions") or not f.get("_pos"):
            continue
        page_no, fx0, fy0, fx1, fy1 = f["_pos"]
        near = [i for i in loose if i not in used and abs(i - fi) <= 4 and raw[i]["_pos"][0] == page_no
                and fx0 - 5 <= (raw[i]["_pos"][1] + raw[i]["_pos"][3]) / 2 <= fx1 + 5]
        if not near:
            continue
        near.sort(key=lambda i: raw[i]["_pos"][1])
        cols = []  # captions in distinct, non-overlapping columns
        for i in near:
            if not cols or raw[i]["_pos"][1] >= raw[cols[-1]]["_pos"][3] - 2:
                cols.append(i)
        cap = lambda i: {"text": raw[i]["text"], "provs": raw[i].get("provs", [])}

        if len(cols) == 1:
            i = cols[0]
            f["_captions"] = [cap(i)]
            f["type"] = _caption_kind(raw[i]["text"])
            used.add(i)
            continue

        page = pdf[page_no - 1]
        rect = fitz.Rect(fx0, fy0, fx1, fy1)
        ink = _ink_columns(page, rect)
        scale = rect.width / len(ink)
        cuts = []
        for left, right in zip(cols, cols[1:]):
            lc = ((raw[left]["_pos"][1] + raw[left]["_pos"][3]) / 2 - fx0) / scale
            rc = ((raw[right]["_pos"][1] + raw[right]["_pos"][3]) / 2 - fx0) / scale
            best, run_start = None, None
            for x in range(max(0, int(lc)), min(len(ink), int(rc)) + 1):
                blank = x < len(ink) and not ink[x]
                if blank and run_start is None:
                    run_start = x
                if (not blank or x == min(len(ink), int(rc))) and run_start is not None:
                    run = (run_start, x)
                    if best is None or run[1] - run[0] > best[1] - best[0]:
                        best = run
                    run_start = None
            if best is None or best[1] - best[0] < 3:
                break  # no clear gap: leave the image whole
            cuts.append(fx0 + (best[0] + best[1]) / 2 * scale)
        if len(cuts) != len(cols) - 1:
            continue

        edges = [fx0] + cuts + [fx1]
        parts = []
        for (x0, x1), i in zip(zip(edges, edges[1:]), cols):
            sub = _trim(page, fitz.Rect(x0, fy0, x1, fy1))
            n_float += 1
            kind = _caption_kind(raw[i]["text"])
            crop = _crop(pdf, paper_id, f"{kind}-{n_float}", (page_no, (sub.x0, sub.y0, sub.x1, sub.y1)))
            if crop is None:
                break
            parts.append({"type": kind, **crop, "_captions": [cap(i)], "region": f["region"],
                          "_pos": (page_no, sub.x0, sub.y0, sub.x1, sub.y1)})
        if len(parts) == len(cols):
            replace[fi] = parts
            used.update(cols)
            (library.assets_dir(paper_id) / f["image"].split("/")[-1]).unlink(missing_ok=True)  # the merged crop
        else:  # a part came out empty: keep the merged image, drop the parts made so far
            for part in parts:
                (library.assets_dir(paper_id) / part["image"].split("/")[-1]).unlink(missing_ok=True)

    out = []
    for i, b in enumerate(raw):
        if i in used:
            continue
        out.extend(replace.get(i, [b]))
    return out, n_float


def _boxes(b: dict) -> list:
    """A block's (page, bbox) boxes: its line provenance, else its overall
    position (code blocks only have the latter)."""
    if b.get("provs"):
        return b["provs"]
    if b.get("_pos"):
        page_no, x0, y0, x1, y1 = b["_pos"]
        return [(page_no, (x0, y0, x1, y1))]
    return []


_LISTING = re.compile(r"^\s*Listing\s*\d+\s*[:.|]", re.I)


def _page_rules(page: fitz.Page) -> list[tuple[float, float, float]]:
    """Horizontal rules on a page, as (y, x0, x1): lines and hairline boxes
    at least 60pt wide (booktabs rules are drawn either way)."""
    out = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 1 and abs(a.x - b.x) >= 60:
                    out.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x)))
            elif it[0] == "re":
                r = it[1]
                if r.height < 2.5 and r.width >= 60:
                    out.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
    return sorted(out)


def _tables_from_rules(raw: list[dict], pdf: fitz.Document, paper_id: str, n_float: int) -> tuple[list[dict], int]:
    """Tables that are boxed text rather than a grid (e.g. DeepSeek-R1's prompt
    template and "aha moment" tables): Docling reads the caption and then the
    contents as ordinary paragraphs or code, with no table at all. For a table
    caption with no figure/table beside it, find the horizontal rules just below
    it (or just above, for a caption under its table), follow rules of the same
    width until another caption or float intervenes, and crop that span as the
    table. Text blocks inside it leave the reading flow."""
    rules_cache: dict[int, list] = {}
    made: dict[int, dict] = {}  # caption index -> new table block

    for ci, c in enumerate(raw):
        text = c.get("text", "")
        if c["type"] != "caption" or not c.get("_pos") or not (_caption_kind(text) == "table" or _LISTING.match(text)):
            continue
        p, cx0, cy0, cx1, cy1 = c["_pos"]
        if any(0 <= j < len(raw) and raw[j]["type"] in ("figure", "table") and raw[j].get("_pos", (None,))[0] == p
               for j in range(ci - 2, ci + 3)):
            continue  # a real table/figure is right there: _attach_captions pairs them
        rules = rules_cache.setdefault(p, _page_rules(pdf[p - 1]))
        mid = (cx0 + cx1) / 2
        spans = [r for r in rules if r[1] - 5 <= mid <= r[2] + 5]
        # Where the chain must stop: other captions and floats on this page.
        stops = [b["_pos"] for k, b in enumerate(raw) if k != ci and b.get("_pos") and b["_pos"][0] == p
                 and (b["type"] in ("figure", "table") or (b["type"] == "caption" and _caption_kind(b.get("text", ""))))]

        def chain(start, direction):
            same = [r for r in spans if abs(r[1] - start[1]) < 12 and abs(r[2] - start[2]) < 12]
            out, prev = [start], start
            for r in (sorted(same) if direction > 0 else sorted(same, reverse=True)):
                if (r[0] - prev[0]) * direction <= 0.5:
                    continue
                lo, hi = sorted((prev[0], r[0]))
                if any(lo < (s[2] + s[4]) / 2 < hi for s in stops):
                    break
                out.append(r)
                prev = r
            return out

        below = [r for r in spans if cy1 - 1 <= r[0] <= cy1 + 45]
        above = [r for r in spans if cy0 - 45 <= r[0] <= cy0 + 1]
        found = chain(below[0], +1) if below else (chain(above[-1], -1) if above else [])
        if len(found) < 2:
            # No closing rule on this page: a listing that runs on over the
            # page break (DeepSeek-R1's Listings 2, 5, 6). Follow its monospace
            # lines instead, across pages, and stack the pieces into one image.
            segs = _monospace_run(pdf, p, cy1, below[0] if below else None, stops, raw, ci)
            if segs:
                n_float += 1
                crop = _crop_stack(pdf, paper_id, f"table-{n_float}", segs)
                if crop:
                    made[ci] = {"type": "table", **crop, "_captions": [{"text": c["text"], "provs": c.get("provs", [])}],
                                "region": c["region"], "_pos": segs[0], "_segs": segs}
            continue
        y0, y1 = min(r[0] for r in found), max(r[0] for r in found)
        x0, x1 = min(r[1] for r in found), max(r[2] for r in found)
        n_float += 1
        crop = _crop(pdf, paper_id, f"table-{n_float}", (p, (x0, y0 - 1, x1, y1 + 1)))
        if crop:
            made[ci] = {"type": "table", **crop, "_captions": [{"text": c["text"], "provs": c.get("provs", [])}],
                        "region": c["region"], "_pos": (p, x0, y0, x1, y1)}

    if not made:
        return raw, n_float

    def inside(b) -> bool:
        area = covered = 0.0
        for page_no, (x0, y0, x1, y1) in _boxes(b):
            area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            for t in made.values():
                for tp, tx0, ty0, tx1, ty1 in t.get("_segs") or [t["_pos"]]:
                    if tp == page_no:
                        covered += max(0.0, min(x1, tx1) - max(x0, tx0)) * max(0.0, min(y1, ty1) - max(y0, ty0))
        return area > 0 and covered / area >= 0.6

    out = []
    for i, b in enumerate(raw):
        if i in made:
            out.append(made[i])
        elif b["type"] in ("figure", "table", "title") or not inside(b):
            out.append(b)
    for t in made.values():
        t.pop("_segs", None)
    return out, n_float


def _mono_lines(page: fitz.Page) -> list[tuple[float, float, float, float, bool, str]]:
    """The page's text lines in reading order, as (x0, y0, x1, y1, monospace, text)."""
    out = []
    for blk in page.get_text("dict")["blocks"]:
        for line in blk.get("lines", []):
            text = "".join(s["text"] for s in line["spans"])
            if not text.strip():
                continue
            mono = sum(len(s["text"]) for s in line["spans"] if s["flags"] & 8)
            x0, y0, x1, y1 = line["bbox"]
            out.append((x0, y0, x1, y1, mono >= 0.6 * len(text.strip()), text.strip()))
    return sorted(out, key=lambda l: (round(l[1]), l[0]))


def _monospace_run(pdf: fitz.Document, p: int, cy1: float, rule, stops: list, raw: list[dict], ci: int) -> list[tuple]:
    """The monospace text that starts right under a listing caption on page p,
    followed onto later pages until the first line of prose (or another
    caption). Returns one (page, x0, y0, x1, y1) box per page, or [] when the
    caption isn't followed by monospace text or the run is too short to be a
    listing. The page number at the foot of each page is skipped."""
    segs, n_lines = [], 0
    x0, x1 = (rule[1], rule[2]) if rule else (None, None)
    for q in range(p, min(p + 8, len(pdf) + 1)):
        page = pdf[q - 1]
        foot = page.rect.height * 0.92
        top = (rule[0] if rule else cy1) if q == p else 0
        later_stops = [s for s in stops if q == p and s[2] > top] + \
                      [b["_pos"] for k, b in enumerate(raw) if k > ci and b.get("_pos") and b["_pos"][0] == q and q != p
                       and (b["type"] in ("figure", "table") or (b["type"] == "caption" and _caption_kind(b.get("text", ""))))]
        limit = min((s[2] for s in later_stops), default=page.rect.height)
        lines = [l for l in _mono_lines(page) if l[1] >= top - 1 and l[3] <= limit + 1
                 and not (l[1] > foot and re.fullmatch(r"\d{1,3}", l[5]))]
        run, ended = [], False
        for l in lines:
            if not l[4] and len(l[5]) > 3:
                ended = True
                break
            run.append(l)
        if q == p and not (run and run[0][4]):
            return []  # prose (or nothing) right under the caption: not a listing
        if run:
            n_lines += len(run)
            lx0, lx1 = min(l[0] for l in run), max(l[2] for l in run)
            segs.append((q, min(lx0, x0) if x0 is not None else lx0, run[0][1] - 2,
                         max(lx1, x1) if x1 is not None else lx1, run[-1][3] + 2))
        if ended or limit < page.rect.height or not run:
            break
    if n_lines < 3:
        return []
    # One width for every piece, so the stacked image lines up.
    lo, hi = min(s[1] for s in segs), max(s[3] for s in segs)
    return [(q, lo, y0, hi, y1) for q, _, y0, _, y1 in segs]


def _crop_stack(pdf: fitz.Document, paper_id: str, name: str, segs: list[tuple]) -> dict | None:
    """Like _crop, for a float that runs over several pages: render each
    page's piece and stack them into one image."""
    if len(segs) == 1:
        q, x0, y0, x1, y1 = segs[0]
        return _crop(pdf, paper_id, name, (q, (x0, y0, x1, y1)))
    from PIL import Image
    import io
    pieces = []
    for q, x0, y0, x1, y1 in segs:
        page = pdf[q - 1]
        rect = fitz.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2) & page.rect
        if rect.width < 12 or rect.height < 8:
            continue
        pieces.append(Image.open(io.BytesIO(page.get_pixmap(clip=rect, dpi=CROP_DPI).tobytes("png"))).convert("RGB"))
    if not pieces:
        return None
    width = max(im.width for im in pieces)
    gap = round(CROP_DPI / 72 * 6)  # a little space where the page broke
    out = Image.new("RGB", (width, sum(im.height for im in pieces) + gap * (len(pieces) - 1)), "white")
    y = 0
    for im in pieces:
        out.paste(im, (0, y))
        y += im.height + gap
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    png = buf.getvalue()
    fname = f"{name}-{hashlib.sha1(png).hexdigest()[:8]}.png"
    (library.assets_dir(paper_id) / fname).write_bytes(png)
    x0, x1 = segs[0][1], segs[0][3]
    return {"image": f"assets/{fname}", "width_pt": round(x1 - x0 + 4, 1), "page": segs[0][0]}


def _merge_split_figures(raw: list[dict], pdf: fitz.Document, paper_id: str, n_float: int) -> tuple[list[dict], int]:
    """The reverse of splitting side-by-side tables: Docling sometimes cuts one
    figure into several pictures (ReAct's Figure 2: two plots side by side,
    one caption) and gives the caption to only one of them. An uncaptioned
    picture next to a captioned one on the same page, under the same caption
    (its centre within the caption's width) and either beside it or directly
    above/below it, is part of that figure: crop the union as one image."""
    absorbed: set[int] = set()
    for fi, f in enumerate(raw):
        if f["type"] != "figure" or not f.get("_captions") or not f.get("_pos"):
            continue
        p, fx0, fy0, fx1, fy1 = f["_pos"]
        cap_boxes = [bb for c in f["_captions"] for pg, bb in c.get("provs", []) if pg == p]
        if not cap_boxes:
            continue
        cx0, cx1 = min(b[0] for b in cap_boxes), max(b[2] for b in cap_boxes)
        parts = []
        for gi in range(max(0, fi - 3), min(len(raw), fi + 4)):
            g = raw[gi]
            if gi == fi or gi in absorbed or g["type"] != "figure" or g.get("_captions") or not g.get("_pos"):
                continue
            gp, gx0, gy0, gx1, gy1 = g["_pos"]
            if gp != p or not (cx0 - 10 <= (gx0 + gx1) / 2 <= cx1 + 10):
                continue
            overlap_y = min(fy1, gy1) - max(fy0, gy0)
            beside = overlap_y >= 0.5 * min(fy1 - fy0, gy1 - gy0)
            stacked = min(abs(gy0 - fy1), abs(fy0 - gy1)) <= 25 and min(fx1, gx1) - max(fx0, gx0) > 0
            if beside or stacked:
                parts.append(gi)
        if not parts:
            continue
        boxes = [f["_pos"]] + [raw[gi]["_pos"] for gi in parts]
        y0, y1 = min(b[2] for b in boxes), max(b[4] for b in boxes)
        # The pieces' boxes can miss labels at the edges (Attention's
        # "Scaled Dot-Product Attention" title): widen to the caption's width,
        # then trim back to what is actually drawn.
        wide = fitz.Rect(min(cx0, *(b[1] for b in boxes)), y0, max(cx1, *(b[3] for b in boxes)), y1)
        r = _trim(pdf[p - 1], wide, pad=2)
        x0, x1 = r.x0, r.x1
        n_float += 1
        crop = _crop(pdf, paper_id, f"figure-{n_float}", (p, (x0, y0, x1, y1)))
        if not crop:
            continue
        for old in [f] + [raw[gi] for gi in parts]:
            (library.assets_dir(paper_id) / old["image"].split("/")[-1]).unlink(missing_ok=True)
        f.update(**crop, _pos=(p, x0, y0, x1, y1))
        absorbed.update(parts)
    return [b for i, b in enumerate(raw) if i not in absorbed], n_float


def _drop_text_inside_floats(raw: list[dict]) -> list[dict]:
    """Docling sometimes emits a table's cells (or a figure's labels) as body
    text as well as the table itself. That text is already in the table/figure
    image, and left in place it also gets glued onto the next paragraph. Drop
    text blocks that lie mostly inside a figure or table on the same page."""
    floats = [b["_pos"] for b in raw if b["type"] in ("figure", "table") and b.get("_pos")]

    def inside(block) -> bool:
        area = covered = 0.0
        for page_no, (x0, y0, x1, y1) in _boxes(block):
            a = max(0.0, x1 - x0) * max(0.0, y1 - y0)
            area += a
            best = 0.0
            for fp, fx0, fy0, fx1, fy1 in floats:
                if fp == page_no:
                    ov = max(0.0, min(x1, fx1) - max(x0, fx0)) * max(0.0, min(y1, fy1) - max(y0, fy0))
                    best = max(best, ov)
            covered += best
        return area > 0 and covered / area >= 0.6

    return [b for b in raw if b["type"] in ("figure", "table", "equation", "title", "heading") or not inside(b)]


def _starts_appendix(heading: str) -> bool:
    """A heading after the reference list starts an appendix ("A Proofs",
    "Attention Visualizations"), unless it is really a reference entry that
    Docling mislabelled as a heading (file names, URLs, years, author lists)."""
    if _APPENDIX.match(heading):
        return True
    looks_like_reference = re.search(r"\.pdf\b|https?:|www\.|\b(19|20)\d{2}\b|et al|,.*,", heading, re.I)
    return heading[:1].isupper() and len(heading.split()) <= 10 and not looks_like_reference


def _looks_tabular(text: str, loose: bool = False) -> bool:
    """Table cells read as text: many numeric tokens, hardly any sentence ends.
    `loose` is for rows next to a known table (e.g. a header row)."""
    tokens = text.split()
    sentence_ends = len(re.findall(r"[a-z]{2}[.!?](\s|$)", text))
    numeric = sum(any(c.isdigit() for c in t) for t in tokens) / max(1, len(tokens))
    if loose:
        numeric_words = sum(any(c.isdigit() for c in t) for t in tokens)
        return 3 <= len(tokens) <= 40 and numeric_words >= 2 and numeric >= 0.15 and sentence_ends == 0
    return len(tokens) >= 12 and numeric >= 0.3 and sentence_ends <= 1


def _is_prose(text: str) -> bool:
    """Running sentences are mostly lowercase words; author and affiliation
    lists are mostly capitalised names."""
    words = [w for w in text.split() if w[:1].isalpha()]
    return len(words) >= 30 and sum(w[0].islower() for w in words) / len(words) >= 0.5


def _grid_order(blocks: list[dict]) -> list[dict]:
    """Order front-matter blocks for author grids: rows of name + affiliation
    cells. Blocks starting within 30pt of a row's first block share the row;
    within a row, read column by column."""
    rows: list[list[dict]] = []
    for b in sorted(blocks, key=lambda b: b["_pos"][2]):
        if rows and b["_pos"][2] - rows[-1][0]["_pos"][2] <= 30:
            rows[-1].append(b)
        else:
            rows.append([b])
    out = []
    for row in rows:
        # Columns: blocks whose horizontal extents overlap.
        cols: list[list] = []  # [x0, x1, blocks]
        for b in sorted(row, key=lambda b: b["_pos"][1]):
            x0, x1 = b["_pos"][1], b["_pos"][3]
            col = next((c for c in cols if x0 < c[1] and x1 > c[0]), None)
            if col:
                col[0], col[1] = min(col[0], x0), max(col[1], x1)
                col[2].append(b)
            else:
                cols.append([x0, x1, [b]])
        for _, _, col_blocks in sorted(cols, key=lambda c: c[0]):
            out.extend(sorted(col_blocks, key=lambda b: b["_pos"][2]))
    return out


_AUTHOR_NOTE = re.compile(r"equal contribution|contributed equally|correspondence|corresponding author|@|work (was )?done|affiliat", re.I)


def _place_footnotes(raw: list[dict]) -> list[dict]:
    """Footnotes arrive wherever they sit in the page's reading order, often
    mid-section. Author notes from page 1 join the front matter; every other
    footnote moves to the end of its section, before the next heading."""
    front_notes, out, pending = [], [], []
    for b in raw:
        if b["type"] == "footnote":
            if b.get("_pos", (0,))[0] == 1 and _AUTHOR_NOTE.search(b.get("text", "")):
                b["region"] = "front"
                front_notes.append(b)
            else:
                pending.append(b)
            continue
        if b["type"] == "heading" and pending:
            out.extend(pending)
            pending = []
        out.append(b)
    out.extend(pending)
    last_front = max((i for i, b in enumerate(out) if b["region"] == "front"), default=0)
    return out[:last_front + 1] + front_notes + out[last_front + 1:]


def _merge_split_paragraphs(raw: list[dict]) -> list[dict]:
    """Docling splits paragraphs that break across a column or page. Rejoin a
    paragraph that ends mid-sentence with the next paragraph, even if figures,
    tables or footnotes were placed between them."""
    out: list[dict] = []
    open_para = None  # index in `out` of a paragraph that ended mid-sentence
    for b in raw:
        if b["type"] == "paragraph" and open_para is not None and out[open_para]["region"] == b["region"] != "front":
            prev = out[open_para]
            joiner = "" if prev["text"].endswith("-") else " "
            prev["text"] += joiner + b["text"]
            prev["provs"] += b["provs"]
            if _SENT_END.search(prev["text"]):
                open_para = None
            continue
        if b["type"] == "paragraph":
            out.append(b)
            open_para = None if _SENT_END.search(b["text"]) else len(out) - 1
            continue
        if b["type"] not in FLOAT_TYPES:
            open_para = None
        out.append(b)
    return out


# Font-name cues, after dropping the subset prefix ("ABCDEF+"). Suffixes cover
# names like LinLibertineTB (bold), LinLibertineTI (italic), Times-BoldItalic.
# Words match any case; the short suffixes must match case exactly ("Medi" is bold, not italic).
_BOLD_FONT = re.compile(r"(?i:bold|black|heavy|semibold|demi|medi|cmbx)|(?<=[a-z])(T?B|Bd|RB|SB)I?$|-B$")
_ITALIC_FONT = re.compile(r"(?i:ital|oblique|cmti)|(?<=[a-z])(T|R|B|TB|RB)?I$|-It$")
# Math fonts (TeX's Computer Modern math, AMS, txfonts/newtx, STIX, Cambria/Latin Modern Math):
# italic letters in formulas aren't emphasis, and their text is shown in a math font.
_MATH_FONT = re.compile(r"cmmi|cmsy|cmex|math|symbol|msbm|msam|eufm|rsfs|txsy|txmi|txex|stix|lmmi|lmsy", re.I)
BOLD, ITALIC = 1, 2


def _span_style(span) -> int:
    font = span["font"].split("+")[-1]
    style = 0
    if span["flags"] & 16 or _BOLD_FONT.search(font):
        style |= BOLD
    if (span["flags"] & 2 or _ITALIC_FONT.search(font)) and not _MATH_FONT.search(font):
        style |= ITALIC
    return style


SUB, SUP, MATH = 1, 2, 3  # a character's code: 0 | SUB | SUP, plus MATH if set in a math font


def _script_chars(page: fitz.Page) -> list[tuple[float, float, int]]:
    """Centres of the characters on lines that have subscripts, superscripts
    or math-font text, as (x, y, code). code = 0, SUB or SUP, plus MATH when
    the character is set in a math font. Subscripts and superscripts are
    characters in a span set noticeably smaller than its line's main text and
    lowered (o_t, a_{t-1}) or raised (x^2, footnote marks) relative to the
    line's baseline."""
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            spans = [sp for sp in line["spans"] if sp.get("chars")]
            if not spans:
                continue
            weight: dict[float, int] = {}
            for sp in spans:
                weight[round(sp["size"], 1)] = weight.get(round(sp["size"], 1), 0) + len(sp["chars"])
            main = max(weight, key=weight.get)
            base = [sp["origin"][1] for sp in spans if abs(sp["size"] - main) < 0.3]
            if not base:
                continue
            baseline = sorted(base)[len(base) // 2]
            kinds = []
            for sp in spans:
                dy = sp["origin"][1] - baseline
                small = sp["size"] <= 0.85 * main
                kind = SUB if small and dy > 0.08 * main else SUP if small and dy < -0.2 * main else 0
                kinds.append(kind + (MATH if _MATH_FONT.search(sp["font"].split("+")[-1]) else 0))
            if not any(kinds):
                continue
            # Every character of such a line, so words can be matched character by character.
            for sp, kind in zip(spans, kinds):
                for ch in sp["chars"]:
                    if ch["c"].strip():
                        x0, y0, x1, y1 = ch["bbox"]
                        out.append(((x0 + x1) / 2, (y0 + y1) / 2, kind))
    return out


def _word_scripts(w, scripts) -> str | None:
    """For a word, a string with one digit per non-space character of its
    text: the character's code (0 normal, 1 subscript, 2 superscript, plus 3
    if in a math font); None when nothing in it is special (or its characters
    can't be lined up)."""
    x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
    inside = sorted((cx, k) for cx, cy, k in scripts if x0 - .5 <= cx <= x1 + .5 and y0 - .5 <= cy <= y1 + .5)
    if not any(k for _, k in inside):
        return None
    chars = [c for c in text if not c.isspace()]
    if len(inside) == len(chars):  # the usual case: the PDF's own characters, in order
        kinds = [k for _, k in inside]
    else:  # ligatures etc.: spread the characters evenly across the box
        step = (x1 - x0) / len(chars)
        kinds = [0] * len(chars)
        for cx, k in inside:
            if k:
                kinds[min(len(chars) - 1, max(0, int((cx - x0) / step)))] = k
    if kinds[0] % MATH and (len(chars) < 2 or not kinds[0] >= MATH):
        # The word starts small: a footnote mark or the like, not a base
        # letter with a script. Keep only its math-font marks.
        kinds = [MATH * (k >= MATH) for k in kinds]
    return "".join(map(str, kinds)) if any(kinds) else None


def _page_words(pdf: fitz.Document) -> dict[int, list[tuple]]:
    """Every word with its box, line, font style (BOLD | ITALIC bits) and which
    of its characters are subscripts or superscripts (see _word_scripts)."""
    pages = {}
    for pno, page in enumerate(pdf, start=1):
        scripts = _script_chars(page)
        styled = [
            (span["bbox"], st)
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if span["text"].strip() and (st := _span_style(span))
        ]

        def style(w):
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            return next((st for b, st in styled if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]), 0)

        pages[pno] = [
            (pno, w[0], w[1], w[2], w[3], w[4], (w[5], w[6]), style(w), _word_scripts(w, scripts) if scripts else None)
            for w in page.get_text("words", sort=False)
        ]
    return pages


def _words_in(words_by_page, provs) -> list[tuple]:
    """Words whose centre lies inside any of the element's boxes, in order."""
    out = []
    for page_no, (x0, y0, x1, y1) in provs:
        for w in words_by_page.get(page_no, []):
            cx, cy = (w[1] + w[3]) / 2, (w[2] + w[4]) / 2
            if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
                out.append(w)
    return out
