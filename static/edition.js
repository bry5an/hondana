// The other edition of a book (/book/{id}): the "is this the same book?" well under the hero and
// the card in the Edit dialog. Loaded before book.js, which owns the page (reloadKeepingPlace,
// bookId) and calls in; the pairing itself is decided server-side (epublib/pairing.py).
"use strict";

// The other edition's language, by this book's.
const ED_OTHER = { ja: "English", en: "Japanese" };

// GET /api/books/{id}/edition, or null (not a Japanese/English book, or the request failed).
let edState = null;
// Set when the Edit dialog's card changed the pairing: closing the dialog reloads the page.
let edChanged = false;

const edWord = (st) => ED_OTHER[st.book.language];

async function edFetch() {
  const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/edition`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// The request keys for a pair: the server files them by language, not by which side this book is.
function edKeys(st, other) {
  const [en, jp] = st.book.language === "en" ? [st.book, other] : [other, st.book];
  return { en_key: en.stable_key, jp_key: jp.stable_key };
}

function edBook(b) {
  const cover = b.cover
    ? `<img class="ed-cover" src="${esc(coverUrl(b))}" alt="" loading="lazy">`
    : `<span class="ed-cover ed-cover--none">${icon("menu_book")}</span>`;
  return `<a class="ed-book" href="/book/${esc(b.book_id)}">${cover}<span class="ed-meta">
    <span class="ed-title"${langAttr(b.title)}>${esc(b.title || "(untitled)")}</span>
    <span class="ed-author"${langAttr(b.author)}>${esc(b.author || "")}</span></span></a>`;
}

// An undo that failed says so in words; the toast shows a thrown message as an error.
async function edUndoCall(method, url, body) {
  try {
    await postJson(url, body, method);
  } catch (e) {
    throw new Error(`Couldn't undo: ${failReason(e)}.`);
  }
}

const edUndo = (call, done) => ({ label: "Undo", busyLabel: "Undoing…", run: call, done });

// ── the well ────────────────────────────────────────────────────────────────
// Asks only about a candidate whose author names match (`signal: "author"`): a weak guess
// would be an endorsement of noise, so those wait in the Edit card's "Possible matches".

const edAuthorCands = (st) => (st && st.mode === "unpaired" ? st.candidates.filter((c) => c.signal === "author") : []);

function renderEditionWell(st) {
  const slot = document.getElementById("edition-well");
  if (!slot) return;
  const cands = edAuthorCands(st);
  if (!cands.length) {
    slot.hidden = true;
    slot.innerHTML = "";
    return;
  }
  const c = cands[0];
  const word = edWord(st);
  slot.hidden = false;
  slot.innerHTML = `<h2 class="ed-q" id="ed-q" tabindex="-1">Is this the ${word} edition?</h2>
    ${edBook(c)}
    <div class="ed-signal" title="${esc(pairSignalTip(c))}">${pairSignalLabel(c)}${cands.length > 1 ? ` · 1 of ${cands.length}` : ""}</div>
    <div class="ed-actions">
      <button type="button" class="btn ed-yes">Yes, same book</button>
      <button type="button" class="ghost-btn ed-not">Not this one</button>
      <button type="button" class="ghost-btn ed-none">No ${word} edition</button>
    </div>`;
}

// Focus lands on what changed: the next question, or the downloads when the well has closed.
function edFocusAfter() {
  const slot = document.getElementById("edition-well");
  const target = slot && !slot.hidden ? document.getElementById("ed-q") : document.getElementById("book-actions");
  if (target) target.focus({ preventScroll: true });
}

// The state changed under the page. If the book is now paired (the scanner may pair what's
// left once a candidate is rejected, or when "no other edition" is lifted) the hero, chips and
// series all derive from that, so reload; otherwise redraw in place. True when it reloaded.
function edSettle(next, message, undo) {
  if (next.mode === "auto" || next.mode === "manual") {
    reloadKeepingPlace("edition-link", { message, undo });
    return true;
  }
  edState = next;
  renderEditionWell(next);
  return false;
}

let edBusy = false;

// One write at a time, and a failed one says what failed instead of doing nothing.
async function edRun(verb, fn) {
  if (edBusy) return;
  edBusy = true;
  try {
    await fn();
  } catch (e) {
    toast(`Couldn't ${verb}: ${failReason(e)}.`);
  } finally {
    edBusy = false;
  }
}

function wireEditionWell() {
  const slot = document.getElementById("edition-well");
  if (!slot) return;
  slot.addEventListener("click", (e) => {
    const st = edState;
    const btn = e.target.closest("button");
    if (!btn || !st) return;
    const c = edAuthorCands(st)[0];
    if (btn.classList.contains("ed-yes") && c) edRun("link those editions", () => edYes(st, c));
    else if (btn.classList.contains("ed-not") && c) edRun("reject that edition", () => edNotThis(st, c));
    else if (btn.classList.contains("ed-none")) edRun(`mark this book`, () => edNoEdition(st));
  });
}

