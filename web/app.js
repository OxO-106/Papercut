"use strict";

const $ = (id) => document.getElementById(id);
const views = ["home", "progress", "reader", "review", "qview"];
let current = null; // loaded paper.json

function show(view) {
  for (const v of views) $(v).hidden = v !== view;
  $("paper-actions").hidden = view !== "reader";
  if (view !== "reader" && openPanelName) setPanel(null);
  if (view !== "reader" && !$("outline").hidden) setOutlineOpen(false, false);
  closeMenu();
  $("nav-library").classList.toggle("active", view === "home");
  $("nav-review").classList.toggle("active", view === "review");
  if (view !== "reader") window.scrollTo(0, 0); // the reader restores its own position
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// ---------- reading settings ----------
// Stored in the library's settings.json (so they follow the library to other
// machines); cached in localStorage so the page paints with them immediately.
const DEFAULTS = { font: "libertine", size: 20, leading: 1.6, width: 860, theme: "auto", hidden: [], lang: "Simplified Chinese", outline: true, outlineTab: "sections" };
// Translation targets: [name sent to the model, label shown].
const LANGS = [
  ["Simplified Chinese", "简体中文"], ["Traditional Chinese", "繁體中文"], ["Japanese", "日本語"],
  ["Korean", "한국어"], ["Spanish", "Español"], ["French", "Français"], ["German", "Deutsch"],
  ["Portuguese", "Português"], ["Russian", "Русский"], ["English", "English"],
];
const CATEGORIES = [
  ["objective", "Objective"], ["novelty", "Novelty"], ["method", "Method"],
  ["result", "Result"], ["limitation", "Limitation"], ["definition", "Definition"],
];
const FONTS = {
  libertine: '"Linux Libertine", Georgia, serif',
  opensans: '"Open Sans", system-ui, sans-serif',
  roboto: '"Roboto", system-ui, sans-serif',
};
let settings = { ...DEFAULTS };
try { Object.assign(settings, JSON.parse(localStorage.getItem("settings") || "{}")); } catch {}
delete settings.ai; // model choice was removed: the app always uses the local model
delete settings.density; // the density slider was removed: every highlight the AI kept is shown

function applySettings() {
  const root = document.documentElement.style;
  root.setProperty("--font-body", FONTS[settings.font] || FONTS.libertine);
  root.setProperty("--font-size", `${settings.size}px`);
  root.setProperty("--line-height", settings.leading);
  root.setProperty("--measure", `${settings.width}px`);
  document.documentElement.dataset.theme = settings.theme;

  $("set-font").value = settings.font;
  $("set-size").value = settings.size;
  $("out-size").textContent = `${settings.size}px`;
  $("set-leading").value = settings.leading;
  $("out-leading").textContent = Number(settings.leading).toFixed(2);
  $("set-width").value = settings.width;
  $("out-width").textContent = `${settings.width}px`;
  for (const b of $("set-theme").children) b.setAttribute("aria-pressed", String(b.dataset.v === settings.theme));

  $("set-lang").value = settings.lang;
  for (const b of $("set-cats").children) b.setAttribute("aria-pressed", String(!settings.hidden.includes(b.dataset.cat)));
  if (current) { applyHighlights(); relayoutNotes(); }
}

let saveTimer;
function updateSettings(patch) {
  Object.assign(settings, patch);
  applySettings();
  try { localStorage.setItem("settings", JSON.stringify(settings)); } catch {}
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) }).catch(() => {});
  }, 400);
}

$("set-font").addEventListener("change", (e) => updateSettings({ font: e.target.value }));
for (const [v, label] of LANGS) $("set-lang").append(new Option(label, v));
$("set-lang").addEventListener("change", (e) => updateSettings({ lang: e.target.value }));
$("set-size").addEventListener("input", (e) => updateSettings({ size: Number(e.target.value) }));
$("set-leading").addEventListener("input", (e) => updateSettings({ leading: Number(e.target.value) }));
$("set-width").addEventListener("input", (e) => updateSettings({ width: Number(e.target.value) }));
$("set-theme").addEventListener("click", (e) => { if (e.target.dataset.v) updateSettings({ theme: e.target.dataset.v }); });
$("set-reset").addEventListener("click", () => updateSettings({ ...DEFAULTS }));
for (const [cat, label] of CATEGORIES) {
  const b = el("button");
  b.type = "button";
  b.dataset.cat = cat;
  b.append(el("span", `swatch ${cat}`), label);
  b.addEventListener("click", () => {
    const hidden = settings.hidden.includes(cat) ? settings.hidden.filter((c) => c !== cat) : [...settings.hidden, cat];
    updateSettings({ hidden });
  });
  $("set-cats").append(b);
}

$("settings-toggle").addEventListener("click", (e) => {
  const open = $("settings").hidden;
  $("settings").hidden = !open;
  e.currentTarget.setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", (e) => {
  if (!$("settings").hidden && !e.target.closest("#settings, #settings-toggle, #picker")) {
    $("settings").hidden = true;
    $("settings-toggle").setAttribute("aria-expanded", "false");
  }
});

applySettings();
api("/api/settings").then((s) => { Object.assign(settings, s); applySettings(); }).catch(() => {});

