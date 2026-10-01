# Papercut — Design Document

| | |
|---|---|
| **Status** | Approved — ready for implementation |
| **Date** | 2026-09-24 |
| **Owner** | oxo106 |
| **Implementer** | Claude (primary), owner (review) |

---

## 1. Overview

Papercut is a personal, zero-cost tool for reading academic papers. It does two things that general PDF viewers do not:

1. **Reflowed reading.** It rebuilds a paper's structure from its PDF and presents it as a single, customizable column (font, size, spacing, width, theme), replacing the fixed two-column print layout.
2. **Rhetorical highlights.** An LLM classifies sentences by the role they play in the paper (objective, novelty, method, result, limitation, definition) and highlights them in color directly in the text, so the reader can skim a paper's argument at a glance and share an annotated copy with others.

### 1.1 Goals

- Read any born-digital academic PDF in a comfortable, single-column view.
- Make the structure of a paper's argument visible through color-coded sentence highlights.
- Let the reader correct the AI and choose which categories to show without re-running it.
- Export a standard PDF, in the paper's original layout, carrying the highlights as real annotations.
- Cost nothing to run.

### 1.2 Non-goals

- Scanned or image-only PDFs (no OCR).
- Library management (tags, search, collections) — Zotero already does this well.
- Accounts, authentication, or multi-user use.
- Hosted share links or any public server.
- A free-form AI summary panel. Highlights are the only AI output shown in the text.

### 1.3 Users

A single user (the owner), reading on two machines:

| Machine | Role | Resources |
|---|---|---|
| **Desktop PC** | Runs the server; does all parsing and AI processing | RTX 4070 Ti SUPER (16 GB VRAM), 32 GB RAM, Python 3.12 |
| **Laptop** | Reads papers; submits new papers to the PC | Low compute; no local processing |

---

## 2. Requirements

### 2.1 Functional

| ID | Requirement |
|---|---|
| F1 | Open a PDF by drag-and-drop or file picker. |
| F2 | Open a paper from an arXiv ID/URL or a DOI (downloaded when a PDF is openly available). |
| F3 | Show a list of recently opened papers. |
| F4 | Parse the PDF into ordered structure: title, authors, headings, paragraphs, figures, tables, equations, captions, footnotes, references. |
| F5 | Render the structure as a single column. Figures, tables and equations appear as image crops at their position in the reading order. |
| F6 | Reading controls: font family, font size, line height, column width, theme (light / sepia / dark), show/hide per highlight category. Settings are global, not per paper. |
| F7 | Classify sentences in the body text and in figure/table captions into at most one highlight category each, with a confidence score. References and appendices are not classified. |
| F8 | Show every highlight the AI chose to keep; the model, not a quota, decides how many. |
| F9 | Clicking a sentence lets the user set, change or clear its category. User edits override AI labels and survive re-processing. |
| F10 | Export the original PDF with highlight annotations at each highlighted sentence, colored by category. |
| F11 | Show a progress bar while a paper is being processed. |
| F12 | Ask questions about the open paper; answers come from the local model, cite passages, and link back to them. |
| F13 | Explain any sentence in plain language on request (hover or sentence menu). |
| F15 | Attach notes to sentences; see them on every device; include them in the export as PDF comments. |
| F16 | Translate a selected word, phrase, sentence or paragraph, or any sentence from its hover button or menu, into a chosen language. |
| F17 | Reopening or refreshing a paper returns to where the reader left off, on any device; the recent list shows how far each paper has been read. |
| F18 | A foldable outline sidebar lists the paper's sections, marks the one being read, and folds the appendix by default. |
| F14 | Keep the paper's own bold and italic text (run-in headings, emphasis) in the reading view. |

### 2.2 Non-functional

| ID | Requirement |
|---|---|
| N1 | **Zero cost.** Default AI runs locally; the paid path is never required. |
| N2 | **Trustworthy highlights.** A highlight always covers an exact sentence from the paper. The LLM selects and labels sentences by ID; it never produces the highlighted text. |
| N3 | **Process once.** Parsing and classification results are saved and reused; opening a processed paper is instant on any machine. |
| N4 | **Portable data.** All data lives in plain files in one library folder that a cloud drive can sync. No database. |
| N5 | **Graceful failure.** If parsing fails or looks wrong, the original PDF is always one click away. |
| N6 | **Future desktop.** The architecture must allow wrapping as a desktop app without a rewrite. |

---

## 3. Architecture

```
            ┌──────────────────────── Desktop PC ─────────────────────────┐
 Browser    │  FastAPI server                                              │
 (PC or     │   ├─ HTTP API + static frontend                              │
  laptop    │   ├─ Job queue (background worker, one job at a time)        │
  via  ────►│   │    1. Parse     — Docling (structure)                    │
 Tailscale) │   │                   PyMuPDF (word boxes, image crops)      │
            │   │    2. Segment   — sentence splitting + box mapping       │
            │   │    3. Classify  — local LLM (Ollama, Qwen 3.5 9B)        │
            │   └─ Export        — PyMuPDF highlight annotations           │
            │                                                              │
            │  Library folder (cloud-drive synced) ◄──── laptop reads too  │
            └──────────────────────────────────────────────────────────────┘
```

### 3.1 Technology choices

