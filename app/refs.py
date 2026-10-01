"""Add a cited paper to the library from its reference-list entry.

A reference entry is free text ("Yao, S., ... ReAct: Synergizing reasoning and
acting in language models. ICLR 2023."). To find the paper:
  1. an arXiv id or DOI written in the entry;
  2. otherwise the title, guessed from the entry's sentences: an exact arXiv
     title match with the same first author, or an OpenAlex work with exactly
     that title;
  3. failing that, Crossref's bibliographic search and OpenAlex's search, each
     given the whole entry; a candidate counts only if its title appears in
     the entry (so a near-miss never adds the wrong paper);
  4. the match's arXiv copy if there is one (most ML papers), else an open-
     access PDF via its DOI.
Then the usual download and ingest, as for a pasted link.
"""

import re
from difflib import SequenceMatcher

import httpx

from . import fetch

HEADERS = {"User-Agent": "Papercut/1.0 (personal research-paper reader)"}
TIMEOUT = httpx.Timeout(20, connect=10)

_ARXIV_IN_TEXT = re.compile(r"(?:arxiv[:\s/.]*(?:abs/|pdf/)?|arxiv\.org/(?:abs|pdf)/)\s*(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_BARE_ARXIV = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")
_DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;)\]])", re.I)


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _title_matches(title: str, entry: str) -> bool:
    t = _norm(title)
    return len(t.split()) >= 2 and len(t) >= 12 and t in _norm(entry)


def _crossref(entry: str) -> list[dict]:
    try:
        r = httpx.get("https://api.crossref.org/works", headers=HEADERS, timeout=TIMEOUT,
                      params={"query.bibliographic": entry[:500], "rows": 5, "select": "DOI,title,author"})
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    out = []
    for it in r.json().get("message", {}).get("items", []):
        title = (it.get("title") or [""])[0]
        authors = it.get("author") or []
        first = (authors[0].get("family") or "") if authors else ""
        out.append({"title": title, "doi": it.get("DOI"), "first_author": first})
    return out


def _openalex(entry: str) -> list[dict]:
    try:
        r = httpx.get("https://api.openalex.org/works", headers=HEADERS, timeout=TIMEOUT,
                      params={"search": _norm(entry)[:300], "per_page": 5,
                              "select": "id,display_name,doi,authorships,locations"})
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    return _openalex_results(r.json())


def _openalex_results(data: dict) -> list[dict]:
    out = []
    for w in data.get("results", []):
        arxiv = None
        for loc in w.get("locations") or []:
            m = fetch._ARXIV_URL.search((loc.get("landing_page_url") or "") + " " + (loc.get("pdf_url") or ""))
            if m:
                arxiv = re.sub(r"v\d+$", "", m.group(1))
                break
        authors = w.get("authorships") or []
        first = ((authors[0].get("author") or {}).get("display_name") or "") if authors else ""
        out.append({"title": w.get("display_name") or "", "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
                    "arxiv": arxiv, "first_author": first})
    return out


def _title_guesses(entry: str) -> list[str]:
    """Likely titles in a reference entry. Entries are mostly "Authors. Title.
    Venue, year." Sentences end after a lowercase letter, digit or "?" (not
    after an initial like "C."); the first sentence is the authors."""
    parts = re.split(r"(?<=[a-z0-9)\]])\.\s+|(?<=[?!])\s+", entry.strip())
    out = []
    for p in parts[1:]:
        p = re.sub(r"[,.]?\s*\(?(19|20)\d{2}[a-z]?\)?\.?$", "", p.strip()).strip(" .,")
        if len(p.split()) >= 2 and not re.match(r"(In|Proceedings|Advances|arXiv|CoRR|URL|https?:)\b", p, re.I):
            out.append(p)
    return out[:3]