// ---------- routing ----------
history.scrollRestoration = "manual"; // the reader restores its own position (below)
window.addEventListener("hashchange", route);
// #/                          library
// #/paper/<id>                the reader; #/paper/<id>/s/<sid> opens it at a sentence
// #/paper/<id>/questions      the questions on their own (a separate window)
// #/review, #/review/<id>     flashcard review, across the library or for one paper
function route() {
  const h = location.hash;
  let m;
  if ((m = h.match(/^#\/paper\/([a-f0-9]+)\/questions/))) showQuestionsView(m[1]);
  else if ((m = h.match(/^#\/paper\/([a-f0-9]+)(?:\/s\/(s\d+))?/))) openPaper(m[1], m[2]);
  else if ((m = h.match(/^#\/review(?:\/([a-f0-9]+))?/))) showReview(m[1]);
  else showHome();
}

// ---------- home: the library ----------
async function showHome() {
  show("home");
  document.title = "Papercut";
  current = null;
  await loadLibrary();
}

async function upload(file) {
  if (!file || !/\.pdf$/i.test(file.name)) return alert("Please choose a PDF file.");
  const body = new FormData();
  body.append("file", file);
  show("progress");
  setProgress({ stage: "Uploading", progress: 0 });
  const { id } = await api("/api/papers", { method: "POST", body });
  location.hash = `#/paper/${id}`;
}

$("file").addEventListener("change", (e) => upload(e.target.files[0]));

// Open from a link: the server downloads the PDF (arXiv, DOI via open-access
// copies, OpenReview, or a direct PDF link), then processes it as usual.
$("link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const link = $("link-input").value.trim();
  if (!link) return;
  $("link-error").hidden = true;
  show("progress");
  setProgress({ stage: "Finding and downloading the PDF…", progress: 0.02 });
  try {
    const r = await fetch("/api/papers/from-link", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ link }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    $("link-input").value = "";
    location.hash = `#/paper/${body.id}`;
  } catch (err) {
    showHome();
    $("link-error").textContent = err.message || String(err);
    $("link-error").hidden = false;
  }
});
const drop = $("drop");
for (const ev of ["dragenter", "dragover"]) {
  document.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
}
for (const ev of ["dragleave", "drop"]) {
  document.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
}
document.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));

// ---------- processing ----------
function setProgress(s) {
  $("progress-stage").textContent = s.stage || "";
  $("progress-fill").style.width = `${Math.round((s.progress || 0) * 100)}%`;
  $("progress-error").hidden = !s.error;
  $("progress-error").textContent = s.error || "";
}

async function waitUntilProcessed(id) {
  show("progress");
  for (;;) {
    const s = await api(`/api/papers/${id}/status`);
    setProgress(s);
    if (s.state === "done") return true;
    if (s.state === "error") return false;
    if (!location.hash.includes(id)) return false; // navigated away
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ---------- reader ----------
async function openPaper(id, sid) {
  if (current?.id === id && !$("reader").hidden) { // already open: just jump
    if (sid) jumpToSentence(sid);
    return;
  }
  let paper;
  try {
    paper = await api(`/api/papers/${id}`);
  } catch (e) {
    if (!String(e).startsWith("409") || !(await waitUntilProcessed(id))) return;
    paper = await api(`/api/papers/${id}`);
  }
  current = paper;
  current.links = {}; // reference block -> library paper (loadConnections)
  document.title = paper.meta.title;
  $("original").hidden = true;
  $("original").removeAttribute("src");
  $("paper").hidden = false;
  setOriginalShown(false);
  render(paper);
  applyHighlights();
  renderNotice();
  editingId = null;
  draft = null;
  show("reader");
  if (openPanelName) PANEL_OPEN[openPanelName]();
  window.scrollTo(0, 0);
  buildOutline(paper);
  buildFloats(paper);
  setOutlineTab(settings.outlineTab, false);
  setOutlineOpen(settings.outline && window.innerWidth >= 1100, false);
  renderNotes();
  loadConnections();
  if (sid) {
    history.replaceState(null, "", `#/paper/${id}`); // a jump link, not a place to come back to
    requestAnimationFrame(() => jumpToSentence(sid));
  } else restorePosition(paper.id);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Sentence text with citations (<span class="cite">), figure/table mentions
// (<span class="xref">) and the paper's own bold/italic text (<strong>/<em>). data-refs
// on a cite/xref lists the linked block ids. Marks may overlap bold ranges, so
// the text is cut at every boundary and each piece wrapped as needed.
function sentenceContent(span, s) {
  const marks = [
    ...(s.cites || []).map(([a, b, refs]) => ({ a, b, refs, cls: "cite" })),
    ...(s.xrefs || []).map(([a, b, refs]) => ({ a, b, refs, cls: "xref" })),
  ].sort((x, y) => x.a - y.a);
  const bold = s.bold || [], italic = s.italic || [];
  const cuts = new Set([0, s.text.length]);
  for (const m of marks) cuts.add(m.a).add(m.b);
  for (const [a, b] of [...bold, ...italic]) cuts.add(a).add(b);
  const points = [...cuts].sort((x, y) => x - y);

  let open = null; // current cite/xref element being filled
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    const piece = s.text.slice(a, b);
    if (!piece) continue;
    const mark = marks.find((m) => m.a <= a && b <= m.b);
    const within = (ranges) => ranges.some(([x, y]) => x <= a && b <= y);
    let node = document.createTextNode(piece);
    if (within(italic)) { const em = el("em"); em.append(node); node = em; }
    if (within(bold)) { const st = el("strong"); st.append(node); node = st; }
    if (mark) {
      if (!open || open._mark !== mark) {
        open = el("span", mark.cls);
        open.dataset.refs = mark.refs.join(" ");
        open._mark = mark;
        span.append(open);
      }
      open.append(node);
    } else {
      open = null;
      span.append(node);
    }
  }
}

function sentenceSpans(parent, ids, paper) {
  ids.forEach((sid, i) => {
    const s = paper.sentences[sid];
    const span = el("span", "s");
    sentenceContent(span, s);
    span.dataset.sid = sid;
    if (s.coverage < 0.8) span.dataset.cov = "low";
    parent.append(span);
    if (i < ids.length - 1) parent.append(" ");
  });
  return parent;
}

// Crops keep their size on the page, scaled to the reading font (papers set body text at ~10pt).
function cropImg(paper, b) {
  const img = el("img");
  img.src = `/api/papers/${paper.id}/${b.image}`;
  img.alt = b.type;
  img.loading = "lazy";
  img.style.width = `calc(${b.width_pt / 10} * var(--font-size))`;
  if (b.px) img.style.aspectRatio = `${b.px[0]} / ${b.px[1]}`; // hold its space before it loads
  return img;
}

function render(paper) {
  const root = $("paper");
  root.replaceChildren();
  root.classList.toggle("debug", new URLSearchParams(location.search).has("debug"));
  // Reference entries are numbered in list order, the same order the server
  // uses to resolve numeric citations like [12].
  paper.refNumbers = {};
  // The references and the appendix each go in a foldable section, folded
  // unless opened before.
  const folds = {};

  for (const b of paper.blocks) {
    const front = b.region === "front";
    const foldable = b.region === "references" || b.region === "appendix";
    if (foldable && !folds[b.region]) {
      folds[b.region] = foldSection(paper, b.region);
      root.append(folds[b.region].section);
    }
    // The "References" heading becomes the fold's bar (outline links land on it).
    if (b.region === "references" && b.type === "heading" && !folds.references.section.dataset.block) {
      folds.references.section.dataset.block = b.id;
      continue;
    }
    const into = foldable ? folds[b.region].body : root;
    const before = into.lastElementChild;
    switch (b.type) {
      case "title":
        into.append(el("h1", null, b.text));
        break;
      case "heading": {
        const lvl = Math.min(4, 1 + (b.level || 1));
        into.append(el(`h${lvl}`, null, b.text));
        break;
      }
      case "paragraph":
        into.append(sentenceSpans(el("p", front ? "front" : null), b.sentences, paper));
        break;
      case "list_item":
        into.append(sentenceSpans(el("p", "li"), b.sentences, paper));
        break;
      case "footnote":
        into.append(sentenceSpans(el("p", "footnote"), b.sentences, paper));
        break;
      case "caption": {
        const p = sentenceSpans(el("p", "caption"), b.sentences, paper);
        p.dataset.block = b.id;
        into.append(p);
        break;
      }
      case "references": {
        const n = (paper.refNumbers[b.id] = Object.keys(paper.refNumbers).length + 1);
        const p = el("p", "ref");
        p.dataset.block = b.id;
        p.append(el("span", "ref-num", `[${n}]`), b.text);
        into.append(p);
        break;
      }
      case "code":
        into.append(el("pre", null, b.text));
        break;
      case "equation": {
        const d = el("div", "eq");
        d.dataset.block = b.id;
        d.append(cropImg(paper, b));
        into.append(d);
        break;
      }
      case "figure":
      case "table": {
        const f = el("figure");
        f.dataset.block = b.id;
        const frame = el("span", "fig-frame"); // the button sits on the image's corner, not the column's
        frame.append(cropImg(paper, b), floatExplainButton(b));
        f.append(frame);
        if (b.caption_sentences?.length) f.append(sentenceSpans(el("figcaption"), b.caption_sentences, paper));
        into.append(f);
        break;
      }
    }
    // Every block is findable by id (outline links, reading progress).
    if (into.lastElementChild !== before) into.lastElementChild.dataset.block = b.id;
  }
}

// ---------- Folded references and appendix ----------
// Both are folded by default so the main text reads as the paper proper; the
// choice is remembered per paper on this device. Anything that jumps into a
// folded section (outline, Ask sources, reading position) unfolds it first.
const FOLD_KEYS = { appendix: "appendix", references: "refs" }; // localStorage prefixes

function foldSection(paper, kind) {
  const section = el("section", `fold fold-${kind}`);
  section.dataset.fold = kind;
  const toggle = el("button", "fold-toggle");
  toggle.type = "button";
  let meta = "";
  if (kind === "appendix") {
    // Count lettered sections ("A", "B"…), not every sub-heading.
    const n = paper.blocks.filter((b) => b.region === "appendix" && b.type === "heading" && headingNumber(b.text)?.length === 1).length;
    if (n) meta = `${n} section${n > 1 ? "s" : ""}`;
  } else {
    const n = paper.blocks.filter((b) => b.type === "references").length;
    if (n) meta = `${n} entr${n > 1 ? "ies" : "y"}`;
  }
  const action = el("span", "fold-action");
  toggle.append(el("span", "fold-label", kind === "appendix" ? "Appendix" : "References"), el("span", "fold-meta", meta), action);
  const body = el("div", "fold-body");
  section.append(toggle, body);
  let open = false;
  try { open = localStorage.getItem(`${FOLD_KEYS[kind]}:${paper.id}`) === "open"; } catch {}
  section.syncToggle = () => {
    const folded = section.classList.contains("folded");
    toggle.setAttribute("aria-expanded", String(!folded));
    action.textContent = folded ? "Show" : "Hide";
  };
  section.classList.toggle("folded", !open);
  section.syncToggle();
  toggle.addEventListener("click", () => {
    const opening = section.classList.contains("folded");
    setFoldOpen(section, opening);
    // Folding from inside the section: land on its bar, not further down the page.
    const top = section.getBoundingClientRect().top;
    if (!opening && top < headerBottom()) window.scrollTo(0, window.scrollY + top - headerBottom() - 8);
  });
  return { section, body };
}

function setFoldOpen(section, open) {
  if (!section || section.classList.contains("folded") === !open) return;
  section.classList.toggle("folded", !open);
  section.syncToggle();
  try { localStorage.setItem(`${FOLD_KEYS[section.dataset.fold]}:${current.id}`, open ? "open" : "folded"); } catch {}
  relayoutNotes();
  updateOutline();
}

// Unfold whatever folded section `node` is in (before scrolling to `node`).
function reveal(node) {
  setFoldOpen(node?.closest(".fold.folded"), true);
}

// The element that stands in for `node` on screen: the folded section's bar
// when `node` is hidden inside it.
function onScreen(node) {
  return node.closest(".fold.folded") || node;
}

// ---------- highlights ----------
// A sentence shows a colour if the user set one, or if the AI chose to keep it
// (every highlight it kept: the model, not a quota, decides how many).
// Candidates the AI dropped in its final pass (tier null: repeats, routine)
// are never shown.
const LEGACY_SHARE = 0.12; // papers labelled by the old one-pass classifier (no tiers)

function highlightable(paper) {
  let n = 0;
  for (const b of paper.blocks) {
    if (b.region !== "body") continue;
    if (["paragraph", "list_item", "caption"].includes(b.type)) n += b.sentences.length;
    else if (b.caption_sentences) n += b.caption_sentences.length;
  }
  return n;
}

function effectiveLabels(paper) {
  const out = {};
  let ai = Object.entries(paper.ai_labels || {});
  if (ai.some(([, l]) => "tier" in l)) {
    ai = ai.filter(([, l]) => l.tier !== null);
  } else {
    // Old labels had no "kept" judgement: show the most confident 12%, as before.
    const position = new Map(Object.keys(paper.sentences).map((sid, i) => [sid, i]));
    ai.sort((a, b) => b[1].confidence - a[1].confidence || position.get(a[0]) - position.get(b[0]));
    ai = ai.slice(0, Math.round(highlightable(paper) * LEGACY_SHARE));
  }
  for (const [sid, l] of ai) out[sid] = l.category;
  for (const [sid, e] of Object.entries(paper.user_edits || {})) {
    if (e.category) out[sid] = e.category;
    else delete out[sid];
  }
  for (const sid of Object.keys(out)) if (settings.hidden.includes(out[sid])) delete out[sid];
  return out;
}

function applyHighlights() {
  const labels = effectiveLabels(current);
  for (const span of $("paper").querySelectorAll(".s")) {
    const cat = labels[span.dataset.sid];
    if (cat) span.dataset.hl = cat;
    else delete span.dataset.hl;
    // The AI's margin note: why it highlighted this sentence.
    const note = cat && current.ai_labels?.[span.dataset.sid]?.note;
    if (note) span.title = `AI note: ${note}`;
    else span.removeAttribute("title");
  }
}

function renderNotice() {
  const n = $("notice");
  const st = current.status || {};
  n.replaceChildren();
  const outdated = st.classified && (st.highlight_version || 1) < (current.highlighter_version || 0);
  if (st.error) n.append(el("span", null, st.error));
  else if (!st.classified) n.append(el("span", null, "This paper has no AI highlights yet."));
  else if (outdated) n.append(el("span", null, "These highlights were made by an older version of the highlighter. Updating re-reads the paper (a few minutes); your own edits and notes stay."));
  else { n.hidden = true; return; }
  const retry = el("button", null, st.error ? "Retry" : outdated ? "Update highlights" : "Highlight now");
  retry.type = "button";
  retry.addEventListener("click", rehighlight);
  n.append(retry);
  n.hidden = false;
}

async function rehighlight() {
  if (!current) return;
  const id = current.id;
  await api(`/api/papers/${id}/highlight`, { method: "POST" });
  if (await waitUntilProcessed(id)) openPaper(id);
}
$("rehighlight").addEventListener("click", rehighlight);

async function refreshAiStatus() {
  try {
    const a = await api("/api/ai");
    $("ai-status").textContent = `Model: ${a.model} (local)${a.ready ? "" : ` — ${a.message}`}`;
  } catch { $("ai-status").textContent = ""; }
}
$("settings-toggle").addEventListener("click", refreshAiStatus);



// Click a sentence to set, change or clear its highlight.
const picker = $("picker");
let pickingSpan = null;

function openPicker(span, x, y) {
  closePicker();
  pickingSpan = span;
  span.classList.add("picking");
  const sid = span.dataset.sid;
  const shown = span.dataset.hl || null;
  const edited = sid in (current.user_edits || {});
  picker.replaceChildren();
  const item = (label, value, swatch) => {
    const b = el("button");
    b.type = "button";
    b.setAttribute("role", "menuitemradio");
    b.setAttribute("aria-checked", String(value === shown));
    if (swatch) b.append(el("span", `swatch ${swatch}`));
    b.append(label);
    b.addEventListener("click", () => setEdit(sid, value));
    picker.append(b);
  };
  const ex = el("button", "picker-explain");
  ex.type = "button";
  ex.setAttribute("role", "menuitem");
  ex.append("Explain this sentence");
  ex.addEventListener("click", () => explainSentence(span));
  const tr = el("button", "picker-explain");
  tr.type = "button";
  tr.setAttribute("role", "menuitem");
  tr.append("Translate this sentence");
  tr.addEventListener("click", () => translateSentence(span));
  const note = el("button", "picker-explain");
  note.type = "button";
  note.setAttribute("role", "menuitem");
  note.append((current.notes || []).some((n) => n.sid === span.dataset.sid) ? "Edit note" : "Add note");
  note.addEventListener("click", () => addNote(span));
  const card = el("button", "picker-explain");
  card.type = "button";
  card.setAttribute("role", "menuitem");
  card.append("Make a flashcard");
  card.addEventListener("click", () => { closePicker(); newCardFrom(sid); });
  const aiNote = span.dataset.hl && current.ai_labels?.[sid]?.note;
  if (aiNote) picker.append(el("p", "picker-note", aiNote));
  picker.append(ex, tr, note, card, el("hr"));
  for (const [cat, label] of CATEGORIES) item(label, cat, cat);
  item("No highlight", null);
  if (edited) {
    picker.append(el("hr"));
    item("Reset to AI's choice", "reset");
  }
  picker.hidden = false;
  // Open at the click point, kept inside the viewport and below the top bar.
  const w = picker.offsetWidth, h = picker.offsetHeight;
  const left = Math.max(12, Math.min(x, window.innerWidth - w - 12));
  const top = y + 14 + h < window.innerHeight ? y + 14 : Math.max(56, y - h - 14);
  picker.style.left = `${window.scrollX + left}px`;
  picker.style.top = `${window.scrollY + top}px`;
  (picker.querySelector('[aria-checked="true"]') || picker.querySelector("button")).focus();
}

function closePicker() {
  picker.hidden = true;
  pickingSpan?.classList.remove("picking");
  pickingSpan = null;
}

async function setEdit(sid, value) {
  closePicker();
  const before = structuredClone(current.user_edits || {});
  // Optimistic update, then save.
  if (value === "reset") delete current.user_edits[sid];
  else current.user_edits[sid] = { category: value };
  applyHighlights();
  try {
    current.user_edits = await api(`/api/papers/${current.id}/edits`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [sid]: value }),
    });
  } catch {
    current.user_edits = before;
    alert("Could not save the highlight. Is the server running?");
  }
  applyHighlights();
}

$("paper").addEventListener("click", (e) => {
  if (e.target.closest(".cite, .xref")) return;
  if (String(window.getSelection())) return; // selecting text, not picking
  const span = e.target.closest(".s");
  if (span) { e.stopPropagation(); pickingSpan === span ? closePicker() : openPicker(span, e.clientX, e.clientY); }
});
document.addEventListener("click", (e) => { if (!e.target.closest("#picker, .s")) closePicker(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePicker(); });

// ---------- Notes ----------
// Note paper lines must be whole screen pixels to render evenly under display
// scaling: line spacing ~24 CSS px rounded to device pixels, thickness 1 device px.
function syncPixelGrid() {
  const dpr = window.devicePixelRatio || 1;
  const root = document.documentElement.style;
  root.setProperty("--note-rule", `${Math.round(24 * dpr) / dpr}px`);
  root.setProperty("--note-hair", `${1 / dpr}px`);
  root.setProperty("--note-inset", `${Math.round(4 * dpr) / dpr}px`);
  root.setProperty("--note-offset", `${Math.round(11 * dpr) / dpr}px`);
  matchMedia(`(resolution: ${dpr}dppx)`).addEventListener("change", () => { syncPixelGrid(); relayoutNotes(); }, { once: true });
}
syncPixelGrid();
const snap = (v) => Math.round(v * (window.devicePixelRatio || 1)) / (window.devicePixelRatio || 1);

// Free-text notes attached to a sentence, stored on the server (so every
// device using it sees them). Shown in the right margin beside their sentence
// when there's room, otherwise as cards under the paragraph.
const notesLayer = el("div", "notes-layer");
document.body.append(notesLayer);
let draft = null; // an unsaved new note: { sid }
let editingId = null;

function noteCard(note) {
  const card = el("div", "note-card");
  card.dataset.id = note.id || "";
  card.dataset.sid = note.sid;
  card.append(el("span", "note-tape a"), el("span", "note-tape b"));
  if (!note.id || note.id === editingId) {
    const ta = el("textarea");
    ta.value = note.text || "";
    ta.placeholder = "Write a note…";
    ta.rows = 3;
    const save = el("button", "note-save", "Save");
    save.type = "button";
    const cancel = el("button", "link", "Cancel");
    cancel.type = "button";
    const bar = el("div", "note-bar");
    bar.append(cancel, save);
    card.append(ta, bar);
    save.addEventListener("click", () => saveNote(note, ta.value));
    cancel.addEventListener("click", () => { editingId = null; draft = null; renderNotes(); });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) saveNote(note, ta.value);
      if (e.key === "Escape") cancel.click();
    });
    setTimeout(() => ta.focus(), 0);
  } else {
    card.append(el("div", "note-text", note.text));
    const bar = el("div", "note-bar");
    const when = new Date((note.updated || note.created) * 1000);
    bar.append(el("span", "note-date", when.toLocaleDateString(undefined, { month: "short", day: "numeric" })));
    const edit = el("button", "link", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => { editingId = note.id; renderNotes(); });
    const del = el("button", "link", "Delete");
    del.type = "button";
    del.addEventListener("click", () => deleteNote(note));
    bar.append(edit, del);
    card.append(bar);
  }
  // Pair the card with its sentence on hover.
  const span = () => $("paper").querySelector(`.s[data-sid="${note.sid}"]`);
  card.addEventListener("mouseenter", () => span()?.classList.add("picking"));
  card.addEventListener("mouseleave", () => span()?.classList.remove("picking"));
  return card;
}

function renderNotes() {
  notesLayer.replaceChildren();
  $("paper").querySelectorAll(".note-card").forEach((c) => c.remove());
  $("paper").querySelectorAll(".s.has-note").forEach((s) => s.classList.remove("has-note"));
  if (!current || $("reader").hidden || $("paper").hidden) return;

  const notes = [...(current.notes || []).filter((n) => n.sid), ...(draft ? [{ ...draft }] : [])];
  const anchored = notes
    .map((n) => ({ n, span: $("paper").querySelector(`.s[data-sid="${n.sid}"]`) }))
    .filter((x) => x.span && !x.span.closest(".fold.folded"));
  for (const { span } of anchored) span.classList.add("has-note");

  const paperBox = $("paper").getBoundingClientRect();
  const side = openPanelName && $(openPanelName); // the open right-hand panel
  const reserved = side ? side.offsetWidth : 0;
  const free = window.innerWidth - reserved - paperBox.right;
  if (free >= 250) {
    // Margin: each card level with its sentence, pushed down to avoid overlaps.
    const width = Math.min(300, free - 64); // leave room for the tape that sticks out on the right
    let bottom = 0;
    anchored.sort((a, b) => a.span.getBoundingClientRect().top - b.span.getBoundingClientRect().top);
    for (const { n, span } of anchored) {
      const card = noteCard(n);
      card.classList.add("in-margin");
      card.style.width = `${width}px`;
      // Whole screen pixels keep the ruled lines crisp.
      card.style.left = `${snap(window.scrollX + paperBox.right + 18)}px`;
      const top = snap(Math.max(window.scrollY + span.getBoundingClientRect().top - 4, bottom + 8));
      card.style.top = `${top}px`;
      notesLayer.append(card);
      bottom = top + card.offsetHeight;
    }
  } else {
    // Inline: after the paragraph (or figure) holding the sentence.
    for (const { n, span } of anchored) {
      const host = span.closest("p, figure, li") || span;
      let after = host;
      while (after.nextElementSibling?.classList.contains("note-card")) after = after.nextElementSibling;
      after.after(noteCard(n));
    }
  }
}

async function saveNote(note, text) {
  text = text.trim();
  if (!text) return;
  try {
    if (note.id) {
      const saved = await api(`/api/papers/${current.id}/notes/${note.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
      });
      current.notes = current.notes.map((n) => (n.id === saved.id ? saved : n));
    } else {
      const saved = await api(`/api/papers/${current.id}/notes`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sid: note.sid, text }),
      });
      (current.notes ||= []).push(saved);
    }
    editingId = null;
    draft = null;
    renderNotes();
  } catch (err) {
    alert(`Could not save the note: ${err.message || err}`);
  }
}

async function deleteNote(note) {
  if (!confirm("Delete this note?")) return;
  await api(`/api/papers/${current.id}/notes/${note.id}`, { method: "DELETE" });
  current.notes = current.notes.filter((n) => n.id !== note.id);
  renderNotes();
}

function addNote(span) {
  closePicker();
  const existing = (current.notes || []).find((n) => n.sid === span.dataset.sid);
  if (existing) { editingId = existing.id; draft = null; }
  else { draft = { sid: span.dataset.sid, text: "" }; editingId = null; }
  renderNotes();
}

// Pick up notes added on another device when this window comes back into focus.
async function refreshNotes() {
  if (!current || editingId || draft || document.hidden) return;
  try {
    const notes = await api(`/api/papers/${current.id}/notes`);
    if (JSON.stringify(notes) !== JSON.stringify(current.notes || [])) { current.notes = notes; renderNotes(); }
  } catch {}
}
window.addEventListener("focus", refreshNotes);
document.addEventListener("visibilitychange", refreshNotes);

// Keep margin notes aligned when the layout moves (images loading, settings, resizing).
let notesTimer;
function relayoutNotes() { clearTimeout(notesTimer); notesTimer = setTimeout(renderNotes, 120); }
window.addEventListener("resize", relayoutNotes);
new ResizeObserver(relayoutNotes).observe($("paper"));

// ---------- Export ----------
// Exports exactly what's on screen: the AI's highlights, visible categories
// and edits decide which sentences are highlighted in the PDF.
$("export").addEventListener("click", async (e) => {
  if (!current) return;
  const btn = e.currentTarget;
  const label = "Export PDF with highlights";
  btn.disabled = true;
  btn.textContent = "Exporting…";
  try {
    const r = await download(await fetch(`/api/papers/${current.id}/export`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ labels: effectiveLabels(current) }),
    }), "highlighted.pdf");
    const skipped = Number(r.headers.get("X-Skipped") || 0);
    const notes = Number(r.headers.get("X-Notes") || 0);
    btn.textContent = `Exported ${r.headers.get("X-Highlights")} highlights` + (notes ? `, ${notes} notes` : "") + (skipped ? ` (${skipped} not placeable)` : "");
  } catch (err) {
    alert(`Export failed: ${err.message || err}`);
  }
  setTimeout(() => { btn.textContent = label; btn.disabled = false; closeMenu(); }, 2500);
});

// ---------- Reading position ----------
// While reading, the sentence at the top of the screen (and how far it sits
// below the header) is saved to the server, so reopening or refreshing the
// paper, on this device or the laptop, returns to it. Sentences rather than
// pixels, because font size, column width and screen size change the layout.
let posTimer = null, restoring = null;

function headerBottom() {
  return document.querySelector(".bar").getBoundingClientRect().bottom;
}

function topSentence() {
  const spans = $("paper").querySelectorAll(".s:not(.fold.folded .s)");
  const top = headerBottom();
  let lo = 0, hi = spans.length - 1, found = null;
  while (lo <= hi) { // first sentence whose bottom is below the header
    const mid = (lo + hi) >> 1;
    if (spans[mid].getBoundingClientRect().bottom > top) { found = spans[mid]; hi = mid - 1; } else lo = mid + 1;
  }
  return found;
}

// Progress counts the main text only: it ends where the references or the
// appendix begin (whichever comes first), so reading up to there is 100%.
function mainEnd() {
  const i = current.blocks.findIndex((b) => b.region === "references" || b.region === "appendix");
  for (const b of i < 0 ? [] : current.blocks.slice(i)) {
    const node = $("paper").querySelector(`[data-block="${b.id}"]`);
    if (node) return window.scrollY + onScreen(node).getBoundingClientRect().top;
  }
  return document.documentElement.scrollHeight;
}

function mainProgress() {
  const max = mainEnd() - window.innerHeight; // the end of the main text reaches the bottom of the screen
  return max > 0 ? Math.max(0, Math.min(100, (100 * window.scrollY) / max)) : 100;
}

function readingPosition() {
  const span = topSentence();
  if (!span) return null;
  return {
    sid: span.dataset.sid,
    offset: span.getBoundingClientRect().top - headerBottom(),
    percent: mainProgress(),
    updated: Date.now() / 1000,
  };
}

function savePosition(keepalive = false) {
  if (!current || $("reader").hidden || $("paper").hidden || restoring) return;
  const pos = readingPosition();
  if (!pos) return;
  try { localStorage.setItem(`pos:${current.id}`, JSON.stringify(pos)); } catch {}
  fetch(`/api/papers/${current.id}/position`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pos), keepalive,
  }).catch(() => {});
}

window.addEventListener("scroll", () => {
  clearTimeout(posTimer);
  posTimer = setTimeout(savePosition, 800);
}, { passive: true });
document.addEventListener("visibilitychange", () => { if (document.hidden) savePosition(true); });
window.addEventListener("pagehide", () => savePosition(true));

function scrollToPosition(pos) {
  const span = $("paper").querySelector(`.s[data-sid="${pos.sid}"]`);
  if (!span) return false;
  reveal(span);
  window.scrollTo(0, window.scrollY + span.getBoundingClientRect().top - headerBottom() - pos.offset);
  return true;
}

async function restorePosition(id) {
  clearTimeout(restoring);
  restoring = -1; // don't save the scroll-to-top of opening as the new position
  let local = null;
  try { local = JSON.parse(localStorage.getItem(`pos:${id}`) || "null"); } catch {}
  const server = await api(`/api/papers/${id}/position`).catch(() => null);
  const pos = [server, local].filter((p) => p && p.sid).sort((a, b) => b.updated - a.updated)[0];
  if (current?.id !== id) return;
  if (!pos || pos.percent < 1 || !scrollToPosition(pos)) { restoring = null; return; }
  // Figures load after this and push the text down; keep the sentence in place
  // until they have, unless the reader starts scrolling first.
  const stop = () => {
    clearTimeout(restoring);
    restoring = null;
    for (const [ev, fn] of listeners) window.removeEventListener(ev, fn, true);
  };
  const reanchor = () => { if (restoring) scrollToPosition(pos); };
  const listeners = [["wheel", stop], ["touchstart", stop], ["keydown", stop], ["mousedown", stop], ["load", reanchor]];
  for (const [ev, fn] of listeners) window.addEventListener(ev, fn, true); // capture: <img> load doesn't bubble
  restoring = setTimeout(stop, 4000);
  showResumeToast(pos);
}

function showResumeToast(pos) {
  document.querySelector(".resume-toast")?.remove();
  const toast = el("div", "resume-toast");
  toast.setAttribute("role", "status");
  const when = pos.updated ? new Date(pos.updated * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
  toast.append(el("span", null, `Resumed where you left off${when ? ` (${when})` : ""}`));
  const top = el("button", "link", "Back to start");
  top.type = "button";
  top.addEventListener("click", () => { window.scrollTo({ top: 0, behavior: "smooth" }); toast.remove(); });
  toast.append(top);
  document.body.append(toast);
  setTimeout(() => toast.classList.add("fade"), 6000);
  setTimeout(() => toast.remove(), 6600);
}

// ---------- Outline ----------
// A foldable sidebar listing the paper's sections. Docling gives every
// heading the same level, so nesting comes from the numbering ("3", "3.2",
// "3.2.1", "A", "A.1"). The appendix is its own group, folded by default.
// The section being read is marked as you scroll.
const TOP_LEVEL = /^(abstract|introduction|background|related work|method(s|ology)?|experiments?|results?|discussion|conclusions?|limitations?|acknowledge?ments?|references|bibliography|appendix|appendices|broader impact|ethics statement|reproducibility statement|data availability|keywords|ccs concepts)\b/i;
let outlineItems = []; // [{block, el (heading in the paper), li}]

function headingNumber(text) {
  // "3", "3.2.1", "A", "A.1", "B.3.2" (+ optional trailing dot) before the title.
  const m = text.match(/^((?:\d{1,2}|[A-H])(?:\.\d{1,2})*)\.?\s+\S/);
  if (!m) return null;
  if (/^[A-H]$/.test(m[1]) && !/^[A-H]\.?\s+[A-Z]/.test(text)) return null; // "A system…" isn't numbered
  return m[1].split(".");
}

function outlineTree(paper) {
  const heads = paper.blocks.filter((b) =>
    b.type === "heading" && b.region !== "front" && !/�/.test(b.text) && b.text.trim().length > 1);
  // Numbering that restarts in each section ("INTRODUCTION", "1.", "2.", "METHODS", "1.")
  // means the numbered headings are subsections of unnumbered ones.
  const firsts = heads.map((b) => headingNumber(b.text)).filter((n) => n && n.length === 1).map((n) => n[0]);
  const restarts = new Set(firsts).size < firsts.length;
  let lastDepth = 1;
  const flat = heads.map((b) => {
    const num = headingNumber(b.text);
    let depth;
    if (num) depth = num.length + (restarts ? 1 : 0);
    else if (TOP_LEVEL.test(b.text.trim()) || b.region === "references") depth = 1;
    else depth = Math.min(lastDepth + 1, 4); // a run-in or unnumbered sub-heading
    if (num) lastDepth = depth;
    else if (depth === 1) lastDepth = 1;
    return { block: b, depth, children: [] };
  });
  // References without a heading of their own still get an entry.
  const firstRef = paper.blocks.find((b) => b.type === "references");
  if (firstRef && !flat.some((n) => n.block.region === "references")) {
    const i = paper.blocks.indexOf(firstRef);
    const at = flat.findIndex((n) => paper.blocks.indexOf(n.block) > i);
    flat.splice(at < 0 ? flat.length : at, 0, { block: firstRef, depth: 1, children: [], label: "References" });
  }
  // Nest: each node goes under the nearest earlier node with a smaller depth.
  const root = { children: [] };
  const stack = [{ depth: 0, node: root }];
  let appendixGroup = null;
  for (const n of flat) {
    if (n.block.region === "appendix" && !appendixGroup) {
      // One group for the whole appendix; an "Appendix" heading becomes its label.
      const isLabel = /^appendi(x|ces)\b/i.test(n.block.text.trim());
      appendixGroup = { block: n.block, depth: 0.5, children: [], appendix: true, label: isLabel ? n.block.text : "Appendix" };
      root.children.push(appendixGroup);
      stack.length = 1;
      stack.push({ depth: 0.5, node: appendixGroup });
      if (isLabel) continue;
    }
    while (stack.length > 1 && stack[stack.length - 1].depth >= n.depth) stack.pop();
    const siblings = stack[stack.length - 1].node.children;
    // Headings inside worked examples ("Setting", "Issue", "### Steps…") repeat; list each once.
    if (!headingNumber(n.block.text) && (/^#/.test(n.block.text) || siblings.some((x) => x.block.text === n.block.text))) continue;
    siblings.push(n);
    stack.push({ depth: n.depth, node: n });
  }
  return root.children;
}

function buildOutline(paper) {
  const list = $("outline-list");
  list.replaceChildren();
  outlineItems = [];
  const paperEl = $("paper");
  const add = (nodes, folded) => {
    const ul = el("ul");
    for (const n of nodes) {
      const li = el("li");
      const row = el("div", "ol-row");
      if (n.children.length) {
        const tw = el("button", "ol-twisty");
        tw.type = "button";
        tw.setAttribute("aria-label", "Show or hide subsections");
        tw.addEventListener("click", () => {
          li.classList.toggle("folded");
          tw.setAttribute("aria-expanded", String(!li.classList.contains("folded")));
        });
        row.append(tw);
        if (folded || n.appendix) li.classList.add("folded");
        tw.setAttribute("aria-expanded", String(!li.classList.contains("folded")));
      } else row.append(el("span", "ol-twisty-space"));
      const a = el("a", n.appendix ? "ol-link ol-appendix" : "ol-link", n.label || n.block.text);
      a.href = "#";
      a.title = n.label || n.block.text;
      const target = paperEl.querySelector(`[data-block="${n.block.id}"]`);
      a.addEventListener("click", (e) => {
        e.preventDefault();
        if (!target) return;
        if ($("paper").hidden) $("toggle-original").click();
        reveal(target);
        window.scrollTo({ top: window.scrollY + target.getBoundingClientRect().top - headerBottom() - 12, behavior: "smooth" });
        if (window.innerWidth < 1100) setOutlineOpen(false, false); // overlay: get out of the way, keep the preference
      });
      row.append(a);
      li.append(row);
      if (target) outlineItems.push({ el: target, li });
      if (n.children.length) li.append(add(n.children, folded || n.appendix));
      ul.append(li);
    }
    return ul;
  };
  list.append(add(outlineTree(paper), false));
  $("outline-empty").hidden = outlineItems.length > 0;
  updateOutline();
}

// Mark the section being read (the last heading above the top of the screen),
// or the nearest visible entry when it sits inside a folded group.
function updateOutline() {
  if ($("outline").hidden || !outlineItems.length) return;
  const line = headerBottom() + 80;
  let active = null;
  for (const it of outlineItems) {
    if (onScreen(it.el).getBoundingClientRect().top <= line) active = it;
    else break;
  }
  $("outline-list").querySelectorAll(".active").forEach((x) => x.classList.remove("active"));
  if (active) {
    let li = active.li;
    for (let p = li.parentElement.closest("li"); p; p = p.parentElement.closest("li")) if (p.classList.contains("folded")) li = p;
    const row = li.querySelector(":scope > .ol-row");
    row.classList.add("active");
    const box = $("outline-list").getBoundingClientRect(), r = row.getBoundingClientRect();
    if (r.top < box.top || r.bottom > box.bottom) row.scrollIntoView({ block: "nearest" });
  }
  const pct = mainProgress();
  $("outline-progress").style.width = `${pct}%`;
}
let outlineFrame = 0;
window.addEventListener("scroll", () => {
  cancelAnimationFrame(outlineFrame);
  outlineFrame = requestAnimationFrame(updateOutline);
}, { passive: true });

function setOutlineOpen(open, remember = true) {
  $("outline").hidden = !open;
  document.body.classList.toggle("outline-open", open);
  $("outline-toggle").setAttribute("aria-expanded", String(open));
  if (remember) updateSettings({ outline: open });
  if (open) { updateOutline(); updateFloats(); }
  relayoutNotes();
}
// ---- Figures & tables tab: every figure and table with a thumbnail, its
// label and caption; click to jump. Appendix ones are grouped and folded, as
// in the Sections tab.
const FLOAT_LABEL = /^\s*(Figure|Fig\.?|Table|Tab\.?|Listing)\s*([A-Z]?\d+)/i;
let floatItems = []; // [{el (the figure in the paper), row}]

function floatCaption(paper, b, byId) {
  const ids = b.caption_sentences?.length ? b.caption_sentences : byId[b.caption_block]?.sentences || [];
  return ids.map((sid) => paper.sentences[sid]?.text || "").join(" ").trim();
}

function buildFloats(paper) {
  const box = $("outline-floats");
  box.replaceChildren();
  floatItems = [];
  const byId = Object.fromEntries(paper.blocks.map((b) => [b.id, b]));
  const groups = { figure: [], table: [], listing: [], appendix: [], other: [] };
  for (const b of paper.blocks) {
    if ((b.type !== "figure" && b.type !== "table") || !b.image) continue;
    const caption = floatCaption(paper, b, byId);
    const m = caption.match(FLOAT_LABEL);
    const kind = m ? (/^t/i.test(m[1]) ? "table" : /^l/i.test(m[1]) ? "listing" : "figure") : b.type;
    const item = {
      b,
      label: m ? `${{ table: "Table", listing: "Listing" }[kind] || "Figure"} ${m[2]}` : null,
      caption: m ? caption.slice(m[0].length).replace(/^[\s.:|—–-]+/, "") : caption,
    };
    if (!m) groups.other.push(item);
    else if (b.region === "appendix") groups.appendix.push(item);
    else groups[kind].push(item);
  }
  const paperEl = $("paper");
  const row = (it) => {
    const a = el("a", "fl-item");
    a.href = "#";
    const img = el("img", "fl-thumb");
    img.src = `/api/papers/${paper.id}/${it.b.image}`;
    img.alt = "";
    img.loading = "lazy";
    const text = el("span", "fl-text");
    text.append(el("strong", null, it.label || (it.b.type === "table" ? "Table" : "Figure")));
    if (it.caption) text.append(el("span", "fl-cap", it.caption));
    a.title = it.caption || "";
    a.append(img, text);
    const target = paperEl.querySelector(`[data-block="${it.b.id}"]`);
    a.addEventListener("click", (e) => {
      e.preventDefault();
      if (!target) return;
      if ($("paper").hidden) $("toggle-original").click();
      reveal(target);
      window.scrollTo({ top: window.scrollY + target.getBoundingClientRect().top - headerBottom() - 12, behavior: "smooth" });
      if (window.innerWidth < 1100) setOutlineOpen(false, false);
    });
    if (target) floatItems.push({ el: target, row: a });
    return a;
  };
  const group = (title, items, folded) => {
    if (!items.length) return;
    const sec = el("section", folded ? "fl-group folded" : "fl-group");
    const head = el("button", "fl-head");
    head.type = "button";
    head.append(el("span", null, title), el("span", "fl-count", String(items.length)));
    head.setAttribute("aria-expanded", String(!folded));
    head.addEventListener("click", () => {
      sec.classList.toggle("folded");
      head.setAttribute("aria-expanded", String(!sec.classList.contains("folded")));
      updateFloats();
    });
    const list = el("div", "fl-items");
    for (const it of items) list.append(row(it));
    sec.append(head, list);
    box.append(sec);
  };
  group("Figures", groups.figure, false);
  group("Tables", groups.table, false);
  group("Listings", groups.listing, false);
  group("Appendix", groups.appendix, true);
  group("Without a caption", groups.other, true);
  if (!box.children.length) box.append(el("p", "outline-empty", "No figures or tables found."));
}

// Mark the figure or table nearest the middle of the screen.
function updateFloats() {
  if ($("outline").hidden || $("outline-floats").hidden || !floatItems.length) return;
  const mid = window.innerHeight / 2;
  let best = null, dist = Infinity;
  for (const it of floatItems) {
    if (onScreen(it.el) !== it.el) continue; // inside a folded section
    const r = it.el.getBoundingClientRect();
    if (r.bottom < headerBottom() || r.top > window.innerHeight) continue;
    const d = Math.abs((r.top + r.bottom) / 2 - mid);
    if (d < dist) { dist = d; best = it; }
  }
  $("outline-floats").querySelectorAll(".fl-item.active").forEach((x) => x.classList.remove("active"));
  if (best && !best.row.closest(".fl-group.folded")) {
    best.row.classList.add("active");
    const box = $("outline-floats").getBoundingClientRect(), r = best.row.getBoundingClientRect();
    if (r.top < box.top || r.bottom > box.bottom) best.row.scrollIntoView({ block: "nearest" });
  }
}
let floatsFrame = 0;
window.addEventListener("scroll", () => {
  cancelAnimationFrame(floatsFrame);
  floatsFrame = requestAnimationFrame(updateFloats);
}, { passive: true });

function setOutlineTab(tab, remember = true) {
  const floats = tab === "floats";
  $("outline-list").hidden = floats;
  $("outline-empty").hidden = floats || outlineItems.length > 0;
  $("outline-floats").hidden = !floats;
  $("tab-sections").setAttribute("aria-selected", String(!floats));
  $("tab-floats").setAttribute("aria-selected", String(floats));
  if (remember) updateSettings({ outlineTab: tab });
  if (floats) updateFloats(); else updateOutline();
}
$("tab-sections").addEventListener("click", () => setOutlineTab("sections"));
$("tab-floats").addEventListener("click", () => setOutlineTab("floats"));

$("outline-toggle").addEventListener("click", () => setOutlineOpen($("outline").hidden));
$("outline-close").addEventListener("click", () => setOutlineOpen(false));

// ---------- Explain and translate ----------
// Hovering a sentence shows two small buttons at its end: a lightbulb
// (explain) and a translate button. Selecting any text (a word, part of a
// sentence, a paragraph) shows a translate button under the selection. The
// sentence menu (click) has both actions for touch screens. Results stream
// into one popover and are cached on the server.
const ICON_BULB = `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
  <circle class="glow" cx="12" cy="10.2" r="3.2"/>
  <path d="M9.2 16.6c0-2-3-3.3-3-6.4a5.8 5.8 0 0 1 11.6 0c0 3.1-3 4.4-3 6.4z M9.5 18.9h5 M10.3 21h3.4"
        fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;
const ICON_TRANSLATE = `<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path fill="currentColor" d="M12.87 15.07l-2.54-2.51.03-.03c1.74-1.94 2.98-4.17 3.71-6.53H17V4h-7V2H8v2H1v1.99h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg>`;

function iconButton(cls, label, svg) {
  const b = el("button", cls);
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = svg;
  return b;
}

const hoverTools = el("div", "hover-tools");
const explainBtn = iconButton("explain-btn", "Explain this sentence", ICON_BULB);
const translateBtn = iconButton("explain-btn translate-btn", "Translate this sentence", ICON_TRANSLATE);
hoverTools.append(explainBtn, translateBtn);
hoverTools.hidden = true;
document.body.append(hoverTools);
const selTools = el("div", "hover-tools sel-tools");
const selTranslateBtn = iconButton("explain-btn translate-btn", "Translate selection", ICON_TRANSLATE);
selTools.append(selTranslateBtn);
selTools.hidden = true;
document.body.append(selTools);
const explainPop = el("div", "explain-pop");
explainPop.hidden = true;
document.body.append(explainPop);
let hoverSpan = null, hoverTimer = null, hideTimer = null;

// Place a popover under the last of the given client rects (a sentence's or a selection's lines).
function placeBelow(node, rects, width) {
  const last = rects[rects.length - 1];
  const w = Math.min(width, window.innerWidth - 24);
  node.style.width = `${w}px`;
  node.style.left = `${window.scrollX + Math.max(12, Math.min(last.left, window.innerWidth - w - 12))}px`;
  node.style.top = `${window.scrollY + last.bottom + 6}px`;
}

function hideHoverTools() {
  hoverTools.hidden = true;
  hoverSpan = null;
}

function showHoverTools(span) {
  const rects = span.getClientRects();
  const last = rects[rects.length - 1];
  if (!last || String(window.getSelection())) return;
  hoverSpan = span;
  hoverTools.hidden = false;
  const w = hoverTools.offsetWidth;
  const x = last.right + 6 + w < window.innerWidth - 8 ? last.right + 6 : last.right - w;
  hoverTools.style.left = `${window.scrollX + x}px`;
  hoverTools.style.top = `${window.scrollY + last.top + (last.height - hoverTools.offsetHeight) / 2}px`;
}

$("paper").addEventListener("mouseover", (e) => {
  const span = e.target.closest?.(".s");
  if (!span || span === hoverSpan || !picker.hidden) return;
  clearTimeout(hoverTimer);
  clearTimeout(hideTimer);
  // A short delay so the buttons don't flicker while the mouse passes over text.
  hoverTimer = setTimeout(() => showHoverTools(span), 350);
});
$("paper").addEventListener("mouseleave", () => {
  clearTimeout(hoverTimer);
  hideTimer = setTimeout(hideHoverTools, 400);
});
hoverTools.addEventListener("mouseenter", () => clearTimeout(hideTimer));
hoverTools.addEventListener("mouseleave", () => { hideTimer = setTimeout(hideHoverTools, 400); });
explainBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (hoverSpan) explainSentence(hoverSpan);
});
translateBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (hoverSpan) translateSentence(hoverSpan);
});
window.addEventListener("scroll", hideHoverTools, { passive: true });

// ---- Selection: a translate button under any selected text in the paper.
let selection = null; // {text, context, rects} captured when the button appears

function selectionInPaper() {
  const sel = window.getSelection();
  if (!sel.rangeCount || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const paper = $("paper");
  if (paper.hidden || !paper.contains(range.commonAncestorContainer)) return null;
  const node = range.commonAncestorContainer;
  if ((node.nodeType === 1 ? node : node.parentElement).closest("textarea, .note")) return null;
  const text = sel.toString().replace(/\s+/g, " ").trim();
  if (!text) return null;
  const rects = [...range.getClientRects()].filter((r) => r.width > 0);
  if (!rects.length) return null;
  // The sentences the selection touches, so a single word is translated in context.
  const sentences = [...paper.querySelectorAll(".s")].filter((s) => range.intersectsNode(s));
  const context = sentences.slice(0, 3).map((s) => current.sentences[s.dataset.sid]?.text || s.textContent).join(" ");
  return { text, context, rects };
}

function updateSelTools() {
  selection = selectionInPaper();
  if (!selection) { selTools.hidden = true; return; }
  hideHoverTools();
  const last = selection.rects[selection.rects.length - 1];
  selTools.hidden = false;
  const w = selTools.offsetWidth;
  const x = Math.max(8, Math.min(last.right - w / 2, window.innerWidth - w - 8));
  selTools.style.left = `${window.scrollX + x}px`;
  selTools.style.top = `${window.scrollY + last.bottom + 6}px`;
}

let pointerDown = false, selTimer = null;
document.addEventListener("pointerdown", (e) => { if (!e.target.closest?.(".sel-tools")) pointerDown = true; });
document.addEventListener("pointerup", (e) => {
  pointerDown = false;
  if (e.target.closest?.(".sel-tools")) return;
  clearTimeout(selTimer);
  selTimer = setTimeout(updateSelTools, 10);
});
// Keyboard and touch selections (handles on phones) don't end with a pointerup here.
document.addEventListener("selectionchange", () => {
  clearTimeout(selTimer);
  if (window.getSelection().isCollapsed) { selTools.hidden = true; return; }
  if (!pointerDown) selTimer = setTimeout(updateSelTools, 300);
});
// Keep the selection when the button is pressed.
selTranslateBtn.addEventListener("mousedown", (e) => e.preventDefault());
selTranslateBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (!selection) return;
  selTools.hidden = true;
  translateText(selection.text, selection.context, selection.rects);
});

// ---- The popover
let popRun = 0; // guards against a stale stream writing into a newer popover

function openPopover(title, quoteText, rects, sid, extraHead) {
  closePicker();
  hideHoverTools();
  closePopover();
  const run = ++popRun;
  const quote = el("p", "explain-quote", quoteText);
  const body = el("div", "explain-body");
  const close = el("button", "explain-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", closePopover);
  const head = el("div", "explain-head");
  const right = el("span", "explain-tools");
  if (extraHead) right.append(extraHead);
  right.append(close);
  head.append(el("strong", null, title), right);
  explainPop.replaceChildren(head, quote, body);
  explainPop.hidden = false;
  placeBelow(explainPop, rects, 460);
  explainPop.dataset.sid = sid || "";
  if (sid) $("paper").querySelector(`.s[data-sid="${sid}"]`)?.classList.add("picking");
  return { run, body };
}

async function streamInto(pop, pending, url, payload) {
  const { run, body } = pop;
  body.replaceChildren(el("p", "pending", pending));
  let text = "";
  try {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done || run !== popRun) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
        if (ev.type === "token") text += ev.text;
        else if (ev.type === "error") throw new Error(ev.message);
      }
      if (text) body.replaceChildren(...text.trim().split(/\n\s*\n/).map((t) => el("p", null, t)));
    }
  } catch (err) {
    if (run === popRun) body.replaceChildren(el("p", "error", String(err.message || err)));
  }
}

