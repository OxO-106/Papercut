"""Outside knowledge for Ask: tools the local model can call.

Research literature (free, no account):
  search_papers   - arXiv search (its relevance beat OpenAlex's search, which
                    also rate-limits anonymous use)
  citing_papers   - later work that cites this paper, most-cited first (OpenAlex)
  get_reference   - the text of this paper's reference [n]
General web (needs an Ollama API key, see web_key()):
  web_search      - Ollama's web search: titles, URLs and page text
  web_fetch       - the text of one web page

Every result that can be cited gets a source number S1, S2, ... (numbered
across the whole answer by Toolbox), which the model cites as [S1] and the
page turns into a link.
"""

import json
import os
import re
from pathlib import Path

import httpx

from . import library
from .config import ROOT

HEADERS = {"User-Agent": "Papercut/1.0 (personal research-paper reader)"}
TIMEOUT = httpx.Timeout(20, connect=10)
KEY_FILE = ROOT.parent / "ollama-api-key.txt"  # D:\Read\ollama-api-key.txt
MAX_TEXT = 3500  # characters of a web page given to the model


def web_key() -> str | None:
    """Ollama API key for web search: env OLLAMA_API_KEY, else the key file."""
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text("utf-8").strip()
    return key or None


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _get(url: str, **params) -> httpx.Response:
    r = httpx.get(url, params=params or None, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


# ---- arXiv

def _arxiv_entries(xml: str) -> list[dict]:
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", entry, re.S)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        aid = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", entry)
        if not aid:
            continue
        authors = re.findall(r"<name>(.*?)</name>", entry)
        arxiv = re.sub(r"v\d+$", "", aid.group(1))  # link the latest version
        out.append({
            "title": tag("title"), "year": tag("published")[:4],
            "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
            "abstract": tag("summary"), "url": f"https://arxiv.org/abs/{arxiv}", "arxiv": arxiv,
        })
    return out


_STOP = set("a an and are as at by for from how in into is it of on or the to via what when which with".split())


def _arxiv_search(query: str, n: int) -> list[dict]:
    """arXiv requires every term; a long query often matches nothing, so
    retry with fewer (the first terms, which models put the topic in)."""
    terms = [w for w in _norm(query).split() if w not in _STOP][:10]
    for k in (len(terms), 5, 3):
        if k > len(terms) or not terms:
            continue
        words = " AND ".join(f"all:{w}" for w in terms[:k])
        found = _arxiv_entries(_get("https://export.arxiv.org/api/query", search_query=words, max_results=n,
                                    sortBy="relevance").text)
        if found:
            return found
    return []


# ---- OpenAlex

def _abstract(inv: dict | None) -> str:
    """OpenAlex stores abstracts as an inverted index."""
    if not inv:
        return ""
    pos = {i: w for w, idx in inv.items() for i in idx}
    return " ".join(pos[i] for i in sorted(pos))


def _openalex_work(w: dict) -> dict:
    authors = [a["author"]["display_name"] for a in w.get("authorships", [])]
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    return {
        "title": w.get("display_name") or "", "year": str(w.get("publication_year") or ""),
        "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
        "abstract": _abstract(w.get("abstract_inverted_index")),
        "cited_by": w.get("cited_by_count"),
        "url": f"https://doi.org/{doi}" if doi else w.get("id", ""),
    }


_OA_FIELDS = "id,display_name,publication_year,authorships,abstract_inverted_index,cited_by_count,doi"


def openalex_id(paper: dict) -> str | None:
    """This paper's OpenAlex id, found once and cached in its folder: from its
    DOI or arXiv id, else by an exact-title arXiv search, else OpenAlex search."""
    cache = library.paper_dir(paper["id"]) / "openalex.json"
    if cache.exists():
        return json.loads(cache.read_text("utf-8")).get("id")
    src = library.source_info(paper["id"])
    title = paper["meta"]["title"]
    doi = src.get("doi")
    if not doi and src.get("arxiv"):
        doi = f"10.48550/arXiv.{src['arxiv']}"
    if not doi:
        try:
            hits = [h for h in _arxiv_entries(_get("https://export.arxiv.org/api/query",
                    search_query=f'ti:"{_norm(title)}"', max_results=5).text) if _norm(h["title"]) == _norm(title)]
            if hits:
                doi = f"10.48550/arXiv.{hits[0]['arxiv']}"
        except httpx.HTTPError:
            pass
    wid = None
    try:
        if doi:
            wid = _get(f"https://api.openalex.org/works/doi:{doi}", select="id").json()["id"]
        else:
            res = _get("https://api.openalex.org/works", filter=f"title.search:{_norm(title)}", per_page=3,
                       select="id,display_name").json()["results"]
            wid = next((w["id"] for w in res if _norm(w["display_name"]) == _norm(title)), None)
    except (httpx.HTTPError, KeyError):
        return None  # try again next time (e.g. rate-limited)
    wid = wid.rsplit("/", 1)[-1] if wid else None
    cache.write_text(json.dumps({"id": wid}), "utf-8")
    return wid


# ---- the tools

