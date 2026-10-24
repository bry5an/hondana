// /add — upload an EPUB, then show what the pipeline made of it.
//
// Adding a book used to end at "Added <title>". The file landed in the library
// with every difficulty number blank until the next full pipeline run, so the
// reader had no idea whether what they just added was within reach. The page
// now waits on the server's per-book chain (analyze → Natively → pair → audiobook
// link → build; see epublib/book_jobs.py) and reports the result: extracted
// metadata, Natively level, and the four reading-load numbers measured against
// the reader's own known words.

// ── Shared helpers ────────────────────────────────────────────────────────────

const statusEl = document.getElementById('upload-status');
const resultEl = document.getElementById('result');

// An <img onerror> for a cover: the glyph STYLE §6 gives a book with none, in the image's place.
function coverGone(img) {
  const ph = document.createElement('span');
  ph.setAttribute('aria-hidden', 'true');
  ph.textContent = 'menu_book';
  ph.className = img.closest('.book-cover') ? 'mi placeholder' : `mi cover-ph ${img.className}`;
  img.replaceWith(ph);
}

// A title or filename wrapped so a screen reader voices Japanese in Japanese (`lang="ja"`).
const named = (text, tag = 'strong') => `<${tag}${langAttr(text)}>${esc(text)}</${tag}>`;

// #upload-status is a live region: rewriting it with identical markup (the job poll repeats
// the same step every 1.5 s) would make a screen reader read it out again. Returns whether
// the banner was actually redrawn. `glyph` null draws the bare well (no icon dot).
let lastStatus = '';
function showStatus(type, html, glyph) {
  const markup = statusHtml(type, html, glyph === null ? undefined : glyph || 'check');
  if (markup === lastStatus && statusEl.firstChild) return false;
  lastStatus = markup;
  keepFocus(statusEl);
  statusEl.innerHTML = markup;
  return true;
}

// Short milestones for a screen reader that don't belong in the banner (file chosen, one
// queue row added / analyzed / failed). Cleared first so repeating a message still speaks.
const announceEl = document.getElementById('sr-announce');
function announce(text) {
  announceEl.textContent = '';
  setTimeout(() => { announceEl.textContent = text; }, 50);
}

// Hand focus to the file picker (after "Add another" / "Add more" removed the button that had it).
function focusPicker() {
  document.getElementById('file-input').focus({ preventScroll: true });
}

// Redrawing a row or the banner destroys the button just pressed (Retry, Add anyway, ×), and focus
// would fall to <body>: move it first to the staged card, or, on the URL path, to the URL field.
function keepFocus(el) {
  if (!el.contains(document.activeElement)) return;
  const card = document.getElementById('staged-card');
  (card.hidden ? document.getElementById('url-input') : card).focus({ preventScroll: true });
}

// Same bands as app.js's card chips — one unknown word is inferable, two force
// a re-read, so the cut points are about sentences you must stop on.
let knownCount = null;
const knownReady = fetch('/api/known-words')
  .then((r) => r.json())
  .then((d) => { if (d && typeof d.count === 'number') knownCount = d.count; })
  .catch(() => {});

// ── Plain-language messages ───────────────────────────────────────────────────
//
// Nothing the server or the network says is shown raw: each failure becomes a sentence with a
// next step, and the original text rides along in a `title` for debugging.

const LANG_NAMES = { ja: 'Japanese', en: 'English' };
const NETWORK_MSG = 'Lost the connection to the server';

// An Error that already carries its plain-language text.
function fail(text, raw, retryable = false) {
  return Object.assign(new Error(text), { text, raw: raw || text, retryable });
}

// {text, raw, retryable} for anything thrown or answered, fit to show.
function describeError(err) {
  if (err && err.text) return { text: err.text, raw: err.raw, retryable: !!err.retryable };
  if (err instanceof TypeError) return { text: NETWORK_MSG, raw: err.message, retryable: true };
  return { text: 'Something went wrong', raw: err && err.message, retryable: false };
}

// The server's `detail` for a failed upload / download / acquire.
function describeServer(status, data) {
  const detail = data && (data.detail || data.message);
  const raw = typeof detail === 'string' ? detail : JSON.stringify(detail || data || status);
  const d = typeof detail === 'string' ? detail : '';
  let text, html;
  if (/exceeds the 100 MB/i.test(d)) text = 'Over the 100 MB limit — try a smaller file';
  else if (/only \.epub|does not appear to be an epub/i.test(d)) text = "That isn't an EPUB file — only .epub files can be added";
  else if (/unsafe filename/i.test(d)) text = "That file name can't be used — rename the file and try again";
  else if (/download failed/i.test(d)) text = "Couldn't download that link — check the address, and that it doesn't need a login";
  else if (/saved, but indexing failed/i.test(d)) {
    text = 'The file was saved, but adding it to the library failed — run Build index on the admin page';
    html = 'The file was saved, but adding it to the library failed — run Build index on the <a href="/admin">admin page</a>';
  }
  else if (status >= 500) text = 'The server hit a problem adding that book — try again in a moment';
  else text = "The server didn't accept that — check the file and try again";
  const indexing = /saved, but indexing failed/i.test(d);  // the file is in; only the index is behind
  return { text, html, raw, indexing, retryable: status >= 500 && !indexing };
}

// The text, with the raw cause on hover. `html` (trusted, with a link) stands in for `text` on screen.
function errHtml(d) {
  const body = d.html || esc(d.text);
  return d.raw && d.raw !== d.text
    ? `<span title="${esc(d.raw)}">${body}</span>`
    : body;
}

const RETRY_BTN = ' · <button type="button" class="link-btn" data-act="retry">Retry</button>';

function showFailure(desc, retry) {
  const html = errHtml(desc) + (desc.retryable && retry ? RETRY_BTN : '');
  if (showStatus('err', html, '!') && desc.retryable && retry) {
    statusEl.querySelector('[data-act="retry"]').addEventListener('click', retry);
  }
}

// What a finished job's `error` ("step: ExcName: message; step: …", see run_book_chain) means
// for the reader: the book is added either way, so each is a note on what didn't happen.
const STEP_NOTES = {
  analyze: "the vocabulary analysis didn't finish",
  enrich: 'no Natively level',
  pair: "couldn't pair editions",
  link: "couldn't check for an audiobook",
  build: "the library index wasn't refreshed",
  'epub-catalog': 'genre & synopsis lookup skipped',
};
const UNREACHABLE_RE = /connection|timed? ?out|resolve|unreachable|ssl|HTTPS?ConnectionPool/i;

function jobNotes(error) {
  if (!error) return [];
  return error.split('; ').map((part) => {
    const step = part.split(':')[0].trim();
    const note = STEP_NOTES[step];
    if (!note) return '';
    if (step === 'enrich') {
      return UNREACHABLE_RE.test(part)
        ? 'no Natively level (learnnatively.com unreachable — retried in 14 days)'
        : 'no Natively level (will be retried later)';
    }
    return note;
  }).filter(Boolean);
}

// jobNotes() as HTML, the raw error on hover; '' when there was none.
function notesHtml(error) {
  const notes = jobNotes(error);
  if (!notes.length) return error ? `<span title="${esc(error)}">some steps didn't finish</span>` : '';
  return `<span title="${esc(error)}">${esc(notes.join(' · '))}</span>`;
}