// A lightbulb in the corner of each figure and table, shown on hover: the
// local model reads the image itself, with its caption and the passages that
// mention it.
function floatExplainButton(b) {
  const kind = b.type === "table" ? "table" : "figure";
  const btn = iconButton("explain-btn float-explain", `Explain this ${kind}`, ICON_BULB);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    explainFloat(b, btn);
  });
  return btn;
}

function explainFloat(b, btn) {
  const byId = Object.fromEntries(current.blocks.map((x) => [x.id, x]));
  const caption = floatCaption(current, b, byId) || (b.type === "table" ? "Table" : "Figure");
  const pop = openPopover("Explanation", caption, [btn.getBoundingClientRect()]);
  streamInto(pop, `Looking at the ${b.type === "table" ? "table" : "figure"}…`, `/api/papers/${current.id}/explain-float`, { block: b.id });
}

function explainSentence(span) {
  const sid = span.dataset.sid;
  const pop = openPopover("Explanation", current.sentences[sid].text, span.getClientRects(), sid);
  streamInto(pop, "Explaining…", `/api/papers/${current.id}/explain`, { sid });
}

function translateSentence(span) {
  const sid = span.dataset.sid;
  const text = current.sentences[sid].text;
  translateText(text, text, span.getClientRects(), sid);
}

