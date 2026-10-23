"use strict";

function showStatus(el, type, html) {
  el.innerHTML = html ? statusHtml(type, html) : "";
}

// ── Current list summary ──────────────────────────────────────────────────────

const summaryEl = document.getElementById("known-summary");

let knownMarked = 0;

// Where the list came from, said the way a person would. The stored source is a file
// name ("_Deck_ deck export at 8_23_20 AM on 7_17_2026.csv", jpdb's own naming) or a
// code ("paste", "seed:top-10000-ja"; known.py and server.py write these).
const SOURCE_WORDS = {
  paste: "a pasted list", marked: "words marked by hand", restore: "a restored list",
  upload: "an uploaded file", import: "an import",
};

function sourceLabel(src) {
  if (!src) return "an import";
  const seed = /^seed:top-(\d+)-(\w+)$/.exec(src);
  if (seed) {
    const lang = LANG_LABEL[seed[2]];
    return `the ${Number(seed[1]).toLocaleString()} most common ${lang ? lang + " " : ""}words`;
  }
  const jpdb = /^_?(.+?)_? deck export at .*\.csv$/i.exec(src);
  if (jpdb) return `jpdb deck “${jpdb[1]}”`;
  return SOURCE_WORDS[src] || src.replace(/\.(csv|tsv|txt)$/i, "");
}

const dateOnly = (iso) => new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
const dateTime = (iso) =>
  new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

// Words set aside from a book's study list as "not a word", each with an undo.
const asideCard = document.getElementById("aside-card");
const asideList = document.getElementById("aside-list");

function renderAside(words) {
  asideCard.hidden = !words.length;
  asideList.innerHTML = words
    .map((w) => `<li><span${langAttr(w)}>${esc(w)}</span>` +
      `<button type="button" class="aside-undo" data-word="${esc(w)}" aria-label="Put back: ${esc(w)}">Put back</button></li>`)
    .join("");
}

asideList.addEventListener("click", async (e) => {
  const btn = e.target.closest(".aside-undo");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    await postJson("/api/known-words/unskip", { words: [btn.dataset.word] });
  } catch {
    btn.disabled = false;
    btn.textContent = "Failed — retry";
    return;
  }
  // The word is unknown again, so it belongs back on the review list.
  refreshSummary();
  loadFrequent();
});