// The reader's active profile language, for "this book isn't in it" copy. Japanese until /api/profiles
// answers (the same default as the book page).
let profileLang = 'ja';
const profileReady = loadActiveProfile().then((p) => { if (p && p.language) profileLang = p.language; });
const offProfile = (book) => !!book.language && book.language !== profileLang;

// ── Rendering ─────────────────────────────────────────────────────────────────
//
// The result answers one question first — can I read this? — so it reads top to bottom as the
// book's cover and title, the reading-load readout (the same drawing /book uses), where the
// book lands in Read Next, and what to do next. The metadata the pipeline extracted is one line
// that opens the edit form in place. The page is built once per book and its parts repainted as
// the analysis lands, so a form being edited while the analysis runs isn't replaced under you.

const resultPart = (id) => document.getElementById(id);

// Natively belongs to Japanese books only (enrich skips the rest), and a null level means "no
// match" only once the lookup has run — and succeeded: when it failed (`natFailed`: the site was
// unreachable, or the page lost track of the job) the chip says "not yet" and the banner says why.
// Blue, like every Natively level (STYLE §8).
function nativelyChip(book, state, natFailed, partial) {
  if (book.language && book.language !== 'ja') return '';
  const nat = book.natively;
  const lv = nat && nat.natively_level;
  if (lv == null) {
    const text = state === 'running' || partial ? 'Matching Natively…' : natFailed ? 'No Natively level yet' : 'No Natively match';
    return `<span class="chip">${text}</span>`;
  }
  const unconfirmed = levelUnconfirmed(nat);
  const label = `Natively Lv ${unconfirmed ? '~' : ''}${lv}`;
  const cls = `chip level${unconfirmed ? ' unconfirmed' : ''}`;
  return nat.natively_url
    ? `<a class="${cls}" href="${esc(nat.natively_url)}" target="_blank" rel="noopener" title="${esc(levelTitle(lv, nat))}">${label}${icon('open_in_new')}<span class="sr-only"> (opens learnnatively.com in a new tab)</span></a>`
    : `<span class="${cls}" title="${esc(levelTitle(lv, nat))}">${label}</span>`;
}

// The same sentence as /book's lang-note (static/book.js languageNoteHtml): reading load is for the
// profile's language, so a book in the other one has no numbers to show.
function languageNote(book) {
  const own = LANG_NAMES[book.language] ? `This is ${book.language === 'en' ? 'an' : 'a'} ${LANG_NAMES[book.language]} book. ` : '';
  return `${own}Reading load is for ${LANG_NAMES[profileLang] || "your language's"} books on this profile.`;
}

// What stands in the readout's place when there are no numbers, in a box of the same shape.
function readoutNote(html) {
  return `<div class="readout readout--load readout--note" id="load-card">` +
         `<div class="readout-label">READING LOAD</div><p class="note-text">${html}</p></div>`;
}

// While the chain runs: the readout's footprint, so nothing jumps when the numbers arrive.
const capitalize = (t) => t.charAt(0).toUpperCase() + t.slice(1);

// A readout's worth of made-up numbers, drawn invisibly so the placeholder takes the height of the
// real one at every width (it was a fixed 170px, and the result jumped 77–197px when the verdict landed).
const GHOST_K = { hard_pct: 30.8, flow_pct: 40.4, i1_pct: 28.8, tokens_per_lookup: 7, unknown_p90: 3, words_to_98: 1000, known_pct: 80.8 };

function readoutWaiting(book, step) {
  const natively = !book.language || book.language === 'ja';
  return `<div class="readout readout--load readout--wait" id="load-card" aria-busy="true">
      <div class="wait-ghost" aria-hidden="true">${loadReadoutInner(GHOST_K)}</div>
      <div class="wait-over">
        <div class="readout-label">READING LOAD</div>
        <p class="wait-line"><span id="wait-step">${esc(capitalize(step || 'analyzing'))}…</span></p>
        <div class="wait-bar" aria-hidden="true"></div>
        <p class="note-text">Review the details above while we ${natively ? 'analyze and match a Natively level' : 'analyze'}.</p>
      </div>
    </div>`;
}

function readoutFor(book, state) {
  if (state === 'running') return readoutWaiting(book, resultEl.dataset.step);
  if (state === 'unsupported_language') {
    return readoutNote("There is no vocabulary pipeline for this book's language (Japanese and English " +
      'are covered), so there are no reading-load numbers. If the language was detected wrongly, correct it with Edit details.');
  }
  if (state === 'no_text') {
    return readoutNote("No extractable text — this EPUB is image-only or DRM-protected, so it can't be tokenized.");
  }
  if (state !== 'ok') {
    return readoutNote(`Analysis failed for this book. Open <a href="/book/${esc(book.book_id)}">its page</a> and choose Analyze now to try again.`);
  }
  const k = book.known || {};
  if (k.hard_pct == null && offProfile(book)) return readoutNote(esc(languageNote(book)));
  if (k.hard_pct == null) {
    return readoutNote('Text analyzed, but reading load needs a vocabulary to measure against — ' +
      '<a href="/known">import your known words</a> to get strain, coverage and words-to-98%.');
  }
  return loadReadoutHtml(k, { jpdb: profileLang === 'ja' });
}

// "汐見夏衛 · あの花シリーズ vol 01 · ライトノベル · Japanese", one part per fact the book has.
function metaParts(book) {
  const series = book.series_name && book.series_name !== book.title  // a standalone's series is its own title
    ? `${named(book.series_name, 'span')}${book.series_order ? ` vol ${esc(book.series_order)}` : ''}`
    : '';
  return [
    book.author ? named(book.author, 'span') : '',
    series,
    book.genre ? named(book.genre, 'span') : '',
    esc(LANG_NAMES[book.language] || ''),
  ].filter(Boolean).join(' · ');
}

// Title, cover and the metadata line: repainted when the book is first drawn, when analysis
// lands, and when the edit form saves.
function paintHead(book) {
  const h = resultPart('result-title');
  h.textContent = book.title || 'Added book';
  h.lang = langAttr(h.textContent) ? 'ja' : '';
  resultPart('meta-parts').innerHTML = metaParts(book);
}

// Where it lands: the Natively level and the Read Next band the verdict puts it in.
function whereHtml(book, state, natFailed, partial) {
  const k = book.known || {};
  const lands = state === 'ok' && k.hard_pct != null && !offProfile(book)
    ? `<span class="where-line">Lands in <b>${esc(queueBand(k.hard_pct).title)}</b> · <a href="/next">Read Next ${icon('arrow_forward')}</a></span>`
    : state === 'running' || partial  // holds the line the verdict will add, so the row doesn't grow
      ? `<span class="where-line where-ghost" aria-hidden="true">Lands in <b>${k.hard_pct != null ? esc(queueBand(k.hard_pct).title) : 'A stretch'}</b> · <a href="/next" tabindex="-1">Read Next ${icon('arrow_forward')}</a></span>`
      : '';
  return nativelyChip(book, state, natFailed, partial) + lands;
}

