// Shared UI helpers, loaded before each page's own script. They are plain
// global functions (no build step), so a page must NOT redeclare any of these
// names with const/let — that is a SyntaxError — and should not keep a private
// copy: change a helper here once instead.
//
//   esc(s)                 HTML-escape; null/undefined/numbers are fine
//   fmt(n) / kFmt(n)       number formatting ("—" for null; 12k from 10000)
//   initial(name)          avatar letter
//   langAttr(text)         ` lang="ja"` when the text holds Japanese, else "" — put it on any element that
//                          shows book titles, series, authors or sentences so a screen reader voices them
//   coverUrl(b)            the /covers/{id} URL of a book, cache-busted when its cover was
//                          hand-replaced — never hand-build /covers/ URLs
//   icon(name)             Material Symbols glyph (font subset: static/material-symbols.woff2)
//   dlEpub(bookId)         the EPUB download button
//   dlAudio(a)             the audiobook button: local M4B download, else an Audible
//                          link, else (opts.placeholder) a disabled "No audiobook" — or,
//                          with opts.source, "audiobook.jp only" for a linked catalog
//                          entry that has no file here and no Audible page
//   dlTrack(abId, file)    one MP3 track link of a folder audiobook
//   statusHtml(type, html, glyph?)   the .status-ok/err/pending banner
//   STRAIN_GRADES, strainBucket(h)   THE strain wording: 7 grades on 4 colours, cut at 10/18/25/33/40/48
//   STRAIN_BANDS           the 4 colour bands (18/33/48) derived from the grades; Read Next's queue bands follow them
//   feelSentence(k)        what a book will feel like to read, in words (the verdict's sentence; /book, /next)
//   loadBarHtml(k, opts)   the load bar: sentences with no / one / two+ unknown words (/book, /next)
//   loadReadoutHtml(k, opts)   the reading-load readout: verdict word + sentence, load bar, to-98% and known (/book, /add)
//   strainStrip(vols, {marks})   one bar per volume, coloured by strain bucket (/series, /add's batch ending); marks draws the band thresholds
//   QUEUE_BANDS, queueBand(hardPct)   Read Next's three bands (Comfortable now / A stretch / Study first) (/next, /add)
//   levelBand / levelUnconfirmed / levelTitle / levelSticker   Natively level display
//   orderCmp(a, b) / byVolume(orderOf)   volume order: numeric-aware ("2" < "10" < "100"),
//                          blank last, title as tiebreak — the one series sort, EPUB and audiobook
//   readingCmp(a, b)       gojūon order: compares two sort keys (the server's `title_sort` /
//                          `series_sort` / `series_reading`, hiragana) as Intl.Collator("ja") does,
//                          Latin ahead of kana; an item's key is that field, else its plain text
//   authorKey(b)           the spelling-independent author key (server `author_key`) for
//                          grouping/sorting/comparing books by author; never for display
//   analysedBooks(books, lang) / oneBookPerSeries(list)   what Read Next ranks, and its one-book-per-series collapse (/next, /profile)
//   vocabOf, diffOf, wordsOf, knownPct, hardPct, flowPct, wordsTo98, isRead   book accessors
//   postJson(url, body, method?)   POST (or `method`) JSON → parsed JSON; throws Error(server `detail`) on a non-2xx
//   copyText(text)         copy to the clipboard → Promise<boolean>; also works on the plain-http LAN address,
//                          where navigator.clipboard doesn't exist
//   failReason(e)          a failed request in words ("the server hit an error"), for "Couldn't save: …"
//   pairSignalLabel(c) / pairSignalTip(c)   a suggested edition's "Author matches" / "Weak guess" and its tooltip (/pair, /book)
//   loadActiveProfile()    the active reader profile ({slug, name, language}) or null
//   toast(msg, "ok"|"error", {action: {label, run}})   non-blocking result; the optional action is an Undo button
//   confirmDialog({...})   native <dialog> confirm → Promise<boolean>
//   promptDialog({...})    native <dialog> with text fields → Promise<{name: value} | null>
//   toast(message, type)   small non-blocking notice (use it instead of alert())
//   manageMode(el, btn)    the Edit/Done toggle on pages with edit + delete controls
//   moreBtn(attrs, label)  the ⋯ button of a row/card's menu; openMenu(btn, items) opens it
// Every download control in the app comes from here so they stay identical.

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function fmt(n) { return n == null ? "—" : Number(n).toLocaleString(); }
function kFmt(n) { return n == null ? "—" : n >= 10000 ? `${Math.round(n / 1000)}k` : Number(n).toLocaleString(); }
// A book's cover image. The server marks /covers/ responses immutable for a year,
// so a replaced cover needs a new URL: `cover_v` is the image's mtime.
function coverUrl(b) {
  return `/covers/${encodeURIComponent(b.book_id)}${b.cover_v ? `?v=${b.cover_v}` : ""}`;
}
// The page is lang="en"; without this a screen reader reads Japanese in an English voice.
const _JA_RE = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]/;
function langAttr(text) { return text && _JA_RE.test(text) ? ' lang="ja"' : ""; }
function initial(name) { return ((name || "?").trim().charAt(0) || "?").toUpperCase(); }