async function refreshSummary() {
  let data;
  try {
    const resp = await fetch("/api/known-words");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch {
    // Keep the headline slot quiet; say what failed and offer the way back.
    summaryEl.innerHTML =
      `<span class="known-summary-err">Couldn't load your list. ` +
      `<button type="button" class="link-btn" id="summary-retry">Try again</button></span>`;
    return;
  }
  knownCount = data.count || 0;
  knownMarked = data.marked || 0;
  renderAside(data.not_words || []);
  window.dispatchEvent(new CustomEvent("known-words-changed", { detail: { count: knownCount } }));
  manageMore.hidden = !data.count;
  const fold = document.getElementById("manage-fold");
  if (data.count) {
    const parts = [`from ${esc(sourceLabel(data.source))}`];
    if (data.updated_at) parts.push(dateOnly(data.updated_at));
    if (data.marked) parts.push(`${data.marked.toLocaleString()} marked by hand`);
    // Exposed vocabulary (known.summary): words met 5+ times in a book marked read count
    // as known too, on top of the list. /profile explains the rule.
    if (data.exposed) {
      parts.push(`+${data.exposed.toLocaleString()} picked up from ` +
        `<a href="/profile">the ${data.books_read.toLocaleString()} ` +
        `${data.books_read === 1 ? "book" : "books"} you've read</a>`);
    }
    summaryEl.innerHTML =
      `<strong>${data.count.toLocaleString()} words</strong> known<span class="sep">·</span>` +
      parts.join('<span class="sep">·</span>');
  } else {
    // Nothing to measure against yet: the import form opens, and the line says where it is.
    summaryEl.innerHTML =
      `<strong>No words yet.</strong> Nothing to measure your books against: ` +
      `<a href="#manage-card" data-open-manage>import a list</a>` +
      ` or seed common words below.`;
  }
  // Manage starts folded: rare once there is a list. With none it is the first job, so it
  // opens; after that it stays as the reader left it while the list changes (an import).
  if (!data.count) fold.open = true;
  applySeedLadderUI(data.seeded_top_n || 0);
  updateSeedVisibility();
  loadSnapshots();
}

summaryEl.addEventListener("click", (e) => {
  if (e.target.closest("#summary-retry")) refreshSummary();
  const open = e.target.closest("[data-open-manage]");
  if (open) {
    e.preventDefault();
    document.getElementById("manage-fold").open = true;
    document.getElementById("manage-card").scrollIntoView({ block: "start" });
  }
});

// ── Manage list: Clear sits behind the card's ⋯ (STYLE.md §7) ─────────────────

document.getElementById("manage-head").insertAdjacentHTML(
  "beforeend", moreBtn('id="manage-more" hidden', "More list actions"));
const manageMore = document.getElementById("manage-more");

manageMore.addEventListener("click", () =>
  openMenu(manageMore, [{ label: "Clear the list…", danger: true, run: clearList }]));

async function clearList() {
  const ok = await confirmDialog({
    title: "Clear the known-words list?",
    message:
      `This removes all ${knownCount.toLocaleString()} words` +
      (knownMarked ? `, including the ${knownMarked.toLocaleString()} you marked by hand` : "") +
      ". Every book's strain, Known % and place in Read Next are then measured against " +
      "an empty list. A copy is kept under “Restore an earlier list”.",
    confirmLabel: "Clear list", danger: true,
  });
  if (!ok) return;
  const statusEl = document.getElementById("clear-status");
  showStatus(statusEl, "pending", "Clearing…");
  try {
    const resp = await fetch("/api/known-words", { method: "DELETE" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    showStatus(statusEl, "ok", "List cleared. The old one is under “Restore an earlier list”.");
  } catch (err) {
    showStatus(statusEl, "err", `Couldn't clear the list: ${esc(err.message)}`);
  } finally {
    refreshSummary();
    loadFrequent();
  }
}

// ── Snapshots ───────────────────────────────────────────────────────────
// A copy of the list is saved before each replace-import, Clear and restore.

const snapshotsEl = document.getElementById("snapshots");
const snapshotListEl = document.getElementById("snapshot-list");
const SNAPSHOT_WHY = { replace: "Before an import", clear: "Before Clear", restore: "Before a restore" };

async function loadSnapshots() {
  let snaps = [];
  try {
    const resp = await fetch("/api/known-words/snapshots");
    if (resp.ok) snaps = await resp.json();
  } catch { /* the card just stays hidden */ }
  snapshotsEl.hidden = !snaps.length;
  document.getElementById("snapshots-n").textContent = snaps.length ? `(${snaps.length})` : "";
  snapshotListEl.innerHTML = snaps.map((s) => {
    const when = s.created_at ? dateTime(s.created_at) : "";
    const marked = s.marked ? ` (${s.marked.toLocaleString()} marked by hand)` : "";
    return `<li><span><strong>${esc(SNAPSHOT_WHY[s.reason] || "Earlier list")}</strong> · ${esc(when)}` +
      `<small>${s.count.toLocaleString()} words${marked} · from ${esc(sourceLabel(s.source))}</small></span>` +
      `<button class="btn" data-restore="${esc(s.id)}" data-count="${s.count}">Restore</button></li>`;
  }).join("");
}

snapshotListEl.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-restore]");
  if (!btn) return;
  const count = Number(btn.dataset.count);
  const ok = await confirmDialog({
    title: "Restore this list?",
    message:
      `Replace the current list (${knownCount.toLocaleString()} words) with this one ` +
      `(${count.toLocaleString()} words)? The current list is saved first, so you can undo this.`,
    confirmLabel: "Restore",
  });
  if (!ok) return;
  const statusEl = document.getElementById("snapshot-status");
  snapshotListEl.querySelectorAll("button").forEach((b) => (b.disabled = true));
  showStatus(statusEl, "pending", "Restoring and recomputing…");
  try {
    const data = await postJson("/api/known-words/restore", { id: btn.dataset.restore });
    showStatus(
      statusEl, "ok",
      `Restored ${data.total.toLocaleString()} words; Known % updated on ${data.books_updated} books.`,
    );
  } catch (err) {
    showStatus(statusEl, "err", esc(err.message));
  } finally {
    refreshSummary();
    loadFrequent();
  }
});

// ── Active-language UI ─────────────────────────────────────────────────────────
// A few page elements read differently depending on the profile's language:
// the seed button (en/ja — see known.SEEDABLE_LANGUAGES), the import card's
// jpdb/reading-column framing (meaningless for English), and the paste
// placeholder. Resolved once from /api/profiles and shared by every consumer
// below rather than each doing its own fetch.

const seedBlock = document.getElementById("seed-block");
const seedBtn = document.getElementById("seed-btn");
const seedLangLabel = document.getElementById("seed-lang-label");
const importIntro = document.getElementById("import-intro");
const jpdbLabel = document.getElementById("fmt-jpdb-label");

const SEEDABLE_LANGS = ["en", "ja"];
const LANG_LABEL = { en: "English", ja: "Japanese" };

let activeLang = null;
let knownCount = 0;

// Mirrors known.seed_common's guard: seeding replaces the imported list, so
// it's only offered to a profile with nothing to lose — an empty list, or
// one already on the seed ladder (continuing never clobbers anything, there
// being nothing non-seed left to clobber).
function updateSeedVisibility() {
  seedBlock.hidden = !(
    SEEDABLE_LANGS.includes(activeLang) && (knownCount === 0 || seededTopN > 0)
  );
}

