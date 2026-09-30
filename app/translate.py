"""Translate a word, sentence or passage of the paper, using the local model.

A single word or short term gets a translation plus a one-line gloss of what
it means in its sentence; longer text gets a plain translation that leaves
math, symbols, citations and names alone. Results are cached in paper.json,
keyed by language and text, so re-opening a translation is instant.
"""

import hashlib
import re
from typing import Iterator

from . import library, llm

MAX_CHARS = 6000
CACHE_SIZE = 500

TERM = """You help someone read the academic paper "{title}" in {lang}.
Translate the given term as it is used in the given sentence.
Reply in {lang}, on one line: the translation, then " — " and a short explanation of what the term means in this sentence.
If the usual {lang} rendering is uncommon or ambiguous, add the English term in parentheses. No preamble."""

PASSAGE = """You translate passages of the academic paper "{title}" into {lang}.
Translate faithfully and fluently, in the register of an academic text.
Keep math, symbols, variable names, citation markers like [12] or (Smith et al., 2020), code, and model or dataset names unchanged.
Output only the translation, with no preamble or notes."""


def _is_term(text: str) -> bool:
    return len(text.split()) <= 4 and not re.search(r"[.!?;:]\s*$", text)


def _key(text: str, lang: str) -> str:
    return hashlib.sha1(f"{lang}\n{text}".encode()).hexdigest()[:16]


def translate(paper: dict, text: str, lang: str, context: str = "") -> Iterator[dict]:
    """Yield {"type": "token"}... then {"type": "done"}."""
    text = re.sub(r"\s+", " ", text).strip()[:MAX_CHARS]
    key = _key(text, lang)
    cached = paper.get("translations", {}).get(key)
    if cached:
        yield {"type": "token", "text": cached}
        yield {"type": "done", "cached": True}
        return

    title = paper["meta"]["title"]
    if _is_term(text):
        system = TERM.format(title=title, lang=lang)
        user = f"Sentence: \"{context or text}\"\n\nTerm: \"{text}\""
        max_tokens = 200
    else:
        system = PASSAGE.format(title=title, lang=lang)
        user = text
        max_tokens = min(4000, 200 + len(text))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    out = []
    for piece in llm.stream_chat(messages, temperature=0.1, max_tokens=max_tokens):
        out.append(piece)
        yield {"type": "token", "text": piece}

    result = "".join(out).strip()
    if result:
        def save(p):
            cache = p.setdefault("translations", {})
            cache[key] = result
            for old in list(cache)[:-CACHE_SIZE]:
                del cache[old]
        library.update_paper(paper["id"], save)
    yield {"type": "done"}
