// /next — which book to read now, measured against the reader's own known words.
//
// The #1 book gets a themed hero card (design 5e) with #2 and #3 as runner-up
// cards below it; the rest is the queue, in strain bands. One order runs through
// all of it (ranked()) — band, then hard sentences in ten, then new words to
// learn — and every key is printed on the row, so the order reads off the page.
"use strict";

const wrap = document.getElementById("next-wrap");
// Rows a band shows at first, and how many each "Show more" adds.
const BAND_PAGE = 10;

// The queue's bands are QUEUE_BANDS (ui.js). One band shows at a time, picked from a tab row;
// each remembers how many of its rows are showing, so a tab round trip keeps your place.
let activeBand = null;
const shownByBand = new Map();

function bandIndex(b) {
  const cls = strainBucket(b.known.hard_pct).cls;
  return QUEUE_BANDS.findIndex((band) => band.buckets.includes(cls));
}

// "Under 18%" / "18–33%" / "33% or more", read off STRAIN_BANDS so the wording
// follows the thresholds.
function bandRange(band) {
  const i = STRAIN_BANDS.findIndex((s) => s.cls === band.buckets[0]);
  const lo = i > 0 ? STRAIN_BANDS[i - 1].below : null;
  const hi = STRAIN_BANDS[STRAIN_BANDS.findIndex((s) => s.cls === band.buckets[band.buckets.length - 1])].below;
  const share = lo == null ? `Under ${hi}%` : hi === Infinity ? `${lo}% or more` : `${lo}–${hi}%`;
  return `${share} of sentences have two or more unknown words`;
}

// Hard sentences (two or more unknown words) in ten — the order's printed key.
// "1 in 10" reads at a glance where 12.2% vs 13.5% doesn't; books that round
// alike are then told apart by the next key, the new words to learn. Rounding
// also keeps a long book's slightly lower strain (acquisition has more chances
// to fire in it) from jumping it over a much cheaper one.
function hardTenths(k) {
  return Math.round(k.hard_pct / 10);
}

function hardPhrase(k) {
  const t = hardTenths(k);
  return t <= 0 ? "Under 1 in 10" : `${t} in 10`;
}

// Band, then hard sentences in ten, then new words to learn. Every key is on
// the row, so a reader can see why #7 sits above #8.
function ranked(list) {
  return [...list].sort(
    (a, b) =>
      bandIndex(a) - bandIndex(b) ||
      hardTenths(a.known) - hardTenths(b.known) ||
      a.known.words_to_98 - b.known.words_to_98
  );
}

// Chips shared between the hero and each queue row: paired edition, series,
// read state, genre — in that priority, matching the badge order the
// mockup's queue rows show.
function badgesHtml(b) {
  const out = [];
  if (b.pair_id) out.push(`<span class="chip pair" title="Has a paired English edition">JP+EN</span>`);
  // A series chip that just repeats the title (standalone / "Vol 01" books)
  // only steals width from the title.
  if (b.series_name && !(b.title || "").includes(b.series_name))
    out.push(
      `<span class="chip" title="${esc(b.series_name)}">${esc(b.series_name)}` +
        `${b.series_order ? ` Vol ${esc(b.series_order)}` : ""}</span>`
    );
  if (b.known.read) out.push(`<span class="chip read">${icon("check")} Read</span>`);
  if (b.genre) out.push(`<span class="chip genre" title="${esc(b.genre)}">${esc(b.genre)}</span>`);
  return out.join("");
}

// Audiobook slot, always rendered: local M4B download, else an Audible link
// when the catalog has one, else a disabled "No audiobook" placeholder.
function audioAction(b, size, tone) {
  return dlAudio(bookAudio(b), { size, placeholder: true, tone, label: b.title || "" });
}

// The download pair of a runner-up or queue row: quiet tone, so the hero's
// coral EPUB stays the page's one primary.
function rowActions(b) {
  return dlEpub(b.book_id, { size: "sm", tone: "quiet", label: b.title || "" }) + audioAction(b, "sm", "quiet");
}

