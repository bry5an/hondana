// Browse UI: fetch the combined index once, group into series client-side,
// filter/sort, with a flat sortable table view for comparing vocab stats.
"use strict";

let BOOKS = [];
let GENRES = [];
let tableSort = { key: "title", dir: 1 };
// Server-confirmed match set for the current query (book_ids), or null when
// there is no query yet / the server hasn't answered — matches() falls back
// to a local substring scan in that window, so typing feels instant and a
// search error degrades gracefully.
let searchIds = null;

const el = (id) => document.getElementById(id);
const grid = el("grid");

// Filter/sort/search state lives in the URL query (see viewstate.js), so it
// survives opening a book and coming back, and can be bookmarked.
const VS = ViewState.create({
  path: "/",
  fields: [
    { param: "q", id: "q" },
    { param: "lang", id: "lang" },
    { param: "genre", id: "genre" },
    { param: "sort", id: "sort", def: "title" },
    { param: "view", id: "view", def: "cards" },
    { param: "paired", id: "pairedOnly" },
    { param: "audio", id: "audioOnly" },
    { param: "unread", id: "hideRead" },
  ],
});

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
  const b = BOOKS.find((x) => x.book_id === bookId);
  if (b) b.title = data.title;
}

async function editSeries(bookId, seriesName, seriesOrder) {
  const resp = await fetch(`/api/books/${bookId}/series`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ series_name: seriesName, series_order: seriesOrder }),
  });
  if (!resp.ok) {
    const d = await resp.json().catch(() => ({}));
    throw new Error(d.detail || `HTTP ${resp.status}`);
  }
  const data = await resp.json();
  const b = BOOKS.find((x) => x.book_id === bookId);
  if (b) {
    b.series_name = data.series_name;
    b.series_order = data.series_order;
  }
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
  BOOKS = BOOKS.filter((b) => b.book_id !== bookId);
  render();
}

grid.addEventListener("click", async (e) => {
  const th = e.target.closest("th[data-sort-key]");
  if (th) {
    const key = th.dataset.sortKey;
    tableSort = { key, dir: tableSort.key === key ? -tableSort.dir : 1 };
    render();
    return;
  }
  const editBtn = e.target.closest(".edit-title-btn[data-book-id]");
  if (editBtn && manage.on) {
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
  const seriesBtn = e.target.closest(".edit-series-btn[data-book-id]");
  if (seriesBtn && manage.on) {
    const v = await promptDialog({
      title: "Edit series", confirmLabel: "Save",
      fields: [
        { name: "name", label: "Series name", value: seriesBtn.dataset.seriesName, placeholder: "Series name (empty = standalone)" },
        { name: "order", label: "Volume order", value: seriesBtn.dataset.seriesOrder, placeholder: "Volume order (e.g. 01, 02)" },
      ],
    });
    if (!v) return;
    try {
      await editSeries(seriesBtn.dataset.bookId, v.name, v.order);
      render();
    } catch (err) {
      toast(`Series update failed: ${err.message}`);
    }
    return;
  }

  const menu = e.target.closest(".row-menu-btn[data-book-id]");
  if (!menu || !manage.on) return;
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

async function setGenre(bookIds, genre) {
  // One request, one build — however many volumes the series card holds.
  const resp = await fetch("/api/books/genre", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book_ids: bookIds, genre }),
  });
  if (!resp.ok) {
    const d = await resp.json().catch(() => ({}));
    throw new Error(d.detail || `HTTP ${resp.status}`);
  }
  for (const b of BOOKS) {
    if (bookIds.includes(b.book_id)) b.genre = genre || null;
  }
}

grid.addEventListener("change", async (e) => {
  const sel = e.target.closest(".genre-select");
  if (!sel || !manage.on) return;
  const bookIds = sel.dataset.bookIds.split(",");
  const genre = sel.value;
  sel.disabled = true;
  try {
    await setGenre(bookIds, genre);
    render();
  } catch (err) {
    toast(`Genre update failed: ${err.message}`);
    sel.disabled = false;
  }
});

const manage = manageMode(grid, el("manage-btn"));