async function edYes(st, c) {
  const keys = edKeys(st, c);
  await postJson("/api/pairing/link", keys);
  // The pairing changes the hero (chip, and an English book's series), so reload; the Undo
  // is carried across the reload as data and offered again on the other side.
  reloadKeepingPlace("edition-link", {
    message: `Linked “${st.book.title}” and “${c.title}”.`,
    undo: { url: "/api/pairing/unlink", body: { ...keys, block: false }, done: "Undone." },
  });
}

async function edNotThis(st, c) {
  const keys = edKeys(st, c);
  await postJson("/api/pairing/block", keys);
  const next = await edFetch();
  // Rejecting one of two same-author books can leave the other the scanner's only confident
  // match, so it pairs it. Say so rather than let the page change under the reader.
  const undo = { url: "/api/pairing/unblock", body: keys, done: "Undone." };
  if (edSettle(next, next.partner ? `Paired with “${next.partner.title}” instead.` : "", undo)) return;
  toast(`Won't suggest “${c.title}” again.`, "ok", {
    anchor: document.getElementById("edition-well"),
    action: edUndo(async () => {
      await edUndoCall("POST", undo.url, undo.body);
      const back = await edFetch();
      if (!edSettle(back, `Paired with “${back.partner && back.partner.title}”.`)) edFocusAfter();
    }),
  });
  edFocusAfter();
}

async function edNoEdition(st) {
  await postJson(`/api/books/${encodeURIComponent(bookId)}/edition/none`, undefined);
  edState = await edFetch();
  renderEditionWell(edState);
  toast(`Marked as having no ${edWord(st)} edition.`, "ok", {
    anchor: document.getElementById("edition-well"),
    action: edUndo(async () => {
      await edUndoCall("DELETE", `/api/books/${encodeURIComponent(bookId)}/edition/none`);
      const back = await edFetch();
      if (!edSettle(back, `Paired with “${back.partner && back.partner.title}”.`)) edFocusAfter();
    }),
  });
  edFocusAfter();
}

// ── the Edit dialog's card ──────────────────────────────────────────────────
// Everything the well can't do: pick by hand, see the weak guesses, unlink, go back to
// automatic. Applies immediately like the audiobook card; closing the dialog reloads.

function editionSectionHtml(st) {
  return `<section class="bd-audiobook bd-edition" aria-labelledby="ed-title">
      <h4 class="bd-section-title" id="ed-title">${edWord(st)} edition</h4>
      <p class="ab-apply">Applies immediately. It isn't part of Save changes.</p>
      <div id="ed-body" class="ab-line">Loading…</div>
    </section>`;
}

function edCandidateRow(c) {
  return `<li class="ed-cand-row">${edBook(c)}
    <span class="ed-signal" title="${esc(pairSignalTip(c))}">${pairSignalLabel(c)}</span>
    <button type="button" class="ab-btn" data-link="${esc(c.book_id)}" aria-label="Link to ${esc(c.title)}">Link</button></li>`;
}

function renderEditionCard(st) {
  const body = document.getElementById("ed-body");
  if (!body) return;
  const word = edWord(st);
  let current = "";
  let actions = "";
  let extra = "";
  if (st.mode === "auto" || st.mode === "manual") {
    const how = st.mode === "manual" ? "Linked by you" : "Matched automatically from the authors' names";
    current = `${edBook(st.partner)}<div class="ab-meta">${how}</div>`;
    actions = `<button type="button" class="ab-btn" data-act="unlink">Unlink…</button>` +
      (st.mode === "manual" ? `<button type="button" class="ab-btn" data-act="auto">Back to automatic</button>` : "");
  } else if (st.mode === "none") {
    current = `<div>No ${word} edition. You marked this book as having none.</div>`;
    actions = `<button type="button" class="ab-btn" data-act="clear-none">Back to automatic</button>
      <button type="button" class="ab-btn" id="ed-change" aria-expanded="false" aria-controls="ed-picker">Link an edition…</button>`;
  } else {
    current = `<div>No ${word} edition linked.</div>`;
    if (st.candidates.length)
      extra = `<h5 class="ed-sub">Possible matches</h5><ul class="ed-cands">${st.candidates.map(edCandidateRow).join("")}</ul>`;
    actions = `<button type="button" class="ab-btn" id="ed-change" aria-expanded="false" aria-controls="ed-picker">Link an edition…</button>
      <button type="button" class="ab-btn" data-act="none">No ${word} edition</button>`;
  }
  body.innerHTML = `${current}${extra}<div class="ab-actions">${actions}</div>
    <div id="ed-picker"></div><div class="ab-note" id="ed-note" role="status"></div>`;
  body.onclick = (e) => edCardClick(e, st);
}