// Open book is the primary action; the EPUB is one tap away; a book that needs studying says where.
function actionsHtml(book, state, partial) {
  const k = book.known || {};
  const study = state === 'ok' && k.hard_pct != null && !offProfile(book) && strainBucket(k.hard_pct).cls.match(/high|heavy/)
    ? `<a class="dl-btn dl-btn--quiet" href="/book/${esc(book.book_id)}#study-card">${icon('menu_book')}Study list</a>`
    : '';
  // The audiobook slot is always there once the chain has linked (PRODUCT.md principle 2): M4B,
  // Audible, or a plain "No audiobook". Before that it would say "none" about a book not yet checked.
  const audio = partial
    ? `<span class="dl-btn dl-btn--audible dl-btn--quiet ghost-slot" aria-hidden="true">${icon('headphones')}Audible${icon('open_in_new')}</span>`  // its place, held
    : dlAudio(bookAudio(book), { tone: 'quiet', placeholder: true, label: book.title || '' });
  return `<a class="submit-btn" href="/book/${esc(book.book_id)}">Open book ${icon('arrow_forward')}</a>` +
    study + dlEpub(book.book_id, { tone: 'quiet', label: book.title || '' }) + audio +
    '<button type="button" class="ghost-btn" id="add-another">Add another</button>';
}

// What the pipeline linked, once it has finished (the progress steps announce both while it runs).
function linkedLine(book, state, partial) {
  if (state === 'running' || partial) return '';
  const parts = [];
  const p = book.pair_partner;
  if (p) parts.push(`Paired with the ${p.language === 'ja' ? 'Japanese' : 'English'} edition, <a href="/book/${esc(p.book_id)}"${langAttr(p.title)}>${esc(p.title || 'open it')}</a>.`);
  return parts.join(' ');
}

function buildResult(book) {
  resultEl.dataset.bookId = book.book_id;
  resultEl.classList.remove('editing');
  resultEl.innerHTML = `
    <section class="book-hero add-hero" aria-labelledby="result-title">
      <div class="book-cover"><img id="result-cover" src="${esc(coverUrl(book))}" alt=""
           onerror="coverGone(this)"></div>
      <div class="add-head">
        <p class="add-arrived">${icon('check')}Added to your library</p>
        <h2 class="result-title" id="result-title" tabindex="-1"></h2>
        <div class="add-meta">
          <span id="meta-parts"></span>
          <button type="button" class="link-btn edit-meta" id="edit-meta" aria-expanded="false" aria-controls="rc-details">Edit details</button>
        </div>
      </div>
      <div class="add-readout" id="readout-slot"></div>
      <div class="add-where" id="where"></div>
    </section>
    <div class="review-card" id="rc-details" hidden></div>
    <div class="result-actions" id="result-actions"></div>
    <p class="result-line" id="linked-line"></p>`;
  wireResult(book);
  resultPart('result-title').focus();
}

// `partial`: the analysis has landed but the rest of the chain (Natively, pairing, links) is still
// running — the verdict is drawn, the Natively chip says it is matching, the links wait.
let paintedReadout = '';
function render(book, state, { natFailed = false, partial = false } = {}) {
  if (resultEl.dataset.bookId !== book.book_id || !resultPart('result-title')) { buildResult(book); paintedReadout = ''; }
  paintHead(book);
  // The same readout drawn again is left alone: no jump under the reader when the job ends, and an
  // open "How this is measured" stays open.
  const readout = readoutFor(book, state);
  if (readout !== paintedReadout) { resultPart('readout-slot').innerHTML = readout; paintedReadout = readout; }
  resultPart('where').innerHTML = whereHtml(book, state, natFailed, partial);
  resultPart('linked-line').innerHTML = linkedLine(book, state, partial);
  const actions = resultPart('result-actions');
  const hadFocus = actions.contains(document.activeElement) ? document.activeElement.id : '';
  actions.innerHTML = actionsHtml(book, state, partial || state === 'running');
  wireAddAnother();
  if (hadFocus) { const el = document.getElementById(hadFocus); if (el) el.focus(); }
}

// The current step of a running analysis, in the readout's placeholder as well as the banner.
function setStep(text) {
  resultEl.dataset.step = text;
  const el = document.getElementById('wait-step');
  if (el) el.textContent = `${capitalize(text)}…`;
}

function wireAddAnother() {
  const again = document.getElementById('add-another');
  if (again) again.addEventListener('click', unstage);
}

function wireResult(book) {
  const toggle = resultPart('edit-meta');
  const form = resultPart('rc-details');
  toggle.addEventListener('click', () => {
    const open = form.hidden;
    form.hidden = !open;
    resultEl.classList.toggle('editing', open);
    toggle.setAttribute('aria-expanded', String(open));
    toggle.textContent = open ? 'Hide details' : 'Edit details';
    // The first field, not the cover row's hidden file input (focusing that does nothing).
    if (open) { const first = form.querySelector('input[type="text"], select'); if (first) first.focus(); }
  });

  // The same form as the book page. A language change queues a fresh pipeline
  // run server-side, so re-enter the add → analyze flow to show its result;
  // anything else just repaints the head.
  BookDetails.mount(form, book.book_id, {
    onSaved: (result, changed) => {
      const v = result.values;
      if (changed.includes('language')) {
        afterAdd(book.book_id, v.title || book.title);
        return;
      }
      if (changed.includes('cover')) {
        const slot = document.querySelector('#result .book-cover');
        slot.innerHTML = `<img id="result-cover" src="${esc(coverUrl({ book_id: book.book_id, cover_v: result.cover.v }))}" alt="" onerror="coverGone(this)">`;
        return;
      }
      paintHead({ ...book, ...v });
      showStatus('ok', `${named(v.title || book.title)} updated`, 'check');
    },
  });
}

// ── The add → analyze sequence ────────────────────────────────────────────────

// The verdict as a sentence, for a screen reader (the hero draws it): "<title>: low strain, lands in
// Comfortable now" — or, with no numbers, the reason there are none.
function verdictLine(book, state, fallbackTitle) {
  const title = book.title || fallbackTitle;
  const k = book.known || {};
  let said;
  if (state === 'running') return '';
  if (state === 'unsupported_language') said = 'no reading load, there is no vocabulary pipeline for this language';
  else if (state === 'no_text') said = 'no extractable text, so no reading load';
  else if (state !== 'ok') said = "the analysis didn't finish";
  else if (k.hard_pct == null && offProfile(book)) {
    said = `${LANG_NAMES[book.language] || 'other-language'} book, no reading load on this profile`;
  } else if (k.hard_pct == null) said = 'no known-words list yet, so no reading load';
  else if (offProfile(book)) said = `${LANG_NAMES[book.language] || 'other-language'} book, no reading load on this profile`;
  else said = `${strainBucket(k.hard_pct).label}, lands in ${queueBand(k.hard_pct).title}`;
  return `${title}: ${said}`;
}

// The queue row of a single upload gives way to the hero once that exists: the file appears once.
function retireQueue() {
  queueEl.innerHTML = '';
  cardEl.hidden = true;
}

// A cleared banner (the hero shows the progress and the result); the next showStatus must redraw.
function clearStatus() {
  keepFocus(statusEl);
  statusEl.innerHTML = '';
  lastStatus = '';
}

