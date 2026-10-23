// Series detail page (/series?name=…). The library grid links here instead of
// carrying volume rows itself; this is where full volume titles, per-volume
// stats and every download control live.
//
// Series names contain spaces, brackets and sometimes "/", so the name arrives
// as a query parameter rather than a path segment — a %2F in a path is
// commonly normalized by a reverse proxy in front of the app before it
// ever sees it.
"use strict";

let VOLS = [];

const el = (id) => document.getElementById(id);
const page = el("page");
const SERIES_NAME = new URLSearchParams(location.search).get("name") || "";
let GENRES = [];


// ── manage mode ──────────────────────────────────────────────────────────────

async function editTitle(bookId, title) {
  const resp = await fetch(`/api/books/${bookId}/title`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) {
    const d = await resp.json().catch(() => ({}));
    throw new Error(d.detail || `HTTP ${resp.status}`);
  }
  const data = await resp.json();
  const b = VOLS.find((x) => x.book_id === bookId);
  if (b) b.title = data.title;
}

async function deleteBook(bookId) {
  try {
    const resp = await fetch(`/api/books/${bookId}`, { method: "DELETE" });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      toast(`Delete failed: ${d.detail || `HTTP ${resp.status}`}`);
      return;
    }
  } catch (e) {
    toast(`Delete failed: ${e.message}`);
    return;
  }
  VOLS = VOLS.filter((b) => b.book_id !== bookId);
  render();
}

// Series-level edits. Each is one request → one server build, then a reload
// so the page shows what the server actually derived (a reset volume may leave
// the series entirely).

// Volumes whose series membership is their own. A pair-inherited translation
// follows its partner, so it is never numbered or renamed from here.
const ownVols = () => VOLS.filter((b) => b.series_source !== "pair");

async function assignSeries(assignments) {
  await postJson("/api/books/series", { assignments });
  await load();
}

const pad = (n, width) => String(n).padStart(width, "0");

// The order a newly added volume gets: one past the highest numeric order.
function nextOrder() {
  const nums = VOLS.map((b) => parseInt(b.series_order, 10)).filter((n) => !isNaN(n));
  return pad(nums.length ? Math.max(...nums) + 1 : 1, 2);
}

// Move volume `i` of the own-volume list one place (`dir` -1 up, +1 down). Two
// volumes with distinct numbers just trade them, so labels like "1.5" survive;
// otherwise (blank or equal numbers) the whole series is numbered 01…NN.
async function moveVolume(bookId, dir) {
  const vols = ownVols();
  const i = vols.findIndex((b) => b.book_id === bookId), j = i + dir;
  if (i < 0 || j < 0 || j >= vols.length) return;
  const [a, b] = [vols[i], vols[j]];
  let items;
  if (a.series_order && b.series_order && a.series_order !== b.series_order) {
    items = [[a, b.series_order], [b, a.series_order]];
  } else {
    const seq = vols.slice();
    [seq[i], seq[j]] = [seq[j], seq[i]];
    const width = Math.max(2, String(seq.length).length);
    items = seq.map((v, k) => [v, pad(k + 1, width)]).filter(([v, o]) => v.series_order !== o);
  }
  await assignSeries(items.map(([v, o]) => ({ book_id: v.book_id, series_name: SERIES_NAME, series_order: o })));
}

async function renameSeries() {
  const v = await promptDialog({
    title: "Rename series", confirmLabel: "Rename",
    message: "Every volume moves to the new name. Using the name of another series merges the two.",
    fields: [{ name: "name", label: "Series name", value: SERIES_NAME, required: true }],
  });
  if (!v || v.name === SERIES_NAME) return;
  const r = await postJson("/api/series/rename", { from_name: SERIES_NAME, to_name: v.name });
  location.replace(`/series?name=${encodeURIComponent(r.series_name)}`);
}