// ── reading load ──────────────────────────────────────────────────────────────

// hard_pct = share of sentences with two or more unknown words. One unknown
// word is inferable from context, two force a re-read, so these cut points are
// about sentences you must stop on. `short` is for tiles that already say
// "STRAIN"; `label` stands on its own in a chip. Seven grades (the words) sit on
// four colours: the colour boundaries (18/33/48) are what Read Next's queue bands
// and the strip's thresholds follow; the grades only vary the wording within one.
const STRAIN_GRADES = [
  { below: 10, label: "very low strain", short: "Very low", cls: "s-low" },
  { below: 18, label: "low strain", short: "Low", cls: "s-low" },
  { below: 25, label: "moderate strain", short: "Moderate", cls: "s-some" },
  { below: 33, label: "notable strain", short: "Notable", cls: "s-some" },
  { below: 40, label: "high strain", short: "High", cls: "s-high" },
  { below: 48, label: "very high strain", short: "Very high", cls: "s-high" },
  { below: Infinity, label: "heavy strain", short: "Heavy", cls: "s-heavy" },
];

// The colour bands: the grades grouped by colour, each ending where its last grade does.
// `first` is the grade that starts the band (what the strip's dashed line names).
const STRAIN_BANDS = STRAIN_GRADES.reduce((bands, g) => {
  const last = bands[bands.length - 1];
  if (last && last.cls === g.cls) last.below = g.below;
  else bands.push({ below: g.below, cls: g.cls, first: g });
  return bands;
}, []);

function strainBucket(h) {
  // A non-number falls through every `<` to the last grade, as the old if-chains did.
  return STRAIN_GRADES.find((g) => h < g.below) || STRAIN_GRADES[STRAIN_GRADES.length - 1];
}

// What reading the book will feel like, in words: the share of clean sentences
// as "N in 10", and how often you'd stop to look a word up.
function feelSentence(k) {
  const tenths = Math.round(k.flow_pct / 10);
  const clean =
    tenths >= 10 ? "Almost every sentence has no unknown words"
    : tenths <= 0 ? "Fewer than 1 in 10 sentences has no unknown words"
    : `${tenths} in 10 sentences have no unknown words`;
  const per = k.tokens_per_lookup != null ? Math.round(k.tokens_per_lookup) : null;
  const lookup =
    per == null ? ""
    : per <= 1 ? ", and you'd look up nearly every word"
    : `, and you'd look a word up about once every ${per} words`;
  return `${clean}${lookup}.`;
}

// Load bar: how a book's sentences land for this reader — no unknown word, one
// (inferable from context), two or more (where you stop and reassemble it) —
// stacked, so the three shares read as one book. `k` is a book's `known` record.
// The numbers are always in the bar's accessible name and tooltip; `legend`
// also prints them underneath. `size: "sm"` is the thin row/runner-up variant.
function loadBarHtml(k, opts = {}) {
  const parts = `No unknown words in ${k.flow_pct}% of sentences, one in ${k.i1_pct}%, two or more in ${k.hard_pct}%`;
  const bar =
    `<div class="load-bar${opts.size === "sm" ? " load-bar--sm" : ""}" role="img" aria-label="${parts}" title="${parts}">` +
    `<i class="f" style="width:${k.flow_pct}%"></i><i class="i" style="width:${k.i1_pct}%"></i>` +
    `<i class="h" style="width:${k.hard_pct}%"></i></div>`;
  if (!opts.legend) return bar;
  return `${bar}
    <div class="load-legend" aria-hidden="true">
      <span><i class="k f"></i>No unknown words <b>${k.flow_pct}%</b></span>
      <span><i class="k i"></i>One unknown <b>${k.i1_pct}%</b></span>
      <span><i class="k h"></i>Two or more <b>${k.hard_pct}%</b></span>
    </div>`;
}