const afterAdd = tracked(async function afterAdd(bookId, fallbackTitle) {
  await profileReady;  // settled long before an upload finishes; this just makes it certain
  delete resultEl.dataset.step;
  if (resultEl.dataset.bookId === bookId) clearStatus();  // re-running a book already drawn
  else showStatus('pending', `Added ${named(fallbackTitle)} — reading metadata…`, 'upload');

  let book = { book_id: bookId, title: fallbackTitle };
  try {
    const resp = await fetch(`/api/books/${bookId}`);
    if (resp.ok) book = await resp.json();
  } catch (err) { /* the review card degrades to the upload's own fields */ }

  // From here the hero says what is happening and the banner stays quiet until there is a note.
  render(book, 'running');
  retireQueue();
  clearStatus();
  announce(`Added ${book.title || fallbackTitle} to your library. Analyzing…`);

  try {
    // The server already queued this book's chain when it was added, so this
    // is idempotent — and it also covers a book that arrived some other way.
    const resp = await fetch(`/api/books/${bookId}/analyze`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw fail("Couldn't start the analysis", data.detail);
    if (data.status === 'skipped') {
      const state = data.reason || 'unsupported_language';
      render(book, state);
      announce(verdictLine(book, state, fallbackTitle));
      return;
    }

    // `analyze` is the chain's first step and ends by writing this reader's reading load, so the
    // moment the job has moved on the verdict exists: draw it now, not after the Natively lookup.
    let early = '';
    const job = await waitForJob(bookId, (step, status) => {
      setStep(stepLabel(step));
      if (early || !step || step === 'analyze' || !status.vocab_status) return;
      if (status.vocab_status === 'ok' && !status.known) return;  // between the two writes: next tick
      const analyzed = { ...book, known: status.known };
      render(analyzed, status.vocab_status, { partial: true });
      early = verdictLine(analyzed, status.vocab_status, fallbackTitle);
      announce(early);
    });

    const fr = await fetch(`/api/books/${bookId}`);
    const fresh = fr.ok ? await fr.json() : book;
    const vocab = fresh.vocab && fresh.vocab.status;
    const state = vocab || (job.error ? 'error' : 'no_text');
    render(fresh, state, { natFailed: /(^|; )enrich:/.test(job.error || '') });
    const verdict = early || verdictLine(fresh, state, fallbackTitle);
    // The book is in the library whatever the chain did, so a failed step is a note, not an error.
    // The hero already shows the result: a clean run adds no banner, only a spoken verdict.
    const notes = notesHtml(job.error);
    if (notes) {
      showStatus('warn',
        (early ? '' : `<span class="sr-only">${esc(verdict)}. </span>`) + `Added, but some steps didn't finish` +
        `<span class="sum-filed">${notes}</span>`, 'warning');
    } else if (!early) {
      announce(verdict);
    }
  } catch (err) {
    render(book, 'error', { natFailed: true });
    showStatus('warn', `${named(book.title || fallbackTitle)} added, but the ` +
                       `analysis didn't finish: ${errHtml(describeError(err))}`, 'warning');
  }
});

const STEP_LABELS = {
  analyze: 'analyzing the text',
  enrich: 'matching a Natively level',
  pair: 'pairing editions',
  link: 'finding the audiobook',
  build: 'building the library index',
  'epub-catalog': 'looking up genre & synopsis',
};
// Never a raw step id: a step this page doesn't know yet reads as the tail end of the chain.
const stepLabel = (step) => STEP_LABELS[step] || 'finishing up';

// Poll the server-side job until it finishes. A long novel can take a minute or
// two to tokenize, so wait generously; a server restart loses the in-memory job
// (status.job is null), in which case whatever vocab exists is the answer.
async function waitForJob(bookId, onStep, timeoutMs = 15 * 60 * 1000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const r = await fetch(`/api/books/${bookId}/status`);
    if (!r.ok) throw fail('Lost track of the analysis job — open the book page to see where it got to');
    const status = await r.json();
    const { job } = status;
    if (!job) return { state: 'done', error: null };
    if (job.state === 'done' || job.state === 'error') return job;
    if (onStep) onStep(job.step, status);
    await new Promise((res) => setTimeout(res, 1500));
  }
  throw fail('Still analyzing after 15 minutes — check the book page later');
}

// ── Drop zone ─────────────────────────────────────────────────────────────────
//
// Choosing or dropping a file is the commit: the files are checked at once (a bad one is
// turned away by name, and stays named while the rest go), the rest start uploading, and the
// zone gives way to a staged card of one row per file. "Add another" / "Add more" brings it back.

const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const queueEl   = document.getElementById('upload-queue');
const cardEl    = document.getElementById('staged-card');
const actionsEl = document.getElementById('staged-actions');
const rejectEl  = document.getElementById('reject-note');
const seriesSlot = document.getElementById('batch-series-slot');

// A batch's banner is its summary, so it sits at the top of the staged card, over the rows; every
// other state keeps it under the card.
function placeStatus(inCard) {
  if (inCard) cardEl.prepend(statusEl);
  else if (cardEl.contains(statusEl)) rejectEl.after(statusEl);
}

const MAX_BYTES = 100 * 1024 * 1024;  // epublib/incoming.py MAX_BYTES
const fmtSize = (n) => (n >= 1048576
  ? `${(n / 1048576).toFixed(n >= 10485760 ? 0 : 1)} MB`
  : `${Math.max(1, Math.round(n / 1024))} KB`);

// The headline promises the answer, in the reader's own numbers.
function paintPromise() {
  const lang = LANG_NAMES[profileLang] ? `${LANG_NAMES[profileLang]} ` : '';
  document.getElementById('dz-promise').textContent = knownCount
    ? `a book — see how it reads against your ${knownCount.toLocaleString()} ${lang}words`
    : "a book — we'll score it once you import your known words";
}
Promise.all([knownReady, profileReady]).then(paintPromise);

// A page left mid-upload loses the upload; the server's analysis, once the file is there, doesn't need the tab.
let inflight = 0;
window.addEventListener('beforeunload', (e) => {
  if (inflight > 0) { e.preventDefault(); e.returnValue = ''; }
});

let staged = false;

// Whether anything on the page is still working: an upload on the wire, or a flow (a single add, a
// batch, a URL download, an analysis) that will paint into the rows and result it was started on.
// Tearing those down mid-flight orphans the flow, so a new add waits for this to be zero.
let flows = 0;
const isBusy = () => inflight > 0 || flows > 0;

// An async function that counts as a flow for as long as it runs.
function tracked(fn) {
  return async (...args) => {
    flows++; syncBusy();
    try { return await fn(...args); }
    finally { flows--; syncBusy(); }
  };
}

const urlBtn  = document.getElementById('url-btn');
const urlBusy = document.getElementById('url-busy');
const urlHelp = document.getElementById('url-input').getAttribute('aria-describedby');

// While busy the URL form says why it can't be used; once idle the "wait" note on the drop goes too.
function syncBusy() {
  const busy = isBusy();
  urlBtn.disabled = busy;
  urlBusy.hidden = !busy;
  document.getElementById('url-input').setAttribute('aria-describedby', busy ? `${urlHelp} url-busy` : urlHelp);
  if (!busy && rejectEl.dataset.wait === 'busy') paintWaitNote();
}

