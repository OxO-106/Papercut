"""Explain one sentence in plain language, using the local model.

Context given to the model: the paper's title, the sentence's section and
paragraph, the abstract, and the few passages elsewhere in the paper that
best match the sentence (where its terms are likely defined). Explanations
are cached per sentence in paper.json.
"""

import re
from typing import Iterator

from . import library, llm
from .ask import passages, retrieve

SYSTEM = """You help someone read the academic paper "{title}".
Explain the given sentence in plain language in 2-4 sentences: what it says, what any jargon, abbreviations or symbols mean, and why it matters for the paper's argument.
Base the explanation on the paper excerpts provided. If something isn't covered by them, keep to what the sentence itself says rather than guessing.
Start directly with the explanation. No preamble, no headings, no bullet points."""


def context(paper: dict, sid: str) -> dict:
    """Section heading and paragraph around the sentence."""
    heading = ""
    for b in paper["blocks"]:
        if b["type"] == "heading":
            heading = b["text"]
        ids = b.get("sentences", []) + b.get("caption_sentences", [])
        if sid in ids:
            para = " ".join(paper["sentences"][i]["text"] for i in ids)
            return {"heading": heading, "paragraph": para, "block": b}
    return {"heading": heading, "paragraph": paper["sentences"][sid]["text"], "block": None}


def explain(paper: dict, sid: str) -> Iterator[dict]:
    """Yield {"type": "token"}... then {"type": "done"}. Cached answers come back at once."""
    cached = paper.get("explanations", {}).get(sid)
    if cached:
        yield {"type": "token", "text": cached}
        yield {"type": "done", "cached": True}
        return

    sentence = paper["sentences"][sid]["text"]
    ctx = context(paper, sid)
    ps = passages(paper)
    abstract = next((p["text"] for p in ps if re.match(r"\s*abstract", p["heading"], re.I)), ps[0]["text"] if ps else "")
    related = [p for p in retrieve(paper, sentence, k=3) if p["text"] not in (ctx["paragraph"], abstract)][:3]

    parts = [f"Abstract: {abstract}"]
    parts += [f"Related passage ({p['heading'] or 'untitled'}): {p['text']}" for p in related]
    parts.append(f"Paragraph containing the sentence (section: {ctx['heading'] or 'untitled'}): {ctx['paragraph']}")
    user = "\n\n".join(parts) + f"\n\nExplain this sentence:\n\"{sentence}\""

    messages = [
        {"role": "system", "content": SYSTEM.format(title=paper["meta"]["title"])},
        {"role": "user", "content": user},
    ]
    text = []
    for piece in llm.stream_chat(messages, temperature=0.2):
        text.append(piece)
        yield {"type": "token", "text": piece}

    result = "".join(text).strip()
    if result:
        library.update_paper(paper["id"], lambda p: p.setdefault("explanations", {}).update({sid: result}))
    yield {"type": "done"}


# ---- Figures and tables: the model looks at the cropped image itself.

FLOAT_SYSTEM = """You help someone read the academic paper "{title}".
You are shown one of its figures or tables as an image, with its caption and the passages that refer to it.
Explain it in plain language in 3-5 sentences: what is plotted or compared (axes, columns, conditions), the main pattern or result, and what it shows for the paper's argument.
Read values from the image where it helps, but don't list every number. If something can't be read reliably, say so rather than guessing.
Start directly with the explanation. No preamble, no headings, no bullet points."""

MAX_SIDE = 1280  # px; larger crops cost many more image tokens for little gain


def _image_b64(path) -> str:
    import base64
    import io

    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def float_context(paper: dict, block_id: str) -> tuple[dict, str, list[str]]:
    """The block, its caption, and the paragraphs that mention it."""
    blocks = {b["id"]: b for b in paper["blocks"]}
    b = blocks[block_id]
    ids = b.get("caption_sentences") or blocks.get(b.get("caption_block"), {}).get("sentences", [])
    caption = " ".join(paper["sentences"][i]["text"] for i in ids)
    mentioning = []
    for other in paper["blocks"]:
        sids = other.get("sentences", [])
        if any(block_id in ids_ for s in sids for _, _, ids_ in paper["sentences"][s].get("xrefs", [])):
            mentioning.append(" ".join(paper["sentences"][s]["text"] for s in sids))
    return b, caption, mentioning[:3]


def explain_float(paper: dict, block_id: str) -> Iterator[dict]:
    """Yield {"type": "token"}... then {"type": "done"}. Cached per image file."""
    b, caption, mentioning = float_context(paper, block_id)
    key = b["image"]  # the crop's file name includes a hash of the image, so it survives re-parses
    cached = paper.get("float_explanations", {}).get(key)
    if cached:
        yield {"type": "token", "text": cached}
        yield {"type": "done", "cached": True}
        return

    kind = "table" if b["type"] == "table" else "figure"
    parts = [f"Caption: {caption}" if caption else f"This {kind} has no caption."]
    parts += [f"Passage referring to it: {p}" for p in mentioning]
    user = "\n\n".join(parts) + f"\n\nExplain this {kind}."
    messages = [
        {"role": "system", "content": FLOAT_SYSTEM.format(title=paper["meta"]["title"])},
        {"role": "user", "content": user, "images": [_image_b64(library.paper_dir(paper["id"]) / b["image"])]},
    ]
    text = []
    for piece in llm.stream_chat(messages, temperature=0.2, max_tokens=700):
        text.append(piece)
        yield {"type": "token", "text": piece}

    result = "".join(text).strip()
    if result:
        library.update_paper(paper["id"], lambda p: p.setdefault("float_explanations", {}).update({key: result}))
    yield {"type": "done"}