// The language menu in the popover's header re-translates on change and
// becomes the default (the same setting as in the Aa panel).
function translateText(text, context, rects, sid) {
  const lang = el("select", "explain-lang");
  lang.setAttribute("aria-label", "Translate to");
  for (const [v, label] of LANGS) lang.append(new Option(label, v));
  lang.value = settings.lang;
  const pop = openPopover("Translation", text, rects, sid, lang);
  const go = () => streamInto(pop, "Translating…", `/api/papers/${current.id}/translate`, { text, context, lang: lang.value });
  lang.addEventListener("change", () => { updateSettings({ lang: lang.value }); pop.run = ++popRun; go(); });
  go();
}

function closePopover() {
  popRun++;
  explainPop.hidden = true;
  const sid = explainPop.dataset.sid;
  if (sid) $("paper").querySelector(`.s[data-sid="${sid}"]`)?.classList.remove("picking");
  explainPop.dataset.sid = "";
}
document.addEventListener("click", (e) => {
  if (!explainPop.hidden && !e.target.closest(".explain-pop, .hover-tools, .float-explain, #picker")) closePopover();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!explainPop.hidden) closePopover();
  selTools.hidden = true;
});

// ---------- Right-hand panels ----------
// Summary, Ask, Questions and Cards share the right side: one open at a time.
const PANELS = { spanel: "summary-toggle", chat: "ask-toggle", qpanel: "questions-toggle", cpanel: "cards-toggle" };
const PANEL_OPEN = {
  spanel: () => loadSummary(),
  chat: () => { renderChat(); $("chat-input").focus(); },
  qpanel: () => loadQuestions(),
  cpanel: () => loadCards(),
};
let openPanelName = null;