// Split a choice into what can upload and what is turned away, each with its reason by name.
function triage(files) {
  const ok = [];
  const rejected = [];
  for (const f of files) {
    if (!/\.epub$/i.test(f.name)) rejected.push(`${named(f.name)} isn't an EPUB — only .epub files can be added`);
    else if (f.size > MAX_BYTES) rejected.push(`${named(f.name)} is ${fmtSize(f.size)} — the limit is 100 MB`);
    else ok.push(f);
  }
  return { ok, rejected };
}

function showRejects(list) {
  rejectEl.innerHTML = list.length
    ? statusHtml('err', list.length === 1 ? list[0] : `${list.length} files weren't added:<br>${list.join('<br>')}`, '!')
    : '';
}

// The files turned away because something was still running: the note names them while it runs, and
// goes on naming them once it is done (that is when they can be dropped again) until the next add.
let refusedWhileBusy = [];
function paintWaitNote() {
  const busy = isBusy();
  const shown = refusedWhileBusy.slice(0, 5).map((n) => named(n)).join(', ') +
    (refusedWhileBusy.length > 5 ? ` and ${refusedWhileBusy.length - 5} more` : '');
  const lead = busy ? 'Wait for these to finish, then add' : 'Not added while the last add was running — drop again';
  rejectEl.dataset.wait = busy ? 'busy' : 'done';
  rejectEl.innerHTML = statusHtml('pending', `${lead}: ${shown}`, null);
  announce(rejectEl.textContent.trim());
}
function clearWaitNote() {
  refusedWhileBusy = [];
  delete rejectEl.dataset.wait;
}

// Files chosen or dropped (anywhere on the page). While something is in flight they are turned away
// out loud; when it is not, a finished result gives way to the new add, as "Add another" would.
function chooseFiles(files) {
  if (!files.length) return;
  if (isBusy()) {
    for (const f of files) if (!refusedWhileBusy.includes(f.name)) refusedWhileBusy.push(f.name);
    paintWaitNote();
    fileInput.value = '';
    return;
  }
  clearWaitNote();
  if (staged || resultEl.dataset.bookId) unstage();
  const { ok, rejected } = triage([...files]);
  showRejects(rejected);
  fileInput.value = '';
  if (ok.length) startUploads(ok);
}

// Bring the zone back: "Add another", "Add more", or after a file that went nowhere.
function unstage() {
  clearWaitNote();
  staged = false;
  placeStatus(false);
  seriesSlot.innerHTML = '';
  queueEl.innerHTML = '';
  actionsEl.innerHTML = '';
  cardEl.hidden = true;
  dropZone.hidden = false;
  resultEl.innerHTML = '';
  delete resultEl.dataset.bookId;
  delete resultEl.dataset.step;
  statusEl.innerHTML = '';
  rejectEl.innerHTML = '';
  fileInput.value = '';
  focusPicker();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// A file that went nowhere (failed, already owned, cancelled) leaves a way back to the picker.
// After "saved, but indexing failed" the file is already in, so choosing again would only hit the
// duplicate check: the way forward is Build index.
function offerStartOver(row) {
  if (row && row.indexFailed) {
    actionsEl.innerHTML = '<a class="ghost-btn" href="/admin">Build index on the admin page</a>';
    return;
  }
  actionsEl.innerHTML = '<button type="button" class="ghost-btn" id="start-over">Choose another file</button>';
  document.getElementById('start-over').addEventListener('click', unstage);
}

// No click handler on the zone: it is a <label for="file-input">, so a click (or Enter/Space on
// the focused input) opens the picker natively.
fileInput.addEventListener('change', () => chooseFiles(fileInput.files));
// A file dropped anywhere on the page is an add: left to the browser it would navigate to (or
// download) the EPUB, which is where a second drop after a result used to end up. Only file drags
// are claimed, so text can still be dropped into the URL field.
const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
window.addEventListener('dragover', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dropZone.classList.toggle('drag-over', !dropZone.hidden);
});
window.addEventListener('dragleave', (e) => {
  if (!e.relatedTarget) dropZone.classList.remove('drag-over');  // left the window
});
window.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  chooseFiles(e.dataTransfer.files);
});

// ── Duplicates ────────────────────────────────────────────────────────────────

// The server answers 409 when the file is already in the library (identical
// bytes, same ISBN/ASIN, or same title + author). Not a failure: show the book you
// already own, with "Open it", and "Add anyway" for a deliberate second edition.
const dupMeta = (book) => [LANG_NAMES[book.language], 'EPUB'].filter(Boolean).join(' · ');

// The single-file flow: a neutral well with the existing book's cover, title and language.
async function showDuplicate(existing, retry, { another = false } = {}) {
  let book = { book_id: existing.book_id, title: existing.title };
  try {
    const resp = await fetch(`/api/books/${encodeURIComponent(existing.book_id)}`);
    if (resp.ok) book = await resp.json();
  } catch (err) { /* the well degrades to the title the 409 carried */ }
  const title = book.title || existing.title;
  const html = `
    <img class="dup-cover" src="${esc(coverUrl(book))}" alt="" onerror="coverGone(this)">
    <div class="dup-body">
      <div class="dup-head">Already in your library</div>
      <div class="dup-title"${langAttr(title)}>${esc(title)}</div>
      <div class="dup-meta">${esc(dupMeta(book))} · matched by ${esc(existing.reason)}</div>
    </div>
    <div class="dup-actions">
      <a class="ghost-btn dup-open" href="/book/${esc(existing.book_id)}">Open it</a>
      <button type="button" class="ghost-btn" data-act="anyway">Add anyway</button>
      ${another ? '<button type="button" class="ghost-btn" data-act="another">Choose another file</button>' : ''}
    </div>`;
  if (showStatus('dup', html, null)) {
    statusEl.querySelector('[data-act="anyway"]').addEventListener('click', retry);
    const again = statusEl.querySelector('[data-act="another"]');
    if (again) again.addEventListener('click', unstage);
  }
}

// A queue row has no room for the well: one line, same neutral tone.
function dupRowHtml(existing) {
  return rowOutcome(`Already in your library (${esc(existing.reason)})`,
    `<a href="/book/${esc(existing.book_id)}">Open it</a>` +
    `<button type="button" class="link-btn" data-act="anyway">Add anyway</button>`);
}

// A queue row's outcome with its actions beside it: each its own item, so a narrow row wraps between
// them and never leaves a separator dangling at the end of a line.
function rowOutcome(text, actions = '') {
  return `<span class="uq-acts"><span>${text}</span>${actions}</span>`;
}
const RETRY_ROW = '<button type="button" class="link-btn" data-act="retry">Retry</button>';

// ── Upload with progress ──────────────────────────────────────────────────────

