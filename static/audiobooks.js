// Audiobooks UI: fetch the audiobook index once, group by series client-side.
// Cards are navigation only — volume titles, runtimes and every download
// control live on /audiobook-series (static/audiobook_series.js).
"use strict";

let GROUPS = [];
let TOTAL = 0;
// Server-confirmed match set for the current query (ab_ids), or null when
// there is no query yet / the server hasn't answered (matches() falls back
// to a local substring scan in that window).
let searchIds = null;

const el = (id) => document.getElementById(id);
const grid = el("grid");

// Filter/sort/search state lives in the URL query (see viewstate.js).
const VS = ViewState.create({
  path: "/audiobooks",
  fields: [
    { param: "q", id: "q" },
    { param: "avail", id: "avail" },
    { param: "genre", id: "genre" },
    { param: "sort", id: "sort", def: "series" },
  ],
});

// ── manage mode ──────────────────────────────────────────────────────────────
// Deleting a whole series from a card would be a footgun (mirrors Library);
// multi-volume series are still deleted one at a time on /audiobook-series.

async function deleteAudiobook(abId) {
  try {
    const resp = await fetch(`/api/audiobooks/${encodeURIComponent(abId)}`, { method: "DELETE" });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      toast(`Delete failed: ${d.detail || `HTTP ${resp.status}`}`);
      return;
    }
  } catch (e) {
    toast(`Delete failed: ${e.message}`);
    return;
  }
  await load();
}

grid.addEventListener("click", async (e) => {
  const menu = e.target.closest(".row-menu-btn[data-ab-id]");
  if (!menu || !manage.on) return;
  openMenu(menu, [{
    label: "Delete audiobook…", danger: true,
    run: async () => {
      const ok = await confirmDialog({
        title: "Delete audiobook", message: `"${menu.dataset.title}"`, warning: FILE_DELETE_WARNING,
        confirmLabel: "Delete", danger: true,
      });
      if (ok) deleteAudiobook(menu.dataset.abId);
    },
  }]);
});

const manage = manageMode(grid, el("manage-btn"));

async function load() {
  const res = await fetch("/api/audiobooks");
  const entries = await res.json();
  TOTAL = entries.length;

  const map = new Map();
  for (const e of entries) {
    const key = `${e.author || ""} ${e.series || e.title || ""}`;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(e);
  }

  GROUPS = [...map.values()].map((vols) => {
    vols.sort(byVolume((e) => e.order));
    const first = vols[0];
    // The volume that actually supplies the level sticker's number — needed
    // alongside the number so the sticker can reflect *that volume's* own
    // match_type/confidence rather than some other volume's.
    const nativelyVol = vols.find((v) => v.natively_level != null);
    return {
      series: first.series || first.title || "(untitled)",
      // Gojūon order: the server's hiragana reading of the series name.
      sortKey: first.series_reading || first.series || first.title || "",
      author: first.author || "",
      genre: vols.map((v) => v.genre).find((x) => x) || "",
      natively: nativelyVol ? nativelyVol.natively_level : null,
      nativelyMeta: nativelyVol
        ? {
            match_type: nativelyVol.natively_match_type,
            confidence: nativelyVol.natively_confidence,
            matched_title: nativelyVol.natively_matched_title,
          }
        : null,
      vols,
      nLocal: vols.filter((v) => v.local).length,
      // Volumes known only by their file name (audiobook_catalog.local_row).
      guessed: vols.some((v) => v.source === "filename"),
      cover: vols.map((v) => v.cover_url).find((c) => c) || null,
    };
  });

  // Populate the genre filter from whatever genres the catalog actually has.
  const genres = [...new Set(GROUPS.map((g) => g.genre).filter(Boolean))].sort(
    (a, b) => a.localeCompare(b, "ja")
  );
  const sel = el("genre");
  for (const name of genres) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    sel.appendChild(o);
  }

  // "Needs details" is offered only while something does.
  document.querySelector('.seg[data-for="avail"] [data-val="details"]').hidden = !GROUPS.some((g) => g.guessed);

  VS.apply();   // URL → controls, now that the genre options exist
  render();
  if (el("q").value.trim()) runSearch();
}