// Natively level as a tilted sticker on the hero's cover. Same confirmed /
// unconfirmed rule and tooltip as every other level display (ui.js); links out
// when a URL exists.
function levelStickerHero(b) {
  const nat = b.natively;
  const lvl = nat && nat.natively_level != null ? nat.natively_level : null;
  if (lvl == null) return "";
  const unconfirmed = levelUnconfirmed(nat);
  const label = `Lv ${unconfirmed ? "~" : ""}${lvl}`;
  const cls = `level-sticker${unconfirmed ? " unconfirmed" : ""}`;
  const title = esc(levelTitle(lvl, nat));
  return nat.natively_url
    ? `<a class="${cls}" href="${esc(nat.natively_url)}" target="_blank" rel="noopener" title="${title}">${label}</a>`
    : `<span class="${cls}" title="${title}">${label}</span>`;
}

function coverHtml(b, glyph = "menu_book") {
  return b.cover
    ? `<img loading="lazy" src="${esc(coverUrl(b))}" alt="">`
    : `${icon(glyph, "placeholder")}`;
}

// Night hero (5e): cover + title/actions on the left, the reading-load readout on
// the right — a strain verdict in words, the load bar, and what it would take to
// make the book comfortable. The card follows the theme — see body.dark in the
// page CSS.
function heroHtml(b, badge) {
  const k = b.known;
  const s = strainBucket(k.hard_pct);
  const actions = [dlEpub(b.book_id, { label: b.title || "" }), audioAction(b)];
  return `
    <div class="book-hero on-hero">
      <div class="book-cover">${levelStickerHero(b)}${coverHtml(b)}</div>
      <div class="book-head">
        <div class="hero-main">
          <span class="top-pick-badge">${icon("star")} ${badge}</span>
          <div>
            <h2 class="book-title"><a href="/book/${esc(b.book_id)}">${esc(b.title || "(untitled)")}</a></h2>
            <div class="book-author">${esc(b.author || "")}</div>
          </div>
          <div class="chips">${badgesHtml(b)}</div>
        </div>
        <div class="book-actions">${actions.join("")}</div>
      </div>
      <div class="readout">
        <p class="load-verdict"><b class="${s.cls}" title="${k.hard_pct}% of sentences have two or more unknown words">${s.short} strain.</b>
          ${feelSentence(k)}</p>
        <div>${loadBarHtml(k, { legend: true })}</div>
        <p class="load-cost"><b>${fmt(k.words_to_98)}</b> new words would take it to 98% coverage
          <span class="load-known">· you know ${k.known_pct}% of its words now</span></p>
      </div>
    </div>`;
}

// Runners-up #2 and #3, side by side under the hero, each with its own meter.
function runnersHtml(list) {
  if (!list.length) return "";
  const cards = list
    .map((b, i) => {
      const s = strainBucket(b.known.hard_pct);
      return `
      <div class="runner-card">
        <div class="queue-cover" data-book-id="${esc(b.book_id)}">${coverHtml(b)}</div>
        <div class="runner-body">
          <div>
            <div class="runner-headline">
              <span class="runner-rank">#${i + 2}</span>
              <span class="chip strain ${s.cls}" title="${b.known.hard_pct}% of sentences have two or more unknown words">${s.label}</span>
            </div>
            <a class="queue-title" href="/book/${esc(b.book_id)}" title="${esc(b.title || "")}">${esc(b.title || "(untitled)")}</a>
            <div class="queue-sub">${esc(b.author || "")}</div>
          </div>
          <div class="runner-load">
            ${loadBarHtml(b.known, { size: "sm" })}
            <div class="load-cost"><b>${hardPhrase(b.known)}</b> sentences hard · <b>${fmt(b.known.words_to_98)}</b> new words</div>
          </div>
          <div class="runner-actions">
            ${rowActions(b)}
          </div>
        </div>
      </div>`;
    })
    .join("");
  return `<div class="runners">${cards}</div>`;
}

