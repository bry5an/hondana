// EN/JP pairing overview: linked pairs, the books worth a decision, and the restore lists.
// Deciding happens on the book page (edition.js).
"use strict";

const el = (id) => document.getElementById(id);

function thumb(b) {
  return b.cover
    ? `<img class="thumb" loading="lazy" src="${esc(coverUrl(b))}" alt="">`
    : `<span class="thumb mi">menu_book</span>`;
}

function authorLine(b) {
  const rom = b.author_romaji && b.language === "ja" ? ` <span class="rom">(${esc(b.author_romaji)})</span>` : "";
  return `${esc(b.author || "")}${rom}`;
}

function bookEl(b) {
  return `<div class="book">${thumb(b)}<div class="meta">
    <div class="bt"${langAttr(b.title)}>${esc(b.title)}</div>
    <div class="ba"${langAttr(b.author)}>${authorLine(b)}</div>
  </div></div>`;
}

// Both books with the swap arrow between them (decorative: the row's buttons name the pair).
function bothBooks(jp, en) {
  return `<div class="pair-books">${bookEl(jp)}<span class="arrow" aria-hidden="true">${icon("swap_horiz")}</span>${bookEl(en)}</div>`;
}

// The attributes an action button carries: both books' keys (for the request) and
// titles (for its accessible name, the confirm and the toast).
function pairAttrs(en, jp) {
  return `data-en="${esc(en.stable_key)}" data-jp="${esc(jp.stable_key)}"
    data-en-title="${esc(en.title)}" data-jp-title="${esc(jp.title)}"`;
}

let DATA = null;

async function load() {
  const res = await fetch("/api/pairing/suggestions");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  DATA = await res.json();
  render();
  el("loadError").innerHTML = "";
  el("pair-main").removeAttribute("aria-busy");
}

function render() {
  // Only worth a line when it's broken: without pykakasi a Japanese author can't be matched
  // to an English name, so every suggestion is a guess.
  el("note").hidden = DATA.romaji_available;
  el("note").textContent = DATA.romaji_available
    ? ""
    : "Japanese authors can't be matched to English names (pykakasi isn't installed), so suggestions will be weak.";

  // Linked pairs
  el("pairCount").textContent = `(${DATA.pairs.length})`;
  el("pairs").innerHTML = DATA.pairs.length
    ? DATA.pairs.map(pairRow).join("")
    : `<li class="no-cands">No linked pairs yet.</li>`;

  // Unpaired JP books whose author matches an English book's: the ones worth a look. Weak
  // guesses aren't listed; they're in the book's Edit card under Possible matches.
  const decide = DATA.suggestions.filter((s) => s.candidates.some((c) => c.signal === "author"));
  el("unpairedCount").textContent = `(${decide.length})`;
  el("suggestions").innerHTML = decide.length
    ? decide.map(decisionRow).join("")
    : `<li class="no-cands">Nothing needs a decision. No book here has an obvious other edition waiting; a new one will show up in this list.</li>`;

  // Pairs the reader unlinked: each one is blocked until they allow it again.
  const blocked = DATA.blocked || [];
  el("blockedSection").hidden = !blocked.length;
  el("blockedCount").textContent = `(${blocked.length})`;
  el("blocked").innerHTML = blocked.map(blockedRow).join("");

  // Books marked "no other edition": each can be put back in play.
  const none = DATA.no_edition || [];
  el("noneSection").hidden = !none.length;
  el("noneCount").textContent = `(${none.length})`;
  el("none").innerHTML = none.map(noneRow).join("");

  // One polite line for a screen reader: the lists redraw silently after an action.
  el("pairStatus").textContent = `${DATA.pairs.length} linked, ${decide.length} need a decision, ${blocked.length} blocked, ${none.length} with no other edition.`;
}

// What a pair's source means, in the words the reader uses.
const SOURCES = {
  auto: ["Automatic", "Paired by the scanner, from the authors' names."],
  override: ["Manual", "Linked by you."],
};

function pairRow(p) {
  const [label, tip] = SOURCES[p.source] || [p.source, ""];
  const badge = `<span class="chip${p.source === "override" ? " pair" : ""}" title="${esc(tip)}">${esc(label)}</span>`;
  return `<li class="row ed-row">
    ${bothBooks(p.jp, p.en)}
    <div class="pair-actions">${badge}
      ${moreBtn(`data-pair ${pairAttrs(p.en, p.jp)}`, `More for ${p.jp.title} and ${p.en.title}`)}
    </div>
  </li>`;
}

function blockedRow(p) {
  return `<li class="row ed-row">
    ${bothBooks(p.jp, p.en)}
    <div class="pair-actions"><button class="unblock btn" ${pairAttrs(p.en, p.jp)}
      aria-label="Allow ${esc(p.jp.title)} and ${esc(p.en.title)} to be paired again">Allow pairing</button></div>
  </li>`;
}

