// Click-to-mark-known, shared by the book page's study list and the frequency
// list on /known.
//
// A deck export records what the reader chose to drill, not what they can
// read, so the everyday words it omits sit at the top of every study list
// until they are clicked off. This is the one-word-at-a-time path into the
// known list.
//
// The write is not free — the server re-normalizes the word list and recomputes
// every book (~1.2 s) — so nothing waits for it: the row and the sidebar's
// Known-words count respond on the click (the count via a `known-words-delta`
// event, nav.js), and the batch is flushed in the background. The short delay
// below only coalesces a run of clicks into one server pass; it is not an undo
// window. Undo is the row itself: a marked row stays put and the same button
// un-marks it. Marking is optimistic but not credulous: a failed batch puts the
// rows and the count back rather than leaving the page claiming something the
// server never stored, and the server's own total settles any difference.
"use strict";

const MARK_DEBOUNCE_MS = 250;

// Every marker with a click not yet saved (or a save in flight). While any is, a
// finished save keeps quiet about the server's total: it would be older than the
// clicks still on their way, and the sidebar count would dip and recover.
const busyMarkers = new Set();

// Two toggles share this machinery: "I know this word" and "not a word" (a name or
// a tokenizer fragment, set aside so it stops topping the list). Each has its own
// button, row class and endpoints, and a marker instance per kind keeps its own state.
const MARK_KINDS = {
  known: { btn: ".mark-btn", cls: "marked", add: "/api/known-words/mark",
           remove: "/api/known-words/unmark", noun: "marked known" },
  skip: { btn: ".skip-btn", cls: "skipped", add: "/api/known-words/skip",
          remove: "/api/known-words/unskip", noun: "set aside as not a word" },
};

