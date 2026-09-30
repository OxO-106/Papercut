"""Find in-text citations and link them to reference-list entries.

Two styles:
  numeric      [12], [3, 5-7]        -> the Nth entry of the reference list
  author-year  (Kiela et al., 2021; Ott et al., 2022), Yao et al. (2023)
               -> the entry whose opening names the surname and which has the year
"""

import re
import unicodedata

_NUMERIC = re.compile(r"\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\]")
# (Kiela et al., 2021; Ott, 2022)  and ACM style  [Xia et al. 2023; Yang 2024]
_PAREN = re.compile(r"\(([^()]*?\b(?:19|20)\d{2}[a-z]?)\)|\[([^\[\]]*?[A-Za-z][^\[\]]*?\b(?:19|20)\d{2}[a-z]?)\]")
_NARRATIVE = re.compile(
    r"\b([A-Z][\w'’`´\-]+)(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-Z][\w'’`´\-]+)?\s*\(((?:19|20)\d{2})[a-z]?\)"
)
_ONE = re.compile(r"([A-Z][\w'’`´\-]+)(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-Z][\w'’`´\-]+)?,?\s+((?:19|20)\d{2})[a-z]?")


def _fold(s: str) -> str:
    """ASCII lowercase, dropping stray accent marks PDFs emit separately ("Rozi`ere")."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[`'^~\"]", "", s)


def clean_reference_entries(entries: list[dict]) -> list[dict]:
    """Merge fragments Docling split off an entry (e.g. 'ARTICLE-NUMBER = {1688...')."""
    out: list[dict] = []
    for e in entries:
        t = e["text"]
        fragment = len(t) < 25 or re.match(r"^[A-Z\-]+\s*=\s*\{", t)
        if out and fragment:
            out[-1]["text"] += " " + t
            e["_merged"] = True
        else:
            out.append(e)
    return out


class Linker:
    def __init__(self, entries: list[dict]):
        self.entries = entries  # [{"id", "text"}] in list order
        self.folded = [_fold(e["text"]) for e in entries]

    def _by_number(self, n: int) -> str | None:
        return self.entries[n - 1]["id"] if 1 <= n <= len(self.entries) else None

    def _by_author_year(self, surname: str, year: str) -> str | None:
        s = _fold(surname)
        best = None
        for e, f in zip(self.entries, self.folded):
            pos = f.find(s, 0, 160)  # first author appears near the start
            if pos >= 0 and year in f and (best is None or pos < best[0]):
                best = (pos, e["id"])
        return best[1] if best else None

    def find(self, text: str) -> list[list]:
        """Return [[start, end, [ref ids]], ...] for citations in `text`."""
        if not self.entries:
            return []
        found = []
        for m in _NUMERIC.finditer(text):
            ids = []
            for part in re.split(r"\s*[,;]\s*", m.group(1)):
                rng = re.match(r"(\d+)\s*[–-]\s*(\d+)$", part)
                lo, hi = (int(rng[1]), int(rng[2])) if rng else (int(part), int(part))
                for n in range(lo, min(hi, lo + 50) + 1):
                    rid = self._by_number(n)
                    if rid and rid not in ids:
                        ids.append(rid)
            if ids:
                found.append([m.start(), m.end(), ids])
        for m in _PAREN.finditer(text):
            ids = []
            for one in _ONE.finditer(m.group(1) or m.group(2)):
                rid = self._by_author_year(one.group(1), one.group(2))
                if rid and rid not in ids:
                    ids.append(rid)
            if ids:
                found.append([m.start(), m.end(), ids])
        for m in _NARRATIVE.finditer(text):
            if any(s <= m.start() < e for s, e, _ in found):
                continue
            rid = self._by_author_year(m.group(1), m.group(2))
            if rid:
                found.append([m.start(), m.end(), [rid]])
        found.sort()
        # drop overlaps (keep the first)
        out, last_end = [], -1
        for f in found:
            if f[0] >= last_end:
                out.append(f)
                last_end = f[1]
        return out