// A searchable list of books outside this series; picking one adds it as the
// next volume. Resolves to the picked book or null.
function pickBook(candidates) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "dlg";
    dlg.innerHTML = `<div class="modal-box pick-box">
        <h3 class="modal-title">Add a book to ${esc(SERIES_NAME)}</h3>
        <input class="modal-input" type="search" placeholder="Search title, author or series" aria-label="Search books">
        <ul class="pick-list" role="listbox"></ul>
        <div class="modal-actions"><button type="button" class="btn" data-cancel>Cancel</button></div>
      </div>`;
    const input = dlg.querySelector("input"), list = dlg.querySelector(".pick-list");
    const show = () => {
      const q = input.value.trim().toLowerCase();
      const hits = candidates.filter((b) =>
        !q || `${b.title || ""} ${b.author || ""} ${b.series_name || ""}`.toLowerCase().includes(q));
      list.innerHTML = hits.slice(0, 40).map((b) =>
        `<li><button type="button" class="pick-item" data-id="${esc(b.book_id)}">
           <span class="pick-title">${esc(b.title || "(untitled)")}</span>
           <span class="pick-sub">${esc([b.author, b.series_name && `in ${b.series_name}`].filter(Boolean).join(" · "))}</span>
         </button></li>`).join("") || `<li class="pick-empty">No matching books</li>`;
    };
    let picked = null;
    list.addEventListener("click", (e) => {
      const item = e.target.closest(".pick-item");
      if (!item) return;
      picked = candidates.find((b) => b.book_id === item.dataset.id);
      dlg.close();
    });
    input.addEventListener("input", show);
    dlg.querySelector("[data-cancel]").addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener("close", () => { dlg.remove(); resolve(picked); });
    document.body.appendChild(dlg);
    show();
    dlg.showModal();
    input.focus();
  });
}

async function addBook() {
  const have = new Set(VOLS.map((b) => b.book_id));
  const all = await (await fetch("/api/books")).json();
  const book = await pickBook(all.filter((b) => !have.has(b.book_id)));
  if (!book) return;
  const order = nextOrder();
  await assignSeries([{ book_id: book.book_id, series_name: SERIES_NAME, series_order: order }]);
  toast(`Added "${book.title}" as volume ${order}`, "ok");
}

async function setSeriesGenre(genre) {
  await postJson("/api/books/genre", { book_ids: VOLS.map((b) => b.book_id), genre });
  for (const b of VOLS) b.genre = genre || null;
}

// Run a manage action; report a failure as a toast and leave the page as it was.
async function attempt(label, fn) {
  try {
    await fn();
  } catch (err) {
    toast(`${label} failed: ${err.message}`);
    render();
  }
}

page.addEventListener("click", async (e) => {
  if (!manage.on) return;
  const act = e.target.closest("[data-act]");
  if (act) {
    const id = act.dataset.bookId;
    const run = {
      rename: () => attempt("Rename", renameSeries),
      add: () => attempt("Add", addBook),
      up: () => attempt("Reorder", () => moveVolume(id, -1)),
      down: () => attempt("Reorder", () => moveVolume(id, 1)),
      reset: () => attempt("Reset", () => assignSeries([{ book_id: id, reset: true }])),
      order: async () => {
        const b = VOLS.find((x) => x.book_id === id);
        const v = await promptDialog({
          title: "Volume number", confirmLabel: "Save",
          message: "Free text — 02, 10, 1.5, 上. Leave empty to clear the number.",
          fields: [{ name: "order", label: "Volume number", value: b.series_order || "" }],
        });
        if (v) await attempt("Number update", () =>
          assignSeries([{ book_id: id, series_name: SERIES_NAME, series_order: v.order }]));
      },
    }[act.dataset.act];
    if (run) await run();
    return;
  }

  const editBtn = e.target.closest(".vol-edit-title[data-book-id]");
  if (editBtn) {
    const v = await promptDialog({
      title: "Edit title", confirmLabel: "Save",
      fields: [{ name: "title", label: "Book title", value: editBtn.dataset.title, required: true }],
    });
    if (!v) return;
    try {
      await editTitle(editBtn.dataset.bookId, v.title);
      render();
    } catch (err) {
      toast(`Title update failed: ${err.message}`);
    }
    return;
  }

  const menu = e.target.closest(".row-menu-btn[data-book-id]");
  if (!menu) return;
  openMenu(menu, [{
    label: "Delete book…", danger: true,
    run: async () => {
      const ok = await confirmDialog({
        title: "Delete book", message: `"${menu.dataset.title}"`, warning: FILE_DELETE_WARNING,
        confirmLabel: "Delete", danger: true,
      });
      if (ok) deleteBook(menu.dataset.bookId);
    },
  }]);
});

const manage = manageMode(page, el("manage-btn"));

page.addEventListener("change", (e) => {
  const sel = e.target.closest("#series-genre");
  if (!sel || !manage.on) return;
  sel.disabled = true;
  attempt("Genre update", async () => { await setSeriesGenre(sel.value); render(); });
});

// ── render ───────────────────────────────────────────────────────────────────