function applyLanguageUI() {
  const isEn = activeLang === "en";
  jpdbLabel.hidden = isEn;
  importIntro.textContent = isEn
    ? "Import a CSV/TSV export (Anki, a spreadsheet, …) or a plain list with " +
      "one word per line. The word must be the first column."
    : "Import a jpdb CSV export, an Anki export (TSV/CSV), or a plain list " +
      "with one word per line. The word must be the first column.";
  pasteInput.placeholder = isEn ? "apple\nrunning\nvocabulary" : "学校\n先生\nトンネル";
  seedLangLabel.textContent = LANG_LABEL[activeLang] || "Japanese";
  updateSeedVisibility();
}

async function initProfile() {
  // No active-language signal (null) leaves the defaults (ja copy, seed hidden)
  // rather than risk showing another profile's language-specific controls.
  const active = await loadActiveProfile();
  activeLang = (active && active.language) || null;
  applyLanguageUI();
}

// Fired immediately; consumers that need activeLang (loadFrequent) await it
// rather than blocking the whole page on the profile fetch.
const profileReady = initProfile();

// Only offered on an empty list (updateSeedVisibility), so there is nothing to replace:
// the confirm says what the 10,000 words will do, not what they overwrite.
seedBtn.addEventListener("click", async () => {
  const label = LANG_LABEL[activeLang] || "Japanese";
  const ok = await confirmDialog({
    title: `Start from the 10,000 most common ${label} words?`,
    message:
      "They go on your list as known, and every book is scored against them. You can " +
      "still mark words by hand, import a deck later, or clear the list.",
    confirmLabel: "Seed the list",
  });
  if (!ok) return;
  const statusEl = document.getElementById("seed-status");
  seedBtn.disabled = true;
  showStatus(statusEl, "pending", "Seeding and recomputing…");
  try {
    const resp = await fetch("/api/known-words/seed", { method: "POST" });
    const data = await resp.json();
    if (resp.ok && data.status === "ok") {
      showStatus(
        statusEl,
        "ok",
        `Seeded ${data.total.toLocaleString()} words; Known % updated on ` +
          `${data.books_updated} books — <a href="/next">see Read Next ${icon("arrow_forward")}</a>`,
      );
    } else {
      showStatus(statusEl, "err", esc(data.detail || data.message || "Unknown error"));
    }
  } catch (err) {
    showStatus(statusEl, "err", `Request failed: ${esc(err.message)}`);
  } finally {
    seedBtn.disabled = false;
    refreshSummary();
    loadFrequent();
  }
});

// ── Seed-common-words ladder ──────────────────────────────────────────────────
// The one-shot "Seed common words" button above seeds the 10k baseline; this
// is the repeatable follow-up — "preview the next N → apply" up to SEED_MAX —
// for a reader who is already fluent and just missing a curated list, so the
// frequent-words review below doesn't stay cluttered with words they've known
// for years. Mirrors known.SEED_STEP/SEED_MAX. JA steps in smaller 100-word
// rungs than EN's 1,000 — kanji vocabulary costs more study effort
// per word, so a coarser ladder would skip past too much at once.

const SEED_STEP = { en: 1000, ja: 100 };
const SEED_MAX = 20000;

const seedBase = document.getElementById("seed-base");
const seedIncrement = document.getElementById("seed-increment");
const seedLevel = document.getElementById("seed-level");
const seedCapNote = document.getElementById("seed-cap-note");
const seedPreviewBtn = document.getElementById("seed-preview-btn");
const seedPreviewBox = document.getElementById("seed-preview-box");
const seedPreviewWords = document.getElementById("seed-preview-words");
const seedApplyBtn = document.getElementById("seed-apply-btn");
const seedPreviewCancel = document.getElementById("seed-preview-cancel");
const seedIncrementStatus = document.getElementById("seed-increment-status");

let seededTopN = 0;
let previewTopN = 0;

function applySeedLadderUI(topN) {
  seededTopN = topN;
  const seeded = topN > 0;
  seedBase.hidden = seeded;
  seedIncrement.hidden = !seeded;
  if (!seeded) return;
  seedLevel.textContent = topN.toLocaleString();
  const atCap = topN >= SEED_MAX;
  seedPreviewBtn.hidden = atCap;
  const step = SEED_STEP[activeLang] || 1000;
  seedPreviewBtn.textContent = `Preview the next ${step.toLocaleString()} words`;
  seedCapNote.textContent = atCap
    ? `That's the top ${SEED_MAX.toLocaleString()} — the practical ceiling for a generic ` +
      "frequency list. From here, mark words off the review list below, or import a list."
    : "";
}

