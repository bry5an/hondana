// Book detail page: /book/{id} — vocab stats, download, series comparison.
"use strict";

const bookId = location.pathname.split("/").filter(Boolean).pop();
const wrap = document.getElementById("book-wrap");


function natLevel(b) {
  return b.natively && b.natively.natively_level != null ? b.natively.natively_level : null;
}

// Natively's level and learner rating describe a Japanese book for a Japanese learner;
// on a book outside the profile's language they mean nothing, so the chips go.
function levelChip(b) {
  if (b.language !== profileLang) return "";
  const nat = b.natively;
  const lvl = natLevel(b);
  if (lvl == null) return "";
  const unconfirmed = levelUnconfirmed(nat);
  const title = levelTitle(lvl, nat);
  const label = `Natively Lv ${unconfirmed ? "~" : ""}${lvl}`;
  const cls = `chip level${unconfirmed ? " unconfirmed" : ""}`;
  return nat.natively_url
    ? `<a class="${cls}" href="${esc(nat.natively_url)}" target="_blank" rel="noopener" title="${esc(title)}">${label}${icon("open_in_new")}</a>`
    : `<span class="${cls}" title="${esc(title)}">${label}</span>`;
}

function chipRow(b) {
  const chips = [];
  chips.push(levelChip(b));
  if (b.language === profileLang && b.natively && b.natively.language_learning_rating != null)
    chips.push(`<span class="chip" title="Natively language-learning rating, out of 5">Learner rating ${b.natively.language_learning_rating}</span>`);
  // The other edition is the main action on a book outside the profile's language
  // (see languageNoteHtml); everywhere else it stays a chip.
  if (b.pair_partner && !editionAction(b))
    chips.push(
      `<a class="chip pair" id="edition-link" href="/book/${esc(b.pair_partner.book_id)}" ` +
      `title="${esc(b.pair_partner.title || "")}">` +
      `${b.pair_partner.language === "ja" ? "JP" : "EN"} edition${icon("arrow_forward")}</a>`
    );
  return chips.join("");
}

// Language the active profile is learning; set in load(). "ja" until then, and
// if /api/profiles fails, which is what the toggle was hard-coded to before.
let profileLang = "ja";

const LANG_NAMES = { ja: "Japanese", en: "English" };
// The partner's reading-load record when it is in the profile's language (fetched
// in load(); only that edition has one) — lets the edition action carry a verdict.
let partnerBook = null;
// The book the hero shows; the audiobook caption compares the catalog entry against it.
let heroBook = null;

// This book is in a language the active profile isn't learning, and its other
// edition is: the one place the page can still say how readable "this" book is.
function editionAction(b) {
  const p = b.pair_partner;
  return b.language !== profileLang && p && p.language === profileLang ? p : null;
}

// A series name this book borrowed from its other edition (`series_source: "pair"`) in a language
// the profile isn't learning: an English book carrying "コーヒーが冷めないうちに", linking to a series page
// of Japanese titles. The book has no series of its own, so it shows none.
function foreignSeries(b) {
  return b.series_source === "pair" && !!b.pair_partner && b.pair_partner.language !== profileLang;
}

function actionRow(b) {
  // Always a slot (PRODUCT principle 2): M4B, Audible, "<source> only" or "No audiobook".
  const actions = [dlEpub(b.book_id), dlAudio(bookAudio(b), { placeholder: true, source: true, plain: true, searchTitle: b.title })];
  // Offer "read" for any analyzed book in the profile's own language — an
  // English profile builds exposed vocabulary from English books. Not gated
  // on b.known: that's absent until a known-words list exists, which would
  // block marking the very first book read. Where it's missing, say why.
  if (b.language === profileLang && b.vocab && b.vocab.status === "ok")
    actions.push(readToggle(b));
  else if (b.language === profileLang)
    actions.push(`<span class="read-hint">Mark as read is available once the book is analyzed.</span>`);
  // A book in another language: the note standing in for the readout says why (languageNoteHtml).
  return actions.join("");
}

// ── audiobook link ──────────────────────────────────────────────────────────
// xref picks each EPUB's audiobook by fuzzy title match. Correcting it (pin a
// catalog entry, say there is none, or go back to automatic) is curation, so it
// lives in the Edit dialog, not in the page you browse. Japanese books
// only — the catalog is Japanese. The hero's caption (renderAbCaption) is what
// the page itself shows of the link.

// A reload drops focus and, on desktop where .main-col scrolls, the scroll position.
// Remember both and put them back once the page has drawn (restorePlace, in load()).
const PLACE_KEY = `book-place:${bookId}`;
//
// `notice` ({message, undo: {url, body, method, done}}) is what to say once the page is back,
// as plain data because a toast can't survive the reload: the other edition's link or "paired
// with X instead", with the Undo that reverses it.
function reloadKeepingPlace(focusId, notice) {
  try {
    const col = document.querySelector(".main-col");
    sessionStorage.setItem(PLACE_KEY, JSON.stringify({ y: window.scrollY, col: col ? col.scrollTop : 0, focusId, notice }));
  } catch { /* private mode: reload anyway */ }
  location.reload();
}

