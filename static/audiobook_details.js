// Edit-details form for an audiobook, the counterpart of book_details.js.
//
//   AudiobookDetails.open(abId, { onClose(changed, state) })    — in a native <dialog>
//   AudiobookDetails.mount(el, abId, { onSaved, actionsEl })     — anywhere else
//
// One form covers title, author, narrators, series + volume, genre, release date
// and description; one Save sends every changed field (POST
// /api/audiobooks/{id}/details → one rebuild). A field with a hand-set value gets
// "Reset" back to what the catalog, the tags or the file name said; the reset
// is staged like any other edit. The cover acts at once (POST/DELETE …/cover),
// like the book form's. Edits are kept in audiobook_metadata_overrides.json and
// survive rescans and re-fetches.
//
// `onSaved(state, changed)` (mount) gets the server's new state and the fields
// sent ("cover" for a cover change).
//
// Class and id names start "abd-", never "ad-": ad blockers hide `.ad-*` elements.
const AudiobookDetails = (() => {
  const dirtyFns = new WeakMap();
  // [api field, label, kind]; a row's fields sit side by side.
  const ROWS = [
    [["title", "TITLE"]],
    [["author", "AUTHOR"], ["narrators", "NARRATOR"]],
    [["series", "SERIES"], ["order", "VOL", "narrow"]],
    [["genre", "GENRE", "select"], ["release_date", "RELEASED"]],
    [["description", "DESCRIPTION", "textarea"]],
  ];
  const FIELDS = ROWS.flat().map(([f]) => f);
  const ORIGIN = {
    filename: "These details are a guess from the file name. Correct them and the book can be linked to its EPUB.",
    tags: "These details come from the file's own tags.",
    catalog: "These details come from the audiobook catalog.",
    manual: "These details were typed in by hand.",
  };
  let genresPromise = null;
  const loadGenres = () =>
    (genresPromise ||= fetch("/api/genres").then((r) => r.json()).then((d) => d.genres || []).catch(() => []));

  async function mount(el, abId, { onSaved = () => {}, actionsEl = null } = {}) {
    el.innerHTML = '<div class="bd-note">Loading…</div>';
    const [resp, genres] = await Promise.all([
      fetch(`/api/audiobooks/${encodeURIComponent(abId)}/details`), loadGenres(),
    ]);
    if (!resp.ok) { el.innerHTML = '<div class="bd-note">Couldn\'t load the details.</div>'; return; }
    draw(el, abId, await resp.json(), genres, onSaved, actionsEl);
  }

  function draw(el, abId, st, genres, onSaved, actionsEl) {
    const v = st.values, auto = st.auto;
    const isSet = (f) => st.overridden.includes(f);
    const reset = new Set();
    const key = esc(abId).replace(/\s/g, "_");
    const id = (f) => `abd-${key}-${f}`;
    const resetBtn = (f) => (isSet(f) ? `<button type="button" class="bd-reset" data-reset="${f}">Reset</button>` : "");
    const hint = (f) => (isSet(f) && auto[f] && f !== "description"
      ? `<span class="bd-auto">originally: ${esc(auto[f])}</span>` : "");

    const control = ([f, , kind]) => {
      if (kind === "select") {
        const opts = ['<option value="">—</option>'].concat(genres.map((g) =>
          `<option value="${esc(g)}"${g === v[f] ? " selected" : ""}>${esc(g)}</option>`));
        if (v[f] && !genres.includes(v[f])) opts.push(`<option value="${esc(v[f])}" selected>${esc(v[f])}</option>`);
        return `<select id="${id(f)}" data-f="${f}">${opts.join("")}</select>`;
      }
      if (kind === "textarea") return `<textarea id="${id(f)}" data-f="${f}" rows="4"${langAttr(v[f])}>${esc(v[f])}</textarea>`;
      const extra = f === "series" ? ` list="abd-series-${key}"` : "";
      const ph = { order: "—", release_date: "YYYY-MM-DD", author: "Unknown", narrators: "Unknown" }[f] || "";
      return `<input type="text" id="${id(f)}" data-f="${f}" value="${esc(v[f])}"${extra}${ph ? ` placeholder="${ph}"` : ""}${langAttr(v[f])}>`;
    };
    const fld = (spec) => `<div class="bd-fld${spec[2] === "narrow" ? " bd-narrow" : ""}">
        <div class="bd-lbl"><label for="${id(spec[0])}">${spec[1]}</label>${resetBtn(spec[0])}</div>
        ${control(spec)}${hint(spec[0])}</div>`;

    const actionsHtml = `<div class="bd-actions">
        <button type="submit" class="bd-save" form="abd-form-${key}" disabled>Save changes</button>
        <span class="bd-status" role="status"></span>
      </div>`;
    const where = st.path ? ` File: <code>${esc(st.path)}</code>` : "";
    el.innerHTML = `
      <p class="bd-origin">${esc(ORIGIN[st.source] || "")}${where}</p>
      <div class="bd-cover"></div>
      <form class="bd-form" id="abd-form-${key}" autocomplete="off">
        ${ROWS.map((row) => (row.length > 1 ? `<div class="bd-row">${row.map(fld).join("")}</div>` : fld(row[0]))).join("")}
        <datalist id="abd-series-${key}">${st.series_names.map((n) => `<option value="${esc(n)}">`).join("")}</datalist>
        ${actionsEl ? "" : actionsHtml}
      </form>`;
    if (actionsEl) actionsEl.innerHTML = actionsHtml;
    drawCover(el.querySelector(".bd-cover"), abId, st.cover, onSaved);

    const form = el.querySelector("form");
    const bar = actionsEl || form;
    const save = bar.querySelector(".bd-save");
    const status = bar.querySelector(".bd-status");
    const input = (f) => form.querySelector(`[data-f="${f}"]`);

    const pending = () => {
      const body = {};
      for (const f of FIELDS) {
        if (reset.has(f)) body[f] = null;
        else if (input(f).value.trim() !== (v[f] || "")) body[f] = input(f).value.trim();
      }
      return body;
    };
    dirtyFns.set(el, () => Object.keys(pending()).length > 0);
    const refresh = () => { save.disabled = !Object.keys(pending()).length; status.textContent = ""; };

    form.addEventListener("input", (e) => {
      const f = e.target.dataset.f;
      reset.delete(f);
      if (f) input(f).classList.remove("bd-staged");
      refresh();
    });
    form.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-reset]");
      if (!btn) return;
      const f = btn.dataset.reset;
      reset.add(f);
      input(f).value = auto[f] || "";
      input(f).classList.add("bd-staged");
      btn.remove();
      refresh();
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = pending();
      if (!Object.keys(body).length) return;
      save.disabled = true;
      status.textContent = "Saving…";
      try {
        const data = await postJson(`/api/audiobooks/${encodeURIComponent(abId)}/details`, body);
        draw(el, abId, data, genres, onSaved, actionsEl);
        (actionsEl || el).querySelector(".bd-status").textContent = "Saved";
        onSaved(data, Object.keys(body));
      } catch (err) {
        status.textContent = `Not saved: ${err.message}`;
        save.disabled = false;
      }
    });
  }

  // Current image + Replace / Reset; acts at once, so unsaved text edits survive it.
  function drawCover(box, abId, cover, onSaved) {
    box.innerHTML = `
      <div class="bd-cover-thumb">${cover.url
        ? `<img src="${esc(cover.url)}" alt="Current cover" referrerpolicy="no-referrer">` : icon("headphones", "placeholder")}</div>
      <div class="bd-cover-side">
        <div class="bd-lbl"><span class="bd-cap">COVER</span>${cover.overridden
          ? '<button type="button" class="bd-reset" data-cover="reset">Reset to the original cover</button>' : ""}</div>
        <div class="bd-cover-actions">
          <button type="button" class="btn bd-cover-pick">${cover.url ? "Replace cover…" : "Add a cover…"}</button>
          <input type="file" class="bd-cover-file" aria-label="Cover image file" accept="image/jpeg,image/png,image/webp,image/gif" hidden>
          <span class="bd-status" role="status"></span>
        </div>
        <span class="bd-auto">${cover.overridden ? "Hand-set — kept when the library is rescanned."
          : "JPEG, PNG, WebP or GIF, up to 10 MB."}</span>
      </div>`;
    const status = box.querySelector(".bd-status");
    const file = box.querySelector(".bd-cover-file");
    const buttons = box.querySelectorAll("button");

    async function send(request) {
      buttons.forEach((b) => { b.disabled = true; });
      status.textContent = "Saving…";
      try {
        const r = await request;
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `failed (${r.status})`);
        drawCover(box, abId, data.cover, onSaved);
        box.querySelector(".bd-status").textContent = "Saved";
        onSaved(data, ["cover"]);
      } catch (err) {
        buttons.forEach((b) => { b.disabled = false; });
        status.textContent = `Not saved: ${err.message}`;
      }
    }

    const endpoint = `/api/audiobooks/${encodeURIComponent(abId)}/cover`;
    box.querySelector(".bd-cover-pick").addEventListener("click", () => file.click());
    file.addEventListener("change", () => {
      if (!file.files.length) return;
      const body = new FormData();
      body.append("file", file.files[0]);
      send(fetch(endpoint, { method: "POST", body }));
    });
    const resetBtn = box.querySelector("[data-cover=reset]");
    if (resetBtn) resetBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Reset to the original cover?",
        message: "The cover you uploaded will be deleted and the original one shown again.",
        warning: "You'd have to upload it again to get it back.",
        confirmLabel: "Delete upload", cancelLabel: "Keep it", danger: true,
      });
      if (ok) send(fetch(endpoint, { method: "DELETE" }));
    });
  }

  // The form in a dialog, as the book page does it: Escape, the backdrop and
  // Close dismiss it (asking first if edits are unsaved), and the dialog hands
  // focus back to whatever opened it. `onClose(changed, state)` runs after it
  // closes: every field saved while it was open, and the last saved state.
  function open(abId, { title = "Edit details", onClose = () => {} } = {}) {
    const dlg = document.createElement("dialog");
    dlg.className = "dlg";
    dlg.setAttribute("aria-labelledby", "abd-title");
    dlg.innerHTML = `<div class="modal-box bd-modal">
        <div class="bd-modal-head">
          <h3 class="modal-title" id="abd-title">${esc(title)}</h3>
          <button type="button" class="ghost-btn" data-close>Close</button>
        </div>
        <div class="bd-scroll"><div class="abd-body"></div></div>
        <div class="bd-modal-foot"></div>
      </div>`;
    const body = dlg.querySelector(".abd-body");
    const changed = new Set();
    let latest = null;
    let closing = false;
    const close = async () => {
      if (closing) return;
      closing = true;
      if (isDirty(body) && !(await confirmDialog({
        title: "Discard changes?",
        message: "You have edits to this audiobook's details that haven't been saved.",
        confirmLabel: "Discard", cancelLabel: "Keep editing", danger: true,
      }))) { closing = false; return; }
      dlg.close();
      dlg.remove();
      onClose([...changed], latest);
    };
    dlg.querySelector("[data-close]").addEventListener("click", close);
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    document.body.appendChild(dlg);
    dlg.showModal();
    mount(body, abId, {
      actionsEl: dlg.querySelector(".bd-modal-foot"),
      onSaved: (state, fields) => { latest = state; fields.forEach((f) => changed.add(f)); },
    });
  }

  const isDirty = (el) => { const f = dirtyFns.get(el); return !!f && f(); };

  return { mount, open, isDirty };
})();