function setPanel(name) {
  openPanelName = name;
  for (const [id, btn] of Object.entries(PANELS)) {
    $(id).hidden = id !== name;
    $(btn).setAttribute("aria-expanded", String(id === name));
  }
  document.body.classList.toggle("chat-open", !!name);
  for (const t of Object.values(panelTimers)) clearTimeout(t);
  relayoutNotes();
  if (name) PANEL_OPEN[name]();
}
const panelTimers = {}; // polling timers of background jobs shown in panels
for (const [id, btn] of Object.entries(PANELS)) {
  $(btn).addEventListener("click", () => setPanel(openPanelName === id ? null : id));
}
for (const id of ["spanel", "qpanel", "cpanel"]) $(`${id}-close`).addEventListener("click", () => setPanel(null));

// ---------- Ask (Q&A) ----------
// Answers stream in from the local model. [n] citations become chips that
// jump to the passage in the paper; the sources list sits under each answer.
function setChatOpen(open) {
  if (open) setPanel("chat");
  else if (openPanelName === "chat") setPanel(null);
}
$("chat-close").addEventListener("click", () => setChatOpen(false));

function jumpToSentence(sid) {
  if (!$("qview").hidden && current) { // the questions window: jump in the reader window
    toReader({ type: "jump", paper: current.id, sid }, `#/paper/${current.id}${sid ? `/s/${sid}` : ""}`);
    return;
  }
  const span = sid && $("paper").querySelector(`.s[data-sid="${sid}"]`);
  if (!span) return;
  if ($("paper").hidden) $("toggle-original").click(); // leave the original-PDF view
  reveal(span);
  span.scrollIntoView({ behavior: "smooth", block: "center" });
  span.classList.add("flash");
  setTimeout(() => span.classList.remove("flash"), 1600);
}