// The notice a reload carried (see reloadKeepingPlace), once, with its Undo if it had one.
function showReloadNotice(n) {
  if (!n || !n.message) return;
  const u = n.undo;
  // Longer than the usual 8 s: this one is read after a page load, and "Paired with X instead"
  // is a surprise worth a moment to take in before the Undo disappears.
  toast(n.message, "ok", u && {
    anchor: document.getElementById("edition-well"),
    duration: 15000,
    action: {
      label: "Undo",
      busyLabel: "Undoing…",
      run: async () => {
        await edUndoCall(u.method || "POST", u.url, u.body);
        reloadKeepingPlace("edition-link", { message: u.done || "Undone." });
      },
    },
  });
}
let savedPlace = null;
function restorePlace() {
  if (savedPlace === null) {
    try {
      savedPlace = JSON.parse(sessionStorage.getItem(PLACE_KEY)) || false;
      sessionStorage.removeItem(PLACE_KEY);
    } catch { savedPlace = false; }
  }
  if (!savedPlace) return;
  if (savedPlace.notice) { const n = savedPlace.notice; savedPlace.notice = null; showReloadNotice(n); }
  const col = document.querySelector(".main-col");
  window.scrollTo(0, savedPlace.y || 0);
  if (col) col.scrollTop = savedPlace.col || 0;
  const el = savedPlace.focusId && document.getElementById(savedPlace.focusId);
  if (el) el.focus({ preventScroll: true });
  // The paired chip is gone after an Undo that unpairs: land on the well's question again
  // (or the downloads), not <body>.
  else if (savedPlace.focusId === "edition-link") edFocusAfter();
}

// Set once the link changes in the dialog: the hero's M4B / Audible button and
// the series volume derive from it, so closing the dialog reloads the page.
let abChanged = false;

function audiobookSectionHtml() {
  return `<section class="bd-audiobook" aria-labelledby="ab-title">
      <h4 class="bd-section-title" id="ab-title">Audiobook</h4>
      <p class="ab-apply">Applies immediately. It isn't part of Save changes.</p>
      <div id="ab-body" class="ab-line">Loading…</div>
    </section>`;
}

// The hero's one-line caption under the files: which catalog entry the slot
// stands for, so the reader needn't scroll to the Audiobook card to see it.
// Only what the slot's own title doesn't already say: a catalog title or volume that
// differs from this book's, or that the link was set by hand. A caption that repeated the
// book's title was noise.
function abCaption(st) {
  if (st.mode === "none") return "No audiobook · set by you";
  if (!(st.link && st.catalog)) return "";
  const c = st.catalog;
  const same = (x, y) => norm(x) === norm(y);
  const parts = [];
  if (!same(c.title, heroBook && heroBook.title)) parts.push(esc(c.title || "(untitled)"));
  if (c.order && !same(c.order, heroBook && heroBook.series_order) &&
      !(Number(c.order) && Number(c.order) === Number(heroBook && heroBook.series_order)))
    parts.push(`Vol ${esc(c.order)}`);
  if (st.mode === "manual") parts.push("set by you");
  return parts.join(" · ");
}
const norm = (t) => String(t ?? "").normalize("NFKC").replace(/\s+/g, "").toLowerCase();

// "Synced audio" under the downloads: only for a book whose audio the app will follow
// (`audio_synced`, or the sync card's fresh, well-matched map); an unsynced audiobook shows nothing.
// audio_sync.js calls this as the card's state changes.
function renderSyncRow(synced) {
  const el = document.getElementById("sync-row");
  if (!el || el.hidden === !synced) return;
  el.hidden = !synced;
  el.innerHTML = synced
    ? `${syncCapsuleHtml()}<span class="sync-row-text"><strong>Synced audio</strong>` +
      `The reader follows the audio and turns the page with it. Tap a word to play from there.</span>`
    : "";
}

function renderAbCaption(st) {
  const el = document.getElementById("ab-caption");
  if (!el) return;
  const text = abCaption(st);
  el.innerHTML = text ? `${icon("headphones")}<span${langAttr(text)}>${text}</span>` : "";
  el.hidden = !text;
}

function abMeta(c) {
  return [c.author, c.series && c.order ? `${c.series} · Vol ${c.order}` : c.series]
    .filter(Boolean).map(esc).join(" · ");
}

function renderAudiobook(st) {
  const body = document.getElementById("ab-body");
  if (!body) return;
  const linked = !!(st.link && st.catalog);
  let current;
  if (st.mode === "none") {
    current = `<div>No audiobook — you marked this book as having none.</div>`;
  } else if (linked) {
    const how = st.mode === "manual"
      ? "Set by you"
      : `<span title="Title similarity ${Math.round(st.link.match_score * 100)}%">Matched automatically from the title</span>`;
    current = `<div><strong>${esc(st.catalog.title || "(untitled)")}</strong></div>
      <div class="ab-meta">${abMeta(st.catalog)}${abMeta(st.catalog) ? " · " : ""}${how}</div>`;
  } else {
    current = `<div>No audiobook found for this book.</div>`;
  }
  body.innerHTML = `${current}
    <div class="ab-actions">
      <button type="button" class="ab-btn" id="ab-change" aria-expanded="false" aria-controls="ab-picker">${linked ? "Wrong audiobook? Change…" : "Link an audiobook…"}</button>
      ${st.mode !== "none" && st.link ? `<button type="button" class="ab-btn" id="ab-none">No audiobook</button>` : ""}
      ${st.mode !== "auto" ? `<button type="button" class="ab-btn" id="ab-reset">Back to automatic</button>` : ""}
    </div>
    <div id="ab-picker"></div>
    <div class="ab-note" id="ab-note" role="status"></div>`;
  document.getElementById("ab-change").addEventListener("click", () => openAbPicker(st));
  const none = document.getElementById("ab-none");
  if (none) none.addEventListener("click", () => saveAbLink(none, { method: "POST", body: { isbn: null } }, st));
  const reset = document.getElementById("ab-reset");
  if (reset) reset.addEventListener("click", () => saveAbLink(reset, { method: "DELETE" }, st));
}