// One bar per volume, height = strain, coloured by strain bucket; read
// volumes are filled, unread outlined. Shows how difficulty moves over the
// series, which no single number can. Unscored volumes keep an empty slot so
// bars stay in volume order.
function strainStrip(vols, opts = {}) {
  const max = Math.max(50, ...vols.map((b) => hardPct(b) ?? 0));
  const bars = vols
    .map((b, i) => {
      const n = esc(b.series_order || String(i + 1));
      const h = hardPct(b);
      if (h == null) {
        return `<div class="strip-col" title="Vol ${n} · not analyzed"><div class="strip-bar none"></div><span>${n}</span></div>`;
      }
      const s = strainBucket(h);
      const read = isRead(b);
      return `<a class="strip-col" href="/book/${encodeURIComponent(b.book_id)}" title="Vol ${n} · ${h}% strain${read ? " · read" : ""}">
        <div class="strip-bar ${s.cls}${read ? " read" : ""}" style="height:${Math.max(6, (h / max) * 100)}%"></div><span>${n}</span></a>`;
    })
    .join("");
  const keyHtml = `<div class="meter-labels"><span>STRAIN BY VOLUME</span><span class="strip-key"><i class="key-read"></i>read <i class="key-unread"></i>unread</span></div>`;
  if (!opts.marks) return `<div class="strip">${keyHtml}<div class="strip-bars">${bars}</div></div>`;
  // Marked: the bars sit on a fixed-height plot with a dashed line at each band threshold, on the
  // same scale as the bars (CSS: --frac is the share of the plot height).
  // Each dashed line says the band that starts at it ("33% high"), so the colours read as words.
  const marks = STRAIN_BANDS.filter((b) => b.below < max)
    .map((b, i) => `<div class="strip-mark" style="--frac:${b.below / max}"><span>${b.below}% ${STRAIN_BANDS[i + 1].first.short.toLowerCase()}</span></div>`).join("");
  return `<div class="strip strip--marked">${keyHtml}<div class="strip-plot">${marks}<div class="strip-bars">${bars}</div></div></div>`;
}

// Read Next's queue bands: low strain is comfortable now, some is a stretch, high and heavy
// merge because the advice is the same (study before reading). /next sections its queue by
// these; /add says which one a new book lands in.
const QUEUE_BANDS = [
  { id: "now", title: "Comfortable now", buckets: ["s-low"] },
  { id: "stretch", title: "A stretch", buckets: ["s-some"] },
  { id: "study", title: "Study first", buckets: ["s-high", "s-heavy"] },
];
const queueBand = (hardPct) => QUEUE_BANDS.find((band) => band.buckets.includes(strainBucket(hardPct).cls));

// The reading-load readout for a book with stats (k = book.known, hard_pct set): the verdict in
// its band ink and in words, the load bar with its legend, then the two figures those don't
// say. One drawing for /book's hero and /add's result. opts.jpdb adds jpdb's own known-% figure
// to "How this is measured", for a jpdb user to compare; meaningless to anyone else.
function loadReadoutHtml(k, opts = {}) {
  return `
    <div class="readout readout--load" id="load-card">${loadReadoutInner(k, opts)}
    </div>`;
}