// fetch() has no upload-progress events, so a big file showed only "Uploading…". `onXhr` hands
// the request out so a row can abort it. The language is always `auto`: the server reads it off
// the book, and the result's edit form corrects a wrong guess. Resolves {status, data}; rejects
// with an error fail() has already put in plain words (`aborted` when the reader cancelled).
function xhrUpload(file, { force = false, defer = false, onXhr = () => {} } = {}, onProgress = () => {}) {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append('file', file);
    body.append('language', 'auto');
    if (force) body.append('force', 'true');
    if (defer) body.append('defer', 'true');
    const xhr = new XMLHttpRequest();
    onXhr(xhr);
    xhr.open('POST', '/api/upload');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onerror = () => reject(fail(NETWORK_MSG, 'network error', true));
    xhr.onabort = () => reject(Object.assign(fail('Cancelled', 'aborted'), { aborted: true }));
    xhr.onload = () => {
      const ct = xhr.getResponseHeader('content-type') || '';
      if (!ct.includes('application/json')) {
        // Not the app answering: a reverse proxy turning the upload away.
        const raw = `Server rejected the upload (${xhr.status})`;
        reject(xhr.status === 413
          ? fail("The server's upload limit (set in nginx, below the app's 100 MB) turned this file away", raw)
          : fail("The server didn't take the upload — try again in a moment", raw, true));
        return;
      }
      try { resolve({ status: xhr.status, data: JSON.parse(xhr.responseText) }); }
      catch (err) { reject(fail("The server's answer wasn't readable — try again", 'unreadable response', true)); }
    };
    xhr.send(body);
  });
}

// ── Several files: a queue with per-file progress, then one pipeline pass ─────

// How a finished book reads, as chips: its strain for this reader and its Natively level — or why
// there is no strain (another language, no text, no known words yet).
function rowChips(row) {
  const book = row.book;
  if (!book) return '';
  const k = book.known || {};
  const chips = [];
  if (offProfile(book)) {
    chips.push(`<span class="chip">${esc(LANG_NAMES[book.language] || 'Other-language')} book · no reading load on this profile</span>`);
  } else if (k.hard_pct != null) {
    const s = strainBucket(k.hard_pct);
    chips.push(`<span class="chip strain ${s.cls}" title="${k.hard_pct}% of sentences have 2+ unknown words">${s.label}</span>`);
  } else {
    chips.push(`<span class="chip">${row.vocab === 'no_text' ? 'No extractable text' : 'No reading load yet'}</span>`);
  }
  const lv = book.natively && book.natively.natively_level;
  if (lv != null && !(book.language && book.language !== 'ja')) {
    chips.push(`<span class="chip level" title="${esc(levelTitle(lv, book.natively))}">Natively Lv ${lv}</span>`);
  }
  return chips.join('');
}

// A row that has its book drawn: finished, finished with notes, or analyzed with the rest of the
// chain (Natively, pairing, links) still running.
const FIN_STATES = ['done', 'warn', 'analyzed'];

// The rows that ended without a book drawn: already owned, cancelled, not added.
const END_ICONS = { dup: 'library_books', cancelled: 'block', err: 'close' };

// A high/heavy book says where to study, as /book's result does.
function rowStudy(row) {
  const k = row.book && row.book.known;
  if (!k || k.hard_pct == null || offProfile(row.book) || !/high|heavy/.test(strainBucket(k.hard_pct).cls)) return '';
  return `<a class="uq-study" href="/book/${esc(row.bookId)}#study-card">Study list</a>`;
}

function rowHtml(row) {
  if (row.book && FIN_STATES.includes(row.state)) {
    const title = row.book.title || row.title || row.file.name;
    return `<img class="uq-cover" src="${esc(coverUrl(row.book))}" alt="" onerror="coverGone(this)">` +
           `<span class="uq-main"><span class="uq-name"><a href="/book/${esc(row.bookId)}"${langAttr(title)}><span class="uq-t">${esc(title)}</span></a></span>` +
           `<span class="uq-chips">${rowChips(row)}${rowStudy(row)}</span>` +
           // A note on what didn't finish is a sentence: under the chips, not squeezed beside the title.
           (row.state === 'warn' ? `<span class="uq-state">${row.text || ''}</span></span>` : `</span><span class="uq-state">${row.text || ''}</span>`);
  }
  const pct = Math.round((row.progress || 0) * 100);
  const link = row.bookId
    ? `<a href="/book/${esc(row.bookId)}"${langAttr(row.title || row.file.name)}><span class="uq-t">${esc(row.title || row.file.name)}</span></a>`
    : `<span${langAttr(row.file.name)}>${esc(row.file.name)}</span>`;
  // Cancel only while the file is still to go or on the wire.
  const cancel = row.state === 'waiting' || row.state === 'up'
    ? `<button type="button" class="uq-x" data-act="cancel" aria-label="Cancel ${esc(row.file.name)}">&times;</button>`
    : '<span></span>';
  const name = `<span class="uq-name">${link}<span class="uq-size">${fmtSize(row.file.size)}</span></span>`;
  // A file that went nowhere has no progress left to show: its outcome is an icon, not a full bar.
  if (END_ICONS[row.state]) {
    return `<span class="uq-ico" aria-hidden="true">${icon(END_ICONS[row.state])}</span>${name}` +
           `<span class="uq-state">${row.text || ''}</span>${cancel}`;
  }
  return `${name}<span class="uq-bar"><span class="uq-fill" style="width:${pct}%"></span></span>` +
         `<span class="uq-state">${row.text || ''}</span>${cancel}`;
}

function paintRow(row) {
  keepFocus(row.el);
  row.el.className = `uq-row uq-${row.state}${row.book && FIN_STATES.includes(row.state) ? ' uq-fin' : END_ICONS[row.state] ? ' uq-end' : ''}`;
  row.el.innerHTML = rowHtml(row);
}

// The cancel ×: a file still waiting never starts; one on the wire is aborted.
function cancelRow(row) {
  if (row.state === 'up' && row.xhr) { row.xhr.abort(); return; }  // uploadRow settles the row
  if (row.state === 'waiting') {
    row.state = 'cancelled'; row.text = 'cancelled';
    paintRow(row);
  }
}

// `single`: the lone file of the one-book flow, whose rich result renders below instead of a summary.
async function uploadRow(row, { force = false, defer = true, single = false } = {}) {
  if (row.state === 'cancelled') return false;
  row.state = 'up'; row.progress = 0; row.text = '0%'; row.indexFailed = false;
  paintRow(row);
  inflight++;
  try {
    if (!(await ensureAdmin())) throw fail('Adding books needs the admin password', 'admin_required', true);
    const { status, data } = await xhrUpload(row.file, { force, defer, onXhr: (x) => { row.xhr = x; } }, (f) => {
      row.progress = f; row.text = `${Math.round(f * 100)}%`;
      const fill = row.el.querySelector('.uq-fill');
      if (fill) fill.style.width = `${Math.round(f * 100)}%`;
      const st = row.el.querySelector('.uq-state');
      if (st) st.textContent = row.text;
    });
    row.progress = 1;
    if (status === 200 && data.status === 'ok') {
      row.bookId = data.book_id; row.title = data.title;
      row.state = 'held'; row.text = single ? 'added' : 'added · waiting for analysis';
    } else if (status === 409 && data.existing) {
      row.state = 'dup'; row.dup = data.existing;
      row.text = single ? 'already in your library' : dupRowHtml(data.existing);
    } else {
      const d = describeServer(status, data);
      row.state = 'err'; row.text = rowOutcome(errHtml(d), d.retryable ? RETRY_ROW : '');
      row.indexFailed = d.indexing;
      announce(`${row.file.name} failed: ${d.text}`);
    }
  } catch (err) {
    if (err.aborted) {
      row.state = 'cancelled'; row.text = 'cancelled'; row.progress = 0;
    } else {
      const d = describeError(err);
      row.state = 'err'; row.text = rowOutcome(errHtml(d), d.retryable ? RETRY_ROW : '');
      announce(`${row.file.name} failed: ${d.text}`);
    }
  } finally {
    inflight--;
    row.xhr = null;
  }
  paintRow(row);
  return row.state === 'held';
}