// Minimal formatting: paragraphs, "- " bullets, **bold**, [n] chips that jump
// to a passage, [S1] chips that open an outside source (paper or web page).
function formatAnswer(text, sources, web = []) {
  const byN = Object.fromEntries(sources.map((s) => [s.n, s]));
  const byS = Object.fromEntries(web.map((s) => [s.n, s]));
  const inline = (line, parent) => {
    const re = /\*\*(.+?)\*\*|\[(S?\d+(?:\s*[,;]\s*S?\d+)*)\]/g;
    let pos = 0, m;
    while ((m = re.exec(line))) {
      parent.append(line.slice(pos, m.index));
      if (m[1]) parent.append(el("strong", null, m[1]));
      else {
        for (const n of m[2].split(/\s*[,;]\s*/)) {
          if (n.startsWith("S")) {
            const ext = byS[n];
            if (!ext) { if (parent.lastChild?.nodeType === 3) parent.lastChild.textContent = parent.lastChild.textContent.replace(/\s+$/, ""); continue; } // an invented source: show nothing
            const link = el("a", `chip chip-web chip-${ext.kind}`, n);
            link.href = ext.url;
            link.target = "_blank";
            link.rel = "noopener";
            link.title = ext.title;
            parent.append(link);
            continue;
          }
          const src = byN[n];
          if (!src) { parent.append(`[${n}]`); continue; }
          const chip = el("button", "chip", n);
          chip.type = "button";
          chip.title = `${src.heading || "Passage"}: ${src.text}`;
          chip.addEventListener("click", () => jumpToSentence(src.sid));
          parent.append(chip);
        }
      }
      pos = m.index + m[0].length;
    }
    parent.append(line.slice(pos));
    return parent;
  };
  // Longer explanations also use "## headings", "1." lists and ``` code
  // blocks (formulas); `code` spans are kept as plain text.
  const frag = document.createDocumentFragment();
  let list = null, code = null;
  for (const raw of text.split("\n")) {
    if (/^\s*```/.test(raw)) {
      if (code) { code = null; } else { code = el("pre", "chat-code"); frag.append(code); list = null; }
      continue;
    }
    if (code) { code.append((code.textContent ? "\n" : "") + raw); continue; }
    const line = raw.trim().replace(/`([^`]+)`/g, "$1");
    if (!line) { list = null; continue; }
    const heading = line.match(/^#{1,4}\s+(.*)/);
    const bullet = line.match(/^[-*•]\s+(.*)/);
    const numbered = line.match(/^\d+[.)]\s+(.*)/);
    if (heading) {
      list = null;
      frag.append(inline(heading[1].replace(/\*\*/g, ""), el("h4", "chat-h")));
    } else if (bullet || numbered) {
      const tag = bullet ? "UL" : "OL";
      if (!list || list.tagName !== tag) { list = el(tag.toLowerCase()); frag.append(list); }
      list.append(inline((bullet || numbered)[1], el("li")));
    } else {
      list = null;
      frag.append(inline(line, el("p")));
    }
  }
  return frag;
}

function sourcesList(sources) {
  const d = el("details", "chat-src");
  d.append(el("summary", null, `Sources (${sources.length})`));
  const ol = el("ol");
  for (const s of sources) {
    const li = el("li", null, `${s.heading ? s.heading + ": " : ""}${s.text}${s.text.length >= 240 ? "…" : ""}`);
    li.value = s.n;
    li.addEventListener("click", () => jumpToSentence(s.sid));
    ol.append(li);
  }
  d.append(ol);
  return d;
}

// What the model looked up, e.g. "Searching papers: “…”".
function stepsList(steps, working = false) {
  const ul = el("ul", working ? "chat-steps working" : "chat-steps");
  for (const s of steps) ul.append(el("li", null, s));
  return ul;
}

// Outside sources the answer can cite as [S1], [S2]…
function webList(web) {
  const d = el("details", "chat-src chat-web");
  d.append(el("summary", null, `Outside sources (${web.length})`));
  const ol = el("ol");
  for (const s of web) {
    const li = el("li");
    const a = el("a", null, s.title || s.url);
    a.href = s.url;
    a.target = "_blank";
    a.rel = "noopener";
    li.append(el("span", "src-n", s.n), " ", a, el("span", "src-kind", s.kind === "web" ? " · web" : " · paper"));
    ol.append(li);
  }
  d.append(ol);
  return d;
}

function renderChat() {
  const log = $("chat-log");
  log.replaceChildren();
  const chat = current?.chat || [];
  if (!chat.length) {
    log.append(el("p", "chat-empty", "Ask anything about this paper: its method, results, limitations, a term you don't know. Answers come from the local model and cite the passages they use. It can also look things up: related or later work, what a cited paper did, or a concept explained in depth."));
  }
  for (const turn of chat) {
    log.append(el("div", "chat-q", turn.q));
    const a = el("div", "chat-a");
    if (turn.steps?.length) a.append(stepsList(turn.steps));
    a.append(formatAnswer(turn.a, turn.sources || [], turn.web || []));
    if (turn.web?.length) a.append(webList(turn.web));
    if (turn.sources?.length) a.append(sourcesList(turn.sources));
    log.append(a);
  }
  log.scrollTop = log.scrollHeight;
}

let asking = false;
async function ask(question) {
  if (asking || !question.trim() || !current) return;
  asking = true;
  $("chat-send").disabled = true;
  const id = current.id;
  const log = $("chat-log");
  log.querySelector(".chat-empty")?.remove();
  log.append(el("div", "chat-q", question));
  const a = el("div", "chat-a");
  a.append(el("p", "pending", "Reading the paper…"));
  log.append(a);
  log.scrollTop = log.scrollHeight;

  let sources = [], web = [], steps = [], text = "", failed = null;
  try {
    const r = await fetch(`/api/papers/${id}/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, search: $("chat-search").getAttribute("aria-pressed") === "true" }),
    });
    if (!r.ok) throw new Error(await r.text());
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
        if (ev.type === "sources") sources = ev.sources;
        else if (ev.type === "token") text += ev.text;
        else if (ev.type === "discard") text = ""; // it went on to use a tool
        else if (ev.type === "step") steps.push(ev.text);
        else if (ev.type === "web") web = ev.sources;
        else if (ev.type === "error") failed = ev.message;
      }
      a.replaceChildren();
      if (steps.length) a.append(stepsList(steps, !text));
      a.append(text ? formatAnswer(text, sources, web) : el("p", "pending", steps.length ? "Reading the results…" : "Thinking…"));
      log.scrollTop = log.scrollHeight;
    }
  } catch (e) {
    failed = String(e.message || e);
  }
  if (failed) a.append(el("p", "error", failed));
  else {
    if (web.length) a.append(webList(web));
    if (sources.length) a.append(sourcesList(sources));
  }
  // Keep the local copy in step with what the server saved.
  if (!failed && current?.id === id) (current.chat ||= []).push({ q: question, a: text.trim(), sources, web, steps });
  asking = false;
  $("chat-send").disabled = false;
}

// Search button: make the model look beyond the paper for the next questions.
$("chat-search").addEventListener("click", (e) => {
  const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
  e.currentTarget.setAttribute("aria-pressed", String(on));
  $("chat-input").placeholder = on ? "Ask; the answer will search papers and the web…" : "Ask a question about the paper…";
  $("chat-input").focus();
});

$("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("chat-input").value;
  $("chat-input").value = "";
  ask(q);
});
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat-form").requestSubmit(); }
});
$("chat-clear").addEventListener("click", async () => {
  if (!current || !confirm("Clear the questions and answers for this paper?")) return;
  await api(`/api/papers/${current.id}/chat`, { method: "DELETE" });
  current.chat = [];
  renderChat();
});

// ---------- Questions ----------
// Insightful questions the AI asked about its own key highlights, each with an
// answer found in the paper or through paper/web search, and an honest
// status. Generated in the background on request (several minutes); stored
// with the paper. They can also be opened in a window of their own
// (#/paper/<id>/questions) or exported as Markdown.
const KIND_LABELS = {
  mechanism: "Why / how", assumption: "Assumption", comparison: "Comparison", generalization: "Generalization",
  implication: "Implication", weakness: "Weakness", background: "Background",
};

// Poll a background job (summary, questions, cards) while its panel is open.
async function loadSideJob(kind, url, panel, render) {
  clearTimeout(panelTimers[kind]);
  if (!current || $(panel).hidden) return;
  const id = current.id;
  let data;
  try {
    data = await api(url(id));
  } catch (e) {
    render(null, { state: "error", error: String(e.message || e) });
    return;
  }
  if (current?.id !== id || $(panel).hidden) return;
  render(data, data.status);
  if (["queued", "running"].includes(data.status?.state)) {
    panelTimers[kind] = setTimeout(() => loadSideJob(kind, url, panel, render), 3000);
  }
}

// The progress bar and error line shared by the side-job panels.
function jobProgress(box, st, hint) {
  const running = ["queued", "running"].includes(st?.state);
  if (running) {
    const prog = el("div", "q-progress");
    prog.append(el("p", null, st.stage || "Working…"));
    const track = el("div", "progress-track");
    const fill = el("div", "progress-fill");
    fill.style.width = `${Math.round((st.progress || 0) * 100)}%`;
    track.append(fill);
    prog.append(track);
    if (hint) prog.append(el("p", "q-hint", hint));
    box.append(prog);
  }
  if (st?.state === "error") box.append(el("p", "error", st.error || "Something went wrong."));
  return running;
}

function loadQuestions() {
  return loadSideJob("questions", (id) => `/api/papers/${id}/insights`, "qpanel", (data, st) => {
    if (data) current.insights = data.insights;
    renderQuestions($("questions"), data?.insights, st);
  });
}

async function startQuestions() {
  if (!current) return;
  await api(`/api/papers/${current.id}/insights`, { method: "POST" }).catch(() => {});
  loadQuestions();
}

function renderQuestions(box, ins, st, standalone = false) {
  box.replaceChildren();
  const running = jobProgress(box, st, "This takes a few minutes: each answer may search papers and the web. You can keep reading.");
  const questions = ins?.questions || [];

  if (!questions.length) {
    if (!running && !standalone) {
      const intro = el("div", "q-intro");
      intro.append(
        el("p", null, "Let the AI think about the paper's key highlights and ask the questions a sharp reader would: why it works, what it assumes, how it compares with other work, whether it generalizes, what could undermine it."),
        el("p", null, "It then tries to answer each one, from the paper where possible and otherwise by searching papers and the web, and says whether the question is answered, partly answered or still open."),
      );
      const go = el("button", "q-go", "Come up with questions");
      go.type = "button";
      go.addEventListener("click", startQuestions);
      intro.append(go);
      box.append(intro);
    } else if (standalone && !running) box.append(el("p", "q-intro", "No questions yet. Open the paper and use Questions to generate them."));
    return;
  }

  const head = el("div", "q-head");
  const when = ins.at ? new Date(ins.at * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
  const counts = {};
  for (const q of questions) counts[q.status] = (counts[q.status] || 0) + 1;
  const tally = ["answered", "partly answered", "open"].filter((s) => counts[s]).map((s) => `${counts[s]} ${s}`).join(", ");
  head.append(el("span", null, `${questions.length} questions${tally ? ` (${tally})` : ""}${when ? ` · ${when}` : ""}`));
  if (!running && !standalone) {
    const redo = el("button", "link", "Ask new questions");
    redo.type = "button";
    redo.addEventListener("click", () => {
      if (confirm("Replace these questions with new ones? This takes a few minutes.")) startQuestions();
    });
    head.append(redo);
  }
  box.append(head);

  const order = new Map(Object.keys(current.sentences).map((sid, i) => [sid, i]));
  const first = (q) => Math.min(...(q.highlights?.length ? q.highlights.map((h) => order.get(h) ?? 1e9) : [1e9]));
  for (const q of [...questions].sort((a, b) => first(a) - first(b))) box.append(questionCard(q));
}

function questionCard(q) {
  const card = el("article", "q-card");
  const meta = el("div", "q-meta");
  meta.append(el("span", "q-kind", KIND_LABELS[q.kind] || q.kind),
              el("span", `q-status q-${(q.status || "").replace(/\s+/g, "-")}`, q.status || ""));
  // Older runs could mention the AI's own highlight numbers ("(Highlight [21])").
  const text = q.q.replace(/\s*\(?\b(?:see )?highlights?\s*(?:\[\d+\](?:\s*(?:,|and|&)\s*)?)+\)?/gi, "");
  card.append(meta, el("h4", "q-text", text));
  for (const sid of q.highlights || []) {
    const s = current.sentences[sid];
    if (!s) continue;
    const about = el("button", "q-about", s.text.length > 150 ? s.text.slice(0, 150) + "…" : s.text);
    about.type = "button";
    about.title = "Go to this highlight";
    about.addEventListener("click", () => jumpToSentence(sid));
    card.append(about);
  }
  const ans = el("div", "chat-a q-answer");
  if (q.steps?.length) ans.append(stepsList(q.steps));
  ans.append(formatAnswer(q.answer || "", q.sources || [], q.web || []));
  if (q.web?.length) ans.append(webList(q.web));
  if (q.sources?.length) ans.append(sourcesList(q.sources));
  card.append(ans);
  const follow = el("button", "link q-follow", "Follow up in Ask");
  follow.type = "button";
  follow.addEventListener("click", () => followUp(q.q));
  card.append(follow);
  return card;
}

function followUp(text) {
  if (!$("qview").hidden) { // the questions window: ask in the reader window
    toReader({ type: "ask", paper: current.id, text }, `#/paper/${current.id}`);
    return;
  }
  setChatOpen(true);
  $("chat-input").value = text + " ";
  $("chat-input").focus();
}

$("qpanel-window").addEventListener("click", () => {
  if (!current) return;
  window.open(`${location.pathname}#/paper/${current.id}/questions`, `questions-${current.id}`, "popup,width=760,height=900");
});
$("qpanel-md").addEventListener("click", () => current && downloadMarkdown(current.id, ["questions"]));

// ---- The questions window. It talks to the reader window over a
// BroadcastChannel: "jump to this sentence", "ask this in Ask". If no reader
// window has this paper open, it becomes the reader itself.
const channel = "BroadcastChannel" in window ? new BroadcastChannel("papercut") : null;

function toReader(msg, fallbackHash) {
  if (!channel) { location.hash = fallbackHash; return; }
  let answered = false;
  const onAck = (e) => { if (e.data?.type === "ack" && e.data.paper === msg.paper) answered = true; };
  channel.addEventListener("message", onAck);
  channel.postMessage(msg);
  setTimeout(() => {
    channel.removeEventListener("message", onAck);
    if (!answered) location.hash = fallbackHash;
  }, 400);
}

channel?.addEventListener("message", (e) => {
  const m = e.data || {};
  if (!current || $("reader").hidden || m.paper !== current.id) return;
  if (m.type === "jump") jumpToSentence(m.sid);
  else if (m.type === "ask") { setChatOpen(true); $("chat-input").value = m.text + " "; $("chat-input").focus(); }
  else return;
  channel.postMessage({ type: "ack", paper: m.paper });
  window.focus();
});

let qviewTimer = null;
async function showQuestionsView(id) {
  show("qview");
  clearTimeout(qviewTimer);
  if (current?.id !== id) {
    try { current = await api(`/api/papers/${id}`); } catch (e) {
      $("qv-list").replaceChildren(el("p", "error", String(e.message || e)));
      return;
    }
  }
  document.title = `Questions · ${current.meta.title}`;
  $("qv-title").textContent = current.meta.title;
  const data = await api(`/api/papers/${id}/insights`).catch(() => null);
  if (!data || $("qview").hidden) return;
  renderQuestions($("qv-list"), data.insights, data.status, true);
  if (["queued", "running"].includes(data.status?.state)) qviewTimer = setTimeout(() => showQuestionsView(id), 3000);
}
$("qv-open").addEventListener("click", () => current && toReader({ type: "jump", paper: current.id, sid: null }, `#/paper/${current.id}`));
$("qv-md").addEventListener("click", () => current && downloadMarkdown(current.id, ["questions"]));
$("qv-print").addEventListener("click", () => window.print());

// ---------- citation popup ----------
const pop = el("div", "cite-pop");
pop.hidden = true;
document.body.append(pop);
let popFor = null;

function floatPreview(b) {
  const byId = Object.fromEntries(current.blocks.map((x) => [x.id, x]));
  const box = el("div", "pop-float");
  if (b.image) {
    const img = el("img");
    img.src = `/api/papers/${current.id}/${b.image}`;
    img.alt = b.type;
    img.addEventListener("load", () => popFor && placePop(popFor));
    box.append(img);
  }
  const capIds = b.caption_sentences?.length ? b.caption_sentences : byId[b.caption_block]?.sentences || b.sentences || [];
  const cap = capIds.map((sid) => current.sentences[sid].text).join(" ");
  if (cap) box.append(el("p", "pop-caption", cap));
  return box;
}

function placePop(c) {
  const r = c.getBoundingClientRect();
  const w = Math.min(c.classList.contains("xref") ? 640 : 460, window.innerWidth - 24);
  pop.style.width = `${w}px`;
  pop.style.left = `${Math.max(12, Math.min(r.left, window.innerWidth - w - 12))}px`;
  const below = r.bottom + 8;
  pop.style.top = `${below + pop.offsetHeight < window.innerHeight ? below : Math.max(56, r.top - pop.offsetHeight - 8)}px`; // stay below the top bar
}

function showCitation(c) {
  const byId = Object.fromEntries(current.blocks.map((b) => [b.id, b]));
  const ids = c.dataset.refs.split(" ");
  if (c.classList.contains("xref")) pop.replaceChildren(...ids.filter((id) => byId[id]).map((id) => floatPreview(byId[id])));
  else pop.replaceChildren(...ids.map((id) => {
    const p = el("p");
    if (current.refNumbers?.[id]) p.append(el("span", "ref-num", `[${current.refNumbers[id]}]`), " ");
    p.append(byId[id]?.text || "");
    if (byId[id]) p.append(" ", refAction(id));
    return p;
  }));
  pop.hidden = false;
  popFor = c;
  placePop(c);
}
function hideCitation() { pop.hidden = true; popFor = null; }

document.addEventListener("mouseover", (e) => {
  const c = e.target.closest?.(".cite, .xref");
  if (c) { if (c !== popFor) showCitation(c); }
  else if (popFor && !e.target.closest?.(".cite-pop")) hideCitation();
});
// Tap on touch screens
document.addEventListener("click", (e) => {
  const c = e.target.closest(".cite, .xref");
  if (c) { popFor === c && !pop.hidden ? hideCitation() : showCitation(c); }
  else if (!e.target.closest(".cite-pop")) hideCitation();
});
window.addEventListener("scroll", hideCitation, { passive: true });

// ---------- Cited papers: open or add to the library ----------
// A reference that is already in the library links to it; any other gets an
// "Add to library" button: the server identifies the paper (arXiv id, DOI,
// or a title match in OpenAlex/Crossref), downloads it and queues it.
function refAction(block) {
  const target = current.links?.[block];
  if (target) {
    const a = el("a", "ref-act in-lib", "Open in Papercut");
    a.href = `#/paper/${target}`;
    return a;
  }
  const b = el("button", "ref-act add", "Add to library");
  b.type = "button";
  b.addEventListener("click", (e) => { e.stopPropagation(); addReference(block, b); });
  return b;
}

async function addReference(block, btn) {
  const id = current.id;
  btn.disabled = true;
  btn.textContent = "Finding the paper…";
  try {
    const r = await fetch(`/api/papers/${id}/references/${block}/add`, { method: "POST" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    if (current?.id !== id) return;
    current.links[block] = body.id;
    const a = el("a", "ref-act in-lib", body.existing ? "Already in your library: open" : "Added: open (processing)");
    a.href = `#/paper/${body.id}`;
    a.title = body.title || "";
    btn.replaceWith(a);
    decorateRefs();
  } catch (err) {
    const msg = el("span", "ref-err", String(err.message || err));
    btn.replaceWith(msg);
  }
}

// The reference list: each entry gets its action on hover.
function decorateRefs() {
  for (const p of $("paper").querySelectorAll("p.ref[data-block]")) {
    if (p.querySelector(".ref-err, .ref-act[disabled]")) continue;
    p.querySelector(".ref-act")?.remove();
    p.append(refAction(p.dataset.block));
  }
}

async function loadConnections() {
  const id = current.id;
  try {
    const c = await api(`/api/papers/${id}/connections`);
    if (current?.id !== id) return;
    current.connections = c;
    for (const x of c.cites) current.links[x.block] ||= x.id;
  } catch { if (current?.id === id) current.connections = null; }
  if (current?.id !== id) return;
  for (const [block, pid] of Object.entries(current.ref_links || {})) current.links[block] ||= pid;
  decorateRefs();
  if (openPanelName === "spanel") renderConnections();
}

// ---------- The original PDF ----------
function setOriginalShown(on) {
  const b = $("toggle-original");
  b.setAttribute("aria-checked", String(on));
  b.textContent = on ? "Show the reflowed text" : "Show original PDF";
  if (!current) return;
  const frame = $("original");
  if (on && !frame.src) frame.src = `/api/papers/${current.id}/pdf`;
  frame.hidden = !on;
  $("paper").hidden = on;
  renderNotes(); // notes belong to the reflowed view
}
$("toggle-original").addEventListener("click", () => {
  setOriginalShown($("toggle-original").getAttribute("aria-checked") !== "true");
  closeMenu();
});

$("reprocess").addEventListener("click", async () => {
  if (!current) return;
  closeMenu();
  await api(`/api/papers/${current.id}/reprocess`, { method: "POST" });
  const id = current.id;
  if (await waitUntilProcessed(id)) { current = null; openPaper(id); }
});

// ---------- The "More" menu ----------
function closeMenu() {
  $("more-menu").hidden = true;
  $("more-toggle").setAttribute("aria-expanded", "false");
}
$("more-toggle").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = $("more-menu").hidden;
  $("more-menu").hidden = !open;
  $("more-toggle").setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

// ---------- Downloads ----------
async function download(r, fallback) {
  if (!r.ok) throw new Error(await r.text());
  const blob = await r.blob();
  const cd = r.headers.get("Content-Disposition") || "";
  const name = decodeURIComponent((cd.match(/filename\*=UTF-8''([^;]+)/) || [, fallback])[1]);
  const a = el("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  return r;
}

// ---------- Markdown export ----------
const MD_PARTS = [
  ["summary", "Summary"], ["highlights", "Highlights (with the AI's margin notes)"], ["notes", "My notes"],
  ["questions", "Questions and answers"], ["cards", "Flashcards"], ["chat", "Ask history"],
];
for (const [v, label] of MD_PARTS) {
  const l = el("label");
  const cb = el("input");
  cb.type = "checkbox";
  cb.value = v;
  cb.checked = v !== "chat";
  l.append(cb, " ", label);
  $("md-parts").append(l);
}
$("export-md").addEventListener("click", () => { closeMenu(); $("md-dialog").showModal(); });
$("md-dialog").addEventListener("close", () => {
  if ($("md-dialog").returnValue !== "ok" || !current) return;
  const parts = [...$("md-parts").querySelectorAll("input:checked")].map((x) => x.value);
  if (parts.length) downloadMarkdown(current.id, parts);
});

async function downloadMarkdown(id, parts) {
  const q = new URLSearchParams({ parts: parts.join(","), hidden: settings.hidden.join(",") });
  try {
    await download(await fetch(`/api/papers/${id}/markdown?${q}`), "notes.md");
  } catch (err) {
    alert(`Export failed: ${err.message || err}`);
  }
}

// ---------- Summary ----------
// A one-page summary the model writes from its own reading notes and key
// highlights (written automatically after highlighting), and how the paper
// connects to the rest of the library.
function loadSummary() {
  renderConnections();
  return loadSideJob("summary", (id) => `/api/papers/${id}/summary`, "spanel", (data, st) => {
    if (data) current.summary = data.summary;
    renderSummary(data?.summary, st);
  });
}

async function startSummary() {
  if (!current) return;
  await api(`/api/papers/${current.id}/summary`, { method: "POST" }).catch(() => {});
  loadSummary();
}

function pageOf(sid) {
  return current.sentences[sid]?.rects?.[0]?.page;
}

// Small chips after a summary line: jump to the highlights it rests on.
function jumpChips(sids) {
  const frag = document.createDocumentFragment();
  for (const sid of sids || []) {
    if (!current.sentences[sid]) continue;
    const c = el("button", "chip", pageOf(sid) ? `p. ${pageOf(sid)}` : "↗");
    c.type = "button";
    c.title = current.sentences[sid].text;
    c.addEventListener("click", () => jumpToSentence(sid));
    frag.append(" ", c);
  }
  return frag;
}

function renderSummary(s, st) {
  const box = $("summary");
  box.replaceChildren();
  const running = jobProgress(box, st, "Written from the AI's reading notes and highlights; about a minute.");
  if (!s) {
    if (!running) {
      const intro = el("div", "q-intro");
      intro.append(el("p", null, "A one-page cheat sheet of the paper: the problem, the approach, the key results with their numbers, the contributions, the limitations and the questions it leaves open. Each point links back to the sentences it rests on."));
      if (current.status?.classified) {
        const go = el("button", "q-go", "Write the summary");
        go.type = "button";
        go.addEventListener("click", startSummary);
        intro.append(go);
      } else intro.append(el("p", null, "It is written from the AI highlights, which this paper doesn't have yet."));
      box.append(intro);
    }
    box.append(el("div", "s-conn"));
    renderConnections();
    return;
  }
  const meta = [s.authors, venueYear(s.venue, s.year)].filter(Boolean).join(" · ");
  if (meta) box.append(el("p", "s-meta", meta));
  if (s.topics?.length) {
    const t = el("div", "s-topics");
    for (const x of s.topics) t.append(el("span", "topic", x));
    box.append(t);
  }
  const tl = el("p", "s-tldr");
  tl.append(el("strong", null, "TL;DR "), s.tldr);
  box.append(tl);
  const section = (title, node) => {
    const sec = el("section", "s-sec");
    sec.append(el("h4", null, title), node);
    box.append(sec);
  };
  const list = (items) => {
    const ul = el("ul");
    for (const it of items) {
      const li = el("li", null, it.text ?? it);
      if (it.highlights) li.append(jumpChips(it.highlights));
      ul.append(li);
    }
    return ul;
  };
  if (s.problem) section("Problem", el("p", null, s.problem));
  if (s.approach) section("Approach", el("p", null, s.approach));
  if (s.results?.length) section("Key results", list(s.results));
  if (s.contributions?.length) section("Contributions", list(s.contributions));
  if (s.limitations?.length) section("Limitations", list(s.limitations));
  if (s.open_questions?.length) section("Open questions", list(s.open_questions));
  const foot = el("div", "q-head");
  const when = s.at ? new Date(s.at * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
  foot.append(el("span", null, `Written by the local model${when ? ` · ${when}` : ""}`));
  if (!running) {
    const redo = el("button", "link", "Rewrite");
    redo.type = "button";
    redo.addEventListener("click", () => { if (confirm("Write the summary again?")) startSummary(); });
    foot.append(redo);
  }
  box.append(foot, el("div", "s-conn"));
  renderConnections();
}

function renderConnections() {
  const box = $("summary").querySelector(".s-conn");
  if (!box || !current) return;
  box.replaceChildren(el("h4", null, "In your library"));
  const c = current.connections;
  if (c === undefined) { box.append(el("p", "pending", "Looking for connections…")); return; }
  if (!c || (!c.cites.length && !c.cited_by.length && !c.related.length)) {
    box.append(el("p", "s-none", "No other papers in your library connect to this one yet."));
    return;
  }
  const group = (title, items, extra) => {
    if (!items.length) return;
    box.append(el("p", "s-conn-head", title));
    const ul = el("ul", "s-conn-list");
    for (const it of items) {
      const li = el("li");
      const a = el("a", null, it.title);
      a.href = `#/paper/${it.id}`;
      li.append(a);
      const more = extra(it);
      if (more) li.append(el("span", "s-conn-extra", more));
      ul.append(li);
    }
    box.append(ul);
  };
  group("This paper cites", c.cites, (x) => `reference [${x.n}]`);
  group("Cited by", c.cited_by, () => "");
  group("Closest in content", c.related.filter((x) => x.score >= 0.6),
        (x) => `${Math.round(x.score * 100)}% similar` + (x.shared.length ? ` · ${x.shared.join(", ")}` : ""));
}

// ---------- Flashcards ----------
function loadCards() {
  return loadSideJob("cards", (id) => `/api/papers/${id}/cards`, "cpanel", (data, st) => {
    if (data) current.cards = data.cards;
    renderCards(data, st);
  });
}

async function startCards() {
  if (!current) return;
  await api(`/api/papers/${current.id}/cards`, { method: "POST" }).catch(() => {});
  loadCards();
}

let cardDraft = null; // {sid, back}: "Make a flashcard" from a sentence

function newCardFrom(sid) {
  cardDraft = { sid, back: current.sentences[sid]?.text || "" };
  if (openPanelName === "cpanel") loadCards();
  else setPanel("cpanel");
}

function renderCards(data, st) {
  const box = $("cards");
  box.replaceChildren();
  const running = jobProgress(box, st, "Written from the summary, highlights and answered questions; about a minute.");
  const deck = data?.cards?.cards || [];
  const info = data?.review;
  if (deck.length) {
    const head = el("div", "fc-head");
    head.append(el("span", null, `${deck.length} cards` + (info?.reviewed ? ` · last reviewed ${ago(info.last)}` : "")));
    const go = el("a", "btn primary", "Review");
    go.href = `#/review/${current.id}`;
    head.append(go);
    box.append(head);
  } else if (!running) {
    const intro = el("div", "q-intro");
    intro.append(el("p", null, "Flashcards, as many as are worth having, for what's worth remembering in this paper: the core idea, key design decisions, results with their numbers, definitions and limitations. Go through them whenever you like, from here or from Review at the top; the ones you find hard come first."));
    const go = el("button", "q-go", "Make flashcards");
    go.type = "button";
    go.addEventListener("click", startCards);
    intro.append(go);
    box.append(intro);
  }

  // Your own card.
  const add = el("details", "fc-add");
  add.append(el("summary", null, "Add your own card"));
  const front = el("textarea");
  front.rows = 2;
  front.placeholder = "Front: a question";
  const back = el("textarea");
  back.rows = 3;
  back.placeholder = "Back: the answer";
  const save = el("button", "primary", "Add card");
  save.type = "button";
  const sid = cardDraft?.sid || null;
  if (cardDraft) { add.open = true; back.value = cardDraft.back; cardDraft = null; setTimeout(() => front.focus(), 0); }
  save.addEventListener("click", async () => {
    try {
      await api(`/api/papers/${current.id}/cards/new`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ front: front.value, back: back.value, sid }),
      });
      loadCards();
    } catch (err) { alert(`Could not add the card: ${err.message || err}`); }
  });
  add.append(front, back, save);
  box.append(add);

  for (const c of deck) box.append(flashcard(c));
  if (deck.length && !running) {
    const redo = el("button", "link", "Write new AI cards");
    redo.type = "button";
    redo.addEventListener("click", () => {
      if (confirm("Replace the AI's cards with new ones? Your own cards stay.")) startCards();
    });
    box.append(redo);
  }
}

function flashcard(c) {
  const card = el("article", "fc-card");
  const meta = el("div", "q-meta");
  meta.append(el("span", "q-kind", c.kind + (c.by === "you" ? " · yours" : "")));
  const del = el("button", "link fc-del", "Delete");
  del.type = "button";
  del.addEventListener("click", async () => {
    if (!confirm("Delete this card?")) return;
    await api(`/api/papers/${current.id}/cards/${c.id}`, { method: "DELETE" }).catch(() => {});
    loadCards();
  });
  meta.append(del);
  const back = el("div", "fc-back");
  back.hidden = true;
  back.append(el("p", null, c.back), jumpChips(c.highlights));
  const reveal = el("button", "link", "Show answer");
  reveal.type = "button";
  reveal.addEventListener("click", () => {
    back.hidden = !back.hidden;
    reveal.textContent = back.hidden ? "Show answer" : "Hide answer";
  });
  card.append(meta, el("p", "fc-front", c.front), back, reveal);
  return card;
}

// ---------- Review ----------
// On demand, one paper at a time: nothing is scheduled or pushed. #/review
// lists the papers that have flashcards; #/review/<id> goes through all of a
// paper's cards, weakest first (the grades decide that order). Space shows
// the answer; 1-4 grade it.
const GRADES = [["again", "Again"], ["hard", "Hard"], ["good", "Good"], ["easy", "Easy"]];
let rv = null;

function ago(t) {
  if (!t) return "";
  const d = (Date.now() / 1000 - t) / 86400;
  if (d < 1) return "today";
  if (d < 2) return "yesterday";
  if (d < 30) return `${Math.round(d)} days ago`;
  return new Date(t * 1000).toLocaleDateString(undefined, { dateStyle: "medium" });
}

async function showReview(pid) {
  show("review");
  document.title = "Review · Papercut";
  rv = null;
  $("rv-count").textContent = "";
  $("rv-stage").replaceChildren(el("p", "pending", "Loading…"));
  if (pid) return startReview(pid);
  $("rv-title").textContent = "Review";
  let decks, libData;
  try {
    [decks, libData] = await Promise.all([api("/api/review"), api("/api/library").catch(() => null)]);
  } catch (e) {
    $("rv-stage").replaceChildren(el("p", "error", String(e.message || e)));
    return;
  }
  const stage = $("rv-stage");
  stage.replaceChildren(el("p", "rv-intro", "Pick a paper to go through its flashcards. Cards you found hard come first."));
  const list = el("div", "rv-decks");
  for (const d of decks) {
    const a = el("a", "rv-deck");
    a.href = `#/review/${d.id}`;
    a.append(el("span", "rv-deck-title", d.title));
    const info = `${d.cards} cards` + (d.reviewed ? ` · ${d.reviewed} reviewed · last ${ago(d.last)}` : " · not reviewed yet");
    a.append(el("span", "rv-deck-info", info));
    list.append(a);
  }
  if (!decks.length) list.append(el("p", "lib-empty", "No paper has flashcards yet."));
  stage.append(list);
  // Papers without cards: make them here.
  const missing = (libData?.papers || []).filter((p) => !p.processing && !p.cards && p.classified);
  if (missing.length) {
    const sec = el("div", "rv-missing");
    sec.append(el("p", null, `${missing.length} paper${missing.length > 1 ? "s have" : " has"} no flashcards yet:`));
    const ul = el("ul");
    for (const p of missing) ul.append(el("li", null, p.title));
    const all = el("button", "primary", missing.length > 1 ? `Make cards for all ${missing.length}` : "Make cards");
    all.type = "button";
    all.addEventListener("click", async () => {
      all.disabled = true;
      for (const p of missing) await api(`/api/papers/${p.id}/cards`, { method: "POST" }).catch(() => {});
      all.textContent = "Queued: each paper takes a minute or two.";
    });
    sec.append(ul, all);
    stage.append(sec);
  }
}

async function startReview(pid) {
  let data;
  try {
    data = await api(`/api/review/${pid}`);
  } catch (e) {
    $("rv-stage").replaceChildren(el("p", "error", String(e.message || e)));
    return;
  }
  rv = { paper: pid, title: data.queue[0]?.title || "", queue: data.queue, i: 0, shown: false, reviewed: 0 };
  $("rv-title").textContent = "Review";
  renderReview();
}

function renderReview() {
  const stage = $("rv-stage");
  stage.replaceChildren();
  const card = rv.queue[rv.i];
  $("rv-count").textContent = card ? `${rv.queue.length - rv.i} to go` : "";
  const top = el("div", "rv-paperbar");
  const t = el("a", "rv-paper", rv.title);
  t.href = `#/paper/${rv.paper}`;
  const back = el("a", "rv-back-link", "All papers");
  back.href = "#/review";
  top.append(t, back);
  stage.append(top);
  if (!card) {
    const box = el("div", "rv-done");
    box.append(el("h3", null, rv.reviewed ? "Done with this paper." : "This paper has no flashcards yet."));
    if (rv.reviewed) box.append(el("p", null, `You went through ${rv.reviewed} card${rv.reviewed > 1 ? "s" : ""}.`));
    const again = el("button", null, "Go through it again");
    again.type = "button";
    again.addEventListener("click", () => startReview(rv.paper));
    const list = el("a", "btn primary", "Pick another paper");
    list.href = "#/review";
    const row = el("div", "rv-actions");
    if (rv.reviewed) row.append(again);
    row.append(list);
    box.append(row);
    stage.append(box);
    return;
  }
  const box = el("article", "rv-card");
  const head = el("div", "rv-top");
  head.append(el("span", "q-kind", card.kind + (card.state ? "" : " · new")));
  box.append(head, el("p", "rv-front", card.front));
  const actions = el("div", "rv-actions");
  if (!rv.shown) {
    const showBtn = el("button", "primary rv-show", "Show answer");
    showBtn.type = "button";
    showBtn.title = "Space";
    showBtn.addEventListener("click", () => { rv.shown = true; renderReview(); });
    actions.append(showBtn);
  } else {
    const ans = el("div", "rv-back");
    ans.append(el("p", null, card.back));
    const sid = (card.highlights || [])[0];
    if (sid) {
      const a = el("a", "rv-source", "See it in the paper");
      a.href = `#/paper/${card.paper}/s/${sid}`;
      ans.append(a);
    }
    box.append(ans);
    GRADES.forEach(([g, label], i) => {
      const b = el("button", `rv-grade rv-${g}`);
      b.type = "button";
      b.title = `${label} (${i + 1})`;
      b.append(el("strong", null, label));
      b.addEventListener("click", () => gradeCard(g));
      actions.append(b);
    });
  }
  stage.append(box, actions, el("p", "rv-keys", rv.shown ? "Keys: 1 Again · 2 Hard · 3 Good · 4 Easy" : "Key: Space shows the answer"));
}

async function gradeCard(g) {
  const card = rv.queue[rv.i];
  let state;
  try {
    state = await api(`/api/review/${card.paper}/${card.id}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ grade: g }),
    });
  } catch (err) { alert(`Could not save: ${err.message || err}`); return; }
  rv.reviewed++;
  if (g === "again") rv.queue.push({ ...card, state }); // once more before this paper is done
  rv.i++;
  rv.shown = false;
  renderReview();
}

document.addEventListener("keydown", (e) => {
  if ($("review").hidden || !rv || e.target.closest("input, textarea, select")) return;
  if (!rv.queue[rv.i]) return;
  if (!rv.shown && (e.key === " " || e.key === "Enter")) { e.preventDefault(); rv.shown = true; renderReview(); }
  else if (rv.shown && /^[1-4]$/.test(e.key)) gradeCard(GRADES[Number(e.key) - 1][0]);
});

// ---------- Library ----------
// Every paper, grouped into collections (the AI proposes them; you can move a
// paper), with status (to read / reading / done), tags, and search: instant
// over titles, authors, topics and summaries, and after a pause through the
// text of every paper.
let lib = { papers: [], collections: [] };
const libView = { status: "all", col: null, q: "" }; // col: null = all, "" = none, else a name
try { Object.assign(libView, JSON.parse(localStorage.getItem("libView") || "{}"), { q: "" }); } catch {}
let libTimer = null, searchTimer = null, searchRun = 0;

async function loadLibrary() {
  clearTimeout(libTimer);
  try { lib = await api("/api/library"); } catch { lib = { papers: [], collections: [] }; }
  renderLibrary();
  // Poll while the AI is organizing or papers are still processing.
  const busy = ["queued", "running"].includes(lib.organize?.state) || lib.papers.some((p) => p.processing);
  if (busy) libTimer = setTimeout(() => { if (!$("home").hidden) loadLibrary(); }, 4000);
}

function saveLibView() {
  try { localStorage.setItem("libView", JSON.stringify({ status: libView.status, col: libView.col })); } catch {}
}

function libMatches(p, q) {
  if (!q) return true;
  const hay = [p.title, p.authors, p.venue, p.year, p.tldr, p.collection, ...(p.topics || []), ...(p.tags || [])]
    .join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
}

function renderLibrary() {
  const papers = lib.papers;
  $("lib-count").textContent = papers.length ? `(${papers.length})` : "";
  for (const b of $("lib-status").children) b.setAttribute("aria-pressed", String(b.dataset.v === libView.status));
  $("lib-sort").value = settings.libSort || "opened";

  // Continue reading: started but not finished, most recent first.
  const going = papers.filter((p) => p.status === "reading" && p.opened).sort((a, b) => b.opened - a.opened).slice(0, 3);
  $("continue").hidden = !going.length || !!libView.q;
  $("continue-list").replaceChildren(...going.map((p) => {
    const a = el("a", "cont-card");
    a.href = `#/paper/${p.id}`;
    a.append(el("span", "cont-title", p.title));
    const bar = el("span", "cont-bar");
    const fill = el("span");
    fill.style.width = `${Math.round(p.percent || 0)}%`;
    bar.append(fill);
    a.append(bar, el("span", "cont-pct", `${Math.round(p.percent || 0)}% read`));
    return a;
  }));

  // Collections.
  const cols = $("lib-cols");
  cols.replaceChildren();
  const byCol = {};
  for (const p of papers) byCol[p.collection || ""] = (byCol[p.collection || ""] || 0) + 1;
  const names = [...new Set([...lib.collections.map((c) => c.name), ...papers.map((p) => p.collection).filter(Boolean)])];
  const colBtn = (label, value, count, about) => {
    const b = el("button", "col-btn");
    b.type = "button";
    b.setAttribute("aria-pressed", String(libView.col === value));
    b.append(el("span", null, label), el("span", "count-muted", String(count)));
    if (about) b.title = about;
    b.addEventListener("click", () => { libView.col = value; saveLibView(); renderLibrary(); });
    cols.append(b);
  };
  colBtn("All papers", null, papers.length);
  for (const n of names) colBtn(n, n, byCol[n] || 0, lib.collections.find((c) => c.name === n)?.about);
  if (byCol[""]) colBtn("Not in a collection", "", byCol[""]);
  if (libView.col && !names.includes(libView.col)) libView.col = null;
  const org = lib.organize || {};
  const busy = ["queued", "running"].includes(org.state);
  const orgBtn = el("button", "link col-org", busy ? "Organizing…" : lib.collections.length ? "Reorganize with AI" : "Organize with AI");
  orgBtn.type = "button";
  orgBtn.disabled = busy;
  orgBtn.title = "Let the local model group your papers into collections by theme. Papers you moved yourself stay where you put them.";
  orgBtn.addEventListener("click", async () => {
    const fresh = lib.collections.length && confirm("Start the collections over? (Cancel keeps the current collections and only files papers that have none.)");
    await api("/api/library/organize", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fresh: !!fresh }) }).catch(() => {});
    loadLibrary();
  });
  cols.append(orgBtn);
  if (org.state === "error") cols.append(el("p", "error small", org.error));

  // Papers.
  const sort = settings.libSort || "opened";
  const cmp = {
    opened: (a, b) => (b.opened || 0) - (a.opened || 0) || (b.added || 0) - (a.added || 0),
    added: (a, b) => (b.added || 0) - (a.added || 0),
    title: (a, b) => a.title.localeCompare(b.title),
    year: (a, b) => (b.year || "0").localeCompare(a.year || "0") || a.title.localeCompare(b.title),
  }[sort];
  const shown = papers
    .filter((p) => libView.status === "all" || p.status === libView.status)
    .filter((p) => libView.col === null || (p.collection || "") === libView.col)
    .filter((p) => libMatches(p, libView.q))
    .sort(cmp);
  const list = $("lib-list");
  list.replaceChildren(...shown.map(libRow));
  if (!papers.length) list.append(el("p", "lib-empty", "No papers yet. Drop a PDF above or paste a link."));
  else if (!shown.length) list.append(el("p", "lib-empty", libView.q ? "No titles, authors or topics match. Matches inside your papers are below." : "No papers here."));
}