// The readout's contents without its panel: /add draws it invisibly under its "analyzing" placeholder
// so the placeholder is exactly as tall as the readout that replaces it.
function loadReadoutInner(k, opts = {}) {
  const band = strainBucket(k.hard_pct);
  const tile = (cls, label, val, title) =>
    `<div class="${cls}" title="${esc(title)}"><div class="stat-label">${label}</div><div class="stat-val">${val}</div></div>`;
  const tiles = [
    tile("st-cost", "To 98%", `${fmt(k.words_to_98)}<small>words</small>`, "New words needed to reach 98% coverage"),
    tile("st-known", "Known", `${k.known_pct}%`, "Known words"),
  ].join("");
  const notes = [
    `9 in 10 sentences have ${k.unknown_p90} or fewer unknown words`,
    k.known_pct_words == null || !opts.jpdb ? "" : `${k.known_pct_words}% by jpdb's count, which also counts particles and auxiliary verbs as known`,
    k.exposed_unique ? `${fmt(k.exposed_unique)} known words come from books you've read` : "",
  ].filter(Boolean).join(" · ");
  return `
      <div class="readout-label">READING LOAD</div>
      <div class="load-split">
        <div class="load-main">
          <p class="load-verdict"><b class="${band.cls}">${band.short} strain.</b> ${feelSentence(k)}</p>
          ${loadBarHtml(k, { legend: true })}
        </div>
        <div class="stat-grid">${tiles}</div>
      </div>
      <details class="readout-how">
        <summary>How this is measured</summary>
        <p class="readout-note">${notes}. Measured against your own vocabulary, sentence by sentence; a word
          stops counting as a lookup after 5 encounters in this book.</p>
      </details>`;
}

// ── volume order ──────────────────────────────────────────────────────────────

// Orders are free text ("01", "10", "1.5", "上", "外伝"). A plain string compare
// puts "100" between "10" and "11"; `numeric` compares digit runs as numbers, and
// a missing order sorts after every real one rather than as the string "99".
function orderCmp(a, b) {
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

// A comparator for books or audiobook entries: `orderOf(x)` supplies the order
// (b.series_order for EPUBs, e.order for audiobooks); ties fall back to title.
function byVolume(orderOf) {
  return (a, b) => orderCmp(orderOf(a), orderOf(b)) || (a.title || "").localeCompare(b.title || "");
}

// ── reading order ─────────────────────────────────────────────────────────────

// Kanji collate by code point, not reading, so the server hands every title and
// series a hiragana key (build.py `_sort_readings`, audiobook_scanner
// `series_reading`). Compare those with this; one collator, built once.
const JA_COLLATOR = new Intl.Collator("ja");
function readingCmp(a, b) { return JA_COLLATOR.compare(a || "", b || ""); }

// ── authors ───────────────────────────────────────────────────────────────────

// "Bill Bryson" / "Bryson Bill" / "廣嶋 玲子" / "廣嶋玲子" share one key (build.py,
// textnorm.author_key). Fall back for an entry from before the key existed.
function authorKey(b) { return b.author_key || (b.author || "").toLowerCase(); }

// ── book accessors (a book as /api/books serves it) ───────────────────────────

function vocabOf(b) { return b.vocab && b.vocab.status === "ok" ? b.vocab : null; }
function diffOf(b) { const v = vocabOf(b); return v ? v.difficulty_avg : null; }
function wordsOf(b) { const v = vocabOf(b); return v ? v.words_total : null; }
function knownPct(b) { return b.known ? b.known.known_pct : null; }
// Reading load — how the book reads for this reader, as opposed to the
// frequency-corpus difficulty above. `hardPct` is the one that predicts effort.
function hardPct(b) { return b.known && b.known.hard_pct != null ? b.known.hard_pct : null; }
function flowPct(b) { return b.known && b.known.flow_pct != null ? b.known.flow_pct : null; }
function wordsTo98(b) { return b.known && b.known.words_to_98 != null ? b.known.words_to_98 : null; }
function isRead(b) { return !!(b.known && b.known.read); }

// The books Read Next can rank (/next, /profile): analysed against the reader's known
// words, in the active profile's language. Belt-and-suspenders: recompute() already scopes known_stats.json to the
// active profile's language, so b.known should never be populated for a book
// in the other language. Re-check b.language here too, in case a profile
// switch lands between known_stats being written and this page loading.
function analysedBooks(books, activeLang) {
  return books.filter(
    (b) =>
      b.known &&
      b.known.hard_pct != null &&
      b.known.words_to_98 != null &&
      (activeLang == null || b.language === activeLang)
  );
}

// "Show all books in series" off: collapse each series to a single
// representative — the earliest volume by series_order that isn't marked
// read, or the earliest volume overall if every volume has been read. Runs
// before the hideRead filter so a read first volume correctly falls through
// to the next unread one instead of just vanishing from its series.
function oneBookPerSeries(list) {
  const groups = new Map();
  for (const b of list) {
    const key = b.series_name || b.book_id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(b);
  }
  const out = [];
  for (const vols of groups.values()) {
    vols.sort(byVolume((b) => b.series_order));
    out.push(vols.find((b) => !b.known.read) || vols[0]);
  }
  return out;
}

// ── Natively level ────────────────────────────────────────────────────────────

// Level → cover-sticker colour band (learnnatively-style: higher = warmer/deeper ink).
function levelBand(n) {
  if (n < 20) return "lv-a";
  if (n < 25) return "lv-b";
  if (n < 30) return "lv-c";
  if (n < 35) return "lv-d";
  return "lv-e";
}

// `nat` is the `natively` record (match_type/confidence/matched_title) of the
// volume that supplied the level, or null. A series-page-only match
// (match_type "series") describes the series in general, not this volume, and a
// low-confidence match may be the wrong book entirely — both are "unconfirmed"
// (dashed border, tilde-prefixed number). "manual_url" is a hand-verified match
// and stays confirmed even though it isn't "item".
function levelUnconfirmed(nat) {
  return !!nat && (nat.match_type === "series" || (nat.confidence != null && nat.confidence < 0.6));
}

function levelTitle(n, nat) {
  let title = `Natively level ${n}`;
  if (nat && nat.match_type === "series") {
    title += " (series average — this volume wasn't matched individually)";
  } else if (levelUnconfirmed(nat)) {
    title += " (low-confidence match)";
  }
  if (levelUnconfirmed(nat) && nat.matched_title) title += ` — matched "${nat.matched_title}"`;
  return title;
}

// The cover sticker on the grid cards (/ and /audiobooks).
function levelSticker(n, nat) {
  if (n == null) return "";
  const unconfirmed = levelUnconfirmed(nat);
  const cls = `lv-badge ${levelBand(n)}${unconfirmed ? " lv-unconfirmed" : ""}`;
  return `<span class="${cls}" title="${esc(levelTitle(n, nat))}">Lv ${unconfirmed ? "~" : ""}${n}</span>`;
}

// ── page plumbing ─────────────────────────────────────────────────────────────

// With a glyph: icon dot + text (the /add banner). Without: text only (/known).
function statusHtml(type, html, glyph) {
  return glyph
    ? `<div class="status-${type}"><span class="status-dot">${icon(glyph)}</span><span>${html}</span></div>`
    : `<div class="status-${type}">${html}</div>`;
}

async function postJson(url, body, method = "POST") {
  const resp = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}

// Copy `text` to the clipboard. A server on the LAN is plain http, which browsers don't treat as
// a secure context, so navigator.clipboard is missing there: fall back to selecting a hidden
// textarea and execCommand("copy").
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through to the textarea */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* ok stays false */ }
  ta.remove();
  return ok;
}