// Poll every added book's server-side job until all have finished.
async function watchRows(rows) {
  const open = new Set(rows.filter((r) => r.bookId));
  while (open.size) {
    await Promise.all([...open].map(async (row) => {
      try {
        const r = await fetch(`/api/books/${row.bookId}/status`);
        if (!r.ok) return;
        const st = await r.json();
        const { job, vocab_status } = st;
        if (!job || job.state === 'done' || job.state === 'error') {
          row.job = job; row.vocab = vocab_status;
          open.delete(row);
          const fr = await fetch(`/api/books/${row.bookId}`);
          if (fr.ok) row.book = await fr.json();
          row.title = (row.book && row.book.title) || row.title;
          // Added either way: a step that failed makes the row an amber note, never a red error.
          const notes = job && job.error ? notesHtml(job.error) : '';
          row.state = notes ? 'warn' : 'done';
          row.text = notes
            ? `added · ${notes}`
            : esc(row.vocab === 'ok' ? 'analyzed'
                : row.vocab === 'no_text' ? 'added · no extractable text'
                : 'added');
          paintRow(row);
        } else if (job.step && job.step !== 'analyze' && st.vocab_status && !(st.vocab_status === 'ok' && !st.known)) {
          // The analysis has landed: show how the book reads now; the rest of the chain is still going.
          if (!row.book) {
            const fr = await fetch(`/api/books/${row.bookId}`);
            if (fr.ok) row.book = { ...(await fr.json()), known: st.known };
          }
          row.vocab = st.vocab_status;
          row.state = row.book ? 'analyzed' : 'work';
          row.text = `${stepLabel(job.step)}…`;
          paintRow(row);
        } else {
          row.state = 'work';
          row.text = job.step ? `${stepLabel(job.step)}…` : 'queued…';
          paintRow(row);
        }
      } catch (err) { /* transient — try again next tick */ }
    }));
    if (open.size) await new Promise((r) => setTimeout(r, 2000));
  }
}

// Two lines: how the batch reads ("3 added · 2 comfortable now · 1 a stretch · Read Next", the Read
// Next bands), then how it was filed ("3 grouped into 本好きの下剋上 · 1 not in a series"). What
// didn't go (already owned, failed, cancelled) is said on the row that carries it; the banner only
// counts it when nothing was added, since then it is the whole story.
function batchSummary(rows) {
  const added = rows.filter((r) => r.book || r.bookId);
  const parts = [`${added.length} added`];
  const bands = new Map(QUEUE_BANDS.map((b) => [b.id, 0]));
  let other = 0;
  for (const r of added) {
    const k = r.book && r.book.known;
    if (r.book && offProfile(r.book)) other++;
    else if (k && k.hard_pct != null) { const id = queueBand(k.hard_pct).id; bands.set(id, bands.get(id) + 1); }
  }
  let scored = 0;
  for (const band of QUEUE_BANDS) {
    const n = bands.get(band.id);
    if (n) { parts.push(`${n} ${band.title.toLowerCase()}`); scored += n; }
  }
  if (other) parts.push(`${other} in another language`);
  if (scored) parts.push('<a href="/next">Read Next</a>');
  const filed = [];
  const bySeries = new Map();
  let noSeries = 0;
  for (const r of added) {
    const name = r.book && r.book.series_name;
    // A standalone repeats its title as its series name: that is not a group.
    if (name && name !== r.book.title) bySeries.set(name, (bySeries.get(name) || 0) + 1);
    else noSeries++;
  }
  const groups = [...bySeries].map(([n, c]) => `${c} grouped into ${named(n, 'span')}`);
  if (groups.length) filed.push(groups.join(', '));
  if (noSeries) filed.push(`${noSeries} not in a series`);
  if (!added.length) {
    const dups = rows.filter((r) => r.state === 'dup').length;
    const failed = rows.filter((r) => r.state === 'err').length;
    const cancelled = rows.filter((r) => r.state === 'cancelled').length;
    if (cancelled) filed.push(`${cancelled} cancelled`);
    if (dups) filed.push(`${dups} already in your library`);
    if (failed) filed.push(`${failed} failed`);
  }
  return parts.join(' · ') + (filed.length ? `<span class="sum-filed">${filed.join(' · ')}</span>` : '');
}

// The series most of a batch belongs to (the usual case: several volumes bought together), or ''.
function batchSeriesName(rows) {
  const added = rows.filter((r) => r.book);
  const counts = new Map();
  for (const r of added) {
    const name = r.book.series_name;
    if (name) counts.set(name, (counts.get(name) || 0) + 1);
  }
  const [name, n] = [...counts].sort((a, b) => b[1] - a[1])[0] || [];
  return name && n >= 2 && n > added.length / 2 ? name : '';
}

// The finished batch in the order it answers "which of these can I read?": a series bought
// together in volume order (the strip below the summary is in volume order too), anything else by
// Read Next band, gentlest first. Books with no strain to rank follow, then what went nowhere:
// already owned, cancelled, not added. Rows that are still going keep their place.
function batchOrder(rows, series) {
  const rank = (r) => {
    if (r.book && FIN_STATES.includes(r.state)) {
      const h = r.book.known && r.book.known.hard_pct;
      if (series && r.book.series_name === series) return [0, 0, 0];
      return !offProfile(r.book) && h != null ? [1, QUEUE_BANDS.indexOf(queueBand(h)), h] : [2, 0, 0];
    }
    return [{ dup: 3, cancelled: 4, err: 5 }[r.state] ?? 2, 0, 0];
  };
  const vol = byVolume((r) => r.book && r.book.series_order);
  return rows.map((row, i) => ({ row, i, rk: rank(row) })).sort((a, b) =>
    a.rk[0] - b.rk[0] || a.rk[1] - b.rk[1] || a.rk[2] - b.rk[2] ||
    (a.rk[0] === 0 ? vol(a.row, b.row) : 0) || a.i - b.i).map((x) => x.row);
}

// Where to begin a series: the first unread volume in the gentlest band any unread volume is in.
function startWithHtml(vols) {
  const open = vols.filter((b) => !isRead(b) && hardPct(b) != null);
  if (!open.length) return '';
  const best = Math.min(...open.map((b) => QUEUE_BANDS.indexOf(queueBand(hardPct(b)))));
  const pick = open.find((b) => QUEUE_BANDS.indexOf(queueBand(hardPct(b))) === best);
  const which = pick.series_order ? `vol ${esc(String(pick.series_order).replace(/^0+(?=\d)/, ''))}` : esc(pick.title || 'this book');
  return `<p class="strip-start">Start with <a href="/book/${esc(pick.book_id)}">${which}</a> · ${esc(QUEUE_BANDS[best].title)}</p>`;
}