seedPreviewBtn.addEventListener("click", async () => {
  const step = SEED_STEP[activeLang] || 1000;
  previewTopN = Math.min(seededTopN + step, SEED_MAX);
  seedPreviewBtn.disabled = true;
  showStatus(seedIncrementStatus, "pending", "Loading…");
  try {
    const resp = await fetch(`/api/known-words/seed/preview?top_n=${previewTopN}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    showStatus(seedIncrementStatus, "ok", "");
    seedPreviewWords.innerHTML = data.words.length
      ? data.words.map((w) => `<span>${esc(w)}</span>`).join("")
      : `<span class="freq-empty">No new words in this range — already known.</span>`;
    seedApplyBtn.textContent = data.words.length
      ? `Add ${data.words.length.toLocaleString()} word${data.words.length === 1 ? "" : "s"}`
      : "Add these words";
    seedApplyBtn.disabled = !data.words.length;
    seedPreviewBox.hidden = false;
  } catch (err) {
    showStatus(seedIncrementStatus, "err", `Couldn't load preview: ${esc(err.message)}`);
  } finally {
    seedPreviewBtn.disabled = false;
  }
});

seedPreviewCancel.addEventListener("click", () => {
  seedPreviewBox.hidden = true;
  showStatus(seedIncrementStatus, "", "");
});

seedApplyBtn.addEventListener("click", async () => {
  seedApplyBtn.disabled = true;
  showStatus(seedIncrementStatus, "pending", "Adding and recomputing…");
  try {
    const resp = await fetch("/api/known-words/seed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ top_n: previewTopN }),
    });
    const data = await resp.json();
    if (resp.ok && data.status === "ok") {
      showStatus(
        seedIncrementStatus,
        "ok",
        `List now ${data.total.toLocaleString()} words (seeded through the top ` +
          `${data.seeded_top_n.toLocaleString()}); Known % updated on ${data.books_updated} books.`,
      );
      seedPreviewBox.hidden = true;
    } else {
      showStatus(seedIncrementStatus, "err", esc(data.detail || data.message || "Unknown error"));
    }
  } catch (err) {
    showStatus(seedIncrementStatus, "err", `Request failed: ${esc(err.message)}`);
  } finally {
    seedApplyBtn.disabled = false;
    refreshSummary();
    loadFrequent();
  }
});

// ── Drop zone + paste ─────────────────────────────────────────────────────────

const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const chosenName = document.getElementById("chosen-name");
const pasteInput = document.getElementById("paste-input");
const importBtn = document.getElementById("import-btn");

function updateImportBtn() {
  importBtn.disabled = !(fileInput.files[0] || pasteInput.value.trim());
}

// The zone is a <label for="file-input">, so a click, Enter or Space opens the picker
// natively; only the drag-and-drop path needs a handler.
const showChosen = (f) => { chosenName.textContent = f ? `Chosen: ${f.name}` : ""; };
fileInput.addEventListener("change", () => {
  showChosen(fileInput.files[0]);
  updateImportBtn();
});
pasteInput.addEventListener("input", updateImportBtn);

dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  const f = e.dataTransfer.files[0];
  if (f) {
    const dt = new DataTransfer();
    dt.items.add(f);
    fileInput.files = dt.files;
    showChosen(f);
    updateImportBtn();
  }
});

// ── Import submit ─────────────────────────────────────────────────────────────
// Merge is the default. A Replace swaps out everything but the hand-marked words,
// so it confirms first, and its result offers Undo: the server names the copy it
// saved before replacing, and restoring that copy is itself undoable.

const importStatus = document.getElementById("import-status");
const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
const readNextLink = () => `<a href="/next">see Read Next ${icon("arrow_forward")}</a>`;
const booksUpdated = (n) => `Strain and Known % updated on ${plural(n, "book", "books")}.`;

function replaceWarning(file) {
  const pasted = pasteInput.value.split("\n").filter((l) => l.trim()).length;
  const what = file ? `“${file.name}”` : `the ${plural(pasted, "line", "lines")} you pasted`;
  const kept = knownMarked
    ? `Your ${plural(knownMarked, "hand-marked word is", "hand-marked words are")} kept, and the`
    : "The";
  return {
    title: "Replace your known-words list?",
    message:
      `Replace your ${plural(knownCount, "word", "words")} with ${what}? ${kept} ` +
      "current list is saved under “Restore an earlier list”.",
    confirmLabel: "Replace list",
    danger: true,
  };
}