function openAbPicker(st) {
  const picker = document.getElementById("ab-picker");
  const toggle = document.getElementById("ab-change");
  if (picker.firstChild) { picker.innerHTML = ""; toggle.setAttribute("aria-expanded", "false"); return; }
  toggle.setAttribute("aria-expanded", "true");
  const currentIsbn = st.link ? st.link.audible_isbn : null;
  picker.innerHTML = `<div class="ab-picker">
      <input type="search" id="ab-q" aria-label="Search the audiobook catalog" placeholder="Search the audiobook catalog by title, author or series" autocomplete="off">
      <ul class="ab-results" id="ab-results"></ul>
      <div class="sr-only" role="status" id="ab-count"></div></div>`;
  const q = document.getElementById("ab-q");
  const list = document.getElementById("ab-results");
  const count = document.getElementById("ab-count");
  let timer = 0, seq = 0;
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const text = q.value.trim();
      const mine = ++seq;
      if (!text) { list.innerHTML = ""; count.textContent = ""; return; }
      try {
        const r = await fetch(`/api/audiobook-catalog/search?q=${encodeURIComponent(text)}`);
        if (!r.ok || mine !== seq) return;
        const { results } = await r.json();
        list.innerHTML = results.length
          ? results.map((c) => {
              const tags = [c.isbn === currentIsbn ? "Linked now" : "", c.local ? "On this device" : ""]
                .filter(Boolean).map((t) => `<span class="ab-tag">${t}</span>`).join("");
              return `<li><button type="button" data-isbn="${esc(c.isbn)}"${c.isbn === currentIsbn ? " disabled" : ""}>
                <span${langAttr(c.title)}>${esc(c.title || "(untitled)")}</span>${tags}<div class="ab-meta"${langAttr(abMeta(c))}>${abMeta(c)}</div></button></li>`;
            }).join("")
          : `<li class="ab-note">No matches.</li>`;
        count.textContent = results.length ? `${results.length} ${results.length === 1 ? "match" : "matches"}` : "No matches";
      } catch { /* leave the previous results */ }
    }, 250);
  });
  list.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-isbn]");
    if (btn) saveAbLink(btn, { method: "POST", body: { isbn: btn.dataset.isbn } }, st);
  });
  q.focus();
}

// What puts the link back as `st` showed it: automatic, "no audiobook", or the hand-picked entry.
function abRestore(st) {
  if (st.mode === "none") return { method: "POST", body: { isbn: null } };
  if (st.mode === "manual" && st.link) return { method: "POST", body: { isbn: st.link.audible_isbn } };
  return { method: "DELETE" };
}

