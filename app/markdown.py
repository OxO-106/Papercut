"""Export a paper's reading as Markdown: summary, highlights (grouped by
section, with the AI's margin notes and your notes), Questions with their
answers and sources, flashcards and the Ask history. Works in any Markdown
editor (Obsidian, Notion, VS Code, GitHub).

Citations of the paper's passages ([3]) become "(p. 5)"-style page
references; outside sources ([S1]) become links.
"""

import re
import time

from .export import filename as pdf_filename
from .highlight import CATEGORIES

PARTS = ("summary", "highlights", "underlines", "notes", "questions", "cards", "chat")
CAT_NAMES = {c: c.capitalize() for c in CATEGORIES}
STATUS = {"answered": "Answered", "partly answered": "Partly answered", "open": "Open"}


def filename(paper: dict, suffix: str = "") -> str:
    return pdf_filename(paper).replace(" (highlighted).pdf", "") + suffix + ".md"


def _labels(paper: dict, hidden: set[str]) -> dict[str, str]:
    """The highlights shown on screen: kept AI labels plus the reader's edits."""
    out = {}
    ai = paper.get("ai_labels", {})
    tiered = any("tier" in l for l in ai.values())
    for sid, l in ai.items():
        if (l.get("tier") is not None) if tiered else l["confidence"] >= 0.7:
            out[sid] = l["category"]
    for sid, e in paper.get("user_edits", {}).items():
        if e.get("category"):
            out[sid] = e["category"]
        else:
            out.pop(sid, None)
    return {sid: c for sid, c in out.items() if c not in hidden}


def _page(paper: dict, sid: str | None) -> int | None:
    s = paper["sentences"].get(sid or "")
    rects = (s or {}).get("rects") or []
    return rects[0]["page"] if rects else None


def _answer(paper: dict, text: str, sources: list[dict], web: list[dict]) -> str:
    """[n] -> (p. N); [S1] -> [title](url)."""
    by_n = {str(s["n"]): s for s in sources}
    by_s = {s["n"]: s for s in web}

    def one(tok):
        tok = tok.strip()
        if tok.startswith("S"):
            s = by_s.get(tok)
            return f"[{tok}]({s['url']})" if s else ""
        s = by_n.get(tok)
        page = _page(paper, s["sid"]) if s else None
        return f"(p. {page})" if page else ""

    def repl(m):
        return " ".join(x for x in (one(t) for t in re.split(r"\s*[,;]\s*", m.group(1))) if x)

    text = re.sub(r"\[(S?\d+(?:\s*[,;]\s*S?\d+)*)\]", repl, text)
    return re.sub(r"[ \t]+([.,;:])", r"\1", text).strip()