async function load() {
  const [booksRes, genresRes] = await Promise.all([
    fetch("/api/books"),
    fetch("/api/genres").catch(() => null),
  ]);
  BOOKS = await booksRes.json();
  if (genresRes && genresRes.ok) {
    const d = await genresRes.json();
    GENRES = (d && d.genres) || [];
  }
  // The filter offers the genres the loaded books actually carry (GENRES is the
  // fuller vocabulary the Edit-mode selects use).
  const sel = el("genre");
  for (const name of [...new Set(BOOKS.map((b) => b.genre).filter(Boolean))].sort((a, b) => a.localeCompare(b, "ja"))) {
    sel.add(new Option(name, name));
  }
  VS.apply();   // URL → controls, now that every option exists
  render();
  if (el("q").value.trim()) runSearch();
  loadSetup();
}

// ── first-run checklist ────────────────────────────────────────────────

// Per device: closing the card, or seeing every required step done, hides it for good.
const SETUP_KEY = "getting-started-hidden";
const setupHidden = () => { try { return localStorage.getItem(SETUP_KEY) === "1"; } catch { return false; } };
const hideSetup = () => {
  try { localStorage.setItem(SETUP_KEY, "1"); } catch { /* the card just comes back next visit */ }
  el("setup").hidden = true;
};

function setupStepHtml(s) {
  const mark = s.done ? icon("check") : "";
  const kind = s.required ? "" : '<span class="setup-opt">Optional</span>';
  return `<li class="setup-step${s.done ? " done" : ""}">
    <span class="setup-mark" aria-hidden="true">${mark}</span>
    <div class="setup-text">
      <strong>${esc(s.label)}</strong>${kind}
      <span class="setup-detail">${esc(s.detail)}</span>
    </div>
    <a class="ghost-btn" href="${esc(s.href)}">${esc(s.link)}${icon("arrow_forward")}</a>
  </li>`;
}

async function loadSetup() {
  if (setupHidden()) return;
  let data;
  try {
    const r = await fetch("/api/setup");
    if (!r.ok) return;
    data = await r.json();
  } catch { return; }   // a checklist is a nicety: never get in the library's way
  if (data.complete) { hideSetup(); return; }
  const left = data.steps.filter((s) => s.required && !s.done).length;
  const box = el("setup");
  box.innerHTML = `<div class="card-head">
      <h2 id="setup-h">Getting started</h2>
      <button type="button" class="ghost-btn" id="setup-close" aria-label="Hide Getting started">${icon("close")}</button>
    </div>
    <p class="hint">${left} ${left === 1 ? "step" : "steps"} left before Hondana can say what you can read.</p>
    <ol class="setup-steps">${data.steps.map(setupStepHtml).join("")}</ol>`;
  box.hidden = false;
  el("setup-close").addEventListener("click", hideSetup);
}

// ── filtering ─────────────────────────────────────────────────────────────────