// `prev` is the state this change replaces; it powers the inline Undo. An undo passes none.
async function saveAbLink(btn, { method, body }, prev = null) {
  const note = document.getElementById("ab-note");
  btn.disabled = true;
  note.textContent = "Saving…";
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/audiobook-link`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error(await r.text());
    // Update in place so unsaved edits in the form above survive; the reload the
    // new link needs for the hero and series volume waits for the dialog to close.
    const st = await r.json();
    abChanged = true;
    renderAbCaption(st);
    renderAudiobook(st);
    const out = document.getElementById("ab-note");
    out.textContent = prev ? "Saved · " : "Undone";
    if (prev) {
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "bd-reset";
      undo.id = "ab-undo";
      undo.textContent = "Undo";
      undo.addEventListener("click", () => saveAbLink(undo, abRestore(prev)));
      out.append(undo);
    }
  } catch (e) {
    btn.disabled = false;
    note.textContent = "Couldn't save — try again.";
  }
}

function meter(value) {
  return `<span class="mini-meter"><i style="width:${Math.max(0, Math.min(100, value))}%"></i></span>`;
}

// ── reading load ────────────────────────────────────────────────────────────
// How the book reads *for this reader*, as opposed to the frequency-corpus
// difficulty above it: what share of sentences land with nothing unknown, one
// unknown (learnable from context), or two or more (where you stop and
// reassemble the sentence).

// What stands in for the readout on a book outside the profile's language: the
// reason there isn't one, and the other edition as a real button with its verdict.
function languageNoteHtml(b) {
  const own = LANG_NAMES[b.language] ? `This is ${b.language === "en" ? "an" : "a"} ${LANG_NAMES[b.language]} book. ` : "";
  const p = editionAction(b);
  let action = "";
  if (p) {
    const k = partnerBook && partnerBook.known;
    const verdict = k && k.hard_pct != null ? `: ${strainBucket(k.hard_pct).short} strain` : "";
    action = `<a class="dl-btn dl-btn--audio lang-action" id="edition-link" href="/book/${esc(p.book_id)}" title="${esc(p.title || "")}">` +
      `${p.language === "ja" ? "JP" : "EN"} edition${verdict}${icon("arrow_forward")}</a>`;
  }
  // No other edition to go to: say where to find a book that does have a verdict.
  if (!action)
    action = `<a class="dl-btn dl-btn--audio lang-action" href="/next">Find ${LANG_NAMES[profileLang] || "your language's"} books in Read Next${icon("arrow_forward")}</a>`;
  return `
    <div class="readout lang-note" id="load-card">
      <div class="readout-label">READING LOAD</div>
      <p class="lang-note-text">${own}Reading load and Mark as read are for ${LANG_NAMES[profileLang] || "your language's"} books on this profile.</p>
      ${action}
    </div>`;
}

function loadHtml(b) {
  const k = b.known;
  if (!k && b.language !== profileLang) return languageNoteHtml(b);
  if (!k || k.hard_pct == null) return "";
  return loadReadoutHtml(k, { jpdb: profileLang === "ja" });
}

// ── study path ──────────────────────────────────────────────────────────────

function highlight(sentence, word) {
  const s = esc(sentence);
  const w = esc(word);
  return w ? s.split(w).join(`<mark>${w}</mark>`) : s;
}

// Readings come from the analyzer in katakana; a learner reads kana readings in hiragana.
// A reading that only repeats the word (a kana or katakana word) is not shown.
function hira(s) {
  return String(s ?? "").replace(/[\u30a1-\u30f6]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60));
}
function readingHtml(w) {
  const r = hira(w.reading);
  return r && r !== hira(w.word) ? esc(r) : "";
}

// The band word in its ink: "moderate strain" as the hero's verdict colours it.
function bandWord(band) {
  return `<b class="study-band ${band.cls}">${band.label}</b>`;
}

// The curve, cut to the steps that matter: today, each row where the band changes,
// and (when nothing does) the farthest point, so "no change" still shows how far it moved.
function curveSteps(curve) {
  const steps = [];
  let prev = null;
  curve.forEach((c, i) => {
    const band = strainBucket(c.hard_pct);
    const step = prev !== null && band !== prev;
    if (i === 0 || step) steps.push({ ...c, band, step });
    prev = band;
  });
  const last = curve[curve.length - 1];
  if (steps.length === 1 && curve.length > 1) steps.push({ ...last, band: strainBucket(last.hard_pct), step: false });
  return steps;
}

// The card's first line: what learning the words below does to the verdict.
function studyLead(steps, curve) {
  const now = steps[0].band;
  const next = steps.find((s) => s.step);
  if (now === STRAIN_GRADES[0]) return `Already ${bandWord(now)}. Words learned from here only trim the last few lookups.`;
  if (next) {
    return `Learn the top ${fmt(next.n)} words below and this becomes ${bandWord(next.band)}` +
      ` (${next.hard_pct}% of sentences with two or more unknown words, from ${steps[0].hard_pct}%).`;
  }
  const far = curve[curve.length - 1];
  return `This stays ${bandWord(now)} whichever of these steps you take: ` +
    `the top ${fmt(far.n)} words only move it from ${steps[0].hard_pct}% to ${far.hard_pct}%.`;
}

// On a phone the list shows this many words at a time (wider screens scroll it in place).
const STUDY_PAGE = 10;

function studyHtml(d) {
  if (d.status !== "ok" || !d.words.length) return "";
  const steps = d.curve.length ? curveSteps(d.curve) : [];
  const lead = steps.length ? `<p class="study-lead">${studyLead(steps, d.curve)}</p>` : "";
  const curve = steps
    .map(
      (c) => `<tr${c.step ? ' class="step"' : ""}>
        <td>${c.n === 0 ? "today" : "+" + fmt(c.n)}</td>
        <td>${loadBarHtml(c)}</td>
        <td class="curve-read"><b class="study-band ${c.band.cls}">${c.band.short} strain</b> ${c.hard_pct}%${c.step ? ' <span class="curve-flag">new band</span>' : ""}</td>
      </tr>`
    )
    .join("");
  const rows = d.words
    .map(
      (w, i) => `<tr data-word="${esc(w.word)}"${i >= STUDY_PAGE ? ' class="beyond"' : ""}>
        <td class="study-word"${langAttr(w.word)}>${esc(w.word)}<div class="study-reading">${readingHtml(w)}</div></td>
        <td class="study-n">${w.count}×</td>
        <td class="study-ex"${langAttr(w.example)}>${w.example ? highlight(w.example, w.word) : "<span class='approx'>no clean example in this book</span>"}</td>
        <td class="study-mark">${skipButton(w.word)}${markButton(w.word)}</td>
      </tr>`
    )
    .join("");
  return `
    <div class="stats-card" id="study-card">
      <h2>Most common words you don't know yet</h2>
      ${lead}
      <p class="hint">${fmt(d.unknown_types)} unknown word types, most frequent first, each in a sentence where it is
        the only unknown word. Recognise one? Hit <strong>${icon("check")}</strong> to add it to your known list;
        the reading load above updates as you go. A name or a fragment of a longer word
        (<strong>${icon("block")}</strong>) can be set aside instead: it counts as known and leaves this list.</p>
      <table class="stats-table curve-table">
        <tr class="section"><td colspan="3">If you learned the top N first</td></tr>
        <tr class="curve-sub-row"><td colspan="3">Share of sentences with two or more unknown words</td></tr>
        ${curve}
      </table>
      <div class="study-wrap">
        <table class="study-table"><tbody>${rows}</tbody></table>
      </div>
      ${d.words.length > STUDY_PAGE
        ? `<button type="button" class="btn study-more" id="study-more">${icon("expand_more")} Show ${STUDY_PAGE} more</button>` : ""}
      <div class="mark-status" id="study-mark-status"></div>
      <div class="mark-status" id="study-skip-status"></div>
    </div>`;
}

// The phone page's last word: the downloads again, so a long scroll ends where you act.
function endHtml(b) {
  return `<div class="book-end"><div class="book-actions">${dlEpub(b.book_id)}${dlAudio(bookAudio(b), { placeholder: true, source: true, plain: true, searchTitle: b.title })}</div></div>`;
}

function readToggle(b) {
  const on = !!(b.known && b.known.read);
  return `<button id="read-btn" class="read-btn${on ? " on" : ""}" data-read="${on ? 1 : 0}" aria-pressed="${on}">${
    on ? `${icon("check")} Read` : "Mark as read"
  }</button>`;
}

function statsRows(b) {
  const v = b.vocab;
  const pct = (x) => (x == null ? "—" : `${x}%`);
  const rows = [
    ["Length (words)", fmt(v.words_total)],
    ["Unique words", fmt(v.unique_words)],
    ["Unique words (used once)", `${fmt(v.unique_words_once)} <span class="approx">(${pct(v.unique_words_once_pct)})</span>`],
    ...(b.language === "ja" ? [
      ["Unique kanji", fmt(v.unique_kanji)],
      ["Unique kanji (used once)", fmt(v.unique_kanji_once)],
      ["Unique kanji readings", `${fmt(v.unique_kanji_readings)} <span class="approx">≈</span>`],
    ] : []),
    ["Average difficulty", `${meter(v.difficulty_avg)}${v.difficulty_avg}/100`],
    ["Peak difficulty (90th percentile)", `${meter(v.difficulty_p90)}${v.difficulty_p90}/100`],
    ["Average sentence length", `${v.avg_sentence_chars} <span class="approx">chars (${v.avg_sentence_words} words)</span>`],
    ["Characters", fmt(v.characters)],
    ["Sentences", fmt(v.sentences)],
  ];
  let html = rows
    .map(([k, val]) => `<tr><td>${k}</td><td>${val}</td></tr>`)
    .join("");

  // Coverage lives in the Reading load card above; only the type-level
  // counts that have no place there are repeated here.
  if (b.known) {
    html += `<tr class="section"><td colspan="2">Your knowledge</td></tr>`;
    html += `<tr><td>Known unique words</td><td>${fmt(b.known.known_unique)} <span class="approx">(${b.known.known_unique_pct}%)</span></td></tr>`;
  }
  return html;
}

function heroHtml(b) {
  const cover = b.cover
    ? `<img src="${esc(coverUrl(b))}" alt="">`
    : `${icon("menu_book", "placeholder")}`;
  const seriesLine = b.series_name && !foreignSeries(b)
    ? `<div class="book-series-line">Series: <a href="/series?name=${encodeURIComponent(b.series_name)}"${langAttr(b.series_name)}><strong>${esc(b.series_name)}</strong></a>` +
      `${b.series_order ? `&nbsp;· <span class="vol-no">Vol ${esc(b.series_order)}</span>` : ""}<span class="vol-nav" id="vol-nav"></span></div>`
    : "";
  return `
    <div class="book-hero on-hero">
      <div class="book-cover">${cover}</div>
      <div class="book-head">
        <div class="book-meta">
          <h1 class="book-title"${langAttr(b.title)}>${esc(b.title || "(untitled)")}</h1>
          <div class="book-author"${langAttr(b.author)}>${authorLink(b.author)}</div>
          ${imprintLine(b)}
          ${seriesLine}
        </div>
        <div class="chips">${chipRow(b)}</div>
        <div class="book-actions" id="book-actions" tabindex="-1">${actionRow(b)}<div class="ab-caption" id="ab-caption" hidden></div><div class="sync-row" id="sync-row" hidden></div></div>
      </div>
      ${loadHtml(b)}
    </div>`;
}

// "KADOKAWA · 2019": the OPF's publisher and publication date, when it has them.
function imprintLine(b) {
  const bits = [b.publisher, b.pub_date && b.pub_date.slice(0, 4)].filter(Boolean);
  return bits.length ? `<div class="book-imprint">${bits.map(esc).join(" · ")}</div>` : "";
}

// The OPF's own description (about 1 in 6 EPUBs carry one), else the linked
// audiobook's listing, plain text.
function synopsisHtml(b) {
  if (!b.description) return "";
  const from = { audiobook: "the audiobook listing", isbn: "Google Books" }[b.description_source];
  const note = from ? `<div class="synopsis-source">From ${from}</div>` : "";
  return `<div class="stats-card book-synopsis"><h2>Synopsis</h2><p${langAttr(b.description)}>${esc(b.description)}</p>${note}</div>`;
}

// The author opens the library filtered to that name. A trailing ";" or "," is
// OPF list punctuation ("James Nestor;"), not part of the name.
function authorLink(author) {
  if (!author) return "";
  const q = author.replace(/[;,\s]+$/, "");
  return `<a href="/?q=${encodeURIComponent(q)}" title="More by ${esc(q)}">${esc(author)}</a>`;
}

// The Edit button lives in the page header (top right, like Edit on /series) and
// opens the form in a native <dialog>, so it appears where the reader is looking
// instead of below the hero. Escape / backdrop / Close dismiss it (asking first if
// the form holds unsaved edits); the dialog hands focus back to the button. For a
// Japanese book it also carries the audiobook-link correction.
function wireDetails(abState) {
  const btn = document.getElementById("edit-details-btn");
  btn.hidden = false;
  btn.addEventListener("click", () => {
    const dlg = document.createElement("dialog");
    dlg.className = "dlg";
    dlg.setAttribute("aria-labelledby", "details-title");
    dlg.innerHTML = `<div class="modal-box bd-modal">
        <div class="bd-modal-head">
          <h3 class="modal-title" id="details-title">Edit details</h3>
          <button type="button" class="ghost-btn" data-close>Close</button>
        </div>
        <div class="bd-scroll">
          <div id="details-body"></div>
          ${edState ? editionSectionHtml(edState) : ""}
          ${abState ? audiobookSectionHtml() : ""}
        </div>
        <div class="bd-modal-foot" id="details-foot"></div>
      </div>`;
    const body = dlg.querySelector("#details-body");
    let closing = false;
    const close = async () => {
      if (closing) return;
      closing = true;
      if (BookDetails.isDirty(body) && !(await confirmDialog({
        title: "Discard changes?",
        message: "You have edits to this book's details that haven't been saved.",
        confirmLabel: "Discard", cancelLabel: "Keep editing", danger: true,
      }))) { closing = false; return; }
      dlg.close();
      dlg.remove();
      if (abChanged || edChanged) reloadKeepingPlace("edit-details-btn");
    };
    dlg.querySelector("[data-close]").addEventListener("click", close);
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    document.body.appendChild(dlg);
    dlg.showModal();
    if (edState) renderEditionCard(edState);
    if (abState) renderAudiobook(abState);
    // The title, series line, vocab and audiobook card all derive from these
    // fields, so reload rather than patching each one.
    BookDetails.mount(body, bookId, {
      actionsEl: dlg.querySelector("#details-foot"),
      onSaved: () => setTimeout(() => reloadKeepingPlace("edit-details-btn"), 400),
    });
  });
}

function statsHtml(b) {
  const knownHint = b.known || b.language !== profileLang
    ? ""
    : ` <a href="/known">Import a known-words list</a> to see how much of it you already know.`;
  if (b.vocab && b.vocab.status === "ok") {
    const v = b.vocab;
    return `
      <details class="stats-card vocab-details">
        <summary><span class="vocab-title">Vocabulary &amp; difficulty</span>
          <span class="vocab-gist">${fmt(v.words_total)} words · ${fmt(v.unique_words)} unique · difficulty ${v.difficulty_avg}/100</span></summary>
        <p class="hint">Computed from the EPUB's full text. Difficulty is frequency-based: 0 = everyday vocabulary,
          100 = very rare words.${knownHint}</p>
        <table class="stats-table">${statsRows(b)}</table>
      </details>`;
  }
  const reason =
    !["ja", "en"].includes(b.language)
      ? "Analysis only runs on Japanese and English EPUBs."
      : b.vocab && b.vocab.status === "no_text"
      ? "No extractable text was found — likely an image-based EPUB or a DRM-protected file."
      : b.vocab && b.vocab.status === "error"
      ? `Analysis failed: ${esc(b.vocab.error || "unknown error")}`
      : "Not analyzed yet.";
  return `
    <div class="stats-card">
      <h2>Vocabulary &amp; difficulty</h2>
      <div class="no-vocab">No text analysis available. ${reason}</div>
    </div>`;
}

// A ja/en book with no vocab entry is either still in the server's per-book
// queue (just added, or "Analyze now" was clicked — see epublib/book_jobs.py) or
// was never queued (copied into the folder, or the server restarted mid-job).
// Ask the server which, rather than guessing from a timer.
function isAnalysisPending(b) {
  return ["ja", "en"].includes(b.language) && !b.vocab;
}

const STEP_LABELS = {
  analyze: "tokenizing the text",
  enrich: "matching a Natively level",
  pair: "pairing editions",
  link: "finding the audiobook",
  build: "building the index",
};

function vocabCard(inner) {
  return `<div class="stats-card"><h2>Vocabulary &amp; difficulty</h2>${inner}</div>`;
}

function showAnalysis(msg, withButton) {
  const vw = document.getElementById("vocab-wrap");
  if (!vw) return;
  vw.innerHTML = vocabCard(
    `<div class="no-vocab">${msg}${withButton ? ` <button class="ab-btn" id="analyze-now">Analyze now</button>` : ""}</div>`
  );
  const btn = document.getElementById("analyze-now");
  if (btn) btn.addEventListener("click", startAnalysis);
}

async function fetchStatus() {
  const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/status`);
  if (!r.ok) throw new Error(`status ${r.status}`);
  return r.json();
}