def _web_list(web: list[dict]) -> list[str]:
    return [f"- [{s['n']}] [{s.get('title') or s['url']}]({s['url']})" for s in web]


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def render(paper: dict, parts=PARTS, hidden: set[str] = frozenset()) -> str:
    S = paper["sentences"]
    s = paper.get("summary") or {}
    src = paper.get("source") or {}
    out = [f"# {paper['meta']['title']}", ""]
    venue, year = s.get("venue", ""), s.get("year", "")
    when = venue if venue and year and year in venue else " ".join(x for x in (venue, year) if x)
    byline = " · ".join(x for x in (s.get("authors"), when) if x)
    if byline:
        out += [f"*{byline}*", ""]
    link = src.get("url") or (f"https://arxiv.org/abs/{src['arxiv']}" if src.get("arxiv") else None) \
        or (f"https://doi.org/{src['doi']}" if src.get("doi") else None)
    if link:
        out += [f"<{link}>", ""]
    if s.get("topics"):
        out += ["Topics: " + ", ".join(f"`{t}`" for t in s["topics"]), ""]

    def jumps(sids):
        pages = sorted({p for p in (_page(paper, x) for x in sids or []) if p})
        return f" (p. {', '.join(map(str, pages))})" if pages else ""

    if "summary" in parts and s:
        refs = [b for b in paper["blocks"] if b["type"] == "references"]
        ref_n = {b["id"]: n for n, b in enumerate(refs, 1)}
        out += ["## Summary", "", f"**In brief.** {s['tldr']}", ""]
        vis = s.get("visuals") or {}
        if vis.get("key_numbers"):
            out += [" · ".join(f"**{k['value']}** {k['label']}" for k in vis["key_numbers"]), ""]
        para = lambda head, text: [f"### {head}", "", text, ""] if text else []
        bullets = lambda head, items: ([f"### {head}", ""] + items + [""]) if items else []
        listed = lambda key: [f"- {it['text']}{jumps(it.get('highlights'))}" for it in s.get(key, [])]
        out += para("Background", s.get("background"))
        if s.get("key_terms"):
            out += ["**Key terms**", ""] + [f"- **{k['term']}**: {k['definition']}" for k in s["key_terms"]] + [""]
        out += para("Objective", s.get("objective")) + para("Problem", s.get("problem"))
        out += bullets("What's new", listed("novelty")) + para("Approach", s.get("approach"))
        out += bullets("Method", [f"{i}. {it['text']}{jumps(it.get('highlights'))}" for i, it in enumerate(s.get("method", []), 1)])
        out += [line for f in vis.get("figures", []) for line in (f"**{f['label']}.** {f['why']}", "")]
        table = []
        if vis.get("table"):
            t = vis["table"]
            cell = lambda c: str(c).replace("|", r"\|")
            table = ([f"*{t['title']}*", ""] if t.get("title") else []) + [
                "| " + " | ".join(map(cell, t["columns"])) + " |", "|" + "---|" * len(t["columns"])] + [
                "| " + " | ".join(map(cell, r)) + " |" for r in t["rows"]] + [""] + ([t["note"], ""] if t.get("note") else [])
        out += (["### Key results", ""] + table + listed("results") + [""]) if table or s.get("results") else []
        out += bullets("Contributions", listed("contributions"))
        out += bullets("Limitations", listed("limitations"))
        out += bullets("Related work", [
            f"- {it['text']}" + (f" [{', '.join(str(ref_n[r]) for r in it['refs'] if r in ref_n)}]" if it.get("refs") else "")
            for it in s.get("related_work", [])])
        if s.get("future_research") or s.get("open_questions"):
            out += ["### What's next", ""]
            if s.get("future_research"):
                out += ["**Future research**", ""] + [f"- {x}" for x in s["future_research"]] + [""]
            if s.get("open_questions"):
                out += ["**Open questions**", ""]
                for q in s["open_questions"]:
                    out += [f"- {q}"] if isinstance(q, str) else [f"- **{q['q']}**{jumps(q.get('at'))}", f"  *Proposed answer:* {q['answer']}"]
                out += [""]
        out += bullets("Later work", [
            f"- [{w['title']}]({w['url']})" + (f" ({w['year']})" if w.get("year") else "") + f": {w['note']}"
            for w in s.get("later_work", [])])
        if s.get("library_notes"):
            from . import catalog
            entries = {pid: catalog._entry(pid) for pid in s["library_notes"]}
            out += bullets("In your library", [f"- **{entries[pid]['title']}**: {note}"
                                               for pid, note in s["library_notes"].items() if entries[pid]])

    notes_by_sid: dict[str, list[dict]] = {}
    for n in paper.get("notes", []):
        notes_by_sid.setdefault(n.get("sid"), []).append(n)

    if "highlights" in parts:
        labels = _labels(paper, set(hidden))
        ai = paper.get("ai_labels", {})
        with_notes = "notes" in parts
        sections, heading = [], "Front matter"
        for b in paper["blocks"]:
            if b["type"] == "heading":
                heading = b["text"]
                continue
            ids = (b.get("sentences") or []) + (b.get("caption_sentences") or [])
            for sid in ids:
                if sid in labels or (with_notes and sid in notes_by_sid):
                    if not sections or sections[-1][0] != heading:
                        sections.append((heading, []))
                    sections[-1][1].append(sid)
        if sections:
            out += ["## Highlights", ""]
            legend = ", ".join(CAT_NAMES[c] for c in CATEGORIES if c not in hidden)
            out += [f"*Categories: {legend}. Notes in italics are the AI's margin notes.*", ""]
            for head, sids in sections:
                out += [f"### {head}", ""]
                for sid in sids:
                    cat = labels.get(sid)
                    page = _page(paper, sid)
                    tag = f"**{CAT_NAMES[cat]}**" if cat else "**Note**"
                    out.append(f"- {tag} {S[sid]['text']}" + (f" (p. {page})" if page else ""))
                    note = cat and ai.get(sid, {}).get("note")
                    if note:
                        out.append(f"  - *{note}*")
                    if with_notes:
                        for n in notes_by_sid.get(sid, []):
                            out.append(f"  - My note: {n['text']}")
                out.append("")

    if "underlines" in parts and paper.get("underlines"):
        order = {sid: i for i, sid in enumerate(S)}
        out += ["## Underlined", ""]
        for u in sorted(paper["underlines"], key=lambda u: (order.get(u["sid"], 0), u["a"])):
            s_ = S.get(u["sid"])
            if s_:
                page = _page(paper, u["sid"])
                out.append(f"- {_utf16_slice(s_['text'], u['a'], u['b'])}" + (f" (p. {page})" if page else ""))
        out.append("")

    if "notes" in parts:
        loose = notes_by_sid.get(None, [])
        if "highlights" not in parts:
            loose = [n for n in paper.get("notes", [])]
        if loose:
            out += ["## My notes", ""]
            for n in loose:
                sid = n.get("sid")
                if sid and sid in S:
                    out += [_quote(S[sid]["text"]), ""]
                out += [n["text"], ""]

    ins = paper.get("insights") or {}
    if "questions" in parts and ins.get("questions"):
        out += ["## Questions", ""]
        for i, q in enumerate(ins["questions"], 1):
            out += [f"### {i}. {q['q']}", "", f"*{STATUS.get(q.get('status'), q.get('status', ''))}*", ""]
            for sid in q.get("highlights", []):
                if sid in S:
                    page = _page(paper, sid)
                    out += [_quote(S[sid]["text"] + (f" (p. {page})" if page else "")), ""]
            out += [_answer(paper, q.get("answer", ""), q.get("sources", []), q.get("web", [])), ""]
            if q.get("web"):
                out += ["Sources:", ""] + _web_list(q["web"]) + [""]

    cards = (paper.get("cards") or {}).get("cards", [])
    if "cards" in parts and cards:
        out += ["## Flashcards", ""]
        for c in cards:
            out += [f"**Q:** {c['front']}  ", f"**A:** {c['back']}", ""]

    chat = paper.get("chat", [])
    if "chat" in parts and chat:
        out += ["## Ask", ""]
        for t in chat:
            out += [f"### {t['q']}", "", _answer(paper, t.get("a", ""), t.get("sources", []), t.get("web", [])), ""]
            if t.get("web"):
                out += ["Sources:", ""] + _web_list(t["web"]) + [""]

    out += ["---", f"*Exported from Papercut on {time.strftime('%Y-%m-%d')}.*", ""]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _utf16_slice(text: str, a: int, b: int) -> str:
    """text[a:b] with a and b counted in UTF-16 units, as the page counts them
    (a math letter like 𝑡 is two)."""
    return text.encode("utf-16-le")[2 * a:2 * b].decode("utf-16-le", errors="ignore")