document.getElementById("import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const format = document.querySelector('input[name="format"]:checked').value;
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const f = fileInput.files[0];
  // Replacing an empty list loses nothing, so only a list with words in it asks first.
  if (mode === "replace" && knownCount && !(await confirmDialog(replaceWarning(f)))) return;

  importBtn.disabled = true;
  showStatus(importStatus, "pending",
    mode === "replace" ? "Replacing and recomputing…" : "Importing and recomputing…");

  const body = new FormData();
  if (f) body.append("file", f);
  else body.append("text", pasteInput.value);
  body.append("format", format);
  body.append("mode", mode);

  try {
    const resp = await fetch("/api/known-words", { method: "POST", body });
    const data = await resp.json();
    if (resp.ok && data.status === "ok") {
      let said;
      if (mode === "replace") {
        const marked = data.marked
          ? `, including your ${plural(data.marked, "hand-marked one", "hand-marked ones")}` : "";
        said = `Replaced your list: ${plural(data.total, "word", "words")} now${marked}.`;
      } else {
        said = `Merged ${plural(data.imported, "word", "words")} into your list ` +
          `(${data.total.toLocaleString()} now).`;
      }
      const undo = data.snapshot
        ? `<button type="button" class="link-btn" data-undo="${esc(data.snapshot)}">Undo</button> · ` : "";
      showStatus(importStatus, "ok",
        `${said} ${booksUpdated(data.books_updated)}` +
        `<span class="status-actions">${undo}${readNextLink()}</span>`);
      fileInput.value = "";
      chosenName.textContent = "";
      pasteInput.value = "";
    } else {
      showStatus(importStatus, "err", esc(data.detail || data.message || "Unknown error"));
    }
  } catch (err) {
    showStatus(importStatus, "err", `Request failed: ${esc(err.message)}`);
  } finally {
    updateImportBtn();
    refreshSummary();
    loadFrequent();
  }
});

importStatus.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-undo]");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Undoing…";
  try {
    const data = await postJson("/api/known-words/restore", { id: btn.dataset.undo });
    showStatus(importStatus, "ok",
      `Undone: your list is back to ${plural(data.total, "word", "words")}. ` +
      `${booksUpdated(data.books_updated)}<span class="status-actions">${readNextLink()}</span>`);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Undo";
    showStatus(importStatus, "err",
      `Couldn't undo: ${esc(err.message)}. The old list is still under “Restore an earlier list”.`);
  } finally {
    refreshSummary();
    loadFrequent();
  }
});

// ── Review frequent words ─────────────────────────────────────────────────────
// The gaps in the list that cost the most: words used across the widest range
// of books. Marking them off here corrects every book at once, where the study
// list on a book page only corrects that book.

// Rows per page. The full list is tens of thousands of types; rendering and
// shipping it all made the card slow to appear, so it loads a page at a time
// (100 by default; 250 and 500 for a long triage session, remembered per browser),
// and "Show more" appends the next page under the rows already there.
const FREQ_PAGE_SIZES = [100, 250, 500];
const freqPageSelect = document.getElementById("freq-page-size");
let freqPage = (() => {
  try {
    const n = Number(localStorage.getItem("known-freq-page"));
    return FREQ_PAGE_SIZES.includes(n) ? n : FREQ_PAGE_SIZES[0];
  } catch (e) { return FREQ_PAGE_SIZES[0]; }
})();
freqPageSelect.value = String(freqPage);
const freqWrap = document.getElementById("freq-wrap");
const freqNote = document.getElementById("freq-note");
const freqSub = document.getElementById("freq-sub");

// "How this works" is open the first time, then stays closed once the reader has closed it.
const freqHowto = document.getElementById("freq-howto");
try { if (localStorage.getItem("known-howto-closed") === "1") freqHowto.open = false; } catch (e) { /* private mode */ }
freqHowto.addEventListener("toggle", () => {
  try { localStorage.setItem("known-howto-closed", freqHowto.open ? "0" : "1"); } catch (e) { /* private mode */ }
});
const freqKeys = document.getElementById("freq-keys");
const freqSkip = document.getElementById("freq-skip");
const freqBulk = document.getElementById("freq-bulk");

// The UniDic reading is katakana; a learner reads furigana. ー passes through.
const toHira = (s) =>
  (s || "").replace(/[ァ-ヶ]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60));