// One queue row. Every ranking key is on the row: the load bar shows how the
// sentences land, then the hard sentences in ten and the new words to learn —
// the order ranked() sorts by. A "Study first" row also links to the book's
// study list, the way to make it readable.
function queueRowHtml(b, rank, band) {
  const k = b.known;
  const study =
    band.id === "study"
      ? `<a class="study-link" href="/book/${esc(b.book_id)}#study-card">Study list ${icon("arrow_forward")}</a>`
      : "";
  return `
      <div class="row queue-row">
        <span class="queue-rank">${rank}</span>
        <div class="queue-cover" data-book-id="${esc(b.book_id)}">${coverHtml(b)}</div>
        <div class="queue-info">
          <a class="queue-title" href="/book/${esc(b.book_id)}" title="${esc(b.title || "")}">${esc(b.title || "(untitled)")}</a>
          <div class="queue-sub">${esc(b.author || "")}</div>
        </div>
        <div class="queue-badges">${badgesHtml(b)}</div>
        <div class="queue-load">
          ${loadBarHtml(k, { size: "sm" })}
          <div class="queue-keys">
            <span><b>${hardPhrase(k)}</b> sentences hard</span>
            <span><b>${fmt(k.words_to_98)}</b> new words</span>
          </div>
          ${study}
        </div>
        <div class="queue-actions">
          ${rowActions(b)}
        </div>
      </div>`;
}

// The queue: a tab per strain band, and the active band's rows below. `start` is
// the rank of its first row (the hero and runners-up take #1–#3). Ranks run on
// across bands, because ranked() sorts band first. A tab's count is every book in
// the band, the ones above included. "Show more" only appends, so there is no
// long list to fold back up, and the other bands are always one tap away.
function queueHtml(list, start, bandTotals) {
  if (!list.length) return "";
  const groups = QUEUE_BANDS.map((band) => ({ band, rows: [] }));
  list.forEach((b, i) => {
    const g = groups[bandIndex(b)];
    g.rows.push(queueRowHtml(b, i + start, g.band));
  });
  const live = groups.filter((g) => g.rows.length);
  const active = live.find((g) => g.band.id === activeBand) || live[0];
  activeBand = active.band.id;

  const tabs = live
    .map(({ band }) => {
      const on = band.id === activeBand;
      return `<button type="button" role="tab" class="seg-btn${on ? " active" : ""}" id="tab-${band.id}"
          data-band="${band.id}" aria-selected="${on}" aria-controls="band-panel" tabindex="${on ? 0 : -1}">
          ${band.title} <span class="band-count">${bandTotals[band.id]}</span></button>`;
    })
    .join("");

  const { band, rows } = active;
  activeRows = rows;
  const shown = Math.min(shownByBand.get(band.id) || BAND_PAGE, rows.length);
  return `
      <div class="band-switch"><div class="seg band-tabs" role="tablist" aria-label="Reading load">${tabs}</div></div>
      <section class="queue-band" id="band-panel" role="tabpanel" aria-labelledby="tab-${band.id}">
        <p class="band-desc">${bandRange(band)}</p>
        <div class="queue-list">${rows.slice(0, shown).join("")}</div>
        ${bandFootHtml(band, shown, rows.length)}
      </section>`;
}

// The active band's rendered rows, kept so "Show more" can append without a re-render.
let activeRows = [];

function bandFootHtml(band, shown, total) {
  const left = total - shown;
  return left > 0
    ? `<div class="band-foot"><span class="band-shown">Showing ${shown} of ${total}</span>` +
        `<button type="button" class="ghost-btn band-more" data-band="${band.id}">Show ${Math.min(BAND_PAGE, left)} more</button></div>`
    : "";
}

// Append the next page of the active band in place. A full render() would rebuild
// the hero and every row, and their lazy covers have no height until they load:
// the page shrinks for a moment and iOS Safari clamps the scroll back to the top.
function showMore(id) {
  const band = QUEUE_BANDS.find((b) => b.id === id);
  const before = shownByBand.get(id) || BAND_PAGE;
  const shown = Math.min(before + BAND_PAGE, activeRows.length);
  shownByBand.set(id, shown);
  const panel = document.getElementById("band-panel");
  panel.querySelector(".queue-list").insertAdjacentHTML("beforeend", activeRows.slice(before, shown).join(""));
  panel.querySelector(".band-foot")?.remove();
  panel.insertAdjacentHTML("beforeend", bandFootHtml(band, shown, activeRows.length));
  // The button is gone: focus the first newly shown title, without scrolling to it.
  const target = panel.querySelectorAll(".queue-title")[before];
  if (target) target.focus({ preventScroll: true });
}