// Analysis completing changes the Reading Load card, known stats, and can
// newly reveal the study card — a full reload covers all of that at once.
async function watchJob() {
  for (;;) {
    const st = await fetchStatus();
    const job = st.job;
    if (job && (job.state === "queued" || job.state === "running")) {
      const what = job.state === "queued" ? "waiting its turn" : STEP_LABELS[job.step] || "working";
      showAnalysis(`Analyzing — ${what}… a long novel can take a minute or two.`, false);
      await new Promise((r) => setTimeout(r, 1500));
      continue;
    }
    if (st.vocab_status) { location.reload(); return; }
    const err = job && job.error ? ` Last run failed: ${esc(job.error)}` : "";
    showAnalysis(`Not analyzed yet.${err}`, true);
    return;
  }
}

async function startAnalysis() {
  const btn = document.getElementById("analyze-now");
  if (btn) { btn.disabled = true; btn.textContent = "Queuing…"; }
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/analyze`, { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    await watchJob();
  } catch (e) {
    showAnalysis("Couldn't start the analysis.", true);
  }
}

// This book's series, in volume order (same author only): the table below the hero and
// the prev/next links beside the series line both read it.
async function seriesVolumes(b) {
  const res = await fetch(`/api/books?series=${encodeURIComponent(b.series_name)}`);
  const all = await res.json();
  return all
    .filter((x) => x.series_name === b.series_name && authorKey(x) === authorKey(b))
    .sort(byVolume((x) => x.series_order));
}

// "← Vol 02 · Vol 04 →" by the series line: the book before and after this one.
function volNavHtml(vols, b) {
  const i = vols.findIndex((x) => x.book_id === b.book_id);
  if (i < 0) return "";
  const link = (x, dir) => {
    const vol = x.series_order ? `Vol ${esc(x.series_order)}` : esc(x.title || "(untitled)");
    const name = `${dir === "prev" ? "Previous" : "Next"} volume: ${x.title || ""}`;
    return `<a class="vol-link" href="/book/${esc(x.book_id)}" aria-label="${esc(name)}" title="${esc(name)}"${langAttr(x.title)}>` +
      (dir === "prev" ? `${icon("arrow_back")}${vol}` : `${vol}${icon("arrow_forward")}`) + `</a>`;
  };
  return [vols[i - 1] && link(vols[i - 1], "prev"), vols[i + 1] && link(vols[i + 1], "next")]
    .filter(Boolean).join("");
}

function seriesHtml(b, vols) {
  if (vols.length < 2) return "";

  // Strain and Known measure a book against the profile's vocabulary; for a book outside its
  // language they would be a column of dashes.
  const withLoad = b.language === profileLang;
  // On a phone each title drops the series name it repeats ("Series 2: Sub" -> "2: Sub").
  const sname = (b.series_name || "").trim();
  const shortTitle = (t) => {
    const rest = sname && t && t.startsWith(sname) ? t.slice(sname.length).replace(/^[\s:：\-－‐·・、,]+/, "") : "";
    return rest && rest !== t ? rest : "";
  };
  const row = (x) => {
    const v = x.vocab && x.vocab.status === "ok" ? x.vocab : null;
    const short = shortTitle(x.title);
    const hard = x.known && x.known.hard_pct != null ? x.known.hard_pct : null;
    const band = hard == null ? null : strainBucket(hard);
    return `<tr class="${x.book_id === b.book_id ? "current" : ""}">
      <td>${esc(x.series_order || "—")}</td>
      <td class="t"><a href="/book/${esc(x.book_id)}"${x.book_id === b.book_id ? ' aria-current="page"' : ""}${langAttr(x.title)}>${short
        ? `<span class="t-full">${esc(x.title)}</span><span class="t-short">${esc(short)}</span>`
        : esc(x.title || "(untitled)")}</a></td>
      <td class="c-load">${band ? `<span class="chip strain ${band.cls}" title="${hard}% of sentences have 2+ unknown words">${band.short}</span>` : "—"}</td>
      <td class="c-more c-load">${x.known ? `${x.known.known_pct}%` : "—"}</td>
      <td class="c-more">${(x.language || "?").toUpperCase()}</td>
      <td class="c-more">${v ? kFmt(v.words_total) : "—"}</td>
      <td class="c-more">${v ? fmt(v.unique_words) : "—"}</td>
      <td class="c-more">${v ? v.difficulty_avg : "—"}</td>
      <td class="c-more c-load">${natLevel(x) ?? "—"}</td>
      <td class="c-dl">${dlEpub(x.book_id, { size: "xs", iconOnly: true, tone: "quiet", label: x.title || "" })}</td>
    </tr>`;
  };

  return `
    <div class="stats-card" id="series-card">
      <h2><span${langAttr(b.series_name)}>${esc(b.series_name)}</span> — all volumes</h2>
      <div class="series-table-wrap">
        <table class="series-table${withLoad ? "" : " no-load"}">
          <thead><tr>
            <th>Vol</th><th>Title</th><th class="c-load">Strain</th><th class="c-more c-load">Known</th><th class="c-more">Lang</th><th class="c-more">Words</th>
            <th class="c-more">Unique</th><th class="c-more">Diff</th><th class="c-more c-load">Natively</th><th><span class="sr-only">Download</span></th>
          </tr></thead>
          <tbody>${vols.map(row).join("")}</tbody>
        </table>
      </div>
    </div>`;
}

// Marking a book read: the cover gives a small bump and a check is pressed onto its
// corner, then peels away (the known-words card's vocabulary, app.css). Un-marking is silent.
function stampCover() {
  const cover = document.querySelector(".book-hero .book-cover");
  if (!cover) return;
  cover.querySelector(".cover-stamp")?.remove();
  const stamp = document.createElement("span");
  stamp.className = "cover-stamp";
  stamp.setAttribute("aria-hidden", "true");
  stamp.innerHTML = icon("check");
  stamp.addEventListener("animationend", () => stamp.remove());
  cover.append(stamp);
  cover.classList.remove("bump");
  void cover.offsetWidth;  // re-trigger the animation on a second mark
  cover.classList.add("bump");
}

async function onReadClick(btn) {
  const read = btn.dataset.read !== "1";
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/read`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ read }),
    });
    if (!r.ok) throw new Error(await r.text());
    // Patch the button in place (focus stays on it) and pull the recomputed reading
    // load, rather than reloading: marking a book read only moves those two things.
    btn.dataset.read = read ? "1" : "0";
    btn.classList.toggle("on", read);
    btn.setAttribute("aria-pressed", String(read));
    btn.innerHTML = read ? `${icon("check")} Read` : "Mark as read";
    btn.disabled = false;
    btn.focus();
    if (read) stampCover();
    refreshLoad();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Failed — retry";
  }
}