// Why a request failed, in words: postJson throws the server's `detail`, or "HTTP 500" when it
// sent none, and a dead connection throws a TypeError ("Failed to fetch"); neither means
// anything to a reader. For "Couldn't <verb>: ${failReason(e)}."
function failReason(e) {
  if (e instanceof TypeError) return "the server didn't answer";
  const m = /^HTTP (\d+)$/.exec(e.message);
  if (m) return +m[1] >= 500 ? "the server hit an error" : "the server rejected the request";
  return e.message.replace(/\.$/, "");
}

// A suggested EN/JP pairing's one honest signal (the server's `signal`): the author names match
// as well as an auto-pair needs, or it's a weak guess; the % is only a tooltip because below
// that bar it can't tell a real match from noise (/pair, /book).
function pairSignalLabel(c) { return c.signal === "author" ? "Author matches" : "Weak guess"; }
function pairSignalTip(c) { return `Author names are ${Math.round(c.author_score * 100)}% alike.`; }

// The profile this device is reading as ({slug, name, language}), or null if
// /api/profiles is unreachable — callers fall back to their Japanese defaults.
async function loadActiveProfile() {
  try {
    const data = await (await fetch("/api/profiles")).json();
    return (data.profiles || []).find((p) => p.slug === data.active) || null;
  } catch {
    return null;
  }
}