// Resolved once from /api/profiles, same pattern as known.js's activeLang.
// Drives both the belt-and-suspenders filter above and the "Japanese"/"English"
// wording below — a reader on an English profile shouldn't read a JP-only hint.
const LANG_LABEL = { ja: "Japanese", en: "English" };
let activeLang = null;

async function initProfile() {
  const active = await loadActiveProfile();
  activeLang = (active && active.language) || null;
}

let BOOKS = [];

// Switch bands without rebuilding the tab row or letting the page shrink. Replacing the
// whole queue leaves the document nearly empty for a layout pass, and iOS Safari clamps
// the scroll position to that and doesn't restore it. So: keep the tapped tab (only its
// state changes), swap just the rows, and hold the queue's height until the new rows are
// laid out.
function swapBand(queue, html) {
  const y = window.scrollY;
  queue.style.minHeight = `${queue.offsetHeight}px`;
  const next = document.createElement("div");
  next.innerHTML = html;
  queue.querySelector(".queue-band").replaceWith(next.querySelector(".queue-band"));
  for (const tab of next.querySelectorAll('[role="tab"]')) {
    const cur = document.getElementById(tab.id);
    if (!cur) continue;
    cur.className = tab.className;
    cur.setAttribute("aria-selected", tab.getAttribute("aria-selected"));
    cur.setAttribute("tabindex", tab.getAttribute("tabindex"));
  }
  window.scrollTo(0, y);
  requestAnimationFrame(() => requestAnimationFrame(() => (queue.style.minHeight = "")));
}

// keepHero: leave the hero and runners-up alone (a band switch only changes the queue).
// Called from input listeners too, so the argument is destructured, never truthy-tested.
function render({ keepHero = false } = {}) {
  const hideRead = document.getElementById("hideRead").checked;
  const allSeriesBooks = document.getElementById("allSeriesBooks").checked;
  const pairedOnly = document.getElementById("pairedOnly").checked;
  const label = LANG_LABEL[activeLang] || "Japanese";
  document.querySelectorAll(".lang-label").forEach((el) => (el.textContent = label));
  let list = analysedBooks(BOOKS, activeLang);
  if (!allSeriesBooks) list = oneBookPerSeries(list);
  if (hideRead) list = list.filter((b) => !b.known.read);
  if (pairedOnly) list = list.filter((b) => b.pair_id);
  document.getElementById("count").textContent = `${list.length} ${label} ${list.length === 1 ? "book" : "books"}.`;
  if (!list.length) {
    const msg = `<div class="no-vocab">No analyzed ${label} books with a known-words list yet.
       Import one at <a href="/known">Known Words</a>, then run Analyze.</div>`;
    document.getElementById("hero").innerHTML = "";
    document.getElementById("queue-heading").hidden = true;
    document.getElementById("queue").innerHTML = msg;
    return;
  }
  const order = ranked(list);
  const bandTotals = Object.fromEntries(QUEUE_BANDS.map((band) => [band.id, 0]));
  order.forEach((b) => bandTotals[QUEUE_BANDS[bandIndex(b)].id]++);
  const badge = pairedOnly ? "Top paired pick" : "Top pick";
  if (!keepHero) document.getElementById("hero").innerHTML = heroHtml(order[0], badge) + runnersHtml(order.slice(1, 3));
  const queue = document.getElementById("queue");
  const html = queueHtml(order.slice(3), 4, bandTotals);
  if (keepHero && queue.querySelector(".queue-band")) swapBand(queue, html);
  else queue.innerHTML = html;
  document.getElementById("queue-heading").hidden = order.length <= 3;
}