// Volume rows share /next's queue-row layout: volume number where the rank
// sits, cover, title + stats line, badges, strain chip, download buttons.
function volRow(b, showLang, pos) {
  const v = vocabOf(b);
  const title = esc(b.title || "(untitled)");

  const sub = [];
  if (v) {
    sub.push(`${kFmt(v.words_total)} words`);
    sub.push(`difficulty ${v.difficulty_avg}`);
  }
  if (knownPct(b) != null) sub.push(`${Math.round(knownPct(b))}% known`);

  const badges = [];
  if (showLang) badges.push(`<span class="chip lang-${b.language}">${(b.language || "?").toUpperCase()}</span>`);
  if (isRead(b)) badges.push(`<span class="chip read">${icon("check")} Read</span>`);

  const h = hardPct(b);
  const s = h != null ? strainBucket(h) : null;
  const strain = s
    ? `<span class="chip strain ${s.cls}" title="${h}% of sentences have 2+ unknown words">${s.label}</span>`
    : "";

  const cover = b.cover
    ? `<img loading="lazy" src="${esc(coverUrl(b))}" alt="">`
    : icon("menu_book", "placeholder");

  return `<div class="row queue-row">
    <span class="queue-rank">${b.series_order ? esc(b.series_order) : ""}</span>
    <div class="queue-cover">${cover}</div>
    <div class="queue-info">
      <a class="queue-title" href="/book/${encodeURIComponent(b.book_id)}">${title}</a>
      <div class="queue-sub">${sub.map((t) => `<span>${t}</span>`).join("")}</div>
    </div>
    <div class="queue-badges">${strain}${badges.join("")}</div>
    <div class="queue-actions">
      ${dlEpub(b.book_id, { size: "sm", tone: "quiet", label: b.title || "" })}${dlAudio(bookAudio(b), { size: "sm", placeholder: true, tone: "quiet", label: b.title || "" })}
      <span class="edit-group">
      ${pos ? `<button class="vol-move vol-up" data-act="up" data-book-id="${esc(b.book_id)}" aria-label="Move ${title} earlier"${pos.first ? " disabled" : ""}>${icon("arrow_back")}</button>
      <button class="vol-move vol-down" data-act="down" data-book-id="${esc(b.book_id)}" aria-label="Move ${title} later"${pos.last ? " disabled" : ""}>${icon("arrow_forward")}</button>
      <button class="vol-text" data-act="order" data-book-id="${esc(b.book_id)}" title="Set the volume number" aria-label="Set volume number for ${title}">#</button>
      ${b.series_source === "override" ? `<button class="vol-text" data-act="reset" data-book-id="${esc(b.book_id)}" title="Back to the automatic series and number" aria-label="Reset series for ${title}">Reset</button>` : ""}` : ""}
      <button class="vol-edit-title" data-book-id="${esc(b.book_id)}" data-title="${esc(b.title || "")}" aria-label="Edit title for ${title}">${icon("edit")}</button>
      ${moreBtn(`data-book-id="${esc(b.book_id)}" data-title="${title}"`, `More actions for ${b.title || "(untitled)"}`)}
      </span>
    </div>
  </div>`;
}

// Series-wide readout: the series read as one long book. Each % is weighted
// by what it is a share of (strain/flow by sentences, known by content tokens),
// so a long vol 5 counts for more than a short vol 1. Only volumes with both a
// vocab analysis and a known entry count. words_to_98 is left out: unknown
// words overlap between volumes, so per-volume figures can't be summed.
function seriesStats(vols) {
  const scored = vols.filter((b) => vocabOf(b) && b.known && b.known.hard_pct != null);
  const words = vols.reduce((n, b) => n + (vocabOf(b) ? vocabOf(b).words_total || 0 : 0), 0);
  if (!scored.length) return { words, scored };
  const wavg = (val, weight) => {
    let num = 0, den = 0;
    for (const b of scored) {
      const w = weight(b), x = val(b);
      if (w && x != null) { num += x * w; den += w; }
    }
    return den ? Math.round((num / den) * 10) / 10 : null;
  };
  const sents = (b) => vocabOf(b).sentences;
  const hards = scored.map(hardPct);
  return {
    words,
    scored,
    hard: wavg(hardPct, sents),
    flow: wavg((b) => b.known.flow_pct, sents),
    known: wavg(knownPct, (b) => vocabOf(b).content_tokens),
    hardMin: Math.min(...hards),
    hardMax: Math.max(...hards),
  };
}