// The picker lists the other language's unpaired books, filtered as you type.
function openEdPicker(st) {
  const picker = document.getElementById("ed-picker");
  const toggle = document.getElementById("ed-change");
  if (picker.firstChild) { picker.innerHTML = ""; toggle.setAttribute("aria-expanded", "false"); return; }
  toggle.setAttribute("aria-expanded", "true");
  picker.innerHTML = `<div class="ab-picker">
      <input type="search" id="ed-q-input" aria-label="Search your ${edWord(st)} books" placeholder="Search your ${edWord(st)} books by title or author" autocomplete="off">
      <ul class="ab-results" id="ed-results"></ul>
      <div class="sr-only" role="status" id="ed-count"></div></div>`;
  const q = document.getElementById("ed-q-input");
  const list = document.getElementById("ed-results");
  const count = document.getElementById("ed-count");
  const hay = (b) => `${b.title || ""} ${b.author || ""} ${b.author_romaji || ""}`.normalize("NFKC").toLowerCase();
  const draw = () => {
    const words = q.value.normalize("NFKC").toLowerCase().split(/\s+/).filter(Boolean);
    const hits = st.others.filter((b) => words.every((w) => hay(b).includes(w))).slice(0, 30);
    list.innerHTML = hits.length
      ? hits.map((b) => `<li><button type="button" data-link="${esc(b.book_id)}">
          <span${langAttr(b.title)}>${esc(b.title || "(untitled)")}</span>
          <div class="ab-meta"${langAttr(b.author)}>${esc(b.author || "")}</div></button></li>`).join("")
      : `<li class="ab-note">${st.others.length ? "No matches." : `No unpaired ${edWord(st)} books.`}</li>`;
    count.textContent = hits.length ? `${hits.length} ${hits.length === 1 ? "match" : "matches"}` : "No matches";
  };
  q.addEventListener("input", draw);
  draw();
  q.focus();
}

function edCardClick(e, st) {
  const btn = e.target.closest("button");
  if (!btn || btn.disabled) return;
  if (btn.id === "ed-change") return openEdPicker(st);
  const all = [...st.candidates, ...st.others];
  const other = btn.dataset.link && all.find((b) => b.book_id === btn.dataset.link);
  const act = btn.dataset.act;
  const partnerKeys = st.partner && edKeys(st, st.partner);
  const link = (keys) => ["POST", "/api/pairing/link", keys];
  if (other) {
    const keys = edKeys(st, other);
    edApply(btn, link(keys), ["POST", "/api/pairing/unlink", { ...keys, block: false }]);
  } else if (act === "none") {
    edApply(btn, ["POST", `/api/books/${encodeURIComponent(bookId)}/edition/none`],
      ["DELETE", `/api/books/${encodeURIComponent(bookId)}/edition/none`]);
  } else if (act === "clear-none") {
    edApply(btn, ["DELETE", `/api/books/${encodeURIComponent(bookId)}/edition/none`],
      ["POST", `/api/books/${encodeURIComponent(bookId)}/edition/none`]);
  } else if (act === "auto") {            // drop the hand-made link, no block: the scanner decides again
    edApply(btn, ["POST", "/api/pairing/unlink", { ...partnerKeys, block: false }], link(partnerKeys));
  } else if (act === "unlink") {
    confirmDialog({
      title: "Unlink these editions?",
      message: `“${st.book.title}” and “${st.partner.title}” will be unpaired, and the scanner won't pair them again. You can undo it here, or use Allow pairing under Blocked pairs on the Editions page.`,
      confirmLabel: "Unlink",
      danger: true,
    }).then((ok) => {
      if (!ok) return;
      // A manual pair comes back with a link; an automatic one only needs its block lifted.
      edApply(btn, ["POST", "/api/pairing/unlink", partnerKeys],
        st.mode === "manual" ? link(partnerKeys) : ["POST", "/api/pairing/unblock", partnerKeys]);
    });
  }
}

let edApplying = false;

// `call` / `undo` are [method, url, body]. Redraws in place so unsaved edits in the form above
// survive; the reload the new pairing needs waits for the dialog to close.
async function edApply(btn, call, undo) {
  if (edApplying) return;
  edApplying = true;
  const note = document.getElementById("ed-note");
  btn.disabled = true;
  note.textContent = "Saving…";
  try {
    await postJson(call[1], call[2], call[0]);
    await edSaved(undo ? "Saved · " : "Undone", undo);
  } catch (e) {
    btn.disabled = false;
    note.textContent = `Couldn't save: ${failReason(e)}.`;
  } finally {
    edApplying = false;
  }
}

async function edSaved(text, undo) {
  edState = await edFetch();
  edChanged = true;
  renderEditionWell(edState);
  renderEditionCard(edState);
  const note = document.getElementById("ed-note");
  note.textContent = text;
  if (!undo) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bd-reset";
  btn.textContent = "Undo";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await postJson(undo[1], undo[2], undo[0]);
      await edSaved("Undone");
    } catch (e) {
      btn.disabled = false;
      note.textContent = `Couldn't undo: ${failReason(e)}.`;
    }
  });
  note.append(btn);
}