function icon(name, cls = "") {
  return `<span class="mi${cls ? " " + cls : ""}" aria-hidden="true">${name}</span>`;
}

function _dlClass(variant, opts) {
  return `dl-btn dl-btn--${variant}${opts.size ? ` dl-btn--${opts.size}` : ""}${opts.iconOnly ? " dl-btn--icon" : ""}` +
    `${opts.tone === "quiet" ? " dl-btn--quiet" : ""}`;
}

// opts: { size: "sm" | "xs", iconOnly: bool, tone: "quiet", label }
// The filled coral EPUB is the page's one primary action; repeated rows (a
// ranked list) pass tone: "quiet" so coral stays a signal. `label` (the book
// title) goes into the accessible name, so a list of these isn't twenty
// identical "Download EPUB" links.
function dlEpub(bookId, opts = {}) {
  const name = opts.label ? `Download EPUB: ${opts.label}` : "Download EPUB";
  return `<a class="${_dlClass("epub", opts)}" href="/download/${encodeURIComponent(bookId)}" ` +
    `title="${esc(name)}"${opts.label ? ` aria-label="${esc(name)}"` : ""}>` +
    `${icon("download")}${opts.iconOnly ? "" : "EPUB"}</a>`;
}

// a: { id, local, audibleUrl, path } — `id` is the Audible ISBN / ab_id the
// /download/audiobook route takes. opts: { size, placeholder, tone, label } as dlEpub,
// `plain` draws the no-file states as text rather than a button shape (the book page's slot,
// where a button that does nothing misleads); `searchTitle` lets audiobook.jp be searched.
// plus `source`: name the catalog source of a linked-but-unreachable entry (its `id`
// is "audiobook.jp 237969" / "kikubon 389") instead of claiming there is no audiobook.
function dlAudio(a, opts = {}) {
  const named = (what) => (opts.label ? ` aria-label="${esc(`${what}: ${opts.label}`)}"` : "");
  if (a && a.local)
    return `<a class="${_dlClass("audio", opts)}" href="/download/audiobook/${encodeURIComponent(a.id)}" ` +
      `title="${esc(a.path || "Download audiobook")}"${named("Download audiobook")}>${icon("headphones")}M4B</a>`;
  if (a && a.audibleUrl)
    return `<a class="${_dlClass("audible", opts)}" href="${esc(a.audibleUrl)}" target="_blank" rel="noopener" ` +
      `title="Open on Audible"${named("Open on Audible")}>${icon("headphones")}Audible${icon("open_in_new")}</a>`;
  if (a && opts.source && a.id && a.id.includes(" ")) {
    const src = a.id.split(" ")[0];
    if (opts.plain) {
      // Plain text, not a button: nothing to press. audiobook.jp can still be searched by title.
      const where = src === "audiobook.jp" && opts.searchTitle
        ? `<a href="https://audiobook.jp/search?keyword=${encodeURIComponent(opts.searchTitle)}" target="_blank" rel="noopener">audiobook.jp</a>`
        : esc(src);
      return `<span class="dl-note">${icon("headphones")}<span>No file here · on ${where}</span></span>`;
    }
    return `<span class="${_dlClass("none", opts)}" title="Linked in the audiobook catalog, but there is no file here and no Audible page">` +
      `${icon("headphones")}${esc(src)} only</span>`;
  }
  if (!opts.placeholder) return "";
  if (opts.plain) return `<span class="dl-note">${icon("headphones")}<span>No audiobook</span></span>`;
  return `<span class="${_dlClass("none", opts)}">${opts.size === "xs" ? "—" : "No audiobook"}</span>`;
}

// One MP3 track of a folder audiobook (the /audiobook-series `.tracks` list):
// `file` is a name from the entry's `files`.
function dlTrack(abId, file) {
  return `<a href="/download/audiobook/${encodeURIComponent(abId)}?part=${encodeURIComponent(file)}">${esc(file)}</a>`;
}

// Adapter for the `audiobook` object on /api/books rows.
function bookAudio(b) {
  const a = b.audiobook;
  return a ? { id: a.audible_isbn, local: a.audiobook_local, audibleUrl: a.audible_url, path: a.audiobook_path } : null;
}


