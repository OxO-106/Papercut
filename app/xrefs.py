"""Link in-text mentions of figures and tables ("Figure 3", "Tables 1 and 2",
"Fig. 4a") to their blocks, so the reader can preview them on hover.

Labels come from caption openings ("Figure 3:", "Table 2."). A standalone
caption block (Docling didn't attach it) is paired with the nearest unlabelled
figure/table block next to it.
"""

import re

# A caption may carry a page number Docling glued on in front ("9 Figure 6:").
# Labels may have a dot ("Table A.3"), which keys leave out.
_CAPTION_LABEL = re.compile(r"^\s*(?:\d{1,3}\s+)?(Figure|Fig\.?|Table|Tab\.?)\s*([A-Z]?\.?\d+)", re.I)
# Mentions in any case ("in figure 1", "TABLE 2"); a part letter ("4a") is lowercase.
_MENTION = re.compile(
    r"\b((?i:Figures?|Figs?\.|Tables?|Tabs?\.))\s*([A-Z]?\.?\d+)[a-z]?((?:\s*(?:,|and|&|–|-|to)\s*[A-Z]?\.?\d+[a-z]?)*)",
)


def _kind(word: str) -> str:
    return "table" if word.lower().startswith("tab") else "figure"


def _key(kind: str, label: str) -> str:
    return f"{kind}:{label.replace('.', '').upper()}"


def label_floats(blocks: list[dict], sentences: dict) -> dict[str, str]:
    """{"figure:3": block_id, "table:A2": block_id}"""
    labels: dict[str, str] = {}

    def caption_text(b):
        return " ".join(sentences[s]["text"] for s in b.get("caption_sentences", []) or b.get("sentences", []))

    for i, b in enumerate(blocks):
        if b["type"] in ("figure", "table"):
            m = _CAPTION_LABEL.match(caption_text(b))
            if m:
                labels.setdefault(_key(_kind(m[1]), m[2]), b["id"])
    for i, b in enumerate(blocks):
        if b["type"] != "caption":
            continue
        m = _CAPTION_LABEL.match(caption_text(b))
        if not m:
            continue
        key = _key(_kind(m[1]), m[2])
        if key in labels:
            continue
        # Pair with an adjacent unlabelled float; captions sit just above or below.
        taken = set(labels.values())
        near = [blocks[j] for j in (i - 1, i + 1, i - 2, i + 2)
                if 0 <= j < len(blocks) and blocks[j]["type"] in ("figure", "table") and blocks[j]["id"] not in taken]
        if near:
            near[0]["caption_block"] = b["id"]
            labels[key] = near[0]["id"]
        else:
            labels[key] = b["id"]  # caption only: better than nothing
    return labels


def find_mentions(text: str, labels: dict[str, str]) -> list[list]:
    """[[start, end, [block ids]], ...]"""
    out = []
    for m in _MENTION.finditer(text):
        kind = _kind(m[1])
        nums = [m[2]] + re.findall(r"[A-Z]?\.?\d+", m[3] or "")
        # "Figures 2-4": expand plain numeric ranges
        rng = re.fullmatch(r"\s*[–-]\s*(\d+)[a-z]?\s*", m[3] or "")
        if rng and m[2].isdigit():
            nums = [str(n) for n in range(int(m[2]), min(int(rng[1]), int(m[2]) + 10) + 1)]
        ids = []
        for n in nums:
            bid = labels.get(_key(kind, n))
            if bid and bid not in ids:
                ids.append(bid)
        if ids:
            out.append([m.start(), m.end(), ids])
    return out