function freqRow(w, lang) {
  // English wordlists carry no reading (analyze._analyze_text_en leaves it blank), so
  // the column is dropped for "en" instead of rendering an always-empty cell.
  const hasReading = lang !== "en";
  // In hiragana, and left out when the word already spells it (より, トンネル).
  const reading = hasReading && w.reading && toHira(w.reading) !== toHira(w.word)
    ? toHira(w.reading) : "";
  // Spellings of one word share a row; both buttons act on them all. A kana spelling
  // that is only the reading again (当たる / あたる) is said once, by the reading.
  const variants = w.variants && w.variants.length > 1 ? w.variants : null;
  const attrs = variants ? ` data-words="${variants.map(esc).join("&#9;")}"` : "";
  const alts = variants
    ? variants.slice(1).filter((v) => !w.reading || toHira(v) !== toHira(w.reading)) : [];
  const alt = alts.length
    ? ` <span class="freq-alt"${langAttr(alts[0])}>${alts.map(esc).join(" · ")}</span>` : "";
  // On a narrow card the number columns drop out and the reading and reach ride
  // under the word instead, so both buttons stay on screen.
  const books = `in ${w.books.toLocaleString()} ${w.books === 1 ? "book" : "books"}`;
  const meta = [reading, books].filter(Boolean).join(" · ");
  return `<tr data-word="${esc(w.word)}"${attrs}>
    <td class="freq-word"><span class="freq-w"${langAttr(w.word)}>${esc(w.word)}</span>${alt}` +
      `<span class="freq-meta"${langAttr(reading)}>${esc(meta)}</span></td>
    ${hasReading ? `<td class="freq-reading" lang="ja">${esc(reading)}</td>` : ""}
    <td class="freq-n freq-books">${w.books.toLocaleString()}</td>
    <td class="freq-n freq-uses">${w.total.toLocaleString()}</td>
    <td class="freq-mark">${skipButton(w.word)}</td>
    <td class="freq-mark freq-markcol">${markButton(w.word)}</td>
  </tr>`;
}

// Filter chips: "all" or "loan" (UniDic goshu 外 — katakana loanwords, a bulk
// of which are words the reader knows from English). Filtered server-side
// (`loan=1`) before paging, so low-reach loanwords (names included) are
// reachable — filtering one page of the overall list would hide them.
let freqData = null;
let freqFilter = "all";
const freqFilters = document.getElementById("freq-filters");

function renderFrequent() {
  const d = freqData;
  const readingHeader = activeLang === "en" ? "" : `<th class="freq-reading">Reading</th>`;
  freqWrap.innerHTML = d.words.length
    ? `<table class="freq-table" aria-labelledby="freq-h">
        <thead><tr>
          <th>Word</th>${readingHeader}
          <th class="freq-n freq-books" title="Books that use it, of ${d.books_scanned} analyzed">Books</th>
          <th class="freq-n freq-uses" title="Times it appears across those books">Uses</th>
          <th class="freq-mark"><span class="freq-h-text">Not a word</span></th>
          <th class="freq-mark freq-markcol"><span class="freq-h-text">I know it</span></th>
        </tr></thead>
        <tbody>${d.words.map((w) => freqRow(w, activeLang)).join("")}</tbody>
      </table>
      <div id="freq-after" tabindex="-1"></div>`
    : `<div class="freq-empty">No loanwords in this list.</div>`;
  renderMore();
  setCurrentRow(freqWrap.querySelector("tbody tr"));
  freqKeys.hidden = freqSkip.hidden = !d.words.length;
  freqFilters.hidden = activeLang === "en";
  freqFilters.querySelectorAll("button").forEach((b) => {
    const on = b.dataset.filter === freqFilter;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  // Re-apply marked and set-aside state to the rebuilt rows.
  markers.forEach((m) => m.sync());
  updateBulk();
}

// The "Show more" button and the count under the table. `failed` keeps the rows
// already shown and turns the button into the retry.
function renderMore(failed = false) {
  const d = freqData;
  let btn = document.getElementById("freq-more");
  if (d.words.length && d.has_more) {
    if (!btn) {
      freqWrap.insertAdjacentHTML("beforeend",
        `<button type="button" class="ghost-btn" id="freq-more"></button>`);
      btn = document.getElementById("freq-more");
    }
    btn.disabled = false;
    btn.textContent = failed ? "Couldn't load more · Try again" : "Show more";
  } else if (btn) {
    btn.remove();
  }
  paintCounts();
  freqNote.textContent = noteText();
  updateBulk();
}

// How many "of N" the list still has. The server's total drops words once their save lands,
// but the rows stay put (struck), so the page takes off the rows ticked since that total was
// read: `freqBaseStruck` is how many were already ticked then.
let freqBaseStruck = 0;
const struckRows = () => freqWrap.querySelectorAll("tbody tr.marked, tbody tr.skipped").length;
function freqTotals() {
  const d = freqData;
  const total = freqFilter === "loan" ? d.loan_types : d.unknown_types;
  return {
    total: Math.max(0, total - Math.max(0, struckRows() - freqBaseStruck)),
    noun: freqFilter === "loan" ? "loanwords" : "unknown words",
  };
}

function noteText() {
  const d = freqData;
  const { total, noun } = freqTotals();
  let text = `Showing ${d.words.length.toLocaleString()} of ${total.toLocaleString()} ${noun} ` +
    `across ${d.books_scanned} analyzed books.`;
  // The end of the list is a moment, not just a missing button.
  if (d.words.length && !d.has_more) {
    const n = markers.reduce((sum, m) => sum + m.count(), 0);
    text += ` That's the end of the list${n ? `: ${n.toLocaleString()} reviewed this session` : ""}.`;
  }
  return text;
}

// The subhead moves with every tick. The note under the table is a live region, so it follows
// after the ticking pauses: one announcement per run of clicks, not one per word.
let noteTimer = null;
function paintCounts() {
  if (!freqData) return;
  const { total, noun } = freqTotals();
  freqSub.textContent = freqData.words.length
    ? `Top ${freqData.words.length.toLocaleString()} of ${total.toLocaleString()} ${noun}, widest reach first.`
    : "";
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { if (freqData) freqNote.textContent = noteText(); }, 1200);
}

freqFilters.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-filter]");
  if (!b || !freqData) return;
  if (b.dataset.filter === freqFilter) return;
  freqFilter = b.dataset.filter;
  loadFrequent();
});

