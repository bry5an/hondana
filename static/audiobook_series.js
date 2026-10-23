// Audiobook series detail page (/audiobook-series?name=…&author=…).
//
// The audiobooks grid groups on author *and* series, so both arrive as query
// parameters and this page rebuilds the identical group. Query parameters
// rather than path segments: series names contain spaces, brackets and
// sometimes "/", and a %2F in a path is commonly normalized by the reverse
// proxy in front of the app before it sees it.
"use strict";

let VOLS = [];
// ab_id → analyzed companion JP EPUB, for the bar-chart link across to /book/{id}
let EPUB_BY_ISBN = new Map();

const el = (id) => document.getElementById(id);
const page = el("page");
const params = new URLSearchParams(location.search);
const SERIES_NAME = params.get("name") || "";
const SERIES_AUTHOR = params.get("author") || "";

// ── manage mode ──────────────────────────────────────────────────────────────

// Two different destructive actions share this dialog, so it states which one
// it is. Deleting a file is permanent; pruning a catalog row is not, and
// saying so stops the prune from feeling riskier than it is.
const DIALOG_COPY = {
  delete: {
    title: "Delete audiobook",
    confirmLabel: "Delete",
    warning: "This will permanently delete the file(s) from disk and cannot be undone.",
  },
  hide: {
    title: "Remove from library",
    confirmLabel: "Remove",
    warning: "This hides the title from your library. No files are deleted, and you " +
             "can restore it from Admin → Pruned audiobooks.",
  },
};

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

async function hideAudiobook(abId) {
  try {
    const resp = await fetch("/api/audiobooks/hide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ab_ids: [abId] }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      toast(`Remove failed: ${d.detail || `HTTP ${resp.status}`}`);
      return;
    }
  } catch (e) {
    toast(`Remove failed: ${e.message}`);
    return;
  }
  await load();
}

// Same dialog for both actions; the copy says which one it is.
function confirmVolume(kind, title) {
  return confirmDialog({ ...DIALOG_COPY[kind], message: `"${title}"`, danger: true });
}

page.addEventListener("click", async (e) => {
  if (!manage.on) return;
  const menu = e.target.closest(".row-menu-btn[data-ab-id]");
  if (!menu) return;
  const { abId, title } = menu.dataset;
  const items = [{
    label: "Remove from library…",
    run: async () => { if (await confirmVolume("hide", title)) hideAudiobook(abId); },
  }];
  // Only a downloaded volume has a file to delete.
  if (menu.dataset.local) items.push({
    label: "Delete file…", danger: true,
    run: async () => { if (await confirmVolume("delete", title)) deleteAudiobook(abId); },
  });
  openMenu(menu, items);
});

const manage = manageMode(page, el("manage-btn"));

// ── render ───────────────────────────────────────────────────────────────────

function runtime(min) {
  if (min == null) return "";
  return min >= 60 ? `${Math.floor(min / 60)}h${min % 60 ? (min % 60) + "m" : ""}` : `${min}m`;
}

function volAction(v) {
  // A folder of tracks (mp3s, or an album of m4a/flac/opus files): one link each.
  if (v.local && v.files && v.files.length) {
    const tracks = v.files.map((f) => dlTrack(v.ab_id, f)).join("");
    const kind = v.format === "mp3-folder" ? "MP3s" : "tracks";
    return `<details class="tracks"><summary>${icon("download")} ${v.files.length} ${kind}</summary>${tracks}</details>`;
  }
  return dlAudio({ id: v.ab_id, local: v.local, audibleUrl: v.audible_url, format: v.format },
                 { size: "sm", placeholder: true });
}

// Companion-EPUB chip: links across to /book/{id}.
function vocabChip(v) {
  const epub = EPUB_BY_ISBN.get(v.ab_id);
  if (!epub) return "";
  const words = epub.vocab.words_total;
  const wordsShort = words >= 10000 ? `${Math.round(words / 1000)}k` : words;
  return `<a class="chip" href="/book/${esc(epub.book_id)}" ` +
    `title="Companion EPUB: ${esc(epub.title || "")} — ${words.toLocaleString()} words (${wordsShort}), difficulty ${epub.vocab.difficulty_avg}/100">` +
    `${icon("bar_chart")} ${epub.vocab.difficulty_avg}</a>`;
}

