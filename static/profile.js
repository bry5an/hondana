"use strict";

const wrap = document.getElementById("profile-wrap");
const live = document.getElementById("profile-live");

const LANG_LABEL = { ja: "Learning Japanese", en: "Learning English" };

// The page is four independent requests. Each lands in `state` as {ok, value} or
// {ok:false, error}; render() draws whatever arrived and names whatever didn't.
const SOURCES = {
  profiles: { url: "/api/profiles", what: "your profile" },
  stats: { url: "/api/known-words", what: "your vocabulary" },
  reads: { url: "/api/profile/read-books", what: "your read books" },
  books: { url: "/api/books", what: "what you can read right now" },
  connect: { url: "/api/connect", what: "the phone connection" },
  // Only there once the iOS app has sent one (404 until then, which is not an error).
  ankiType: { url: "/api/profile/anki-note-type", what: "your Anki note type", optional: true },
};
const state = {};

function fetchPart(key) {
  return fetch(SOURCES[key].url).then((r) => (SOURCES[key].optional && r.status === 404 ? null : okJson(r))).then(
    (value) => { state[key] = { ok: true, value }; },
    (e) => { state[key] = { ok: false, error: e.message }; },
  );
}

function okJson(r) {
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function load() {
  return Promise.all(Object.keys(SOURCES).map(fetchPart)).then(render);
}

function retry(key) {
  return fetchPart(key).then(render).then(() => refocus(key));
}

// render() replaces the whole page, so the button that was just pressed no longer exists
// and focus would drop to the top of the document. Put it back where the action was: the
// section's "Try again" if it failed again, else the section's own landmark (each carries
// data-focus="<source key>" and tabindex="-1"), else the page.
function refocus(key) {
  const target = (state[key].ok ? wrap.querySelector(`[data-focus="${key}"]`) : wrap.querySelector(`[data-retry="${key}"]`)) || wrap;
  target.focus();
}

function errorText(key) {
  return key === "books" ? "Couldn't check what you can read right now." : `Couldn't load ${SOURCES[key].what}.`;
}

// Say something to screen readers. A node inserted already filled (role=alert/status) is
// not reliably announced, so the page keeps one persistent polite region and writes here.
// It is emptied first so the same words twice (a second failed retry) are heard again,
// and a pending write is dropped so only the last call speaks.
let announceTimer;
function announce(msg) {
  clearTimeout(announceTimer);
  live.textContent = "";
  if (msg) announceTimer = setTimeout(() => { live.textContent = msg; }, 50);
}

function errorHtml(key) {
  const err = state[key].error;
  return `
    <div class="section-error">
      <p title="${esc(err)}">${esc(errorText(key))}</p>
      <button type="button" class="btn section-retry" data-retry="${key}">Try again</button>
    </div>`;
}

function sinceHtml(p) {
  if (!p.created_at) return "";
  const d = new Date(p.created_at);
  if (isNaN(d)) return "";
  const label = d.toLocaleDateString(undefined, { year: "numeric", month: "long" });
  return `<span class="profile-hero-since">Reader since ${esc(label)}</span>`;
}

function heroHtml(p) {
  return `
    <div class="stats-card profile-hero" data-focus="profiles" tabindex="-1">
      <span class="profile-avatar profile-hero-avatar">${esc(initial(p.name))}</span>
      <div class="profile-hero-body">
        <div class="profile-hero-name">${esc(p.name)}</div>
        <div class="profile-hero-meta">
          <span class="profile-hero-lang-pill">${esc(LANG_LABEL[p.language] || p.language || "")}</span>
          ${sinceHtml(p)}
        </div>
      </div>
      <button type="button" class="switch-reader-btn">Switch reader</button>
    </div>`;
}

// What the counts are for: how many unread books Read Next would call comfortable right
// now, counted the way /next counts them by default (analysed books in the reader's
// language, one per series, unread), so the number here is the one /next opens on.
function outcomeHtml(p, stats, books) {
  const lang = { ja: "Japanese", en: "English" }[p.language] || "";
  const list = oneBookPerSeries(analysedBooks(books, p.language || null)).filter((b) => !b.known.read);
  const now = list.filter((b) => queueBand(hardPct(b)).id === "now").length;
  const stretch = list.filter((b) => queueBand(hardPct(b)).id === "stretch").length;
  const toNext = `<a class="btn btn-primary outcome-link" href="/next">See Read Next</a>`;
  const toKnown = `<a class="btn outcome-link" href="/known">Review frequent words</a>`;
  let line, links, note = "";
  if (!list.length && !stats.count) {
    line = `<b>Nothing to rank yet.</b> Import the words you know and Read Next can tell you which ${esc(lang)} books you can read.`;
    links = `<a class="btn btn-primary outcome-link" href="/known">Add known words</a>`;
  } else if (!list.length) {
    line = `<b>No unread ${esc(lang)} books have been analysed yet,</b> so Read Next has nothing to rank.`;
    note = `Books are analysed when you add them. Add one on <a href="/add">Add Book</a>, or run the analyse step from <a href="/admin">Admin</a>.`;
    links = toKnown;
  } else if (!now) {
    line = `<b>No unread ${esc(lang)} books are low strain yet.</b> ${fmt(stretch)} of ${fmt(list.length)} are a stretch.
      Learning more of the frequent words is the quickest way to change that.`;
    links = toKnown + toNext;
  } else {
    line = `<b>${fmt(now)} of ${fmt(list.length)}</b> unread ${esc(lang)} ${list.length === 1 ? "book is" : "books are"} low strain for you right now.`;
    links = toNext + toKnown;
  }
  // The terms the headline uses, defined where it uses them.
  if (list.length) {
    note = `Counts one book per series. Low strain means under ${STRAIN_BANDS[0].below}% of a book's sentences have two or more words you don't know.`;
  }
  return `
    <section class="stats-card profile-outcome" aria-label="What you can read" data-focus="books" tabindex="-1">
      <p class="outcome-line">${line}</p>
      ${note ? `<p class="outcome-note">${note}</p>` : ""}
      <div class="outcome-links">${links}</div>
    </section>`;
}

function outcomeSlot() {
  if (state.books.ok) {
    // Without the profile (language) or the word count the line can't be worded; those
    // sections already say they failed.
    if (!state.profiles.ok || !state.stats.ok) return "";
    return outcomeHtml(activeProfile(), state.stats.value, state.books.value);
  }
  return `
    <section class="stats-card profile-outcome" aria-label="What you can read" data-focus="books" tabindex="-1">
      ${errorHtml("books")}
    </section>`;
}

function activeProfile() {
  const data = state.profiles.value;
  const profiles = data.profiles || [];
  return profiles.find((p) => p.slug === data.active) || profiles[0] || { name: "?", language: "" };
}

// "Met 5+ times in books you finished": the plain-language definition, shared by the
// tile and the fold below it. EXPOSURE_MIN comes from the server so the two cannot drift.
function exposureMin(s) { return s.exposure_min || 5; }

// Nothing yet to show for exposure or reads: no finished book, so no exposed words.
function lowSignal(s) { return !s.exposed && !s.books_read; }

// A list the app seeded with the most frequent words is common vocabulary, not the
// reader's own; marks she added by hand make it partly hers again.
function isSeeded(s) { return /^seed:/.test(s.source || "") && !s.marked; }

// Each tile says where it goes: an arrow leaves for /known; a chevron reveals something
// further down this page.
function tile(cls, href, num, label, sub, go) {
  return `
    <li><a class="stats-card stat-tile ${cls}" href="${href}">
      <b>${fmt(num)}</b>
      <span class="stat-label">${label}</span>
      <span class="stat-sub">${sub}</span>
      <span class="stat-go">${icon(go)}</span>
    </a></li>`;
}

function knownTile(s) {
  const seeded = isSeeded(s);
  return tile(
    "stat-known", "/known", s.count,
    seeded ? "Common words" : "Known words",
    seeded ? `the ${fmt(s.seeded_top_n || s.count)} most frequent, to start you off` : "on your list",
    "arrow_forward",
  );
}

function statsHtml(s) {
  const solo = lowSignal(s);
  const tiles = solo
    ? knownTile(s)
    : knownTile(s) +
      tile("stat-exposed", "#exposed-explainer", s.exposed, "Exposed words",
        `met ${exposureMin(s)}+ times in books you've read`, "expand_more") +
      tile("stat-read", "#read-list", s.books_read, "Books read", "marked finished", "expand_more");
  return `
    <div class="stat-section">
      <h2 class="section-label" data-focus="stats" tabindex="-1">Vocabulary</h2>
      <ul class="stat-grid${solo ? " stat-grid--solo" : ""}">${tiles}</ul>
      ${solo ? `<p class="stat-hint">Finish a book and the words you meet ${exposureMin(s)}+ times in it start counting as known.</p>` : ""}
    </div>`;
}

// Closed by default: the tiles already say what each count is in one line; this is the
// "so what" and the terms Read Next's numbers use. No deck vocabulary: both profiles
// have a known-words list, and only some have ever used a study deck.
function explainerHtml(s) {
  if (lowSignal(s)) return "";
  const n = exposureMin(s);
  return `
    <details class="stats-card profile-explainer" id="exposed-explainer">
      <summary>
        <span class="explainer-icon">${icon("lightbulb")}</span>
        <span class="explainer-title">What these numbers mean</span>
      </summary>
      <div class="explainer-body">
        <dl>
          <dt>Known words</dt>
          <dd>The words on your list.</dd>
          <dt>Exposed words</dt>
          <dd>Words you've met ${n} or more times in a single book you've marked read. You've seen them often enough to
            recognise them, even if they were never on your list, so Read Next counts them as known when it scores your
            other books. A book is never scored against its own exposed words: finishing a book lowers the strain of the
            books after it, not its own.</dd>
          <dt>Known %</dt>
          <dd>The share of a book's words that are on your list or exposed.</dd>
          <dt>Strain</dt>
          <dd>The share of a book's sentences with two or more unknown words. Under ${STRAIN_BANDS[0].below}% is low strain.</dd>
        </dl>
      </div>
    </details>`;
}

function monthLabel(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !isNaN(d) ? d.toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "";
}

function readTile(b, showDate) {
  const cover = b.cover
    ? `<img src="${esc(coverUrl(b))}" alt="">`
    : `${icon("menu_book", "placeholder")}`;
  const date = showDate ? monthLabel(b.read_at) : "";
  // The edit button is a sibling of the link, not inside it: a button in an <a> is invalid
  // and would navigate as well.
  return `
    <div class="read-item">
      <a class="read-tile" href="/book/${esc(b.book_id)}">
        <div class="read-cover">${cover}<span class="read-check">${icon("check")}</span></div>
        <div class="read-tile-title"${langAttr(b.title)}>${esc(b.title || "(untitled)")}</div>
        <div class="read-tile-author"${langAttr(b.author)}>${esc(b.author || "")}</div>
        ${date ? `<div class="read-tile-date">${esc(date)}</div>` : ""}
      </a>
      <button type="button" class="read-date-edit" data-book="${esc(b.book_id)}"
              aria-label="Change the date you read ${esc(b.title || "this book")}">${icon("edit")}</button>
    </div>`;
}

// When the orphaned marks were made, in words: "in Jul 2026" or "between Mar and Jul 2026".
function orphanWhen(orphans) {
  const stamps = orphans.map((o) => o.read_at).filter(Boolean).sort();
  if (!stamps.length) return "";
  const first = monthLabel(stamps[0]);
  const last = monthLabel(stamps[stamps.length - 1]);
  if (!first || !last) return "";
  return first === last ? `in ${first}` : `between ${first} and ${last}`;
}

// What the leftover marks are, for the notice and the confirm: their titles where any is
// on record, else when they were made.
function orphanWho(orphans) {
  const named = orphans.filter((o) => o.title).map((o) => o.title);
  if (!named.length) return orphanWhen(orphans);
  const rest = orphans.length - named.length;
  return `(${named.join(", ")}${rest ? ` and ${rest} more` : ""})`;
}

// The leftover line: marks for books that are no longer in the library. Housekeeping,
// so it sits below the books; the button removes only the marks.
function orphanHtml(orphans) {
  if (!orphans.length) return "";
  const n = orphans.length;
  const who = orphanWho(orphans);
  return `
    <div class="orphan-note">
      <span>${fmt(n)} ${n === 1 ? "book" : "books"} you marked read ${esc(who)} ${n === 1 ? "is" : "are"} no longer in the library.</span>
      <button type="button" class="btn orphan-clear">Clear ${n === 1 ? "this read mark" : `these ${fmt(n)} read marks`}</button>
    </div>`;
}

function readListHtml(books, orphans) {
  // A date under every cover is noise when they are all the same month (a bulk mark, or
  // simply a young account); say it once under the heading instead.
  const months = new Set(books.map((b) => monthLabel(b.read_at)).filter(Boolean));
  const showDate = months.size > 1;
  const sub = months.size === 1 && books.length > 1 ? `<p class="read-sub">All marked read in ${esc([...months][0])}.</p>` : "";
  let body;
  if (books.length) {
    body = `<div class="read-grid">${books.map((b) => readTile(b, showDate)).join("")}</div>`;
  } else if (orphans.length) {
    body = `<p class="empty-msg">None of the books you marked read are in the library any more.</p>`;
  } else {
    body = `<p class="empty-msg">No books marked read yet. Open a book from <a href="/next">Read Next</a> or your <a href="/">library</a> and mark it read from its page to see it here.</p>`;
  }
  return `<div class="stats-card" id="read-list"><h2 data-focus="reads" tabindex="-1">Books you've read</h2>${sub}${body}${orphanHtml(orphans)}</div>`;
}

// The note type the Hondana iOS app sent, as a package desktop Anki can import (AnkiMobile can't
// make one). Absent until the app has sent it.
function ankiTypeHtml() {
  if (!state.ankiType.ok) return `<div class="stats-card" data-focus="ankiType" tabindex="-1"><h2>Anki note type</h2>${errorHtml("ankiType")}</div>`;
  const t = state.ankiType.value;
  if (!t) return "";
  return `
    <div class="stats-card anki-type" data-focus="ankiType" tabindex="-1">
      <h2>Anki note type</h2>
      <p><b${langAttr(t.name)}>${esc(t.name)}</b> · <span${langAttr(t.fields.join(" "))}>${t.fields.map(esc).join(", ")}</span></p>
      <p class="stat-hint">Import the file into Anki on your computer (double-click it), then sync and press Fetch from Anki in the app.
        It brings one sample card in a deck called Hondana; delete that deck afterwards.</p>
      <a class="dl-btn dl-btn--epub" href="/profile/anki-note-type.apkg" download>${icon("download")}Download .apkg</a>
    </div>`;
}

// How to connect Hondana for iPhone: the app finds the server by itself over Bonjour;
// this is the fallback, the address to type and a QR code its camera opens straight into
// the app, on this reader's profile.
function connectHtml() {
  if (!state.connect.ok) return `<div class="stats-card" data-focus="connect" tabindex="-1"><h2>Connect Hondana for iPhone</h2>${errorHtml("connect")}</div>`;
  const c = state.connect.value;
  const lan = c.lan_substituted
    ? `<p class="stat-hint">You opened this page as localhost, so this is the address of this computer on your network.</p>`
    : "";
  return `
    <div class="stats-card connect-card" id="connect" data-focus="connect" tabindex="-1">
      <img class="connect-qr" src="/api/connect/qr.svg" width="176" height="176"
           alt="QR code that connects Hondana for iPhone to ${esc(c.url)}">
      <div class="connect-body">
        <h2>Connect Hondana for iPhone</h2>
        <p>Open the app and it should find this server by itself. If it doesn't, point the iPhone camera at the
          code, or type this address in Settings › Server:</p>
        <p><code class="connect-url">${esc(c.url)}</code></p>
        ${lan}
        <p class="stat-hint">Your iPhone needs to be on the same network.</p>
        <p><a class="connect-open" href="${esc(c.link)}">Using this page on the iPhone? Open Hondana</a></p>
      </div>
    </div>`;
}

function readsSlot() {
  if (state.reads.ok) return readListHtml(state.reads.value.books, state.reads.value.orphans);
  return `<div class="stats-card" id="read-list"><h2 data-focus="reads" tabindex="-1">Books you've read</h2>${errorHtml("reads")}</div>`;
}

async function clearOrphans(orphans) {
  const who = orphanWho(orphans);
  const ok = await confirmDialog({
    title: `Clear ${orphans.length} read ${orphans.length === 1 ? "mark" : "marks"}?`,
    message: "These books are no longer in the library. This removes only their \"read\" marks from " +
      "this profile; no files are touched." + (who ? ` Marked ${who.replace(/^\(|\)$/g, "")}.` : ""),
    warning: "This can't be undone. If a book was only moved to another folder, relink its mark first " +
      "with cli.py recover-moves.",
    confirmLabel: "Clear",
  });
  if (!ok) return;
  try {
    const r = await fetch("/api/profile/orphan-reads", { method: "DELETE" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    await load();
    refocus("reads");
    if (state.reads.ok) announce(`Cleared ${orphans.length} read ${orphans.length === 1 ? "mark" : "marks"}.`);
  } catch (e) {
    toast(`Couldn't clear them: ${e.message}`);
  }
}

// A local YYYY-MM-DD (not toISOString(), which is UTC and can be yesterday's date).
function localDay(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// Change the day a book was finished. A mark made before read_at was kept has none,
// so the picker starts on today.
async function editReadDate(bookId) {
  const book = state.reads.value.books.find((b) => b.book_id === bookId);
  if (!book) return;
  const when = book.read_at ? new Date(book.read_at) : null;
  const today = localDay(new Date());
  const v = await promptDialog({
    title: "When did you finish it?",
    message: book.title || "",
    fields: [{ name: "day", label: "Date read", type: "date", required: true, max: today,
               value: when && !isNaN(when) ? localDay(when) : today }],
    confirmLabel: "Save",
  });
  if (!v) return;
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/read-date`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ read_at: v.day }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    await load();
    refocus("reads");
    if (state.reads.ok) announce("Date updated.");
  } catch (e) {
    toast(`Couldn't change the date: ${e.message}`);
  }
}

function render() {
  const keep = wrap.querySelector("details")?.open;
  wrap.innerHTML =
    (state.profiles.ok ? heroHtml(activeProfile()) : `<div class="stats-card" data-focus="profiles" tabindex="-1">${errorHtml("profiles")}</div>`) +
    outcomeSlot() +
    (state.stats.ok
      ? statsHtml(state.stats.value) + explainerHtml(state.stats.value)
      : `<div class="stat-section"><h2 class="section-label" data-focus="stats" tabindex="-1">Vocabulary</h2><div class="stats-card">${errorHtml("stats")}</div></div>`) +
    connectHtml() +
    ankiTypeHtml() +
    readsSlot();
  const fold = wrap.querySelector("#exposed-explainer");
  if (fold && (keep || location.hash === "#exposed-explainer")) fold.open = true;
  wrap.removeAttribute("aria-busy");
  announce(Object.keys(SOURCES).filter((k) => !state[k].ok).map(errorText).join(" "));
}

// One set of listeners for the page's life: render() replaces the markup, not the wrap.
wrap.addEventListener("click", (e) => {
  const t = e.target.closest("[data-retry], .switch-reader-btn, .orphan-clear, .stat-exposed, .read-date-edit");
  if (!t) return;
  if (t.dataset.retry) {
    retry(t.dataset.retry);
  } else if (t.classList.contains("switch-reader-btn")) {
    e.stopPropagation();
    if (window.openProfileMenu) window.openProfileMenu();
  } else if (t.classList.contains("orphan-clear")) {
    clearOrphans(state.reads.value.orphans);
  } else if (t.classList.contains("read-date-edit")) {
    editReadDate(t.dataset.book);
  } else {
    // The Exposed tile jumps to the fold; open it so the jump shows something.
    wrap.querySelector("#exposed-explainer").open = true;
  }
});

load();