// Cover tooltip on queue and runner-up rows (each cover carries a data-book-id):
// the exact figures behind the row's rounded ones.
function tooltip() {
  const tip = document.getElementById("tip");

  function showTip(b, rect) {
    tip.innerHTML =
      `<strong>${esc(b.title || "")}</strong>` +
      `<span>${b.known.hard_pct}% of sentences with 2+ unknown words · ${b.known.flow_pct}% with none</span>` +
      `<span>${fmt(b.known.words_to_98)} new words to 98% coverage · ${b.known.known_pct}% known</span>` +
      (b.vocab && b.vocab.words_total != null
        ? `<span>${fmt(b.vocab.words_total)} total words</span>`
        : "");
    tip.hidden = false;
    // Keep the tooltip on-screen: it is wider than the gutter on a phone.
    const maxLeft = window.scrollX + document.documentElement.clientWidth - tip.offsetWidth - 8;
    tip.style.left = `${Math.max(window.scrollX + 8, Math.min(rect.left + window.scrollX, maxLeft))}px`;
    tip.style.top = `${rect.top + window.scrollY - tip.offsetHeight - 12}px`;
  }

  // Touch has no hover, and the browser's emulated mouseover/mouseout on tap is
  // unreliable, so touch taps are handled explicitly: tap a cover to show its
  // tip, tap it again or anywhere else to dismiss.
  let touchEl = null;
  let lastPointer = "mouse";
  document.addEventListener("pointerdown", (e) => { lastPointer = e.pointerType; }, true);
  document.addEventListener("click", (e) => {
    if (touchEl && !e.target.closest("[data-book-id]")) {
      tip.hidden = true;
      touchEl = null;
    }
  });

  function wire(container, selector) {
    container.addEventListener("mouseover", (e) => {
      if (lastPointer !== "mouse") return;
      const el = e.target.closest(selector);
      if (!el) return;
      const b = BOOKS.find((x) => x.book_id === el.dataset.bookId);
      if (!b) return;
      showTip(b, el.getBoundingClientRect());
    });
    container.addEventListener("mouseout", (e) => {
      if (lastPointer !== "mouse") return;
      if (e.target.closest(selector)) tip.hidden = true;
    });
    container.addEventListener("click", (e) => {
      if (lastPointer === "mouse") return;
      const el = e.target.closest(selector);
      if (!el) return;
      // Let links (title, download) behave normally; only a tap on a cover
      // shows the tip.
      if (e.target.closest("a")) return;
      if (touchEl === el && !tip.hidden) {
        tip.hidden = true;
        touchEl = null;
        return;
      }
      const b = BOOKS.find((x) => x.book_id === el.dataset.bookId);
      if (!b) return;
      touchEl = el;
      showTip(b, el.getBoundingClientRect());
    });
  }

  wire(document.getElementById("queue"), ".queue-cover");
  wire(document.getElementById("hero"), ".queue-cover");
}

function pickBand(id, focusTab) {
  activeBand = id;
  // Rebuilding the hero too would drop its lazy cover for a moment: the page shrinks
  // and iOS Safari clamps the scroll to the top.
  render({ keepHero: true });
  if (focusTab) document.getElementById(`tab-${id}`).focus({ preventScroll: true });
}

// The tab row sticks under the page header. The header's height varies (the
// filters wrap), so CSS reads it from --header-h; on phones the header isn't sticky.
function trackHeaderHeight() {
  const header = document.querySelector("header");
  if (!header || !window.ResizeObserver) return;
  const set = () => document.documentElement.style.setProperty("--header-h", `${header.offsetHeight}px`);
  set();
  new ResizeObserver(set).observe(header);
}

async function load() {
  trackHeaderHeight();
  await initProfile();
  BOOKS = await (await fetch("/api/books")).json();
  render();
  tooltip();
  const queue = document.getElementById("queue");
  queue.addEventListener("click", (e) => {
    const tab = e.target.closest('[role="tab"]');
    if (tab) return pickBand(tab.dataset.band);
    const more = e.target.closest(".band-more");
    if (!more) return;
    showMore(more.dataset.band);
  });
  // Roving tabindex: arrows move between tabs (and select, as the panel is cheap to swap).
  queue.addEventListener("keydown", (e) => {
    const tab = e.target.closest('[role="tab"]');
    if (!tab) return;
    const tabs = [...queue.querySelectorAll('[role="tab"]')];
    const at = tabs.indexOf(tab);
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (to == null) return;
    e.preventDefault();
    pickBand(tabs[(to + tabs.length) % tabs.length].dataset.band, true);
  });
  document.getElementById("hideRead").addEventListener("input", render);
  document.getElementById("allSeriesBooks").addEventListener("input", render);
  document.getElementById("pairedOnly").addEventListener("input", render);
}

load().catch((e) => {
  wrap.innerHTML = `<div class="error-box">Failed to load: ${esc(e.message)}</div>`;
});
