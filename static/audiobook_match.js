// "Find on Audible": match a local audiobook to its Audible book by hand.
//
//   AudiobookMatch.open(abId, { onClose(result), onHand() })
//
// The dialog searches Audible (GET /api/audible/search, pre-filled from the file's tags
// or name), or takes a pasted ASIN / Audible link, then POSTs the choice to
// /api/audiobooks/{id}/match. The file becomes that book: its id changes to the ASIN and
// its listening position, alignment and edits go with it. A match made by hand can be
// taken back (DELETE …/match). `onClose(result)` runs once the dialog is gone, with the
// server's reply to a match or a removal (the new id's details, `old_ab_id`,
// `rename_error`) or null if nothing changed. `onHand` is the "Enter the details by hand"
// link, which the page points at the details form.
//
// Class and id names start "abm-", never "ad-": ad blockers hide `.ad-*` elements.
const AudiobookMatch = (() => {
  // Mirrors audible.parse_asin: an ASIN inside a link, or a bare one.
  function parseAsin(text) {
    const m = (text || "").match(/(^|[^A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])/i);
    if (m) return m[2].toUpperCase();
    const bare = (text || "").trim().toUpperCase();
    return /^\d{9}[\dX]$/.test(bare) ? bare : null;
  }

  async function getJson(url) {
    const r = await fetch(url);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  }

  function duration(min) {
    if (!min) return "";
    return min >= 60 ? `${Math.floor(min / 60)}h${min % 60 ? ` ${min % 60}m` : ""}` : `${min}m`;
  }

  function meta(c) {
    return [
      c.author,
      c.narrators ? `read by ${c.narrators}` : "",
      c.series ? `${c.series}${c.order ? ` #${c.order}` : ""}` : "",
      duration(c.runtime_min),
      (c.release_date || "").slice(0, 4),
    ].filter(Boolean).join(" · ");
  }

  function open(abId, { onClose = () => {}, onHand = null } = {}) {
    const enc = encodeURIComponent(abId);
    const dlg = document.createElement("dialog");
    dlg.className = "dlg";
    dlg.setAttribute("aria-labelledby", "abm-title");
    dlg.innerHTML = `<div class="modal-box bd-modal">
        <div class="bd-modal-head">
          <h3 class="modal-title" id="abm-title">Find on Audible</h3>
          <button type="button" class="ghost-btn" data-close>Close</button>
        </div>
        <div class="bd-scroll"><div class="abm-body"><div class="bd-note">Loading…</div></div></div>
        <div class="bd-modal-foot"></div>
      </div>`;
    const body = dlg.querySelector(".abm-body");
    const foot = dlg.querySelector(".bd-modal-foot");
    let result = null;
    let busy = false;

    const finish = () => {
      dlg.close();
      dlg.remove();
      onClose(result);
    };
    const close = () => { if (!busy) finish(); };
    dlg.querySelector("[data-close]").addEventListener("click", close);
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    document.body.appendChild(dlg);
    dlg.showModal();

    const handLink = () => (onHand
      ? `<p class="abm-hand">Not on Audible, or not the right book? <button type="button" class="link-btn" data-hand>Enter the details by hand</button></p>`
      : "");
    const wireHand = () => {
      const btn = body.querySelector("[data-hand]");
      if (btn) btn.addEventListener("click", () => { finish(); onHand(); });
    };

    (async () => {
      let st;
      try {
        const r = await fetch(`/api/audiobooks/${enc}/match`);
        if (!r.ok) throw new Error();
        st = await r.json();
      } catch {
        body.innerHTML = '<div class="bd-note">Couldn\'t load this audiobook.</div>';
        return;
      }
      draw(st);
    })();

    function draw(st) {
      if (!st.matchable) {
        body.innerHTML = `<p class="bd-origin">${esc(st.reason)}</p>${handLink()}`;
        wireHand();
        return;
      }
      const mp = st.marketplace;
      let current = "";
      if (st.mode === "assigned") {
        current = `<p class="bd-origin">Matched by hand to <code>${esc(st.asin)}</code>. Choose another book to
          change it, <button type="button" class="link-btn" data-unmatch>or remove the match</button>.</p>`;
      } else if (st.mode === "tag") {
        current = `<p class="bd-origin">The file's tags say it is <code>${esc(st.asin)}</code>. A match you make
          here wins over the tag.</p>`;
      }
      body.innerHTML = `
        ${current}
        <form class="abm-search" role="search">
          <div class="bd-fld"><div class="bd-lbl"><label for="abm-q-title">TITLE</label></div>
            <input type="text" id="abm-q-title" value="${esc(st.query.title)}"${langAttr(st.query.title)}></div>
          <div class="bd-fld"><div class="bd-lbl"><label for="abm-q-author">AUTHOR</label></div>
            <input type="text" id="abm-q-author" value="${esc(st.query.author)}"${langAttr(st.query.author)}></div>
          <button type="submit" class="btn abm-go">Search</button>
        </form>
        <p class="bd-note abm-where">Searching Audible ${esc(mp.label)}. Your Audible store is set in
          <a href="/settings#metadata">Settings</a>.</p>
        <p class="bd-status abm-status" role="status"></p>
        <ul class="abm-results"></ul>
        <form class="abm-paste">
          <div class="bd-fld"><div class="bd-lbl"><label for="abm-paste">OR PASTE AN ASIN OR AN AUDIBLE LINK</label></div>
            <div class="abm-paste-row">
              <input type="text" id="abm-paste" placeholder="B0CCN9M612" autocomplete="off" spellcheck="false">
              <button type="submit" class="btn">Use it</button>
            </div></div>
        </form>
        ${handLink()}`;
      wireHand();

      const status = body.querySelector(".abm-status");
      const list = body.querySelector(".abm-results");
      const titleIn = body.querySelector("#abm-q-title");
      const authorIn = body.querySelector("#abm-q-author");
      let chosen = null;
      let shown = [];
      let ticket = 0;

      const unmatch = body.querySelector("[data-unmatch]");
      if (unmatch) unmatch.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "Remove the match?",
          message: "The file goes back to what its tags and name say. Your place in the book stays with the file.",
          confirmLabel: "Remove match", cancelLabel: "Keep it",
        });
        if (!ok) return;
        busy = true;
        status.textContent = "Removing…";
        try {
          result = await postJson(`/api/audiobooks/${enc}/match`, undefined, "DELETE");
          busy = false;
          finish();
        } catch (err) {
          busy = false;
          status.textContent = `Not removed: ${err.message}`;
        }
      });

      function drawFoot() {
        if (!chosen) { foot.innerHTML = ""; return; }
        foot.innerHTML = `<div class="abm-confirm">
            <div class="abm-chosen">Make this file <strong${langAttr(chosen.title)}>${esc(chosen.title || chosen.asin)}</strong>
              <code>${esc(chosen.asin)}</code></div>
            ${st.can_rename ? `<label class="abm-rename"><input type="checkbox" id="abm-rename">
              Also add <code>[${esc(chosen.asin)}]</code> to the file name</label>` : ""}
            <div class="bd-actions">
              <button type="button" class="bd-save" data-match>Match</button>
              <span class="bd-status" role="status"></span>
            </div>
          </div>`;
        const save = foot.querySelector("[data-match]");
        const note = foot.querySelector(".bd-status");
        save.addEventListener("click", async () => {
          busy = true;
          save.disabled = true;
          note.textContent = "Matching…";
          try {
            result = await postJson(`/api/audiobooks/${enc}/match`, {
              asin: chosen.asin,
              rename: !!(foot.querySelector("#abm-rename") || {}).checked,
            });
            busy = false;
            finish();
          } catch (err) {
            busy = false;
            save.disabled = false;
            note.textContent = `Not matched: ${err.message}`;
          }
        });
      }

      function choose(asin, title) {
        chosen = { asin, title };
        list.querySelectorAll(".abm-item").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.asin === asin)));
        drawFoot();
      }

      function item(c) {
        const here = c.asin === abId;
        const note = here ? "This file" : c.local ? "Already a file here" : "";
        const cover = c.cover
          ? `<img src="${esc(c.cover)}" alt="" referrerpolicy="no-referrer" loading="lazy">`
          : icon("headphones", "placeholder");
        const text = meta(c);
        return `<li class="abm-row">
          <button type="button" class="abm-item" data-asin="${esc(c.asin)}" aria-pressed="false"${c.local ? " disabled" : ""}>
            <span class="abm-cover">${cover}</span>
            <span class="abm-text">
              <span class="abm-name"${langAttr(c.title)}>${esc(c.title)}</span>
              <span class="abm-meta"${langAttr(text)}>${esc(text)}</span>
              ${note ? `<span class="abm-note">${note}</span>` : ""}
            </span>
          </button>
          <a class="abm-open" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer"
             title="Open on Audible" aria-label="${esc(`Open ${c.title} on Audible`)}">${icon("open_in_new")}</a>
        </li>`;
      }

      async function search() {
        const title = titleIn.value.trim(), author = authorIn.value.trim();
        if (!title && !author) { status.textContent = "Type a title or an author to search for."; return; }
        const mine = ++ticket;
        status.textContent = "Searching…";
        list.innerHTML = "";
        try {
          const data = await getJson(`/api/audible/search?${new URLSearchParams({ title, author })}`);
          if (mine !== ticket) return;
          shown = data.results;
          list.innerHTML = shown.map(item).join("");
          status.textContent = shown.length
            ? `${shown.length} ${shown.length === 1 ? "book" : "books"} on Audible ${data.marketplace.label}. Pick the right one.`
            : "No books found. Try fewer words, only the title, or paste the ASIN.";
          if (chosen) choose(chosen.asin, chosen.title);
        } catch (err) {
          if (mine !== ticket) return;
          status.textContent = `Couldn't search: ${err.message}`;
        }
      }

      body.querySelector(".abm-search").addEventListener("submit", (e) => { e.preventDefault(); search(); });
      list.addEventListener("click", (e) => {
        const btn = e.target.closest(".abm-item");
        if (!btn || btn.disabled) return;
        const c = shown.find((x) => x.asin === btn.dataset.asin);
        if (c) choose(c.asin, c.title);
      });
      body.querySelector(".abm-paste").addEventListener("submit", (e) => {
        e.preventDefault();
        const asin = parseAsin(body.querySelector("#abm-paste").value);
        if (!asin) { status.textContent = "That isn't an Audible ASIN or link."; return; }
        status.textContent = "";
        const known = shown.find((x) => x.asin === asin);
        choose(asin, known ? known.title : null);
      });

      if (st.query.title || st.query.author) search();
      titleIn.focus();
    }
  }

  return { open, parseAsin };
})();