// "NeurIPS 2017", not "NeurIPS 2017 2017" when the venue already has the year.
function venueYear(venue, year) {
  return venue && year && venue.includes(year) ? venue : [venue, year].filter(Boolean).join(" ");
}

function libRow(p) {
  const row = el("article", "lib-row");
  const main = el("div", "lib-row-main");
  const a = el("a", "lib-title", p.title);
  a.href = `#/paper/${p.id}`;
  main.append(a);
  const meta = [p.authors, venueYear(p.venue, p.year)].filter(Boolean).join(" · ");
  if (meta) main.append(el("div", "lib-meta", meta));
  if (p.processing) main.append(el("div", "lib-meta", "Processing…"));
  if (p.tldr) main.append(el("p", "lib-tldr", p.tldr));
  const tags = el("div", "lib-tags");
  for (const t of p.topics || []) {
    const b = el("button", "topic", t);
    b.type = "button";
    b.title = "Show papers on this topic";
    b.addEventListener("click", () => { $("lib-search").value = t; onLibSearch(); });
    tags.append(b);
  }
  for (const t of p.tags || []) {
    const b = el("span", "tag");
    b.append(t);
    const x = el("button", "tag-x", "×");
    x.type = "button";
    x.title = "Remove this tag";
    x.addEventListener("click", () => setShelf(p, { tags: p.tags.filter((y) => y !== t) }));
    b.append(x);
    tags.append(b);
  }
  const addTag = el("button", "tag-add", "+ tag");
  addTag.type = "button";
  addTag.addEventListener("click", () => {
    const input = el("input", "tag-input");
    input.placeholder = "tag";
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.value.trim()) setShelf(p, { tags: [...(p.tags || []), input.value.trim()] });
      if (e.key === "Escape") renderLibrary();
    });
    input.addEventListener("blur", () => { if (input.value.trim()) setShelf(p, { tags: [...(p.tags || []), input.value.trim()] }); });
    addTag.replaceWith(input);
    input.focus();
  });
  if (!p.processing) tags.append(addTag);
  main.append(tags);

  const side = el("div", "lib-side");
  if (!p.processing) {
    const status = el("select", "lib-sel");
    status.setAttribute("aria-label", "Status");
    for (const [v, label] of [["to-read", "To read"], ["reading", "Reading"], ["done", "Done"]]) status.append(new Option(label, v));
    status.value = p.status;
    status.title = p.status_set ? "Set by you" : "From your reading progress";
    status.addEventListener("change", () => setShelf(p, { status: status.value }));
    const col = el("select", "lib-sel");
    col.setAttribute("aria-label", "Collection");
    col.append(new Option("No collection", ""));
    const names = [...new Set([...lib.collections.map((c) => c.name), ...lib.papers.map((x) => x.collection).filter(Boolean)])];
    for (const n of names) col.append(new Option(n, n));
    col.append(new Option("New collection…", "__new"));
    col.value = p.collection || "";
    col.title = p.collection_set_by === "ai" ? "Chosen by the AI" : p.collection_set_by === "you" ? "Chosen by you" : "";
    col.addEventListener("change", () => {
      let v = col.value;
      if (v === "__new") {
        v = (prompt("Name of the new collection:") || "").trim();
        if (!v) { col.value = p.collection || ""; return; }
      }
      setShelf(p, { collection: v || null });
    });
    side.append(status, col);
    if (p.percent >= 1) {
      const bar = el("span", "cont-bar");
      const fill = el("span");
      fill.style.width = `${Math.round(p.percent)}%`;
      bar.append(fill);
      side.append(bar);
    }
    const counts = [
      p.highlights && `${p.highlights} highlights`, p.notes && `${p.notes} notes`,
      p.questions && `${p.questions} questions`, p.cards && `${p.cards} cards`,
    ].filter(Boolean).join(" · ");
    if (counts) side.append(el("span", "lib-counts", counts));
  }
  row.append(main, side);
  return row;
}