// ── dialogs, toasts, manage mode ──────────────────────────────────────────────
// Native <dialog>.showModal(): Escape closes, focus is trapped inside and
// returns to the opener, and the page behind is inert — none of which the old
// hand-rolled overlays did. The dialog builds its own markup, so pages carry no
// modal HTML. Never use alert()/confirm()/prompt(): they block the page and
// look out of place.

const FILE_DELETE_WARNING = "This will permanently delete the file(s) from disk and cannot be undone.";

function _openDialog({ title, message = "", warning = "", fields = [], confirmLabel = "OK",
                       cancelLabel = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "dlg";
    const titleId = `dlg-title-${Math.random().toString(36).slice(2, 8)}`;
    dlg.setAttribute("aria-labelledby", titleId);
    const inputs = fields.map((f) =>
      `<input class="modal-input" type="${esc(f.type || "text")}" name="${esc(f.name)}" aria-label="${esc(f.label || f.name)}"
              placeholder="${esc(f.placeholder ?? f.label ?? "")}" value="${esc(f.value ?? "")}"${f.max ? ` max="${esc(f.max)}"` : ""}${f.required ? " required" : ""}>`
    ).join("");
    // method=dialog: Enter in a field submits (native `required` validation runs first).
    dlg.innerHTML = `<form method="dialog" class="modal-box">
        <h3 class="modal-title" id="${titleId}">${esc(title)}</h3>
        ${message ? `<p class="modal-desc">${esc(message)}</p>` : ""}
        ${warning ? `<p class="modal-warning">${esc(warning)}</p>` : ""}
        ${inputs}
        <div class="modal-actions">
          <button type="button" class="btn" data-cancel>${esc(cancelLabel)}</button>
          <button type="submit" class="btn${danger ? " btn-danger" : ""}" value="ok">${esc(confirmLabel)}</button>
        </div>
      </form>`;
    // Settle from the events themselves rather than waiting on the async `close`
    // event: submit (confirm), the Cancel button, a backdrop click, and `cancel`
    // (Escape). `close` stays as a fallback for any other way the dialog ends.
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      const values = {};
      for (const f of fields) values[f.name] = dlg.querySelector(`[name="${f.name}"]`).value.trim();
      if (dlg.open) dlg.close();  // returns focus to whatever opened it
      dlg.remove();
      resolve(ok ? values : null);
    };
    dlg.querySelector("form").addEventListener("submit", (e) => { e.preventDefault(); finish(true); });
    dlg.querySelector("[data-cancel]").addEventListener("click", () => finish(false));
    // A click on the backdrop lands on the <dialog> itself; the form fills the rest.
    dlg.addEventListener("click", (e) => { if (e.target === dlg) finish(false); });
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); });
    // Escape normally arrives as `cancel`; handling the key too makes it
    // independent of the browser's close-watcher behaviour.
    dlg.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); finish(false); } });
    dlg.addEventListener("close", () => finish(dlg.returnValue === "ok"));
    document.body.appendChild(dlg);
    dlg.showModal();
    const first = dlg.querySelector("input");
    if (first) { first.focus(); first.select(); }
  });
}

// danger: styles the confirm button red and leaves focus on Cancel (the first
// button), so a stray Enter cannot delete anything.
async function confirmDialog(opts) {
  return (await _openDialog(opts)) !== null;
}

// fields: [{name, label, value, placeholder, required, type, max}] (`type` defaults to "text";
// "date" gives the native picker, `max` its latest day). Resolves to the trimmed
// values keyed by name, or null if cancelled.
function promptDialog(opts) {
  return _openDialog(opts);
}