function matches(b) {
  const q = el("q").value.trim().toLowerCase();
  if (q) {
    if (searchIds) {
      // Server-confirmed result set (authoritative once it arrives).
      if (!searchIds.has(b.book_id)) return false;
    } else {
      // No server answer yet (typing) or search failed → local substring.
      const hay = `${b.title || ""} ${b.author || ""} ${b.series_name || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
  }
  const lang = el("lang").value;
  if (lang && b.language !== lang) return false;

  if (el("pairedOnly").checked && !b.pair_id) return false;
  if (el("audioOnly").checked && !b.audiobook) return false;
  const hideRead = el("hideRead");
  if (hideRead && hideRead.checked && isRead(b)) return false;
  const genre = el("genre").value;
  if (genre && b.genre !== genre) return false;
  return true;
}

// ── series grouping ───────────────────────────────────────────────────────────

function groupBooks(list) {
  // Key on series name alone (not author) so translated editions with a
  // pair-inherited series land in the same group as the JP originals.
  const map = new Map();
  for (const b of list) {
    const key = b.series_name ? `s|${b.series_name}` : `solo|${b.book_id}`;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(b);
  }
  return [...map.values()].map((vols) => {
    vols.sort(byVolume((b) => b.series_order));
    // Stats describe volume 1, not an average: vol 1 is what you'd actually
    // pick up, and averaging it with vol 5 describes a book nobody reads.
    // Vol 1 can be unanalyzed (image-only/DRM — see epublib/analyze.py) or
    // lack a `known` entry, so fall back to the earliest volume that has the
    // value rather than rendering a blank card.
    const firstVal = (fn) => vols.map(fn).find((x) => x != null) ?? null;
    const first = vols[0];
    const langs = [...new Set(vols.map((v) => v.language).filter(Boolean))];
    // The volume that actually supplies the level sticker's number — needed
    // alongside the number so the sticker can reflect *that volume's* own
    // match_type/confidence rather than some other volume's.
    const nativelyVol = vols.find((b) => b.natively && b.natively.natively_level != null);
    return {
      name: first.series_name || first.title || "(untitled)",
      // Gojūon order: the series' reading, or a solo book's own title's.
      sortKey: (first.series_name ? first.series_sort : first.title_sort) || first.series_name || first.title || "",
      author: first.author || "",
      authorKey: authorKey(first),
      vols,
      langs,
      coverBook: vols.find((v) => v.cover) || null,
      audio: vols.some((v) => v.audiobook),
      // Synced only when every volume that has an audiobook is (a series that is half synced isn't).
      audioSynced: vols.some((v) => v.audiobook) && vols.filter((v) => v.audiobook).every((v) => v.audio_synced === true),
      genre: firstVal((b) => b.genre || null),
      natively: nativelyVol ? nativelyVol.natively.natively_level : null,
      nativelyMeta: nativelyVol ? nativelyVol.natively : null,
      diff: firstVal(diffOf),
      words: firstVal(wordsOf),
      known: firstVal(knownPct),
      hard: firstVal(hardPct),
      read: vols.every(isRead),
    };
  });
}

function sortGroups(list) {
  const mode = el("sort").value;
  const arr = [...list];
  const nullLast = (fn, dir) => (a, b) => {
    const va = fn(a), vb = fn(b);
    if (va == null && vb == null) return readingCmp(a.sortKey, b.sortKey);
    if (va == null) return 1;
    if (vb == null) return -1;
    return (va - vb) * dir || readingCmp(a.sortKey, b.sortKey);
  };
  if (mode === "strain") arr.sort(nullLast((g) => g.hard, 1));
  else if (mode === "level") arr.sort(nullLast((g) => g.natively, 1));
  else if (mode === "difficulty") arr.sort(nullLast((g) => g.diff, 1));
  else if (mode === "words") arr.sort(nullLast((g) => g.words, 1));
  else if (mode === "known") arr.sort(nullLast((g) => g.known, -1));
  else if (mode === "author")
    // On the key, not the display string, so one author's spellings stay in one run.
    arr.sort((a, b) => a.authorKey.localeCompare(b.authorKey) || readingCmp(a.sortKey, b.sortKey));
  else arr.sort((a, b) => readingCmp(a.sortKey, b.sortKey));
  return arr;
}

// ── card badges ───────────────────────────────────────────────────────────────

// Worded strain reads nicer than a bare percentage. The cut points live in ui.js
// (strainBucket) and are absolute in hard_pct terms (share of sentences with 2+
// unknown content words): roughly under 1-in-6 sentences tripping you is comfortable.
// ── cards ─────────────────────────────────────────────────────────────────────

// One card shape for everything. A card is a fixed-size link into a detail
// page — solo books to /book/{id}, series to /series?name=… — and carries no
// downloads and no volume rows; those live on the detail pages. The four chips
// are the ones you scan for: language, strain, known %, read state. Difficulty,
// length and the JP+EN pairing are on the detail page, where a fixed card
// height isn't fighting them for room.
function card(g) {
  const solo = g.vols.length === 1;
  const first = g.vols[0];
  const href = solo
    ? `/book/${encodeURIComponent(first.book_id)}`
    : `/series?name=${encodeURIComponent(g.name)}`;
  const label = solo ? first.title || "(untitled)" : g.name;

  const cover = g.coverBook
    ? `<img loading="lazy" src="${esc(coverUrl(g.coverBook))}" alt="">`
    : `${icon(solo ? "menu_book" : "library_books", "placeholder")}`;
  const badge = solo
    ? ""
    : `<span class="series-badge" title="${g.vols.length} volumes">${icon("library_books")} ${g.vols.length}</span>`;
  // Natively level sticker (top-left), the cover's hero badge.
  const lv = levelSticker(g.natively, g.nativelyMeta);
  // Audiobook sticker on the cover corner, when any volume has a linked audiobook; the dark
  // capsule when it is synced. Hidden while managing so the delete button takes the corner.
  const audioBadge = g.audio ? audioBadgeHtml(g.audioSynced) : "";

  const chips = [];
  if (g.genre)
    chips.push(`<span class="chip genre genre-chip" title="${esc(g.genre)}">${esc(g.genre)}</span>`);
  // Genre has no per-series storage — a series card's dropdown bulk-applies the
  // choice to every volume's book_id via the same per-book endpoint /add uses,
  // keeping genre's single-source-of-truth model (genre_overrides.json) intact.
  const genreOpts = GENRES.map(
    (gn) => `<option value="${esc(gn)}"${gn === g.genre ? " selected" : ""}>${esc(gn)}</option>`
  ).join("");
  chips.push(
    `<select class="chip genre genre-select" data-book-ids="${g.vols.map((v) => esc(v.book_id)).join(",")}" aria-label="Genre">` +
      `<option value=""${g.genre ? "" : " selected"}>—</option>${genreOpts}</select>`
  );
  if (g.langs.length === 1)
    chips.push(`<span class="chip lang-${g.langs[0]}">${g.langs[0].toUpperCase()}</span>`);
  else if (g.langs.length > 1)
    chips.push(`<span class="chip">${g.langs.map((l) => l.toUpperCase()).join("+")}</span>`);
  if (g.hard != null) {
    const s = strainBucket(g.hard);
    chips.push(
      `<span class="chip strain ${s.cls}" title="${g.hard}% of sentences have 2+ unknown words — the ones you stop and reassemble">${s.label}</span>`
    );
  }
  if (g.known != null)
    chips.push(`<span class="chip known" title="Known words">${icon("check")} ${g.known}%</span>`);
  if (g.read) chips.push(`<span class="chip read">${icon("check")} Read</span>`);

  // Deleting or renaming a whole series from a card would be a footgun;
  // series volumes are managed one at a time on the series page.
  const menuBtn = solo
    ? moreBtn(`data-book-id="${esc(first.book_id)}" data-title="${esc(first.title || "(untitled)")}"`,
              `More actions for ${first.title || "this book"}`)
    : "";
  const editTitleBtn = solo
    ? `<button class="edit-title-btn" data-book-id="${esc(first.book_id)}" data-title="${esc(first.title || "")}" aria-label="Edit title for ${esc(first.title || "this book")}">${icon("edit")}</button>`
    : "";
  const editSeriesBtn = solo
    ? `<button class="edit-series-btn" data-book-id="${esc(first.book_id)}" data-series-name="${esc(first.series_name || "")}" data-series-order="${esc(first.series_order || "")}" aria-label="Edit series for ${esc(first.title || "this book")}">Series</button>`
    : "";

  return `
    <div class="card">
      ${solo ? `<div class="edit-group">${editTitleBtn}${editSeriesBtn}${menuBtn}</div>` : ""}
      <a class="cover-link" href="${href}"><div class="cover">${cover}${lv}${badge}${audioBadge}</div></a>
      <div class="card-body">
        <div class="title" title="${esc(label)}"><a class="title-link" href="${href}">${esc(label)}</a></div>
        <div class="author">${esc(g.author)}</div>
        <div class="chips">${chips.join("")}</div>
      </div>
    </div>`;
}

// Paired mode shows individual books rather than series groups; wrap each in a
// one-volume group so it goes through the same card builder.
function bookCard(b) {
  return card(groupBooks([b])[0]);
}

// ── table view ────────────────────────────────────────────────────────────────

// One entry per column: `get` drives sorting, `cell` rendering. Keeping both
// on the same object is what stops the two from drifting apart.
const TABLE_COLS = [
  {
    key: "title", label: "Title", cls: "t",
    get: (b) => b.title || "",
    sortKey: (b) => b.title_sort || b.title || "",   // gojūon order
    cell: (v, b) =>
      `<a href="/book/${esc(b.book_id)}">${esc(v || "(untitled)")}</a>` +
      (isRead(b) ? ` <span class="read-tick" title="Read">${icon("check")}</span>` : ""),
  },
  { key: "series", label: "Series", cls: "t s", get: (b) => b.series_name || "",
    sortKey: (b) => b.series_sort || b.series_name || "", cell: esc },
  { key: "words", label: "Words", get: wordsOf, cell: (v) => v.toLocaleString() },
  { key: "unique", label: "Unique", get: (b) => (vocabOf(b) ? vocabOf(b).unique_words : null), cell: (v) => v.toLocaleString() },
  { key: "once", label: "Once %", get: (b) => (vocabOf(b) ? vocabOf(b).unique_words_once_pct : null), cell: (v) => v + "%" },
  { key: "diff", label: "Diff", get: diffOf },
  { key: "p90", label: "P90", get: (b) => (vocabOf(b) ? vocabOf(b).difficulty_p90 : null) },
  { key: "known", label: "Known %", get: knownPct, cell: (v) => v + "%" },
  { key: "strain", label: "Strain %", get: hardPct, cell: (v) => `<span class="strain">${v}%</span>` },
  { key: "flow", label: "Flow %", get: flowPct, cell: (v) => v + "%" },
  { key: "to98", label: "→98%", get: wordsTo98, cell: (v) => v.toLocaleString() },
];

function sortedForTable(list) {
  const col = TABLE_COLS.find((c) => c.key === tableSort.key) || TABLE_COLS[0];
  return [...list].sort((a, b) => {
    const va = col.get(a), vb = col.get(b);
    if (va == null && vb == null) return readingCmp(a.title_sort || a.title, b.title_sort || b.title);
    if (va == null) return 1;
    if (vb == null) return -1;
    const cmp = typeof va === "string" ? (col.sortKey ? readingCmp(col.sortKey(a), col.sortKey(b)) : va.localeCompare(vb)) : va - vb;
    return cmp * tableSort.dir;
  });
}

function renderTable(list) {
  const sorted = sortedForTable(list);

  const ths = TABLE_COLS.map(
    (c) =>
      `<th data-sort-key="${c.key}" class="${c.key === tableSort.key ? (tableSort.dir === 1 ? "asc" : "desc") : ""}">${c.label}</th>`
  ).join("");

  const rows = sorted
    .map(
      (b) =>
        `<tr>${TABLE_COLS.map((c) => {
          const v = c.get(b);
          const cls = c.cls ? ` class="${c.cls}"` : "";
          return `<td${cls}>${v == null || v === "" ? "" : (c.cell || String)(v, b)}</td>`;
        }).join("")}</tr>`
    )
    .join("");

  grid.innerHTML = `<div class="table-wrap"><table class="lib-table">
    <thead><tr>${ths}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

// ── phone list view ───────────────────────────────────────────────────────────

// The wide stats table is unreadable on a phone, so List there uses the same
// row layout as /next's queue (.queue-row): cover, title, chips, downloads.
// Tapping a cover shows the same strain/flow/words tooltip /next does.
const phoneMq = window.matchMedia("(max-width: 639px)");

function listRow(b) {
  const id = esc(b.book_id);
  const chips = [];
  if (b.language) chips.push(`<span class="chip lang-${esc(b.language)}">${esc(b.language.toUpperCase())}</span>`);
  const h = hardPct(b);
  if (h != null) {
    const s = strainBucket(h);
    chips.push(`<span class="chip strain ${s.cls}">${s.label}</span>`);
  }
  if (isRead(b)) chips.push(`<span class="chip read">${icon("check")} Read</span>`);
  if (b.pair_id) chips.push(`<span class="chip pair" title="Has a paired English edition">JP+EN</span>`);
  const cover = b.cover
    ? `<img loading="lazy" src="${esc(coverUrl(b))}" alt="">`
    : icon("menu_book", "placeholder");
  return `
    <div class="row queue-row">
      <div class="queue-cover" data-book-id="${id}">${cover}</div>
      <div class="queue-info">
        <a class="queue-title" href="/book/${id}" title="${esc(b.title || "")}">${esc(b.title || "(untitled)")}</a>
        <div class="queue-sub">${esc(b.author || "")}</div>
      </div>
      <div class="queue-badges">${chips.join("")}</div>
      <div class="queue-actions">${dlEpub(b.book_id, { size: "sm" })}${dlAudio(bookAudio(b), { size: "sm", placeholder: true })}</div>
    </div>`;
}

function renderPhoneList(list) {
  grid.innerHTML = `<div class="queue-list">${sortedForTable(list).map(listRow).join("")}</div>`;
}

function tipHtml(b) {
  const k = b.known, v = b.vocab;
  if (!k) return `<strong>${esc(b.title || "")}</strong><span>${esc(b.author || "")}</span><span>No reading-load stats yet</span>`;
  return (
    `<strong>${esc(b.title || "")}</strong>` +
    `<span>Strain ${k.hard_pct}% · Flow ${k.flow_pct}%</span>` +
    `<span>${(k.words_to_98 ?? 0).toLocaleString()} words to 98% · ${k.known_pct}% known</span>` +
    (v && v.words_total != null ? `<span>${v.words_total.toLocaleString()} total words</span>` : "")
  );
}

// Tap a cover to show its tip; tap it again or anywhere else to dismiss.
function wireListTips() {
  const tip = el("tip");
  let active = null;
  grid.addEventListener("click", (e) => {
    const cov = e.target.closest(".queue-cover[data-book-id]");
    if (!cov) return;
    if (active === cov && !tip.hidden) {
      tip.hidden = true;
      active = null;
      return;
    }
    const b = BOOKS.find((x) => x.book_id === cov.dataset.bookId);
    if (!b) return;
    active = cov;
    tip.innerHTML = tipHtml(b);
    tip.hidden = false;
    const rect = cov.getBoundingClientRect();
    const maxLeft = window.scrollX + document.documentElement.clientWidth - tip.offsetWidth - 8;
    tip.style.left = `${Math.max(window.scrollX + 8, Math.min(rect.left + window.scrollX, maxLeft))}px`;
    tip.style.top = `${rect.top + window.scrollY - tip.offsetHeight - 12}px`;
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".queue-cover[data-book-id]")) {
      tip.hidden = true;
      active = null;
    }
  });
  // The list re-renders (and the tip's anchor vanishes) on any filter change.
  window.addEventListener("scroll", () => { tip.hidden = true; active = null; }, { passive: true });
}
wireListTips();
phoneMq.addEventListener("change", () => render());

// ── render ────────────────────────────────────────────────────────────────────

function pairRow(jp, en) {
  return `<div class="pair-row">
    <div class="pair-side">${jp ? bookCard(jp) : ""}</div>
    <div class="pair-sep">${icon("swap_horiz")}</div>
    <div class="pair-side">${en ? bookCard(en) : ""}</div>
  </div>`;
}

// Nothing to show: say why rather than leave a blank page. An empty library is
// the first thing a new install sees, so it says how books get in.
function emptyHtml() {
  if (!BOOKS.length) {
    return `<div class="lib-empty"><strong>Your library is empty.</strong> Add EPUBs on
      <a href="/add">Add Book</a>, or point Hondana at the folder you keep them in
      (<a href="/settings#library">Settings</a>) and run <strong>Scan EPUBs</strong> on
      <a href="/admin">Admin</a>.</div>`;
  }
  return `<div class="lib-empty">No books match these filters. Clear the search or turn a filter off.</div>`;
}

function render() {
  const filtered = BOOKS.filter(matches);
  const pairedMode = el("pairedOnly").checked;
  const tableMode = el("view").value === "table" && !pairedMode;

  grid.classList.toggle("paired-mode", pairedMode);
  grid.classList.toggle("table-mode", tableMode);

  if (!filtered.length) {
    grid.classList.remove("phone-list");
    grid.innerHTML = emptyHtml();
    el("count").textContent = BOOKS.length ? `0 of ${BOOKS.length} books` : "0 books";
    return;
  }

  if (pairedMode) {
    const byId = Object.fromEntries(BOOKS.map((b) => [b.book_id, b]));
    const seen = new Set();
    const pairs = [];

    for (const b of filtered) {
      if (!b.pair_id || seen.has(b.pair_id)) continue;
      seen.add(b.pair_id);
      const partner = b.pair_partner ? byId[b.pair_partner.book_id] : null;
      const jp = b.language === "ja" ? b : (partner?.language === "ja" ? partner : null);
      const en = b.language === "en" ? b : (partner?.language === "en" ? partner : null);
      pairs.push({ jp, en });
    }

    const mode = el("sort").value;
    pairs.sort((a, b) => {
      const pa = a.jp ?? a.en, pb = b.jp ?? b.en;
      const lvl = (p) => (p && p.natively ? p.natively.natively_level : null);
      if (mode === "level") return (lvl(pa) ?? 999) - (lvl(pb) ?? 999);
      if (mode === "difficulty") return (diffOf(pa) ?? 999) - (diffOf(pb) ?? 999);
      if (mode === "words") return (wordsOf(pa) ?? 1e9) - (wordsOf(pb) ?? 1e9);
      if (mode === "known") return (knownPct(pb) ?? -1) - (knownPct(pa) ?? -1);
      if (mode === "author") return (pa ? authorKey(pa) : "").localeCompare(pb ? authorKey(pb) : "");
      return (pa?.title || "").localeCompare(pb?.title || "");
    });

    grid.innerHTML = pairs.length ? pairs.map(({ jp, en }) => pairRow(jp, en)).join("") : emptyHtml();
    el("count").textContent = `${pairs.length} pair(s) of ${BOOKS.length} books`;
    return;
  }

  grid.classList.toggle("phone-list", tableMode && phoneMq.matches);
  if (tableMode) {
    if (phoneMq.matches) renderPhoneList(filtered);
    else renderTable(filtered);
    el("count").textContent = `${filtered.length} of ${BOOKS.length} books`;
    return;
  }

  const groups = sortGroups(groupBooks(filtered));
  grid.innerHTML = groups.map(card).join("");
  const nSeries = groups.filter((g) => g.vols.length > 1).length;
  const books =
    filtered.length === BOOKS.length
      ? `${BOOKS.length} books`
      : `${filtered.length} of ${BOOKS.length} books`;
  el("count").textContent = books + (nSeries ? ` · ${nSeries} series` : "");
}

// Segmented pill controls (language, view) are wired generically in nav.js —
// they drive the hidden #lang / #view selects, whose input events the listeners
// below already handle.

["lang", "genre", "pairedOnly", "audioOnly", "hideRead", "sort", "view"].forEach((id) => {
  el(id).addEventListener("input", () => { render(); VS.write(); });
});

// Search is server-side (substring scan over the full JSON index, so it can
// reach fields — like descriptions — that aren't loaded client-side). Each
// keystroke filters instantly with a local substring pass, then upgrades to
// the server-confirmed match set once it responds.
const runSearch = LibrarySearch.debounce(async () => {
  const q = el("q").value.trim();
  if (!q) { searchIds = null; render(); return; }
  try {
    const { ids } = await LibrarySearch.fetchIds("epub", q);
    if (el("q").value.trim() !== q) return;   // a newer keystroke superseded us
    searchIds = ids;
  } catch { searchIds = null; }               // fall back to substring
  render();
}, 200);

el("q").addEventListener("input", () => {
  searchIds = null;   // invalidate the previous match set while typing
  render();           // instant substring feedback
  runSearch();        // debounced tokenized upgrade
  VS.write();
});

load().catch((e) => {
  grid.innerHTML = `<div class="error-box">Failed to load library: ${e}</div>`;
});