function readoutHtml(st, vols) {
  if (!st.scored.length) return "";
  const s = strainBucket(st.hard);
  const nRead = vols.filter(isRead).length;
  const range = st.scored.length > 1 ? ` (volumes range ${st.hardMin}–${st.hardMax}%)` : "";
  // Neutral tiles (.readout--neutral): a fixed colour per metric could contradict the
  // verdict ("high" in green), so only the Strain value is inked, by its bucket.
  const tile = (label, val, title, valCls = "") =>
    `<div title="${title}"><div class="stat-label">${label}</div><div class="stat-val ${valCls}">${val}</div></div>`;
  const tiles = [
    tile("Strain", s.label.replace(" strain", ""), `${st.hard}% of sentences across the series have 2+ unknown words${range}`, s.cls),
    tile("Read", `${nRead}<small>/ ${vols.length} vols</small>`, "Volumes marked read"),
    tile("Known", `${st.known}%`, "Known words across the series"),
    tile("Flow", `${st.flow}%`, "Sentences across the series with no unknown words"),
  ].join("");
  const partial = st.scored.length < vols.length
    ? `<div class="readout-note">Based on ${st.scored.length} of ${vols.length} volumes</div>`
    : "";
  return `
    <div class="readout readout--neutral">
      <div class="readout-label">SERIES READOUT</div>
      <div class="stat-grid">${tiles}</div>
      ${strainStrip(vols)}
      ${partial}
    </div>`;
}

// Series-wide controls, shown only in Edit mode (CSS). The genre select reads
// "Mixed" when the volumes disagree; picking one sets them all.
function toolsHtml() {
  const genres = new Set(VOLS.map((b) => b.genre || ""));
  const cur = genres.size === 1 ? [...genres][0] : null;
  const opts = [
    cur === null ? `<option value="" selected disabled>Mixed</option>` : "",
    `<option value=""${cur === "" ? " selected" : ""}>— none —</option>`,
    ...GENRES.map((g) => `<option value="${esc(g)}"${g === cur ? " selected" : ""}>${esc(g)}</option>`),
  ].join("");
  return `<div class="series-tools">
      <button class="btn" data-act="rename">Rename series</button>
      <button class="btn" data-act="add">Add a book…</button>
      <label class="series-genre">Genre <select id="series-genre">${opts}</select></label>
    </div>`;
}

// The volumes in the reader's language: the ones the hero's cover, author, readout and
// "Next up" describe. A pair-inherited translation sits in this series too, but has no
// known stats for this profile and isn't a volume they'd pick up next.
function readerVolsOf(vols) {
  const langs = new Set(vols.filter((b) => b.known).map((b) => b.language));
  return langs.size ? vols.filter((b) => langs.has(b.language)) : vols;
}

// Whole-number volume labels the list skips between two neighbours: "Vols 02–03 …".
// Only reader-language volumes count, and a fractional label ("1.5") never makes a gap.
function gapNote(prev, cur) {
  const whole = (b) => (/^\d+$/.test(b.series_order || "") ? parseInt(b.series_order, 10) : null);
  const a = whole(prev), z = whole(cur);
  if (a == null || z == null || z - a < 2) return "";
  const width = Math.max(prev.series_order.length, 2);
  const lo = pad(a + 1, width), hi = pad(z - 1, width);
  return z - a === 2 ? `Vol ${lo} isn't in the library` : `Vols ${lo}–${hi} aren't in the library`;
}

// If "Start with / Next up" is a high-strain volume and an unread volume in an easier
// Read Next band exists, name the gentlest one: the default stays the first unread volume,
// the reader is told there is a softer way in.
function gentlerHint(next, readerVols) {
  const hp = hardPct(next);
  if (hp == null || queueBand(hp).id !== "study") return "";
  const rank = (b) => QUEUE_BANDS.indexOf(queueBand(hardPct(b)));
  const easier = readerVols
    .filter((b) => b !== next && !isRead(b) && hardPct(b) != null && rank(b) < rank(next))
    .sort((a, b) => hardPct(a) - hardPct(b))[0];
  if (!easier) return "";
  const vol = easier.series_order ? `Vol ${esc(easier.series_order)}` : esc(easier.title || "another volume");
  return `<a class="next-up-hint" href="/book/${encodeURIComponent(easier.book_id)}">${vol} is gentler · ${strainBucket(hardPct(easier)).label}</a>`;
}