// Worded strain comes from ui.js's strainBucket(), like every other page.
// Volume rows share /next's queue-row layout. Audiobooks carry no reading-load
// stats of their own, so the strain chip borrows the analyzed companion EPUB's
// (when there is one) and the stats line is runtime instead of word counts.
function volRow(v) {
  const rt = runtime(v.runtime_min);
  const title = esc(v.title || "(untitled)");
  const epub = EPUB_BY_ISBN.get(v.ab_id);

  const badges = [];
  if (v.natively_level != null) badges.push(`<span class="chip" title="Natively level">Lv ${esc(String(v.natively_level))}</span>`);
  badges.push(vocabChip(v));
  if (v.genre) badges.push(`<span class="chip genre" title="${esc(v.genre)}">${esc(v.genre)}</span>`);

  const h = epub && epub.known && epub.known.hard_pct != null ? epub.known.hard_pct : null;
  const s = h != null ? strainBucket(h) : null;
  const strain = s
    ? `<span class="chip strain ${s.cls}" title="Companion EPUB: ${h}% of sentences have 2+ unknown words">${s.label}</span>`
    : "";

  const cover = v.cover_url
    ? `<img loading="lazy" src="${esc(v.cover_url)}" alt="" referrerpolicy="no-referrer">`
    : icon("headphones", "placeholder");

  // One ⋯ menu per volume. "Remove from library" applies to every volume — the rows worth
  // pruning are usually the ones never downloaded — while "Delete file" only appears when
  // there is a downloaded file (data-local).
  const menuBtn = moreBtn(
    `data-ab-id="${esc(v.ab_id)}" data-title="${title}"${v.local ? " data-local=\"1\"" : ""}`,
    `More actions for ${v.title || "(untitled)"}`);

  return `<div class="row queue-row${v.local ? "" : " missing"}">
    <span class="queue-rank">${v.order ? esc(v.order) : ""}</span>
    <div class="queue-cover">${cover}</div>
    <div class="queue-info">
      <span class="queue-title">${title}</span>
      <div class="queue-sub">${rt ? `<span>${rt}</span>` : ""}</div>
    </div>
    <div class="queue-badges">${strain}${badges.join("")}</div>
    <div class="queue-actions">
      ${volAction(v)}
      <span class="edit-group">${menuBtn}</span>
    </div>
  </div>`;
}

function render() {
  if (!VOLS.length) {
    page.innerHTML =
      `<a class="back-link" href="/audiobooks">${icon("arrow_back")} Audiobooks</a>` +
      `<p class="empty-msg">No audiobook series named "${esc(SERIES_NAME)}" found.</p>`;
    return;
  }

  const first = VOLS[0];
  const coverUrl = VOLS.map((v) => v.cover_url).find((c) => c) || null;
  const cover = coverUrl
    ? `<img src="${esc(coverUrl)}" alt="" referrerpolicy="no-referrer">`
    : `${icon("headphones", "placeholder")}`;

  const nLocal = VOLS.filter((v) => v.local).length;
  const nVols = VOLS.length;
  const totalMin = VOLS.reduce((s, v) => s + (v.runtime_min || 0), 0);

  const chips = [
    `<span class="chip ${nLocal === nVols ? "have-all" : nLocal ? "have-some" : "have-none"}">` +
    `${nLocal}/${nVols} local</span>`,
  ];
  if (totalMin) chips.push(`<span class="chip" title="Total runtime">${icon("timer")} ${runtime(totalMin)}</span>`);

  page.innerHTML = `
    <a class="back-link" href="/audiobooks">${icon("arrow_back")} Audiobooks</a>
    <div class="series-hero audio">
      <div class="cover">${cover}</div>
      <div class="series-hero-body">
        <h2>${esc(SERIES_NAME)}</h2>
        <div class="author">${esc(first.author || "")}</div>
        <div class="meta">${nVols} volume${nVols === 1 ? "" : "s"}</div>
        <div class="chips">${chips.join("")}</div>
      </div>
    </div>
    <div class="series-vols">
      <h3>Volumes</h3>
      <div class="queue-list">${VOLS.map(volRow).join("")}</div>
    </div>`;
}

async function load() {
  const [res, resBooks] = await Promise.all([
    fetch("/api/audiobooks"),
    fetch("/api/books"),
  ]);
  const entries = await res.json();
  const books = await resBooks.json().catch(() => []);

  EPUB_BY_ISBN = new Map();
  for (const b of books) {
    const isbn = b.audiobook && b.audiobook.audible_isbn;
    if (isbn && b.language === "ja" && b.vocab && b.vocab.status === "ok" &&
        !EPUB_BY_ISBN.has(isbn)) {
      EPUB_BY_ISBN.set(isbn, b);
    }
  }

  // Rebuild the grid's group key exactly, or a series whose entries share a
  // name but not an author would pull in the wrong volumes.
  VOLS = entries.filter(
    (e) => (e.series || e.title || "") === SERIES_NAME && (e.author || "") === SERIES_AUTHOR
  );
  VOLS.sort(byVolume((e) => e.order));
  document.title = SERIES_NAME ? `${SERIES_NAME} — Audiobooks` : "Audiobook series";
  render();
}

load().catch((e) => {
  page.innerHTML = `<div class="error-box">Failed to load series: ${e}</div>`;
});