// Marking a word known changes this book's reading load, so pull the
// recomputed figures rather than leaving the card contradicting the list
// underneath it. Only this card moves — a full reload would cost the study
// path's EPUB read and scroll the reader back to the top mid-review.
async function refreshLoad() {
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(bookId)}`);
    if (!r.ok) return;
    const card = document.getElementById("load-card");
    const b = await r.json();
    const html = loadHtml(b);
    if (card && html) card.outerHTML = html;
    return b.known;
  } catch {
    /* the card keeps the numbers it already has */
  }
}

// The header's "Book" is a page label, not the page's subject: the book's own title
// (the hero's h1) is. Same look, no heading semantics, so a screen reader's h1 is the title.
function demotePageTitle() {
  const h = document.getElementById("page-title");
  if (!h || h.tagName !== "H1") return;
  const p = document.createElement("p");
  p.id = "page-title";
  p.className = "page-title";
  p.textContent = h.textContent;
  h.replaceWith(p);
}

// "Skip to downloads" targets the hero's #book-actions; a page with no book has none.
function dropSkipLink() {
  const a = document.querySelector(".skip-link");
  if (a) a.remove();
}

async function load() {
  // Not a blank page while the book loads.
  wrap.innerHTML = `<div class="no-vocab card-pending" role="status">Loading…</div>`;
  const [resp, active, edInfo] = await Promise.all([
    fetch(`/api/books/${encodeURIComponent(bookId)}`),
    loadActiveProfile(),
    // Fetched with the book so the well is in the first paint: it opens up room under the hero.
    edFetch().catch(() => null),
  ]);
  if (active) profileLang = active.language;
  edState = edInfo && edInfo.eligible ? edInfo : null;
  if (!resp.ok) {
    document.title = "Book not found — Hondana";
    demotePageTitle();
    dropSkipLink();
    wrap.innerHTML = `<div class="no-vocab not-found"><h1 class="error-title">Book not found.</h1>
      <p>It may have been deleted or renamed.</p>
      <a class="btn" href="/">${icon("arrow_back")} Back to library</a></div>`;
    return;
  }
  const b = await resp.json();
  heroBook = b;
  const edition = editionAction(b);
  if (edition) {
    try {
      const r = await fetch(`/api/books/${encodeURIComponent(edition.book_id)}`);
      if (r.ok) partnerBook = await r.json();
    } catch { /* the action simply carries no verdict */ }
  }
  document.title = `${b.title || "Book"} — Hondana`;
  demotePageTitle();
  const pending = isAnalysisPending(b);
  wrap.innerHTML =
    heroHtml(b) + `<section class="edition-well" id="edition-well" aria-labelledby="ed-q" hidden></section>` + synopsisHtml(b) +
    `<div id="vocab-wrap">${pending ? vocabCard('<div class="no-vocab">Checking analysis status…</div>') : statsHtml(b)}</div>` +
    // The audio-sync card (audio_sync.js) fills this in for a Japanese book with a local audiobook.
    // It sits right after the vocabulary card, so the series card inserted later goes before it.
    (b.language === "ja" ? `<section class="stats-card sync-card" id="sync-slot" aria-labelledby="sync-card-title" hidden></section>` : "");
  renderSyncRow(b.audio_synced === true);
  if (pending) watchJob().catch(() => showAnalysis("Not analyzed yet.", true));

  // The link state feeds the hero's caption and the Edit dialog's audiobook section.
  let abState = null;
  if (b.language === "ja") {
    try {
      const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/audiobook-link`);
      if (r.ok) { abState = await r.json(); renderAbCaption(abState); }
    } catch { /* no caption, no section */ }
  }

  if (b.language === "ja") mountAudioSync().catch(() => { /* the card is optional */ });
  renderEditionWell(edState);
  wireEditionWell();
  wireDetails(abState);
  restorePlace();
  const btn = document.getElementById("read-btn");
  if (btn) btn.addEventListener("click", () => onReadClick(btn));

  // Both cards arrive after the page: the study path reads the EPUB server-side, the
  // series needs the whole library list. Fetched together; the series card goes straight
  // after the vocabulary card whichever lands first (comparing volumes is what a reader
  // does before committing to a list of 60 words), the study list always last.
  // The series and study cards land after the page; hold their room so the content below
  // doesn't jump as they arrive. Each placeholder is replaced, or removed if there is no card.
  const hold = (id, text) => `<div class="stats-card card-pending" id="${id}" aria-hidden="true">${text}</div>`;
  const wantSeries = b.series_name && !foreignSeries(b);
  const wantStudy = b.known && b.known.hard_pct != null;
  if (wantSeries) document.getElementById("vocab-wrap").insertAdjacentHTML("afterend", hold("series-pending", "Loading volumes…"));
  if (wantStudy) wrap.insertAdjacentHTML("beforeend", hold("study-pending", "Loading the study list…"));
  await Promise.all([showSeries(b), showStudy(b)]);
  document.querySelectorAll(".card-pending[id]").forEach((e) => e.remove());
  wrap.insertAdjacentHTML("beforeend", endHtml(b));
}

