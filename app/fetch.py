"""Open a paper from a link: arXiv ID/URL, DOI, OpenReview, or a direct PDF URL.

DOIs are resolved to an open-access PDF through OpenAlex (free, no key). A
paywalled DOI with no open copy is reported rather than guessed at.
"""

import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx

HEADERS = {"User-Agent": "Papercut/0.1 (personal paper reader)"}
MAX_BYTES = 150 * 1024 * 1024

_ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?"
_ARXIV_OLD = r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
_ARXIV_ID = re.compile(rf"^(?:arxiv:\s*)?({_ARXIV_NEW}|{_ARXIV_OLD})$", re.I)
_ARXIV_URL = re.compile(rf"arxiv\.org/(?:abs|pdf|html)/({_ARXIV_NEW}|{_ARXIV_OLD})", re.I)
_ARXIV_DOI = re.compile(rf"^10\.48550/arxiv\.({_ARXIV_NEW})$", re.I)
_DOI = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.I)


class FetchError(Exception):
    """A message the reader can act on."""


def resolve(link: str) -> dict:
    """Work out where the PDF lives. Returns {"pdf_url", "arxiv"?, "doi"?, "url"}."""
    link = link.strip().strip("<>").strip()
    if not link:
        raise FetchError("Paste an arXiv link or ID, a DOI, or a PDF link.")

    m = _ARXIV_ID.match(link) or _ARXIV_URL.search(link)
    if m:
        return _arxiv(m.group(1))

    doi_match = _DOI.search(link)
    if doi_match and ("doi.org/" in link or link.lower().startswith(("10.", "doi:"))):
        doi = doi_match.group(1).rstrip(".,;)")
        a = _ARXIV_DOI.match(doi)
        if a:
            return _arxiv(a.group(1))
        return {"pdf_urls": _open_access_pdfs(doi), "doi": doi, "url": f"https://doi.org/{doi}"}

    if not re.match(r"^https?://", link, re.I):
        raise FetchError("That doesn't look like an arXiv ID, a DOI or a web link.")
    u = urlparse(link)
    if u.netloc.endswith("openreview.net"):
        pid = parse_qs(u.query).get("id", [None])[0]
        if pid:
            return {"pdf_urls": [f"https://openreview.net/pdf?id={pid}"], "url": link}
    return {"pdf_urls": [link], "url": link}  # hope it's a PDF; download() checks


def _arxiv(arxiv_id: str) -> dict:
    return {"pdf_urls": [f"https://arxiv.org/pdf/{arxiv_id}"], "arxiv": arxiv_id, "url": f"https://arxiv.org/abs/{arxiv_id}"}


def _open_access_pdfs(doi: str) -> list[str]:
    """Free copies to try, best first: an arXiv version, other open-access
    copies (repositories), then the publisher's PDF (publishers often block
    automated downloads, so it goes last)."""
    try:
        r = httpx.get(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}", headers=HEADERS, timeout=20)
    except httpx.HTTPError:
        raise FetchError("Couldn't reach OpenAlex to look up that DOI. Check your internet connection.")
    if r.status_code == 404:
        raise FetchError(f"No paper found for DOI {doi}.")
    r.raise_for_status()
    work = r.json()
    locations = [loc for loc in [work.get("best_oa_location")] + work.get("locations", []) if loc]

    urls: list[str] = []
    for loc in locations:
        m = _ARXIV_URL.search((loc.get("landing_page_url") or "") + " " + (loc.get("pdf_url") or ""))
        if m:
            urls.append(_arxiv(m.group(1))["pdf_urls"][0])
    authors = work.get("authorships") or []
    first_author = ((authors[0].get("author") or {}).get("display_name") or "") if authors else ""
    if not urls and work.get("title") and first_author:
        found = _arxiv_by_title(work["title"], first_author)
        if found:
            urls.append(found)
    publisher = []
    for loc in locations:
        pdf = loc.get("pdf_url")
        if pdf and pdf not in urls:
            host_type = (loc.get("source") or {}).get("type")
            target = urls if host_type == "repository" else publisher
            if pdf not in target:
                target.append(pdf)
    urls += [u for u in publisher if u not in urls]
    if not urls:
        raise FetchError("This paper has no free PDF that Papercut can find (it may be paywalled). "
                         "Download the PDF yourself and drop it here.")
    return urls


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _arxiv_by_title(title: str, first_author: str) -> str | None:
    """Find an arXiv preprint with exactly this title (ignoring case and
    punctuation) and the same first author's surname: titles alone can clash
    ("Deep learning")."""
    surname = _norm_title(first_author).split()[-1] if first_author.strip() else ""
    words = _norm_title(title)
    try:
        r = httpx.get("https://export.arxiv.org/api/query", headers=HEADERS, timeout=20,
                      params={"search_query": f'ti:"{words}"', "max_results": 5})
    except httpx.HTTPError:
        return None
    for entry in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        t = re.search(r"<title>(.*?)</title>", entry, re.S)
        i = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", entry)
        names = re.findall(r"<name>(.*?)</name>", entry)
        first = _norm_title(names[0]) if names else ""
        if t and i and _norm_title(t.group(1)) == words and surname and first.endswith(surname):
            return f"https://arxiv.org/pdf/{i.group(1)}"
    return None


def download(pdf_urls: list[str]) -> Path:
    """Try each candidate until one yields a real PDF."""
    last = None
    for url in pdf_urls:
        try:
            return _download_one(url)
        except FetchError as e:
            last = e
    if len(pdf_urls) > 1:
        raise FetchError(f"None of the {len(pdf_urls)} free copies could be downloaded ({last}). "
                         "Download the PDF yourself and drop it here.")
    raise last


def _download_one(pdf_url: str) -> Path:
    """Download to a temporary file and check it really is a PDF."""
    try:
        with httpx.stream("GET", pdf_url, headers=HEADERS, follow_redirects=True, timeout=60) as r:
            if r.status_code >= 400:
                raise FetchError(f"The server refused the download (HTTP {r.status_code}).")
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            size = 0
            with tmp:
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FetchError("That PDF is larger than 150 MB.")
                    tmp.write(chunk)
    except httpx.HTTPError as e:
        raise FetchError(f"Download failed: {type(e).__name__}.")
    path = Path(tmp.name)
    if not path.read_bytes()[:1024].lstrip().startswith(b"%PDF"):
        path.unlink(missing_ok=True)
        raise FetchError("That link didn't lead to a PDF (it may need a login, or be a web page).")
    return path


def filename_for(info: dict) -> str:
    if info.get("arxiv"):
        return f"arXiv {info['arxiv'].replace('/', '_')}.pdf"
    if info.get("doi"):
        return f"doi {re.sub(r'[^A-Za-z0-9._-]+', '_', info['doi'])}.pdf"
    name = Path(urlparse(info["pdf_urls"][0]).path).name or "paper.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"