freqPageSelect.addEventListener("change", () => {
  freqPage = Number(freqPageSelect.value);
  try { localStorage.setItem("known-freq-page", String(freqPage)); } catch (e) { /* private mode */ }
  loadFrequent();
});

freqWrap.addEventListener("click", (e) => {
  if (e.target.closest("#freq-more")) loadFrequent(true);
  else if (e.target.closest("#freq-retry")) loadFrequent();
});

// `more` appends the page ranked below the last row shown. The cursor is that
// row's rank key rather than an offset: marking words known drops them from
// the server's list, which would shift an offset and skip rows.
async function loadFrequent(more = false) {
  const moreBtn = document.getElementById("freq-more");
  try {
    const params = new URLSearchParams({ n: freqPage });
    if (freqFilter === "loan") params.set("loan", "1");
    if (more && freqData && freqData.words.length) {
      const last = freqData.words[freqData.words.length - 1];
      params.set("after_books", last.books);
      params.set("after_total", last.total);
      params.set("after_word", last.word);
    }
    if (moreBtn) {
      moreBtn.disabled = true;
      if (more) moreBtn.textContent = "Loading…";
    }
    // Fired in parallel with the profile fetch — only the render step below
    // needs activeLang, not the request itself.
    const freqPromise = fetch(`/api/known-words/frequent?${params}`);
    await profileReady;
    const resp = await freqPromise;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();

    if (!d.words.length && !more && freqFilter === "all") {
      freqNote.textContent = "";
      freqKeys.hidden = freqSkip.hidden = freqBulk.hidden = true;
      freqWrap.innerHTML = `<div class="freq-empty">${
        d.status === "ok"
          ? "Nothing to review — every word in your library is already in your list."
          : "No analyzed books yet. Run the <em>Analyze vocab</em> stage from the Admin page."
      }</div>`;
      return;
    }

    const tbody = more && freqData && freqWrap.querySelector("tbody");
    if (tbody) {
      // Under the rows already shown, not a rebuild: ticked rows and the reader's
      // place on the page stay as they are.
      freqBaseStruck = struckRows();   // the new total already leaves out the ticks that saved
      freqData = { ...d, words: freqData.words.concat(d.words) };
      tbody.insertAdjacentHTML("beforeend", d.words.map((w) => freqRow(w, activeLang)).join(""));
      renderMore();
      markers.forEach((m) => m.sync());
      updateBulk();
      return;
    }
    freqData = d;
    freqBaseStruck = 0;
    renderFrequent();
  } catch (err) {
    if (more && freqData) {
      renderMore(true);
      return;
    }
    freqNote.textContent = "";
    freqKeys.hidden = freqSkip.hidden = freqBulk.hidden = true;
    freqWrap.innerHTML =
      `<div class="freq-empty">Couldn't load the list: ${esc(err.message)}. ` +
      `<button type="button" class="link-btn" id="freq-retry">Try again</button></div>`;
  }
}

// Delegated on the card, so they survive the table being swapped in. A save's reply
// carries the list's new total (mark/unmark), so the tally can say it before
// refreshSummary's own fetch returns.
const freqCard = document.getElementById("freq-card");
let lastMoved = { kind: "", n: 0 };
const onFreqSaved = (kind) => (last) => {
  if (last && typeof last.total === "number") knownCount = last.total;
  lastMoved = { kind, n: (last && last.books_moved) || 0 };
  // Both tallies redraw (once this save has settled) so the note moves to the one that saved.
  setTimeout(() => markers.forEach((m) => m.sync()), 0);
  refreshSummary();
};
// What ticking did: the reward is that books' numbers moved, so say how many. Said once, on
// the tally of whichever button saved last, so the pill doesn't repeat it.
const movedNote = (kind) => {
  const n = lastMoved.kind === kind ? lastMoved.n : 0;
  return n ? `Known % moved on ${n.toLocaleString()} ${n === 1 ? "book" : "books"}` : "";
};
const markers = [
  createMarker({
    container: freqCard,
    status: document.getElementById("freq-mark-status"),
    detail: () => [`${knownCount.toLocaleString()} on your list`, movedNote("known")].filter(Boolean).join(" · "),
    onSaved: onFreqSaved("known"),
  }),
  createMarker({
    container: freqCard,
    kind: "skip",
    status: document.getElementById("freq-skip-status"),
    detail: () => movedNote("skip"),
    onSaved: onFreqSaved("skip"),
  }),
];

