// Edit-details form, shared by the book page and the /add review card.
//
//   BookDetails.mount(el, bookId, { onSaved(result, changed), actionsEl })
//
// One form covers title, author, language, series + volume and genre, and one
// Save sends every changed field in a single request (POST /api/books/{id}/details
// → one rebuild). A field with a hand-set value gets "Reset to automatic"; the
// reset is staged like any other edit and goes out with the Save. The audiobook
// match has its own control on the book page, so it isn't duplicated here.
// The cover is the exception to "one Save": an image can't ride the JSON
// request, so Replace / Reset act at once through POST/DELETE /api/books/{id}/cover
// and leave any half-edited text fields alone.
//
// `actionsEl` (optional) is where the Save bar is drawn instead of at the foot of the form —
// the book page's dialog passes its fixed footer so Save stays in view. The button joins the
// form through its `form` attribute, so submit, dirty tracking and Enter work the same.
//
// `BookDetails.isDirty(el)` says whether the mounted form holds unsaved edits, so the
// host can ask before it closes (the cover acts at once, so it never counts).
//
// `onSaved(result, changed)` gets the server's new details state plus the list of
// fields that were sent ("cover" for a cover change); result.job is set when a language change queued a
// pipeline run.
const BookDetails = (() => {
  const dirtyFns = new WeakMap();  // mount element → () => has unsaved edits
  const LANGS = [["ja", "日本語"], ["en", "English"], ["other", "Other"]];
  let genresPromise = null;
  const loadGenres = () =>
    (genresPromise ||= fetch("/api/genres").then((r) => r.json()).then((d) => d.genres || []).catch(() => []));

  async function mount(el, bookId, { onSaved = () => {}, actionsEl = null } = {}) {
    el.innerHTML = '<div class="bd-note">Loading…</div>';
    const [resp, genres] = await Promise.all([
      fetch(`/api/books/${encodeURIComponent(bookId)}/details`), loadGenres(),
    ]);
    if (!resp.ok) { el.innerHTML = '<div class="bd-note">Couldn\'t load the details.</div>'; return; }
    draw(el, bookId, await resp.json(), genres, onSaved, actionsEl);
  }

  function draw(el, bookId, st, genres, onSaved, actionsEl = null) {
    const v = st.values, auto = st.auto;
    const isSet = (f) => st.overridden.includes(f);
    const reset = new Set();  // staged "back to automatic"

    const langOpts = LANGS.map(([k, label]) =>
      `<option value="${k}"${k === v.language ? " selected" : ""}>${label}</option>`).join("");
    const genreOpts = ['<option value="">—</option>'].concat(genres.map((g) =>
      `<option value="${esc(g)}"${g === v.genre ? " selected" : ""}>${esc(g)}</option>`)).join("");
    const id = (f) => `bd-${esc(bookId)}-${f}`;  // ties each label to its control
    const resetBtn = (f) => (isSet(f)
      ? `<button type="button" class="bd-reset" data-reset="${f}">Reset to automatic</button>` : "");
    const hint = (f) => (isSet(f) && auto[f] ? `<span class="bd-auto">automatic: ${esc(auto[f])}</span>` : "");

    const actionsHtml = `<div class="bd-actions">
          <button type="submit" class="bd-save" form="bd-form-${esc(bookId)}" disabled>Save changes</button>
          <span class="bd-status" role="status"></span>
        </div>`;

    el.innerHTML = `
      <div class="bd-cover"></div>
      <form class="bd-form" id="bd-form-${esc(bookId)}" autocomplete="off">
        <div class="bd-fld">
          <div class="bd-lbl"><label for="${id("title")}">TITLE</label>${resetBtn("title")}</div>
          <input type="text" id="${id("title")}" data-f="title" value="${esc(v.title)}">${hint("title")}
        </div>
        <div class="bd-row">
          <div class="bd-fld">
            <div class="bd-lbl"><label for="${id("author")}">AUTHOR</label>${resetBtn("author")}</div>
            <input type="text" id="${id("author")}" data-f="author" value="${esc(v.author)}" placeholder="Unknown">${hint("author")}
          </div>
          <div class="bd-fld">
            <div class="bd-lbl"><label for="${id("language")}">LANGUAGE</label>${resetBtn("language")}</div>
            <select id="${id("language")}" data-f="language">${langOpts}</select>${hint("language")}
          </div>
          <div class="bd-fld">
            <div class="bd-lbl"><label for="${id("genre")}">GENRE</label>${resetBtn("genre")}</div>
            <select id="${id("genre")}" data-f="genre">${genreOpts}</select>${hint("genre")}
          </div>
        </div>
        <div class="bd-row">
          <div class="bd-fld">
            <div class="bd-lbl"><label for="${id("series")}">SERIES</label>${resetBtn("series")}</div>
            <input type="text" id="${id("series")}" data-f="series_name" list="bd-series-${esc(bookId)}"
                   value="${esc(v.series_name)}" placeholder="Standalone">
            <datalist id="bd-series-${esc(bookId)}">${st.series_names.map((n) => `<option value="${esc(n)}">`).join("")}</datalist>
          </div>
          <div class="bd-fld bd-narrow">
            <div class="bd-lbl"><label for="${id("vol")}">VOL</label></div>
            <input type="text" id="${id("vol")}" data-f="series_order" value="${esc(v.series_order)}" placeholder="—">
          </div>
        </div>
        ${st.suggestion
          ? `<div class="bd-suggest">Looks like ${st.suggestion.series_order ? `vol ${esc(st.suggestion.series_order)} of ` : "part of "}
               <b>${esc(st.suggestion.series_name)}</b> —
               <button type="button" class="bd-reset" id="bd-use-suggestion">use it</button></div>`
          : ""}
        ${actionsEl ? "" : actionsHtml}
      </form>`;
    if (actionsEl) actionsEl.innerHTML = actionsHtml;

    drawCover(el.querySelector(".bd-cover"), bookId, st.cover, onSaved);

    const form = el.querySelector("form");
    const bar = actionsEl || form;
    const save = bar.querySelector(".bd-save");
    const status = bar.querySelector(".bd-status");
    const input = (f) => form.querySelector(`[data-f="${f}"]`);
    const seriesInputs = [input("series_name"), input("series_order")];

    // What the form would send: only what differs from what the page showed.
    const pending = () => {
      const body = {};
      for (const f of ["title", "author", "language", "genre"]) {
        if (reset.has(f)) body[f] = null;
        else if (input(f).value.trim() !== (v[f] || "")) body[f] = input(f).value.trim();
      }
      if (reset.has("series")) body.series = null;
      else if (seriesInputs[0].value.trim() !== (v.series_name || "") ||
               seriesInputs[1].value.trim() !== (v.series_order || "")) {
        body.series = { name: seriesInputs[0].value.trim(), order: seriesInputs[1].value.trim() };
      }
      return body;
    };
    dirtyFns.set(el, () => Object.keys(pending()).length > 0);
    const refresh = () => { save.disabled = !Object.keys(pending()).length; status.textContent = ""; };

    form.addEventListener("input", (e) => {
      const f = e.target.dataset.f;
      reset.delete(f === "series_name" || f === "series_order" ? "series" : f);
      if (f === "series_name" || f === "series_order") seriesInputs.forEach((i) => i.classList.remove("bd-staged"));
      else input(f).classList.remove("bd-staged");
      refresh();
    });

    form.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-reset]");
      if (btn) {
        const f = btn.dataset.reset;
        reset.add(f);
        if (f === "series") {
          // The automatic series isn't known client-side, so show the field as handed back.
          seriesInputs.forEach((i) => { i.value = ""; i.placeholder = "automatic"; i.classList.add("bd-staged"); });
        } else {
          input(f).value = auto[f] || "";
          input(f).classList.add("bd-staged");
        }
        btn.remove();
        refresh();
      } else if (e.target.id === "bd-use-suggestion") {
        seriesInputs[0].value = st.suggestion.series_name;
        seriesInputs[1].value = st.suggestion.series_order || "";
        e.target.closest(".bd-suggest").remove();
        refresh();
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = pending();
      if (!Object.keys(body).length) return;
      save.disabled = true;
      status.textContent = body.language ? "Saving — re-analyzing in the new language…" : "Saving…";
      try {
        const r = await fetch(`/api/books/${encodeURIComponent(bookId)}/details`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `save failed (${r.status})`);
        draw(el, bookId, data, genres, onSaved, actionsEl);
        (actionsEl || el).querySelector(".bd-status").textContent = "Saved";
        onSaved(data, Object.keys(body));
      } catch (err) {
        status.textContent = `Not saved: ${err.message}`;
        save.disabled = false;
      }
    });
  }

  // The cover row: current image + Replace / Reset. Redrawn on its own after a
  // change so unsaved edits in the form below survive it.
  function drawCover(box, bookId, cover, onSaved) {
    const url = (c) => coverUrl({ book_id: bookId, cover_v: c.v });
    box.innerHTML = `
      <div class="bd-cover-thumb">${cover.has
        ? `<img src="${esc(url(cover))}" alt="Current cover">` : icon("menu_book", "placeholder")}</div>
      <div class="bd-cover-side">
        <div class="bd-lbl"><span class="bd-cap">COVER</span>${cover.overridden
          ? '<button type="button" class="bd-reset" data-cover="reset">Reset to EPUB cover</button>' : ""}</div>
        <div class="bd-cover-actions">
          <button type="button" class="btn bd-cover-pick">${cover.has ? "Replace cover…" : "Add a cover…"}</button>
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
        drawCover(box, bookId, data.cover, onSaved);
        box.querySelector(".bd-status").textContent = "Saved";
        onSaved(data, ["cover"]);
      } catch (err) {
        buttons.forEach((b) => { b.disabled = false; });
        status.textContent = `Not saved: ${err.message}`;
      }
    }

    const endpoint = `/api/books/${encodeURIComponent(bookId)}/cover`;
    box.querySelector(".bd-cover-pick").addEventListener("click", () => file.click());
    file.addEventListener("change", () => {
      if (!file.files.length) return;
      const body = new FormData();
      body.append("file", file.files[0]);
      send(fetch(endpoint, { method: "POST", body }));
    });
    const reset = box.querySelector("[data-cover=reset]");
    if (reset) reset.addEventListener("click", async () => {
      // Deleting the upload can't be undone, so name what goes before it goes.
      const ok = await confirmDialog({
        title: "Reset to the EPUB's cover?",
        message: "The cover you uploaded will be deleted and the EPUB's own cover shown again.",
        warning: "You'd have to upload it again to get it back.",
        confirmLabel: "Delete upload", cancelLabel: "Keep it", danger: true,
      });
      if (ok) send(fetch(endpoint, { method: "DELETE" }));
    });
  }

  const isDirty = (el) => { const f = dirtyFns.get(el); return !!f && f(); };

  return { mount, isDirty };
})();