function createMarker({ container, status, onSaved, detail, kind = "known" }) {
  const K = MARK_KINDS[kind];
  // What the server has stored for this session, and what the rows currently
  // show. The difference between them is the batch still owed to the server;
  // toggling a word twice before a flush cancels out and sends nothing.
  const confirmed = new Set();
  const desired = new Set();
  // A review-list row can stand for several spellings (当たる / あたる); the tally counts the
  // rows ticked, not the spellings behind them. unitOf maps a spelling to the first
  // spelling of the row that introduced it.
  const unitOf = new Map();
  const count = () => new Set([...desired].map((w) => unitOf.get(w) ?? w)).size;
  let timer = null;
  let flushing = false;
  let failed = false;
  let failedWords = [];   // the words in the batch that didn't save, named in the error
  const self = {};
  const dirty = () =>
    desired.size !== confirmed.size || [...desired].some((w) => !confirmed.has(w));
  const track = () => busyMarkers[timer !== null || flushing || dirty() ? "add" : "delete"](self);
  // Announce "3 words marked known · saving…" and failures without moving focus.
  if (status) status.setAttribute("role", "status");

  function render() {
    if (!status) return;
    if (failed) {
      status.className = "mark-status err";
      // Spellings of one word fail together, so name up to two before counting.
      const named = failedWords.slice(0, 2).map((w) => `“${w}”`).join(", ");
      const more = failedWords.length - 2;
      status.textContent = failedWords.length
        ? `Couldn't save ${named}${more > 0 ? ` and ${more} more` : ""} — click again to retry`
        : "Couldn't save — click a word to retry";
      return;
    }
    const n = count();
    if (!n && !confirmed.size) {
      status.className = "mark-status";
      status.textContent = "";
      return;
    }
    const busy = flushing || timer !== null;
    status.className = "mark-status" + (busy ? " pending" : " ok");
    status.textContent =
      `${n} ${n === 1 ? "word" : "words"} ${K.noun}` + (busy ? " · saving…" : detail && detail() ? ` · ${detail()}` : "");
  }

  function sync() {
    container.querySelectorAll("[data-word]").forEach((row) => {
      const marked = desired.has(row.dataset.word);
      row.classList.toggle(K.cls, marked);
      const btn = row.querySelector(K.btn);
      if (btn) btn.setAttribute("aria-pressed", String(marked));
    });
    render();
  }

  async function post(url, words) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ words }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  // The count shown beside the page moves by the words toggled, now.
  function announce(delta) {
    if (kind === "known" && delta) {
      window.dispatchEvent(new CustomEvent("known-words-delta", { detail: { delta } }));
    }
  }

  async function flush() {
    timer = null;
    // A click landed mid-flush; let the current batch finish and pick it up.
    if (flushing) return schedule();

    const add = [...desired].filter((w) => !confirmed.has(w));
    const remove = [...confirmed].filter((w) => !desired.has(w));
    if (!add.length && !remove.length) { track(); return render(); }

    flushing = true;
    failed = false;
    render();
    try {
      let last = null;
      if (add.length) last = await post(K.add, add);
      if (remove.length) last = await post(K.remove, remove);
      add.forEach((w) => confirmed.add(w));
      remove.forEach((w) => confirmed.delete(w));
      flushing = false;
      track();
      if (last && typeof last.total === "number" && !busyMarkers.size) {
        window.dispatchEvent(new CustomEvent("known-words-changed", { detail: { count: last.total } }));
      }
      if (onSaved) onSaved(last);
    } catch (err) {
      // Take back the count the clicks showed (known kind only: it is the one with a delta).
      if (kind === "known" && desired.size !== confirmed.size) announce(confirmed.size - desired.size);
      desired.clear();
      confirmed.forEach((w) => desired.add(w));
      failedWords = [...add, ...remove];
      failed = true;
    } finally {
      flushing = false;
      track();
      sync();
    }
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, MARK_DEBOUNCE_MS);
    track();
    render();
  }

  // `word` may be a list: a review-list row that stands for several
  // spellings (あたる / 当たる) marks or unmarks all of them together, keyed on
  // the first. `setMany` takes a list of such rows and syncs once.
  function apply(word, on) {
    const words = (Array.isArray(word) ? word : [word]).filter(Boolean);
    let moved = 0;
    words.forEach((w) => {
      unitOf.set(w, words[0]);
      if (on === desired.has(w)) return;
      on ? desired.add(w) : desired.delete(w);
      moved += on ? 1 : -1;
    });
    return moved;
  }

  function setMany(rows, on) {
    const moved = rows.reduce((n, r) => n + apply(r, on), 0);
    failed = false;
    announce(moved);
    sync();
    schedule();
  }

  const set = (word, on) => setMany([word], on);

  function toggle(word) {
    const words = (Array.isArray(word) ? word : [word]).filter(Boolean);
    if (words.length) set(words, !desired.has(words[0]));
  }

  container.addEventListener("click", (e) => {
    const btn = e.target.closest(K.btn);
    if (!btn) return;
    const row = btn.closest("[data-word]");
    if (row) toggle(row.dataset.words ? row.dataset.words.split("\t") : row.dataset.word);
  });

  return { toggle, set, setMany, sync, flush, count };
}

// A row's mark button. The row itself carries data-word; the marker reads it
// back on click, so the word never has to survive a round trip through markup.
// `word` goes into its accessible name, so a list of sixty isn't sixty identical buttons;
// aria-pressed (kept in step by sync()) says whether it is marked.
function markButton(word = "") {
  const label = word ? `I know this word: ${word}` : "I know this word";
  return `<button class="mark-btn" type="button" aria-pressed="false" aria-label="${esc(label)}" ` +
    `title="I know this word — add it to my known list">${icon("check")}</button>`;
}

// The "not a word" button: names (ティシャ) and fragments (ばあ, cut from おばあちゃん) that
// no one can learn. Same contract as markButton.
function skipButton(word = "") {
  const label = word ? `Not a word: ${word}` : "Not a word";
  return `<button class="skip-btn" type="button" aria-pressed="false" aria-label="${esc(label)}" ` +
    `title="Not a word — a name or fragment. Counts as known, and leaves this list">${icon("block")}</button>`;
}