def _same_title(title: str, guess: str, their_author: str, our_author: str) -> bool:
    """Exactly the same title, ignoring case, spaces and punctuation
    ("auto-completion" = "autocompletion"); or nearly the same ("model" vs
    "models") by the same first author."""
    a, b = re.sub(r"[^a-z0-9]", "", title.lower()), re.sub(r"[^a-z0-9]", "", guess.lower())
    if a == b:
        return True
    surname = _norm(our_author)
    return bool(surname) and _norm(their_author).endswith(surname) and SequenceMatcher(None, a, b).ratio() >= 0.9


def _first_author(entry: str) -> str:
    """The first author's surname-ish last word ("Sainbayar Sukhbaatar, ..." -> "Sukhbaatar")."""
    first = re.split(r",|\s+and\s+|(?<=[a-z]{2})\.\s", entry, maxsplit=1)[0]  # not at an initial ("T. Liu")
    words = re.findall(r"[A-Za-zÀ-ž'\-]{2,}", first)
    return words[-1] if words else ""


def _openalex_title(title: str) -> list[dict]:
    try:
        r = httpx.get("https://api.openalex.org/works", headers=HEADERS, timeout=TIMEOUT,
                      params={"filter": f"title.search:{_norm(title)}", "per_page": 5,
                              "select": "id,display_name,doi,authorships,locations"})
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    return _openalex_results(r.json())


def _by_title(entry: str) -> tuple[dict, str] | None:
    """Find the paper by its title: an exact arXiv title match with the same
    first author, else an OpenAlex work whose title is the guessed title."""
    author = _first_author(entry)
    for guess in _title_guesses(entry):
        pdf = fetch._arxiv_by_title(guess, author) if author else None
        if pdf:
            return fetch._arxiv(re.sub(r"v\d+$", "", pdf.rsplit("/", 1)[-1])), guess
        # An arXiv copy is the most reliable download.
        for c in sorted(_openalex_title(guess), key=lambda c: not c.get("arxiv")):
            if _same_title(c["title"], guess, c.get("first_author", ""), author):
                if c.get("arxiv"):
                    return fetch._arxiv(c["arxiv"]), c["title"]
                if c.get("doi"):
                    try:
                        return fetch.resolve(c["doi"]), c["title"]
                    except fetch.FetchError:
                        raise fetch.FetchError(f"Found “{c['title']}”, but no free PDF of it. "
                                               "Download it yourself and drop it on the home page.")
    return None


def resolve(entry: str) -> tuple[dict, str | None]:
    """Where to download the cited paper: (fetch.resolve-style info, title).
    Raises fetch.FetchError with a message the reader can act on."""
    m = _ARXIV_IN_TEXT.search(entry) or (re.search(r"arxiv", entry, re.I) and _BARE_ARXIV.search(entry))
    if m:
        return fetch._arxiv(m.group(1)), None
    d = _DOI_IN_TEXT.search(entry)
    if d:
        return fetch.resolve(d.group(1)), None

    found = _by_title(entry)
    if found:
        return found
    candidates = [c for c in _openalex(entry) + _crossref(entry) if c["title"] and _title_matches(c["title"], entry)]
    if not candidates:
        raise fetch.FetchError("Couldn't identify this reference in OpenAlex or Crossref. "
                               "If you find the paper, paste its link on the home page.")
    # Prefer a candidate with an arXiv copy, then one with a DOI.
    candidates.sort(key=lambda c: (not c.get("arxiv"), not c.get("doi")))
    best = candidates[0]
    if best.get("arxiv"):
        return fetch._arxiv(best["arxiv"]), best["title"]
    pdf = fetch._arxiv_by_title(best["title"], best.get("first_author") or "")
    if pdf:
        arxiv = pdf.rsplit("/", 1)[-1]
        return fetch._arxiv(re.sub(r"v\d+$", "", arxiv)), best["title"]
    if best.get("doi"):
        return fetch.resolve(best["doi"]), best["title"]
    raise fetch.FetchError(f"Found “{best['title']}”, but no free PDF of it. Download it yourself and drop it on the home page.")