| Concern | Choice | Reason |
|---|---|---|
| Server | Python 3.12, FastAPI, Uvicorn | The best PDF tooling is in Python; FastAPI is small and async. |
| Structure parsing | **Docling** | MIT-licensed; outputs reading order, element types and **page provenance (bounding boxes)** for each element, which export depends on. |
| Low-level PDF | **PyMuPDF** | Word-level coordinates, image cropping, writing highlight annotations. |
| Sentence splitting | pySBD (or equivalent) | Handles academic abbreviations ("et al.", "Fig. 3", "e.g."). |
| LLM | **Ollama** with `qwen3.5:9b-q8_0` (11 GB, fits the 16 GB GPU fully) | Free, private, offline; used for highlights, Q&A and Explain. One request at a time. |
| Embeddings | **Ollama** with `qwen3-embedding:0.6b` (0.6 GB) | Semantic retrieval for Q&A and Explain. |
| Frontend | Plain HTML/CSS/JS (light Svelte only if complexity demands it) | No build step; easy for the owner to read and change. |
| Remote access | **Tailscale** | Private network between the owner's devices; nothing exposed to the internet. |
| Desktop wrapper | pywebview or Tauri with a Python sidecar | Reuses the server and frontend unchanged. |

### 3.2 Processing pipeline