function decisionRow(s) {
  const c = s.candidates.find((x) => x.signal === "author");
  return `<li class="row ed-row decision-row">
    ${bothBooks(s.jp, c)}
    <div class="pair-actions"><span class="chip" title="The authors' names match.">Same author</span>
      <a class="btn" href="/book/${encodeURIComponent(s.jp.book_id)}"
        aria-label="Compare ${esc(s.jp.title)} with ${esc(c.title)}">Compare…</a></div>
  </li>`;
}

function noneRow(b) {
  return `<li class="row ed-row">
    ${bookEl(b)}
    <div class="pair-actions"><button class="restore btn" data-id="${esc(b.book_id)}" data-title="${esc(b.title)}"
      aria-label="Allow ${esc(b.title)} to be paired again">Look for an edition</button></div>
  </li>`;
}

// ── actions ──────────────────────────────────────────────────────────────────

function failText(verb, e) {
  const tail = e instanceof TypeError ? " Check the connection and try again."
    : /^HTTP 5/.test(e.message) ? " Reload the page to see what was saved." : "";
  return `Couldn't ${verb}: ${failReason(e)}.${tail}`;
}

function loadFailed(e) {
  el("loadError").innerHTML = `<div class="section-error" role="alert">
    <p>${DATA ? "Couldn't refresh the list" : "Couldn't load the pairs"}: ${esc(failReason(e))}.</p>
    <button type="button" class="btn" id="retryLoad">Try again</button>
  </div>`;
  if (!DATA) {
    el("pairs").innerHTML = "";
    el("suggestions").innerHTML = "";
    el("pair-main").removeAttribute("aria-busy");
  }
}

// After a re-render the clicked row is gone; put focus where it was in the list (the next
// row slides into the spot), else on the section heading, so a keyboard or screen-reader
// user isn't thrown back to the top of the page.
function restoreFocus(spot) {
  const list = spot && el(spot.list);
  const row = list && (list.children[spot.index] || list.lastElementChild);
  let target = row && row.querySelector("button");
  if (!target) {
    const section = list && list.closest("section");
    target = (section && section.querySelector("h2")) || document.querySelector("main h2");
    if (target) target.tabIndex = -1;
  }
  if (target) target.focus({ preventScroll: true });
}

let busy = false;

// One write at a time: the server serialises them anyway, and a second click while the
// list is about to be redrawn would act on a row that no longer exists. Resolves true
// when the change landed.
async function act(control, verb, url, body, done, method) {
  if (busy) return false;
  busy = true;
  const row = control.closest(".row");
  const spot = row && { list: row.parentElement.id, index: [...row.parentElement.children].indexOf(row) };
  control.disabled = true;
  try {
    await postJson(url, body, method);
  } catch (e) {
    control.disabled = false;
    busy = false;
    toast(failText(verb, e));
    return false;
  }
  toast(done, "ok");
  try {
    await load();
    restoreFocus(spot);
  } catch (e) {
    loadFailed(e);
  }
  busy = false;
  return true;
}

const keys = (d) => ({ en_key: d.en, jp_key: d.jp });

// Unlinking also blocks the pair, so the scanner can't quietly pair it again.
async function unlinkPair(control, d) {
  const ok = await confirmDialog({
    title: "Unlink these editions?",
    message: `“${d.jpTitle}” and “${d.enTitle}” will be unpaired, and the scanner won't pair them again. To undo it, use Allow pairing under Blocked pairs.`,
    confirmLabel: "Unlink",
    danger: true,
  });
  if (ok) await act(control, "unlink those editions", "/api/pairing/unlink", keys(control.dataset), `Unlinked “${d.jpTitle}” and “${d.enTitle}”. Undo it under Blocked pairs.`);
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("button.row-menu-btn[data-pair], button.unblock, button.restore, #retryLoad");
  if (!btn || btn.disabled) return;
  if (btn.id === "retryLoad") {
    try { await load(); } catch (err) { loadFailed(err); }
    return;
  }
  const d = btn.dataset;
  if (btn.classList.contains("row-menu-btn")) {
    // Unlinking is rare and a settled pair is the good state, so it lives behind the ⋯ (STYLE §7).
    openMenu(btn, [{ label: "Unlink…", danger: true, run: () => unlinkPair(btn, d) }]);
  } else if (btn.classList.contains("restore")) {
    await act(btn, "put that book back", `/api/books/${encodeURIComponent(d.id)}/edition/none`, undefined,
      `“${d.title}” will be matched with an edition again.`, "DELETE");
  } else {
    await act(btn, "allow pairing", "/api/pairing/unblock", keys(d), `“${d.jpTitle}” and “${d.enTitle}” can be paired again.`);
  }
});

load().catch(loadFailed);