TOOLS = [
    {"type": "function", "function": {
        "name": "search_papers",
        "description": "Search the research literature (arXiv) for papers on a topic, method or concept: related work, alternatives, background. Returns titles, years, authors and abstracts.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords, e.g. 'retrieval augmented code generation benchmark'"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "citing_papers",
        "description": "Later papers that cite the paper being read: the most-cited and the newest. OpenAlex's citation links are incomplete, so also use search_papers for specific follow-up work.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_reference",
        "description": "The full entry of reference [n] in the paper's reference list (authors, title, venue). Use it to then search for that paper.",
        "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}},
]
WEB_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (encyclopedias, documentation, tutorials, blogs, news). Good for explaining a concept, tool or technique in depth. Returns page titles, URLs and text.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Read the text of one web page by URL (e.g. one found by web_search).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
]


class Toolbox:
    """Runs tool calls for one answer and numbers the sources it returns."""

    def __init__(self, paper: dict):
        self.paper = paper
        self.sources: list[dict] = []  # [{"n": "S1", "title", "url", "kind"}]
        self.web = web_key() is not None

    def tools(self) -> list[dict]:
        return TOOLS + (WEB_TOOLS if self.web else [])

    def _cite(self, kind: str, title: str, url: str) -> str:
        for s in self.sources:
            if s["url"] == url:
                return s["n"]
        n = f"S{len(self.sources) + 1}"
        self.sources.append({"n": n, "title": title, "url": url, "kind": kind})
        return n

    def _papers(self, works: list[dict], empty: str) -> str:
        if not works:
            return empty
        out = []
        for w in works:
            n = self._cite("paper", w["title"], w["url"])
            meta = ", ".join(x for x in (w.get("authors"), w.get("year")) if x)
            cited = f"; cited by {w['cited_by']}" if w.get("cited_by") is not None else ""
            out.append(f"[{n}] {w['title']} ({meta}{cited})\n{(w.get('abstract') or 'No abstract available.')[:900]}")
        return "\n\n".join(out)

    def describe(self, name: str, args: dict) -> str:
        """A short line for the page while the tool runs."""
        return {
            "search_papers": f"Searching papers: “{args.get('query', '')}”",
            "citing_papers": "Finding later work that cites this paper",
            "get_reference": f"Looking up reference [{args.get('n', '?')}]",
            "web_search": f"Searching the web: “{args.get('query', '')}”",
            "web_fetch": f"Reading {args.get('url', 'a page')}",
        }.get(name, name)

    def run(self, name: str, args: dict) -> str:
        try:
            return getattr(self, "_" + name)(**args)
        except TypeError:
            return f"Error: bad arguments for {name}."
        except AttributeError:
            return f"Error: there is no tool called {name}."
        except httpx.HTTPStatusError as e:
            return f"The search service answered {e.response.status_code}; try another tool or query."
        except httpx.HTTPError as e:
            return f"The search service couldn't be reached ({type(e).__name__})."

    def _search_papers(self, query: str) -> str:
        return self._papers(_arxiv_search(query, 6), "No papers found; try fewer or other keywords.")

    def _citing_papers(self) -> str:
        wid = openalex_id(self.paper)
        if not wid:
            return "This paper couldn't be found in OpenAlex, so its citations are unknown. Try search_papers instead."
        # Most-cited (the influential follow-ups) and newest (where the field is now).
        seen, works = set(), []
        for sort in ("cited_by_count:desc", "publication_date:desc"):
            r = _get("https://api.openalex.org/works", filter=f"cites:{wid}", sort=sort, per_page=5, select=_OA_FIELDS)
            for w in r.json().get("results", []):
                if w["id"] not in seen:
                    seen.add(w["id"])
                    works.append(_openalex_work(w))
        return self._papers(works, "No citing papers found yet.")

    def _get_reference(self, n) -> str:
        refs = [b for b in self.paper["blocks"] if b["type"] == "references"]
        n = int(n)
        if not 1 <= n <= len(refs):
            return f"There is no reference [{n}]; the list has {len(refs)} entries."
        return f"Reference [{n}]: {refs[n - 1]['text']}"

    def _web_search(self, query: str) -> str:
        r = httpx.post("https://ollama.com/api/web_search", json={"query": query, "max_results": 5},
                       headers={**HEADERS, "Authorization": f"Bearer {web_key()}"}, timeout=TIMEOUT)
        r.raise_for_status()
        out = []
        for res in r.json().get("results", []):
            n = self._cite("web", res.get("title") or res.get("url", ""), res.get("url", ""))
            out.append(f"[{n}] {res.get('title', '')} ({res.get('url', '')})\n{(res.get('content') or '')[:1500]}")
        return "\n\n".join(out) or "No web results."

    def _web_fetch(self, url: str) -> str:
        r = httpx.post("https://ollama.com/api/web_fetch", json={"url": url},
                       headers={**HEADERS, "Authorization": f"Bearer {web_key()}"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        n = self._cite("web", data.get("title") or url, url)
        return f"[{n}] {data.get('title', '')} ({url})\n{(data.get('content') or '')[:MAX_TEXT]}"