// opts: {action: {label, run, busyLabel}, duration}. With an action the toast carries one
// button (an Undo, say): it runs `run()` (async is fine), disables itself and shows
// `busyLabel` meanwhile, then dismisses the toast; if `run` throws, its message is shown
// as an error toast, so throw something readable. A toast with an action lasts 8 s and
// holds still while the pointer or keyboard focus is on it. `duration` (ms) overrides.
function toast(message, type = "error", opts = {}) {
  let region = document.getElementById("toast-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "toast-region";
    region.className = "toast-region";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
  }
  const { action, anchor } = opts;
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  let timer;
  let busy = false;
  const dismiss = () => { clearTimeout(timer); t.remove(); };
  if (action) {
    t.classList.add("has-action");
    const text = document.createElement("span");
    text.textContent = message;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "link-btn toast-action";
    btn.textContent = action.label;
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (busy) return;
      busy = true;
      btn.disabled = true;
      btn.textContent = action.busyLabel || "Working…";
      try {
        await action.run();
        dismiss();
      } catch (err) {
        dismiss();
        toast(err && err.message ? err.message : "That didn't work.", "error");
      }
    });
    t.append(text, btn);
  } else {
    t.textContent = message;
  }
  t.addEventListener("click", () => { if (!busy) dismiss(); });
  // The region is fixed-position, so where it sits in the DOM is free: next to the control
  // the reader just used, an Undo is the next Tab stop instead of the last one on the page.
  if (action && anchor && anchor.isConnected) anchor.after(region);
  region.appendChild(t);
  const ms = opts.duration ?? (action || type === "error" ? 8000 : 4000);
  const arm = () => { clearTimeout(timer); timer = setTimeout(dismiss, ms); };
  if (action) {
    for (const ev of ["mouseenter", "focusin"]) t.addEventListener(ev, () => clearTimeout(timer));
    for (const ev of ["mouseleave", "focusout"]) t.addEventListener(ev, () => { if (!busy) arm(); });
  }
  arm();
}

// The Edit/Done toggle. Adds .managing to `container` (CSS reveals the edit
// controls and the ⋯ menu under it) and keeps the button's label/state in step;
// handlers read `mode.on`.
function manageMode(container, button, { label = "Edit", doneLabel = "Done" } = {}) {
  const mode = { on: false };
  button.addEventListener("click", () => {
    mode.on = !mode.on;
    container.classList.toggle("managing", mode.on);
    button.classList.toggle("active", mode.on);
    button.innerHTML = mode.on ? `${icon("check")}${esc(doneLabel)}` : `${icon("edit")}${esc(label)}`;
    button.setAttribute("aria-pressed", String(mode.on));
  });
  return mode;
}

// ── row menu (⋯) ──────────────────────────────────────────────────────────────
// Destructive actions live here rather than beside the edit buttons, so deleting
// is ⋯ → item → confirmDialog. `attrs` is a ready-made attribute string for the
// click handler to read (`data-book-id="…" data-title="…"`); `label` names the row
// for screen readers. The glyph is text, not icon(): more_horiz isn't in the
// self-hosted Material Symbols subset.
function moreBtn(attrs, label) {
  return `<button class="row-menu-btn" ${attrs} aria-haspopup="menu" aria-label="${esc(label)}">⋯</button>`;
}

// items: [{label, danger, run}]. A native popover in the top layer, so no card or
// row can clip it; it brings Escape, outside-click dismissal and focus return with
// it. Arrow keys move between items, and choosing one closes the menu before
// run() starts (run() normally opens a confirmDialog).
function openMenu(anchor, items) {
  const menu = document.createElement("div");
  menu.className = "row-menu";
  menu.setAttribute("popover", "auto");
  menu.setAttribute("role", "menu");
  menu.innerHTML = items.map((it, i) =>
    `<button type="button" role="menuitem" class="row-menu-item${it.danger ? " danger" : ""}" data-i="${i}">${esc(it.label)}</button>`
  ).join("");
  const buttons = [...menu.querySelectorAll("button")];

  menu.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    menu.hidePopover();
    items[b.dataset.i].run();
  });
  menu.addEventListener("keydown", (e) => {
    const at = buttons.indexOf(document.activeElement);
    const next = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: buttons.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    buttons[(next + buttons.length) % buttons.length].focus();
  });
  menu.addEventListener("toggle", (e) => {
    if (e.newState !== "closed") return;
    menu.remove();
    anchor.focus();
  });

  document.body.appendChild(menu);
  menu.showPopover();
  // Right-aligned under the button; above it when the viewport has no room below.
  const a = anchor.getBoundingClientRect();
  const m = menu.getBoundingClientRect();
  const below = a.bottom + 4 + m.height <= window.innerHeight;
  menu.style.top = `${Math.max(4, below ? a.bottom + 4 : a.top - 4 - m.height)}px`;
  menu.style.left = `${Math.max(4, Math.min(a.right - m.width, window.innerWidth - m.width - 4))}px`;
  buttons[0].focus();
}