function matches(g) {
  const q = el("q").value.trim().toLowerCase();
  if (q) {
    if (searchIds) {
      // A group matches if any of its volumes is in the server-confirmed result set.
      if (!g.vols.some((v) => searchIds.has(v.ab_id))) return false;
    } else {
      const hay = `${g.series} ${g.author} ${g.vols.map((v) => v.title || "").join(" ")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
  }
  const avail = el("avail").value;
  if (avail === "local" && g.nLocal === 0) return false;
  if (avail === "full" && g.nLocal !== g.vols.length) return false;
  if (avail === "missing" && g.nLocal !== 0) return false;
  if (avail === "details" && !g.guessed) return false;
  const genre = el("genre").value;
  if (genre && g.genre !== genre) return false;
  return true;
}

function sortGroups(list) {
  const mode = el("sort").value;
  const arr = [...list];
  if (mode === "author") {
    arr.sort((a, b) => a.author.localeCompare(b.author) || readingCmp(a.sortKey, b.sortKey));
  } else if (mode === "volumes") {
    arr.sort((a, b) => b.vols.length - a.vols.length || readingCmp(a.sortKey, b.sortKey));
  } else if (mode === "level") {
    // Natively level ascending (easiest first); unrated series sort to the end.
    arr.sort((a, b) => {
      const va = a.natively, vb = b.natively;
      if (va == null && vb == null) return readingCmp(a.sortKey, b.sortKey);
      if (va == null) return 1;
      if (vb == null) return -1;
      return va - vb || readingCmp(a.sortKey, b.sortKey);
    });
  } else {
    arr.sort((a, b) => readingCmp(a.sortKey, b.sortKey));
  }
  return arr;
}

// Groups key on author *and* series (unlike the EPUB page), so the detail page
// needs both to find its way back to the same group.
function seriesHref(g) {
  return `/audiobook-series?name=${encodeURIComponent(g.series)}` +
    `&author=${encodeURIComponent(g.author)}`;
}

function card(g) {
  const cover = g.cover
    ? `<img loading="lazy" src="${esc(g.cover)}" alt="" referrerpolicy="no-referrer">`
    : `${icon("headphones", "placeholder")}`;
  const href = seriesHref(g);
  const lv = levelSticker(g.natively, g.nativelyMeta);

  const chips = [];
  if (g.guessed) chips.push(`<span class="chip needs-details" title="Details guessed from the file name: open it to add them">Needs details</span>`);
  else if (g.genre) chips.push(`<span class="chip genre" title="${esc(g.genre)}">${esc(g.genre)}</span>`);
  chips.push(
    `<span class="chip ${g.nLocal === g.vols.length ? "have-all" : g.nLocal ? "have-some" : "have-none"}">` +
    `${g.nLocal}/${g.vols.length} local</span>`
  );

  const badge = g.vols.length > 1
    ? `<span class="series-badge" title="${g.vols.length} volumes">${icon("library_books")} ${g.vols.length}</span>`
    : "";

  // Only a solo, locally-downloaded entry is safe to delete straight from the
  // card — nothing to delete for an Audible-only entry, and a multi-volume
  // series is deleted one volume at a time on /audiobook-series.
  const soloLocal = g.vols.length === 1 && g.vols[0].local;
  const menuBtn = soloLocal
    ? moreBtn(`data-ab-id="${esc(g.vols[0].ab_id)}" data-title="${esc(g.series)}"`, `More actions for ${g.series}`)
    : "";

  return `
    <div class="card ab-card">
      ${soloLocal ? `<div class="edit-group">${menuBtn}</div>` : ""}
      <a class="cover-link" href="${href}"><div class="cover">${cover}${lv}${badge}</div></a>
      <div class="card-body">
        <div class="title" title="${esc(g.series)}"><a class="title-link" href="${href}">${esc(g.series)}</a></div>
        <div class="author">${esc(g.author)}</div>
        <div class="chips">${chips.join("")}</div>
      </div>
    </div>`;
}

function render() {
  const filtered = sortGroups(GROUPS.filter(matches));
  grid.innerHTML = filtered.map(card).join("");
  const series =
    filtered.length === GROUPS.length
      ? `${GROUPS.length} series`
      : `${filtered.length} of ${GROUPS.length} series`;
  el("count").textContent = `${series} · ${TOTAL} audiobooks`;
}

// #avail is driven by its segmented pills (wired generically in nav.js); the
// listener here re-renders on its input event just as it does for genre/sort.
["avail", "genre", "sort"].forEach((id) => {
  el(id).addEventListener("input", () => { render(); VS.write(); });
});

// Server-side search over the full JSON index. Instant local substring on
// keystroke, upgraded to the server-confirmed match set when it responds.
const runSearch = LibrarySearch.debounce(async () => {
  const q = el("q").value.trim();
  if (!q) { searchIds = null; render(); return; }
  try {
    const { ids } = await LibrarySearch.fetchIds("audiobooks", q);
    if (el("q").value.trim() !== q) return;
    searchIds = ids;
  } catch { searchIds = null; }
  render();
}, 200);

el("q").addEventListener("input", () => {
  searchIds = null;
  render();
  runSearch();
  VS.write();
});

load().catch((e) => {
  grid.innerHTML = `<div class="error-box">Failed to load audiobooks: ${e}</div>`;
});
