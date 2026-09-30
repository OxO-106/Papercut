"""Add a cited paper to the library from its reference-list entry.

A reference entry is free text ("Yao, S., ... ReAct: Synergizing reasoning and
acting in language models. ICLR 2023."). To find the paper:
  1. an arXiv id or DOI written in the entry;
  2. otherwise Crossref's bibliographic search and OpenAlex's search, each
     given the whole entry; a candidate counts only if its title appears in
     the entry (so a near-miss never adds the wrong paper);
  3. the match's arXiv copy if there is one (most ML papers), else an open-
     access PDF via its DOI.
Then the usual download and ingest, as for a pasted link.
"""

import re

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
    out = []
    for w in r.json().get("results", []):
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


def resolve(entry: str) -> tuple[dict, str | None]:
    """Where to download the cited paper: (fetch.resolve-style info, title).
    Raises fetch.FetchError with a message the reader can act on."""
    m = _ARXIV_IN_TEXT.search(entry) or (re.search(r"arxiv", entry, re.I) and _BARE_ARXIV.search(entry))
    if m:
        return fetch._arxiv(m.group(1)), None
    d = _DOI_IN_TEXT.search(entry)
    if d:
        return fetch.resolve(d.group(1)), None

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