// ── Fast path: keyboard, bulk, undo ───────────────────────────────────────────
// One word per click is a long evening at 33,000 words, so the list is also a
// keyboard surface. Focus is roving: exactly one row (and its two buttons) is a Tab
// stop, so a hundred rows cost one Tab, not two hundred; ↑/↓ move between rows,
// k ticks "I know this word", n sets it aside as not a word, u takes back the last
// action. Keys act on the row that has focus and then move on to the next one.
const [knownMarker, skipMarker] = markers;
const undoStack = [];   // {marker, rows, on}: what u reverses, newest last; rows = lists of spellings
const rowWords = (row) => (row.dataset.words ? row.dataset.words.split("\t") : [row.dataset.word]);
const freqRows = () => [...freqWrap.querySelectorAll("tbody tr[data-word]")];

function setCurrentRow(row) {
  freqRows().forEach((r) => {
    const on = r === row;
    r.tabIndex = on ? 0 : -1;
    r.querySelectorAll("button").forEach((b) => { b.tabIndex = on ? 0 : -1; });
  });
}

function focusRow(row) {
  if (!row) return;
  setCurrentRow(row);
  row.focus();
  row.scrollIntoView({ block: "nearest" });
}

// "Mark these N as known…": the loanword rows on screen that aren't yet ticked or
// set aside. Shown only on the Loanwords filter, where most of the list is words
// the reader knows from English.
function bulkRows() {
  return freqRows().filter((r) => !r.classList.contains("marked") && !r.classList.contains("skipped"));
}

function updateBulk() {
  paintCounts();
  const n = freqFilter === "loan" && freqData ? bulkRows().length : 0;
  freqBulk.hidden = !n;
  freqBulk.textContent = `Mark these ${n.toLocaleString()} as known…`;
}

freqBulk.addEventListener("click", async () => {
  const rows = bulkRows();
  if (!rows.length) return;
  const groups = rows.map(rowWords);
  const ok = await confirmDialog({
    title: `Mark ${rows.length.toLocaleString()} loanwords as known?`,
    message: `Every loanword shown below that you haven't ticked or set aside will go on your ` +
      `known list. Names and fragments belong under “not a word” instead: skim the rows first. ` +
      `You can undo this right after with U, or untick words one by one.`,
    confirmLabel: `Mark ${rows.length.toLocaleString()} as known`,
  });
  if (!ok) return;
  knownMarker.setMany(groups, true);
  undoStack.push({ marker: knownMarker, rows: groups, on: true });
  updateBulk();
});

freqWrap.addEventListener("keydown", (e) => {
  const row = e.target.closest("tbody tr[data-word]");
  if (!row || e.ctrlKey || e.metaKey || e.altKey) return;
  const rows = freqRows();
  const i = rows.indexOf(row);
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (key === "ArrowDown" || key === "ArrowUp") {
    e.preventDefault();
    focusRow(rows[Math.max(0, Math.min(rows.length - 1, i + (key === "ArrowDown" ? 1 : -1)))]);
  } else if (key === "Home" || key === "End") {
    e.preventDefault();
    focusRow(key === "Home" ? rows[0] : rows[rows.length - 1]);
  } else if (key === "k" || key === "n") {
    e.preventDefault();
    const marker = key === "k" ? knownMarker : skipMarker;
    const words = rowWords(row);
    const on = !row.classList.contains(key === "k" ? "marked" : "skipped");
    marker.set(words, on);
    undoStack.push({ marker, rows: [words], on });
    updateBulk();
    focusRow(rows[i + 1] || row);
  } else if (key === "u") {
    e.preventDefault();
    const last = undoStack.pop();
    if (!last) return;
    last.marker.setMany(last.rows, !last.on);
    updateBulk();
    focusRow(rows.find((r) => rowWords(r)[0] === last.rows[0][0]) || row);
  }
});

// A click on a row's own buttons moves the keyboard's place there too, and keeps the
// bulk count honest (this runs after the markers' own click handlers on the card).
freqCard.addEventListener("click", (e) => {
  const row = e.target.closest("tbody tr[data-word]");
  if (row) setCurrentRow(row);
  updateBulk();
});

// The skip link lands past the table (on #freq-after), so the next Tab is "Show more".
freqSkip.addEventListener("click", (e) => {
  const after = document.getElementById("freq-after");
  if (!after) return;
  e.preventDefault();
  after.focus();
});

refreshSummary();
loadFrequent();