// The strain strip across all of the batch's volumes (and any already in the library), with the
// band thresholds marked and where to start.
async function batchSeriesHtml(rows, name) {
  if (!name) return '';
  const added = rows.filter((r) => r.book);
  let vols = added.filter((r) => r.book.series_name === name).map((r) => r.book);
  try {
    const resp = await fetch(`/api/books?series=${encodeURIComponent(name)}`);
    if (resp.ok) vols = await resp.json();
  } catch (err) { /* the strip covers the books just added */ }
  vols = vols.filter((b) => !offProfile(b)).sort(byVolume((b) => b.series_order));
  if (vols.length < 2 || !vols.some((b) => hardPct(b) != null)) return '';
  return `<div class="readout batch-series"><div class="readout-label"${langAttr(name)}>${esc(name)}</div>` +
    `${strainStrip(vols, { marks: true })}${startWithHtml(vols)}</div>`;
}

// The answer, at the top of the staged card: the summary banner, the series strip when there is
// one, then the rows in reading order. Run when the batch ends and again after a late Retry.
async function settleBatch(rows) {
  const tone = batchTone(rows);
  showStatus(tone, batchSummary(rows), TONE_GLYPH[tone]);
  const series = batchSeriesName(rows);
  for (const row of batchOrder(rows, series)) queueEl.appendChild(row.el);
  seriesSlot.innerHTML = await batchSeriesHtml(rows, series);
}

// Green whenever a book was added (the rows say what else went wrong, beside the files that did);
// red only when nothing was and something failed.
function batchTone(rows) {
  const added = rows.some((r) => r.bookId);
  const failed = rows.some((r) => r.state === 'err' && !r.bookId);
  return failed && !added ? 'err' : 'ok';
}
const TONE_GLYPH = { ok: 'check', warn: 'warning', err: '!' };

// The staged card: a row per file, and the zone put away.
function stage(files) {
  staged = true;
  placeStatus(files.length > 1);
  seriesSlot.innerHTML = '';
  dropZone.hidden = true;
  cardEl.hidden = false;
  cardEl.setAttribute('aria-label', files.length > 1 ? `Adding ${files.length} files` : `Adding ${files[0].name}`);
  queueEl.innerHTML = '';
  actionsEl.innerHTML = '';
  resultEl.innerHTML = '';
  delete resultEl.dataset.bookId;
  delete resultEl.dataset.step;
  statusEl.innerHTML = '';
  const rows = files.map((file) => {
    const el = document.createElement('div');
    queueEl.appendChild(el);
    const row = { file, el, state: 'waiting', text: 'waiting', progress: 0 };
    paintRow(row);
    return row;
  });
  cardEl.focus({ preventScroll: true });
  return rows;
}

function startUploads(files) {
  const rows = stage(files);
  if (rows.length === 1) runSingle(rows[0]);
  else runBatch(rows);
}

// One file: the verdict-first result (afterAdd) once it lands; Retry / Add anyway / cancel on its row.
const runSingle = tracked(async function runSingle(row, force = false) {
  queueEl.onclick = async (e) => {
    const act = e.target.closest('[data-act]');
    if (!act || row.busy) return;
    if (act.dataset.act === 'cancel') { cancelRow(row); return; }
    row.busy = true;
    await runSingle(row, act.dataset.act === 'anyway');
    row.busy = false;
  };
  keepFocus(statusEl);
  actionsEl.innerHTML = '';
  statusEl.innerHTML = '';
  if (cardEl.hidden) { queueEl.appendChild(row.el); cardEl.hidden = false; }  // "Add anyway" from the well: the row is back to show the upload
  const ok = await uploadRow(row, { force, defer: false, single: true });
  if (ok) {
    await afterAdd(row.bookId, row.title || row.file.name);
    return;
  }
  if (row.state === 'dup') {
    // The well says it, with the book you own: the queue row would repeat it, so it steps aside.
    retireQueue();
    await showDuplicate(row.dup, () => runSingle(row, true), { another: true });
    const well = statusEl.firstElementChild;  // focus lands on the one statement, not on <body>
    well.tabIndex = -1;
    well.focus({ preventScroll: true });
    return;
  }
  offerStartOver(row);  // a failure keeps Retry on its row, but the file may be the wrong one
});

const runBatch = tracked(async function runBatch(rows) {
  let released = false;
  // "Retry" / "Add anyway" / cancel on a row, before or after the batch has been released.
  queueEl.onclick = tracked(async (e) => {
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const row = rows.find((r) => r.el.contains(act));
    if (!row || row.busy) return;
    if (act.dataset.act === 'cancel') { cancelRow(row); return; }
    row.busy = true;
    const ok = await uploadRow(row, { force: act.dataset.act === 'anyway', defer: !released });
    if (ok && released) {  // the batch already ran; this one gets its own pass
      await fetch('/api/books/analyze-batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_ids: [row.bookId] }),
      });
      await watchRows([row]);
    }
    await settleBatch(rows);
    row.busy = false;
  });

  showStatus('pending', `Uploading ${rows.length} files…`, 'upload');
  // One at a time: each upload takes the index lock to scan + build.
  for (const row of rows) await uploadRow(row);

  const added = rows.filter((r) => r.bookId);
  if (added.length) {
    showStatus('pending', `${added.length} added · analyzing…`, 'upload');
    await fetch('/api/books/analyze-batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_ids: added.map((r) => r.bookId) }),
    });
    released = true;
    await watchRows(rows);
  } else {
    released = true;
  }
  await settleBatch(rows);
  resultEl.innerHTML = '<div class="result-actions"><button type="button" class="ghost-btn" id="add-another">Add more</button></div>';
  document.getElementById('add-another').addEventListener('click', unstage);
});

// ── URL form submit ───────────────────────────────────────────────────────────

const submitUrl = tracked(async function submitUrl(url, force) {
  const urlInput = document.getElementById('url-input');

  if (staged) {  // the staged card belongs to a file upload; this book gets its own result
    staged = false;
    placeStatus(false);
    seriesSlot.innerHTML = '';
    queueEl.innerHTML = ''; actionsEl.innerHTML = '';
    cardEl.hidden = true; dropZone.hidden = false;
  }
  resultEl.innerHTML = '';
  showStatus('pending', 'Downloading…', 'upload');
  statusEl.scrollIntoView({ block: 'nearest' });

  try {
    const resp = await fetch('/api/acquire', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, language: 'auto', force }),
    });
    const data = await resp.json();
    if (resp.ok && data.status === 'ok') {
      urlInput.value = '';
      document.getElementById('url-card').open = false;  // the result's own primary is the next step
      await afterAdd(data.book_id, data.title || 'book');
    } else if (resp.status === 409 && data.existing) {
      await showDuplicate(data.existing, () => submitUrl(url, true));
    } else {
      showFailure(describeServer(resp.status, data), () => submitUrl(url, force));
    }
  } catch (err) {
    showFailure(describeError(err), () => submitUrl(url, force));
  }
});

document.getElementById('url-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (isBusy()) return;  // the button says why it's disabled; Enter in the field gets here too
  submitUrl(document.getElementById('url-input').value.trim(), false);
});