async function setShelf(p, patch) {
  try {
    await api(`/api/library/${p.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  } catch (err) { alert(`Could not save: ${err.message || err}`); }
  loadLibrary();
}

$("lib-status").addEventListener("click", (e) => {
  const v = e.target.closest("button")?.dataset.v;
  if (!v) return;
  libView.status = v;
  saveLibView();
  renderLibrary();
});
$("lib-sort").addEventListener("change", (e) => { updateSettings({ libSort: e.target.value }); renderLibrary(); });

// Search: instant over the list; after a pause, through the papers' text.
function onLibSearch() {
  libView.q = $("lib-search").value.trim();
  renderLibrary();
  clearTimeout(searchTimer);
  if (libView.q.length < 3) { $("lib-inside").hidden = true; return; }
  searchTimer = setTimeout(searchInside, 450);
}
$("lib-search").addEventListener("input", onLibSearch);
$("lib-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(searchTimer); searchInside(); }
  if (e.key === "Escape") { $("lib-search").value = ""; onLibSearch(); }
});

async function searchInside() {
  const q = libView.q;
  if (!q) return;
  const run = ++searchRun;
  const box = $("lib-passages");
  $("lib-inside").hidden = false;
  box.replaceChildren(el("p", "pending", "Searching inside your papers…"));
  let r;
  try { r = await api(`/api/search?q=${encodeURIComponent(q)}`); } catch (e) {
    box.replaceChildren(el("p", "error", String(e.message || e)));
    return;
  }
  if (run !== searchRun) return;
  box.replaceChildren();
  if (!r.passages.length) { box.append(el("p", "lib-empty", "Nothing found inside the papers.")); return; }
  const words = q.toLowerCase().split(/\s+/).filter((w) => w.length > 2);
  const mark = (text) => {
    const frag = document.createDocumentFragment();
    if (!words.length) { frag.append(text); return frag; }
    const re = new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
    let pos = 0, m;
    while ((m = re.exec(text))) {
      frag.append(text.slice(pos, m.index), el("mark", null, m[0]));
      pos = m.index + m[0].length;
    }
    frag.append(text.slice(pos));
    return frag;
  };
  const byPaper = new Map();
  for (const p of r.passages) {
    if (!byPaper.has(p.paper)) byPaper.set(p.paper, []);
    byPaper.get(p.paper).push(p);
  }
  for (const [pid, items] of byPaper) {
    const g = el("div", "hit-group");
    const t = el("a", "hit-paper", items[0].title);
    t.href = `#/paper/${pid}`;
    g.append(t);
    for (const it of items.slice(0, 3)) {
      const a = el("a", "hit");
      a.href = `#/paper/${pid}/s/${it.sid}`;
      if (it.heading) a.append(el("span", "hit-head", it.heading));
      const p = el("span", "hit-text");
      p.append(mark(it.text + (it.text.length >= 400 ? "…" : "")));
      a.append(p);
      g.append(a);
    }
    box.append(g);
  }
}

route();