function render() {
  if (!VOLS.length) {
    page.innerHTML =
      `<a class="back-link" href="/">${icon("arrow_back")} Library</a>` +
      `<p class="empty-msg">No series named "${esc(SERIES_NAME)}" in the library.</p>`;
    return;
  }

  const readerVols = readerVolsOf(VOLS);
  // Cover and author come from the reader's own volumes, not whichever edition sorts first.
  const first = readerVols[0];
  const coverBook = readerVols.find((v) => v.cover) || VOLS.find((v) => v.cover) || null;
  const cover = coverBook
    ? `<img src="${esc(coverUrl(coverBook))}" alt="">`
    : `${icon("library_books", "placeholder")}`;

  const st = seriesStats(readerVols);
  const nVols = VOLS.length;
  const allRead = readerVols.every(isRead);

  const langs = [...new Set(VOLS.map((v) => v.language).filter(Boolean))];
  const chips = [];
  for (const l of langs) chips.push(`<span class="chip lang-${l}">${l.toUpperCase()}</span>`);
  if (allRead) chips.push(`<span class="chip read">${icon("check")} Series read</span>`);
  if (first.pair_id && first.pair_partner) chips.push(`<span class="chip pair">JP+EN</span>`);

  const nReader = readerVols.length;
  const meta = [`${nReader} volume${nReader === 1 ? "" : "s"}`];
  if (nVols > nReader) meta[0] += ` + ${nVols - nReader} translated`;
  if (st.words) meta.push(`${kFmt(st.words)} words`);

  // "Next up": the first unread volume — where you'd pick the series back up.
  const next = readerVols.find((b) => !isRead(b)) || readerVols[0];
  const nextLabel = allRead ? "Start again" : next === readerVols[0] ? "Start with" : "Next up";
  const nextOrder = next.series_order ? `Vol ${esc(next.series_order)}` : "";

  const showLang = langs.length > 1;

  page.innerHTML = `
    <a class="back-link" href="/">${icon("arrow_back")} Library</a>
    <div class="book-hero">
      <div class="book-cover">${cover}<span class="series-badge" title="${meta[0]}">${icon("library_books")} ${nReader}</span></div>
      <div class="book-head">
        <div class="hero-main">
          <div>
            <h2 class="book-title">${esc(SERIES_NAME)}</h2>
            <div class="book-author">${esc(first.author || "")}</div>
            <div class="book-meta">${meta.join(" · ")}</div>
          </div>
          <div class="chips">${chips.join("")}</div>
        </div>
        <div class="next-up">
          <div class="next-up-label">${nextLabel}${nextOrder ? ` · ${nextOrder}` : ""}</div>
          <a class="next-up-title" href="/book/${encodeURIComponent(next.book_id)}">${esc(next.title || "(untitled)")}</a>
          ${allRead ? "" : gentlerHint(next, readerVols)}
          <div class="book-actions">${dlEpub(next.book_id)}${dlAudio(bookAudio(next), { placeholder: true })}</div>
        </div>
      </div>
      ${nReader > 1 ? readoutHtml(st, readerVols) : ""}
    </div>
    <div class="series-vols">
      <div class="vols-head">
        <h3>Volumes</h3>
        ${toolsHtml()}
      </div>
      <div class="queue-list">${VOLS.map((b) => {
        // Translations follow their partner's order, so they get no move controls.
        const own = ownVols(), i = own.indexOf(b);
        const prev = readerVols.indexOf(b) > 0 ? readerVols[readerVols.indexOf(b) - 1] : null;
        const gap = prev ? gapNote(prev, b) : "";
        return (gap ? `<div class="vol-gap">${gap}</div>` : "") +
          volRow(b, showLang, i < 0 ? null : { first: i === 0, last: i === own.length - 1 });
      }).join("")}</div>
    </div>`;
}

async function load() {
  const [res, genres] = await Promise.all([
    fetch(`/api/books?series=${encodeURIComponent(SERIES_NAME)}`),
    GENRES.length ? null : fetch("/api/genres").then((r) => r.json()).catch(() => null),
  ]);
  if (genres) GENRES = genres.genres || [];
  const books = await res.json();
  // Match on series name alone, the same key the library grid groups on, so
  // pair-inherited translated editions stay with the JP originals.
  VOLS = books.filter((b) => b.series_name === SERIES_NAME);
  // A translation shares its partner's number: the reader's own edition leads the pair.
  const mine = new Set(readerVolsOf(VOLS));
  VOLS.sort((a, b) => orderCmp(a.series_order, b.series_order) || mine.has(b) - mine.has(a)
    || (a.title || "").localeCompare(b.title || ""));
  document.title = SERIES_NAME ? `${SERIES_NAME} — Series` : "Series";
  render();
}

load().catch((e) => {
  page.innerHTML = `<div class="error-box">Failed to load series: ${e}</div>`;
});