1. **Ingest.** Copy the PDF into the library, compute its SHA-256, create the paper folder. If the hash already exists, open the existing paper.
2. **Parse (Docling).** Produce ordered elements with type and page bounding box.
3. **Crop.** For figure, table and equation elements, render the bounding box region with PyMuPDF to a PNG.
4. **Segment.** Split paragraph and caption text into sentences. Map each sentence to its word boxes on the page (PyMuPDF words within the element's box, aligned by text), producing one or more rectangles per sentence — sentences may span lines, columns and pages.
5. **Highlight.** The LLM reads the paper, marks it up section by section and then chooses what matters most (section 4.3). Unknown IDs and unknown categories are discarded.
6. **Save.** Write `paper.json`. Report progress after each stage.

Stages are idempotent and saved individually, so re-classifying (for example with a different model) does not require re-parsing.

### 3.3 Local model

All AI runs on the PC through Ollama (`app/llm.py`). Every request uses one context size (32k, enough for a whole paper's main text; one size so Ollama never reloads the model between features; the model then takes ~10 GB of the 16 GB GPU), keeps models loaded for 30 minutes, caps output length, times out after 3 minutes with one retry (20 minutes for thinking calls), and disables Qwen's presence penalty for JSON output (it caused runaway generation on repeated keys). Functions: `chat()` for structured output (JSON schema; optional `think` for reasoning first) and `stream_chat()` for Q&A answers. The model and Ollama URL can be overridden under `"ai"` in `settings.json`; there is no cloud provider.
---

## 4. Highlight system

### 4.1 Categories

| Color | Category | Meaning |
|---|---|---|
| 🟩 Green | **Objective** | What the paper sets out to do; the research question. |
| 🟦 Blue | **Novelty** | What is new: contributions, departures from prior work. |
| 🟪 Purple | **Method** | Key design decisions of the approach. |
| 🟥 Red | **Result** | Findings, measured outcomes, what was shown. |
| 🟧 Orange | **Limitation** | Caveats, failure cases, threats to validity, future work. |
| 🟨 Yellow | **Definition** | Introduction of a key term or concept. |

Each sentence has at most one category.

### 4.2 Rules

- **Scope:** body text and figure/table captions. Not references or appendices.
- **How many:** every highlight the AI kept in its final pass is shown; there is no density setting (a slider existed until the read → mark → choose highlighter made the model's own selection trustworthy). Labels from the old one-pass classifier, which had no such judgement, show their most confident 12%.
- **Grounding:** the LLM sees sentences with IDs and returns IDs only. The highlighted text is always the parser's text, never generated text.

### 4.3 How the model highlights (read → mark → choose)

Highlights should be what a careful reader would mark with a pen: the sentences that carry the essence of the paper, not every sentence that fits a category. The local model works in three passes (`app/highlight.py`), about 5–7 minutes per paper:

1. **Read** (thinking on). The model reads the whole main text in one request and writes reading notes (≤350 words): problem, core idea, contributions, key results with numbers, what is surprising, limitations, key terms. Stored as `reading_notes`.
2. **Mark.** Section by section, in chunks of whole paragraphs (≤45 sentences) with paragraph boundaries visible, the model gets its reading notes and the sentences it has already highlighted, and marks candidates: category, importance (5 = belongs in a one-paragraph summary, 4 = key evidence, 3 = notable) and a margin note of at most 15 words on why it matters. It is told most paragraphs get zero or one highlight and not to repeat a point already highlighted.
3. **Choose** (thinking on). The model sees all its candidates together, with section and notes, merges candidates that make the same point (keeping the most specific, e.g. with numbers), drops routine ones and keeps every other candidate that is genuinely meaningful, insightful or important (no quota: long, dense papers keep more), and sorts them into tiers: 1 = the essence, 2 = key support, 3 = worth noting. With many candidates (204 in one long paper) the thinking used up the whole output budget before any answer, so the budget is sized to the context left, and if no answer comes back it decides again without thinking; if that fails too, only pass-2 importance 4–5 marks are kept.

Confidence = tier base (0.9 / 0.75 / 0.6; 0.3 for candidates dropped in pass 3) + 0.02 × (importance − 3), which orders them (tier 1 first). All kept highlights (tiers 1–3) are shown; candidates dropped in pass 3 are stored (tier `null`) but never shown. If pass 3 fails, tiers fall back to pass-2 importance. Each AI label stores `category, confidence, tier, note`; the note appears as the sentence's tooltip and at the top of its click menu ("AI note").

Measured against the previous one-pass classifier (which labelled each section in isolation and gave ~95% of labels a confidence of 0.8–0.9, making the density cut-off arbitrary), at 12% density: SWE-bench's headline result is highlighted once instead of three times, and filler sentences ("In this section we explain…") are gone. On Attention Is All You Need, 24 essence sentences are shown instead of 51 (the old ties overshot the density). The previous labels are kept in `library/backup-highlights-v1/`.
- **User edits:** stored separately from AI labels, with priority over them. Re-running the AI never overwrites an edit.

---

## 5. Data model

### 5.1 Library layout

```
<library>/
  settings.json                 # global reading settings
  recent.json                   # recently opened papers
  shelf.json                    # your status, tags and collection per paper
  collections.json              # the AI's collections and which paper is in which
  reviews.json                  # flashcard review state, "<paper id>:<card id>"
  pending-jobs.json             # unfinished background jobs, resumed on start
  papers/
    <sha256-prefix>/
      original.pdf
      paper.json                # structure, sentences, labels, edits
      assets/
        fig-3-1.png             # crops: figures, tables, equations
```

### 5.2 `paper.json` (schema sketch)

```jsonc
{
  "schema_version": 1,
  "id": "a1b2c3d4",
  "source": { "filename": "Paper 1.pdf", "arxiv": null, "doi": null },
  "meta": { "title": "…", "authors": ["…"] },
  "status": { "parsed": true, "classified": true, "model": "ollama:qwen…", "error": null },
  "blocks": [
    { "id": "b12", "type": "heading",   "level": 1, "text": "1 Introduction" },
    { "id": "b13", "type": "paragraph", "section": "b12", "sentences": ["s40", "s41"] },
    { "id": "b14", "type": "figure",    "image": "assets/fig-3-1.png", "caption_sentences": ["s42"] },
    { "id": "b15", "type": "equation",  "image": "assets/eq-4-2.png" }
  ],
  "sentences": {
    "s40": { "text": "…", "rects": [{ "page": 1, "bbox": [x0, y0, x1, y1] }] }
  },
  "ai_labels":  { "s40": { "category": "objective", "confidence": 0.91 } },
  "user_edits": { "s41": { "category": "novelty" }, "s43": { "category": null } },
  "summary":   { "tldr": "…", "results": [{ "text": "…", "highlights": ["s40"] }], "topics": ["…"], … },
  "cards":     { "cards": [{ "id": "…", "front": "…", "back": "…", "highlights": ["s40"], "by": "ai" }] },
  "ref_links": { "b301": "<paper id>" }   // cited papers added to the library
}
```

Block types: `title`, `authors`, `heading`, `paragraph`, `list`, `figure`, `table`, `equation`, `footnote`, `references`.

---

## 6. User experience

### 6.1 Opening a paper
Drop a PDF, pick a file, paste an arXiv/DOI link, or choose from recent papers. New papers show a progress bar (parsing → segmenting → classifying) until processing completes; processed papers open instantly.

### 6.1a Opening from a link
The home page accepts an arXiv ID or URL (`2303.11366`, `arXiv:2303.11366v4`, `arxiv.org/abs|pdf/…`, old-style `hep-th/9901001`), a DOI (`10.1145/…`, `doi.org/…`, arXiv DOIs `10.48550/arXiv.…`), an OpenReview forum link, or any direct PDF link. For a DOI, free copies are tried in order: an arXiv version (from OpenAlex, or an arXiv search requiring an exact title **and** first-author match — generic titles clash), repository copies, then the publisher PDF (publishers often block automated downloads). Paywalled papers with no free copy are reported, not guessed. Every download is checked to really be a PDF. The paper's arXiv ID / DOI / URL is recorded in `source.json` and `paper.json`.

### 6.2 Reading view
Single column in the chosen typography. Figures, tables and equations are image crops placed in reading order. Text is left-aligned with automatic hyphenation. A toolbar toggles the original PDF view.

### 6.3 Controls (global)
Font family (Linux Libertine, Open Sans, Roboto — bundled from the owner's font set), font size (14–30px), line height, column width (480–1600px, default 860px), theme (auto / light / sepia / dark), per-category visibility. Settings are stored in the library's `settings.json`, so they follow the library to other machines.

### 6.4 Editing highlights
Click a sentence to open a small category picker: six categories, "No highlight", and (for edited sentences) "Reset to AI's choice". The change is saved immediately.

### 6.4a Citations
Numeric (`[12]`, `[3, 5-7]`) and author–year (`(Kiela et al., 2021; Ott et al., 2022)`, `Yao et al. (2023)`, ACM `[Xia et al. 2023]`) citations are linked to reference-list entries at parse time. They render in a muted colour; hovering or tapping one shows the full reference(s).

### 6.4a2 Figure and table captions
Captions render below their figure or table, as in the paper. Loose captions Docling didn't attach are paired with the uncaptioned figure/table beside them. When several captions sit side by side under one detected image (e.g. SWE-bench Tables 2–3, Tree of Thoughts Tables 4–6), the image is cut at the blank vertical strips between the caption columns, each part trimmed to its content and given its own caption. Captions are recognised before table detection, so number-heavy captions ("…GPT-4 vs GPT-3.5") are never mistaken for tables, and captions wrapped onto a second line are rejoined. Crop files are named by a hash of their content, so a changed crop always gets a new URL.


### 6.4a2b One figure cut into pieces
The reverse of splitting side-by-side tables: Docling sometimes cuts one figure into several pictures and gives the caption to one (ReAct's Figure 2: two plots side by side; Attention's Figure 2: two diagrams). An uncaptioned picture within three blocks of a captioned one, on the same page, with its centre inside the caption's width, and either beside it (≥50% vertical overlap) or directly above/below it (≤25pt gap, overlapping horizontally) is merged into it. The merged crop spans the caption's width and is then trimmed to the ink, so edge labels the pieces' boxes missed ("Scaled Dot-Product Attention") are kept. Pictures with their own captions are never merged. Dry runs over all 13 papers changed exactly these two figures.

### 6.4a3 Boxed tables (text between rules)
Some tables are not grids but boxed text between horizontal rules: DeepSeek-R1's prompt template and "aha moment" (Tables 1–2) and its evaluation-prompt tables, SWE-bench's appendix example trajectories, ReAct's prompts, and "Listing N" boxes. Docling reads these as a caption followed by ordinary paragraphs, code and even headings, with no table. For a "Table N"/"Listing N" caption with no figure or table within two blocks, `_tables_from_rules` looks for a horizontal rule (a line, or a hairline box, ≥60pt wide, under the caption's centre) within 45pt below the caption (or above it, for a caption under its table), follows rules of the same width until another caption or float lies in between, and crops from the first to the last rule as the table; the caption goes under it as usual, and every text block ≥60% inside the box (including headings like "PROMPT" and code blocks, which are located by their overall position) leaves the reading flow. Needs at least two rules; a box whose rule chain doesn't close on the caption's page is followed instead by its **monospace lines** (PyMuPDF font flag), across up to 8 pages, until the first line of prose or another caption; each page's piece is rendered and the pieces are stacked into one image (DeepSeek-R1 Listings 2, 5, 6 run over 2–4 pages). "Listing N" captions that Docling read as a paragraph or as code are recognised as captions first. Dry runs: DeepSeek-R1 +16 tables (Tables 1, 2, 18–32, Listings 1, 4), SWE-bench +10 (Tables 25–34), ReAct +5, Attention unchanged.

### 6.4a4 Cited papers: open or add
Each reference-list entry, and each reference in a citation's hover card, gets an action: **Open in Papercut** when the cited paper is already in the library (matched by its title appearing in the entry, or by an earlier add), otherwise **Add to library**. Adding (`app/refs.py`) uses an arXiv id or DOI written in the entry; otherwise it asks OpenAlex's search and Crossref's bibliographic search with the whole entry and accepts a candidate only if its title appears in the entry, so a near-miss never adds the wrong paper. It then prefers the paper's arXiv copy, else an open-access PDF via the DOI, downloads it like a pasted link and queues it; the link is remembered in `ref_links` (kept across re-parses). Endpoint: `POST /api/papers/{id}/references/{block}/add`.

### 6.4b Figure and table mentions
Mentions such as "Figure 3", "Tables 1 and 2" or "Fig. 4a" are linked at parse time to the figure/table whose caption starts with that label. They are underlined with dots; hovering or tapping shows the figure or table image with its caption and a "Go to" button.

### 6.4c Footnotes
Author notes on page 1 (equal contribution, correspondence, emails) are shown with the front matter. Other footnotes are moved to the end of their section so they do not interrupt paragraphs.

### 6.4d Ask (Q&A)
An **Ask** panel answers questions about the open paper with the local model, as a small retrieval-augmented setup:
- **Passages:** every paragraph, list item, caption and footnote of the main text and appendix (not front matter or references).
- **Retrieval (hybrid):** BM25 keyword ranking and semantic ranking from `qwen3-embedding:0.6b` are merged by reciprocal rank fusion, for the question plus the previous question (so follow-ups work). Passages containing highlights of the kind the question asks about (e.g. "limitations" → Limitation sentences) get a boost. The abstract is always included. Up to 8 passages, given in reading order. Passage vectors are computed once per paper (after highlighting) and cached in the paper folder as `embeddings.npz`, keyed by passage text; if the embedding model is unavailable, retrieval falls back to BM25 alone.
- **Answering:** the model must answer only from the numbered passages, cite them as `[n]`, and say so when the passages don't contain the answer. The last 3 turns are included for follow-ups. Answers stream in as they are generated.
- **Reading the answer:** `[n]` citations become chips that scroll to and flash the passage; each answer lists its sources.
- **History:** questions and answers are saved per paper in `paper.json` (`chat`, last 50 turns) and survive re-parsing. **Clear** removes them.

### 6.4d2 Ask can look beyond the paper (research tools)
Ask gives the local model tools (Ollama tool calling; `app/research.py`), which it uses when the question needs more than the paper: related or later work, what a cited paper did, or a concept the paper uses but doesn't explain. It answers from the paper's passages when they suffice.

| Tool | Source | Notes |
|---|---|---|
| `search_papers(query)` | arXiv API | Titles, authors, year, abstract. arXiv's relevance beat OpenAlex search, which also rate-limits anonymous use. |
| `citing_papers()` | OpenAlex `cites:` filter | 5 most-cited + 5 newest citing works. The paper's OpenAlex id is found once (DOI / arXiv id / exact-title arXiv match) and cached as `openalex.json`. Citation links are incomplete, so the prompt tells the model to also search by name. |
| `get_reference(n)` | the paper itself | Full text of reference [n], to then search for it. |
| `web_search(query)`, `web_fetch(url)` | Ollama web search API | Only when an Ollama API key is present (`D:\Read\ollama-api-key.txt` or env `OLLAMA_API_KEY`); page text is truncated for the model. Wikipedia and DuckDuckGo were tried: Wikipedia refuses clients without contact details, DuckDuckGo's free API returns almost nothing. |

When to search: left alone, the 9B model answered a "explain BM25 thoroughly" question from memory, with two factual errors and invented [S1]/[S2] citations. So a question that plainly asks for outside sources (in-depth explanation, related/later work, what reference [n] did, "look it up") gets a note telling the model to search first, and the **Search** pill in the question box forces that for any question. Once tool results are in, the final answer is generated with thinking on (BM25 test: parameters explained correctly, claims cited to the right sources; ~20–30 s). [S..] citations that match no real source are removed from the saved answer and not shown while streaming. Answers may use headings, numbered lists and code blocks (formulas), which the panel renders.

Up to 4 tool rounds, then the model must answer. When a search is wanted, the first turn also runs with thinking on: without it the model skipped the tools for Questions' literature/web questions and wrote "external research indicates…" from memory. arXiv search retries with fewer terms when a long query matches nothing. Outside results are numbered [S1], [S2]… across the answer and cited like passages; the page shows them as dashed chips that open the source in a new tab, lists what was looked up ("Searching papers: …") above the answer and the outside sources in a folded list below it. The chat history stores `web` and `steps` with each answer. Deep concept explanations may run to several paragraphs; ordinary answers stay short.

### 6.4d2b Job queue survives restarts
The background queue (parse, highlight, questions) is in memory, so a server restart used to drop a running batch. Unfinished jobs are now also written to `library/pending-jobs.json` and re-queued when the server starts; the job that was running starts over.

### 6.4d3 Questions about the highlights
A **Questions** button in the top bar opens its own side panel (in the same place as Ask; one of the two is open at a time). On request ("Come up with questions") a background job (`app/insights.py`, job kind `insights`, status kept under `<paper id>:insights` so the paper never looks busy) does two things:

1. **Ask** (thinking on): the model gets its reading notes and its key highlights (tiers 1–2, plus the user's own highlights) with their margin notes, and writes ~10 questions a sharp reader would ask about them, not ones the sentence already answers. Each has a kind (why/how, assumption, comparison, generalization, implication, weakness, background), the 1–2 highlights it is about, and where an answer most likely is (this paper, other papers, the web).
2. **Answer**: each question goes through Ask's pipeline (`ask.answer(save=False)`): paper passages, plus paper/web search when the answer lies outside the paper. Answers are ≤~150 words with citations and end with a status: answered, partly answered, or open.

Cards show the kind, status, question, the highlight(s) it is about (click to jump), the answer with [n]/[S] chips, what was looked up, and "Follow up in Ask". Stored in `paper.json` as `insights` (kept across re-parses, highlights re-anchored by text); "Ask new questions" replaces them. Endpoints: `GET/POST /api/papers/{id}/insights`.

### 6.4d4 Questions in their own window, and export
The Questions panel can **Open in window** (`#/paper/{id}/questions`, a popup window; in the installed app, its own app window) and **Export** the questions and answers as Markdown. The window lists the same cards and has Export and Print. It talks to the reader window over a `BroadcastChannel`: clicking a highlight or source jumps there in the reader, and "Follow up in Ask" fills the reader's Ask box; if no reader window has the paper open (no reply within 0.4 s), the window itself turns into the reader.

### 6.4d5 Summary
The **Summary** panel is a one-page cheat sheet written by a background job (`app/summary.py`, kind `summary`) that runs automatically after every highlighting (and once, on start, for papers highlighted before summaries existed). With thinking on, the model gets its reading notes, its key highlights with margin notes, the abstract, the section outline and the front matter, and writes: TL;DR, problem, approach, 3–5 key results with numbers, contributions, limitations, open questions, plus authors, year, venue and 3–6 topic tags. Results, contributions and limitations carry the highlights they rest on, shown as page chips that jump to the sentence. Below it, **In your library** shows how the paper connects to the rest of the library (6.7). Endpoints: `GET/POST /api/papers/{id}/summary`, `GET /api/papers/{id}/connections`.

### 6.4d6 Flashcards and review
The **Cards** panel holds the paper's flashcards: as many as the model judges worth remembering (no fixed number), written (`app/cards.py`, kind `cards`) from the summary, key highlights and answered Questions (question on the front, a 1–3 sentence answer on the back, a kind, and the highlights it tests), plus your own (**Add your own card**, or **Make a flashcard** in a sentence's menu, which prefills the back with the sentence). Rewriting the AI's cards keeps yours. Review is **on demand, one paper at a time**: nothing is scheduled, counted or pushed. **Review** in the top bar lists the papers that have cards (how many, how many reviewed, when last), and `#/review/{id}` goes through all of a paper's cards; cards from different papers are never mixed. Space shows the answer; 1–4 grade it Again / Hard / Good / Easy. Grades decide the order next time: a small SM-2 scheduler keeps an ease and an interval per card, and a session starts with the cards whose interval has run out (most overdue first), then cards never reviewed, then the rest; "Again" brings a card back once more before the paper is done. Review state is in `reviews.json` keyed by paper and card id (a hash of the front), so it survives rewrites of the back and re-parses. The page also lists papers without cards and can queue them all.

### 6.4e Explain a sentence
Hovering a sentence shows a small **Explain** button at its end (the sentence menu has the same action for touch screens). The local model explains the sentence in 2–4 plain-language sentences — what it says, its jargon or symbols, and why it matters — using the paper's abstract, the sentence's paragraph, and the three passages that best match it (hybrid retrieval, as above). The hover control is a small lightbulb icon at the end of the sentence. The explanation streams into a popover under the sentence and is cached per sentence in `paper.json` (`explanations`), surviving re-parses.

### 6.4e1 Explain a figure or table
Hovering a figure or table shows a lightbulb in the image's top-right corner (always visible on touch screens). The local model (Qwen3.5 is multimodal; Ollama lists `vision`) is sent the cropped image, downscaled to at most 1280 px on its longer side, plus the caption and up to three paragraphs that mention it (from the cross-reference links), and explains in 3–5 plain sentences what is compared, the main pattern, and what it shows for the paper's argument. The answer streams into the same popover as sentence Explain, typically in 10–15 s, and is cached in `paper.json` (`float_explanations`, keyed by the crop's file name, which includes a hash of the image, so it survives re-parses). Endpoint: `POST /api/papers/{id}/explain-float {block}`.

### 6.4e2 Translate
Next to the lightbulb, hovering a sentence shows a **translate** button (the sentence menu has **Translate this sentence**). Selecting any text in the reading view (a word, part of a sentence, a paragraph) shows a translate button under the selection. The translation streams into the same popover as Explain; its header has a language menu (简体中文 by default, also Traditional Chinese, Japanese, Korean, Spanish, French, German, Portuguese, Russian, English) that re-translates on change and is also in the Aa panel. A selection of up to four words without end punctuation is treated as a term: the model gives its translation plus a one-line meaning in the surrounding sentence(s), which the page sends as context. Longer text is translated faithfully, keeping math, symbols, citation markers and names unchanged. Results are cached in `paper.json` (`translations`, keyed by language + text, last 500), surviving re-parses. Endpoint: `POST /api/papers/{id}/translate {text, lang, context?}` → NDJSON stream.

### 6.4e3 Reading position
While reading, the page saves the sentence at the top of the screen and its distance below the header (debounced 0.8 s, and on tab hide/close) to `position.json` in the paper's folder, a separate small file so scrolling never rewrites `paper.json`. The newest of the server copy and the browser's own copy wins, so the laptop and PC share one position. A sentence rather than a pixel offset is stored because font size, column width and screen size change the layout. On open, the paper scrolls back to that sentence and keeps it in place while figures load (up to 4 s, or until the reader scrolls); a toast says "Resumed where you left off" with a **Back to start** button. The percentage counts the main text only: it reaches 100% when the end of the main text, where the references or the appendix begin, whichever is first, reaches the bottom of the screen. The recent list shows it. Endpoints: `GET/PUT /api/papers/{id}/position`.

### 6.4e4 Outline
The **Outline** button opens a sidebar on the left (pushing the text over on screens ≥1100 px, overlaying it on smaller ones, where it closes after a jump). Open/closed is a setting and follows the library. Docling gives all headings one level, so nesting comes from the numbering (`3` → `3.2` → `3.2.1`, `A` → `A.1`); if top-level numbers repeat (numbering restarts per section, e.g. "INTRODUCTION / 1. … / METHODS / 1. …"), numbered headings nest under the unnumbered ones. Unnumbered headings are top level if they are standard section names (Abstract, References, Acknowledgements…), otherwise sub-entries of the current section. Garbled headings (U+FFFD), markdown `#` headings and repeated labels inside worked examples ("Setting", "Issue"…) are left out. The appendix is one group, folded by default along with its subsections. While scrolling, the current section is highlighted (or its folded group), and the header shows a thin main-text progress bar.

### 6.4e4b Figures & tables tab
The outline sidebar has two tabs, **Sections** and **Figures & tables** (the choice is a setting). The second lists every cropped figure and table with a thumbnail, its label and the start of its caption, in groups: Figures, Tables, Appendix (folded) and Without a caption (folded). Labels come from the caption's opening ("Figure 3", "Table A2"), the same rule the parser uses for cross-references; the caption's own label decides figure vs. table. Clicking an entry jumps to it (unfolding the appendix if needed); the one nearest the middle of the screen is highlighted while scrolling.

### 6.4e5 Folded references and appendix
In the reading view the references and the appendix each sit behind a bar ("▸ References · 70 entries · Show", "▸ Appendix · 6 sections · Show"), folded by default; the choice is remembered per paper in the browser. The paper's own "References" heading becomes the bar, so outline links land on it. Citation hover previews keep working while the list is folded. Jumps into it (outline entries, Ask source chips, a restored reading position) unfold it first. While folded, its bar stands in for the hidden blocks in the outline's current-section marker and the progress calculation, and notes on appendix sentences are not laid out. Figure, table and equation images reserve their height before loading (the server adds each crop's pixel size, `px`, read from the PNG header, when it serves `paper.json`), so jumps and restored positions don't drift as images load.

### 6.4f Bold and italic text
Words set in bold or italic in the PDF (run-in headings such as "**Evaluation metrics.**" or "*Contributions.*", bold numbers in results, the authors' emphasis) are detected from PyMuPDF's font flags and font names (e.g. `LinLibertineTB`, `NimbusRomNo9L-Medi`, `…-ItalicMT`) and stored per sentence as character ranges (`bold`, `italic`), then rendered in the reading view. Math-italic fonts (CMMI etc.) are ignored.

### 6.4g Notes
Any sentence can carry a free-text note (sentence menu → **Add note** / **Edit note**). Notes are stored in `paper.json` (`notes`: id, sentence id, text, timestamps) on the PC, so every device using the server sees the same notes; an open page re-fetches them when its window regains focus. They appear in the right margin beside their sentence when at least 250 px is free, otherwise as cards under the paragraph; noted sentences get a yellow underline, and hovering a card outlines its sentence. Notes survive re-parses (re-anchored by sentence text).

### 6.4h Outdated highlights
When the highlighter changes (its version is stored with each paper's highlights), a paper highlighted by an older version shows a notice with **Update highlights**, which re-runs the AI; your edits and notes are kept.

### 6.5 Export
Produces a copy of the original PDF with:
- A highlight annotation over each visible highlighted sentence's rectangles, colored by category, with the category name as the annotation comment.
- A sticky-note annotation on page 1 containing the color legend.
- Each note as a PDF comment (speech-bubble icon) in the margin beside its sentence's line.

The paper's pages are not modified; recipients can view, change or delete annotations in any PDF reader. Export respects the AI's kept highlights, category visibility and user edits.

**Markdown** (More → Export Markdown…, `app/markdown.py`): choose any of summary, highlights (grouped by section, with category, page number, the AI's margin note and your notes), your notes, questions and answers, flashcards and the Ask history. Passage citations become page references "(p. 5)", outside sources become links. Hidden categories are left out. Endpoint: `GET /api/papers/{id}/markdown?parts=…&hidden=…`.

### 6.5b Phones (designed for an iPhone Air, 420 x 912 points)
Below 760 px wide: the reader's buttons become a bottom tab bar with icons (Outline, Summary, Ask, Questions, Cards, More), the More menu opens upward from it, and the top bar shows the paper's title. Side panels and the outline fill the screen between the two bars. Safe-area insets keep clear of the Dynamic Island and the home indicator (`viewport-fit=cover`); inputs are 16 px so iOS doesn't zoom on focus; hover buttons are off on touch screens (tapping a sentence opens its menu). The library's collections become a sideways-scrolling row of chips and "Continue reading" a swipeable row. Text size and line height are kept separately for phones (`phoneSize`, `phoneLeading`, default 18 px / 1.6), since the settings are shared through the server. Apple web-app meta tags make "Add to Home Screen" open Papercut full screen.

### 6.6 Library
The home page is the library (`app/catalog.py`):
- **Continue reading:** up to three papers started but not finished.
- **Collections** on the left. The model groups the library into 2–8 themed collections (kind `organize`, a library-wide job); it runs automatically when a new paper's summary is ready and it has no collection, keeping the existing collections and only filing new papers. **Reorganize with AI** can keep them or start over. You can move a paper to another or a new collection; your choice wins over the AI's.
- **Each paper:** title, authors · venue year, TL;DR, AI topic tags (click to filter), your own tags, status (to read / reading / done: from reading progress, ≥95% = done, unless you set it), collection, a progress bar and counts of highlights, notes, questions and cards.
- **Filters and sort:** status, collection; last opened, recently added, title, year.
- **Search:** filters the list instantly by title, authors, venue, year, summary, topics, tags and collection; after a pause (or Enter), it also searches **inside** every paper: each passage is scored by embedding similarity to the query plus keyword overlap, at most four passages per paper, grouped by paper with the matches marked; a click opens the paper at that passage (`#/paper/{id}/s/{sid}`). Per-paper passages and vectors are cached in memory by `paper.json`'s modification time (a query takes ~0.2 s after the first). Endpoints: `GET /api/library`, `PUT /api/library/{id}`, `POST /api/library/organize`, `GET /api/search?q=`.

### 6.7 Connections between papers
For a paper, `connections` lists: the library papers it **cites** (its reference entries that contain another library paper's title, or that were added from its references), the library papers that **cite it**, and the papers **closest in content** (cosine similarity of the mean passage embeddings, with shared topic tags). Shown in the Summary panel; reference entries that are in the library link to them.

---

## 7. Deployment and multi-machine use

- **Now:** the server runs on the PC; the browser opens `http://localhost:8000`.
- **Laptop (milestone 6):** the server keeps listening on 127.0.0.1 only. `tailscale serve --bg 8000` publishes it to the owner's tailnet at a private HTTPS address (`https://<pc>.<tailnet>.ts.net`); nothing is exposed to the LAN or internet and no firewall rules are needed. The laptop only needs Tailscale, signed in to the same account, and a browser; all uploads, processing and AI run on the PC. A web-app manifest lets Edge install Papercut as a windowed app.
- **Starting:** one entry point, *Start menu → Papercut* (`launch-papercut.vbs` → `start-papercut.ps1 -App`): starts Ollama, the server (hidden, detached, log in `papercut.log`) and Tailscale Serve if not already running, then opens Papercut in an Edge app window. Health is checked with `/api/health`, which doesn't touch Ollama. `stop-papercut.bat` stops the server. Nothing is added to Windows startup.
- **Offline reading:** the library folder lives in a cloud-synced directory, so processed papers are also available as files on the laptop.
- **Desktop (milestone 7):** the same server and frontend packaged in a native window.

---

## 8. Milestones

| # | Milestone | Done when | Status |
|---|---|---|---|
| 1 | **Parse + single-column view** | All papers in the test set open in a readable single column: correct reading order, headings, figures/equations/tables as crops with captions. Original-PDF toggle works. | Done |
| 2 | **Reading controls** | All controls in §6.3 work and persist across sessions. | Done |
| 3 | **AI highlights** | Sentences are classified with confidence; slider, category toggles and click-to-edit work, using the local model. | Done |
| 4 | **Export** | Exported PDF shows correct highlights in the original layout, including sentences spanning lines, columns and pages; legend note on page 1. | Done |
| 5 | **arXiv/DOI + recent list** | Papers open from an arXiv ID/URL or open-access DOI; recent list works. | Done |
| 6 | **Laptop access** | Laptop can upload, process (on PC) and read over Tailscale. | Done on the PC side (verified over the tailnet address); laptop to confirm |
| 7 | **Desktop app** | App launches as a native window with no terminal. | Next |

Milestones 1–4 are the core product. Milestone 1 carries the most risk, because parse quality determines whether the product works at all; it is validated against the test set before later milestones begin.

---

## 9. Test corpus

Ten real papers the owner reads, stored in `D:\Read\Papers\` (`Paper 1.pdf` … `Paper 10.pdf`). Every milestone is checked against this set. Papers that parse badly are kept as regression cases.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Docling mis-orders text or misses elements on unusual layouts | Unreadable reflow | Original-PDF toggle always available; regression corpus; per-paper fallback to PyMuPDF text blocks if needed. |
| Sentence ↔ page-box mapping fails (hyphenation, ligatures, column breaks) | Export highlights misplaced or missing | Map through Docling provenance plus word-level alignment, not page text search; report unmapped sentences instead of guessing. |
| Local model labels poorly | Misleading highlights | Confidence threshold, category toggles, user edits, prompt iteration on the test corpus. |
| Processing is slow | Friction on new papers | Process once and cache; background queue; progress bar. |

---

## 11. Decision log

| # | Decision | Alternatives considered |
|---|---|---|
| D1 | Personal tool, single user | Lab tool; public product |
| D2 | Born-digital PDFs only | arXiv-only; including scans (OCR) |
| D3 | Medium reflow fidelity; equations/tables/figures as image crops | Text-only; full math/table reconstruction |
| D4 | Sentence-level rhetorical highlights; no summary panel | Separate generated summary |
| D5 | Six-category taxonomy (§4.1) | Scholar-style three-color scheme |
| D6 | No density control: show every highlight the AI kept (a confidence-threshold slider was used until D29) | Fixed cap; per-section cap; density slider |
| D7 | User-editable highlights | Read-only AI output |
| D8 | Export to original layout with PDF annotations; legend as page-1 sticky note and per-highlight comments | Export reflowed view; added legend page |
| D9 | Local model only (Ollama, Qwen 3.5 9B); Gemini was tried and removed | Pluggable local + cloud providers |
| D10 | Local web app now; desktop wrapper later | Electron/Tauri first; browser extension |
| D11 | Laptop uses the PC's server via Tailscale Serve (private HTTPS, server stays on localhost) | Binding the server to the Tailscale/LAN interface; local processing on laptop; public server |
| D12 | Single library folder of plain files, cloud-synced | Sidecar next to each PDF; database |
| D13 | Docling + PyMuPDF | Marker; PyMuPDF alone |
| D14 | Global reading settings; no justified text | Per-paper settings; justify option |
| D15 | Progress bar during processing | Show original PDF while processing |
| D16 | Open via file/drag-drop, arXiv/DOI, recent list; no library management | Full library with tags and search |
| D17 | In-text citations shown muted; hover/tap shows the linked reference entry | Leave as plain text; option to hide citations |
| D18 | Reading fonts: Linux Libertine (default), Open Sans, Roboto, bundled with the app | System fonts only |
| D19 | No model picker: one local model for everything | Picker between local and Gemini models |
| D20 | Re-parsing keeps AI labels and user edits, matched by sentence text; the AI only re-runs on request | Re-run AI on every re-parse |
| D21 | Q&A over the open paper: hybrid BM25 + Qwen3-Embedding retrieval, highlight-aware boost, local model, cited answers | BM25 only; whole paper in context |
| D22 | Explain on hover, cached per sentence | Explanations only through the Q&A panel |
| D23 | Bold and italic preserved from PDF font flags and names | Plain text only |
| D24 | Open papers from arXiv/DOI/OpenReview/PDF links; DOIs resolved to free copies via OpenAlex + arXiv title+author search | Publisher PDFs only; Unpaywall (needs an email) |
| D26 | Translation by the local model, for any selection or sentence; target language as a setting; cached by text | Browser or cloud translation service |
| D27 | Reading position saved as a sentence anchor, server-side, restored automatically | Pixel scroll offset; browser-only storage |
| D28 | Outline nested by section numbering; appendix folded; progress counts the main text only | Flat heading list; progress over the whole document |
| D29 | Highlights by read → mark → choose: whole-paper reading notes, paragraph-aware marking with margin notes, paper-wide dedupe and tiering | One-pass per-section classification with a free confidence score |
| D30 | Summary, flashcards and collections generated by the local model as background jobs, from its own reading notes and highlights | Summaries from the abstract only; cards and collections by hand only |
| D31 | Review on demand, one paper at a time; SM-2 state only orders the cards (weakest first) | Daily due-card queues across the library with reminders (felt like pressure) |
| D32 | Cross-paper search: embedding similarity plus keyword overlap over every paper's cached passage vectors | A separate search index; keyword search only |
| D33 | A cited paper is added only when a search result's title appears in the reference entry | Taking the top search hit |
| D25 | Sentence-anchored notes stored server-side; margin layout when room, inline otherwise; exported as PDF comments | Notes kept in the browser only; not exported |