async function showSeries(b) {
  if (!b.series_name || foreignSeries(b)) return;
  try {
    const vols = await seriesVolumes(b);
    const nav = document.getElementById("vol-nav");
    if (nav) nav.innerHTML = volNavHtml(vols, b);
    const html = seriesHtml(b, vols);
    if (html) document.getElementById("vocab-wrap").insertAdjacentHTML("afterend", html);
    restorePlace();
  } catch {
    /* series is optional */
  }
}

async function showStudy(b) {
  if (!(b.known && b.known.hard_pct != null)) return;
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/study?n=60`);
    if (!r.ok) return;
    wrap.insertAdjacentHTML("beforeend", studyHtml(await r.json()));
    const card = document.getElementById("study-card");
    if (!card) return;
    // The readout swaps in the hero, off-screen from here; when marking words moves the
    // verdict, say so on the status line the reader is already watching.
    const opened = b.known.hard_pct;
    let verdictNote = "";
    // The two actions share one verdict note and one reading-load refresh.
    const onSaved = async () => {
      const k = await refreshLoad();
      if (!k || k.hard_pct == null) return;
      const now = strainBucket(k.hard_pct);
      verdictNote = now === strainBucket(opened) ? "" : `Now ${now.label}`;
      markers.forEach((m) => m.sync());
    };
    const markers = [
      createMarker({
        container: card,
        status: document.getElementById("study-mark-status"),
        detail: () => verdictNote,
        onSaved,
      }),
      createMarker({
        container: card,
        kind: "skip",
        status: document.getElementById("study-skip-status"),
        detail: () => verdictNote,
        onSaved,
      }),
    ];
    const more = document.getElementById("study-more");
    if (more) more.addEventListener("click", () => {
      const hidden = card.querySelectorAll("tr.beyond");
      hidden.forEach((tr, i) => { if (i < STUDY_PAGE) tr.classList.remove("beyond"); });
      const left = hidden.length - STUDY_PAGE;
      if (left <= 0) more.remove();
      else more.lastChild.textContent = ` Show ${Math.min(STUDY_PAGE, left)} more`;
    });
    // /next's "Study list" links here; the card arrives after the browser
    // has already looked for the #study-card target, so scroll to it now.
    if (location.hash === "#study-card") card.scrollIntoView({ block: "start" });
    // The study card makes the page tall enough to scroll back to where it was.
    restorePlace();
  } catch {
    /* study path is optional */
  }
}

load().catch((e) => {
  demotePageTitle();
  dropSkipLink();
  wrap.innerHTML = `<div class="no-vocab not-found"><h1 class="error-title">Couldn't load this book.</h1>
    <p>${esc(e.message)}</p>
    <a class="btn" href="/">${icon("arrow_back")} Back to library</a></div>`;
});
