// Keep a page's filter/sort/search controls in the URL query, so a filtered
// view survives opening a book and coming back, and can be bookmarked or shared.
//
//   const VS = ViewState.create({ path: "/", fields: [
//     { param: "q",    id: "q" },
//     { param: "sort", id: "sort", def: "title" },
//     { param: "paired", id: "pairedOnly" },          // checkbox → "1"
//   ] });
//   VS.apply();   // after the controls' options exist: URL → controls (absent = default)
//   VS.write();   // after any control changed: controls → URL (defaults omitted)
//
// The query is also remembered per `path` in localStorage; nav.js uses that so the
// sidebar's plain "/" and "/audiobooks" links come back to the last view. The URL
// itself is authoritative: an explicit query (an author link's /?q=Name) wins.
"use strict";

window.ViewState = (function () {
  const PREFIX = "viewstate:";

  function create({ path, fields }) {
    const node = (f) => document.getElementById(f.id);
    const isCheck = (n) => n.type === "checkbox";

    // Pills (.seg[data-for]) mirror a hidden <select>; keep their highlight in step.
    function syncPills(id, value) {
      document.querySelectorAll(`.seg[data-for="${id}"] .seg-btn`).forEach((b) => {
        b.classList.toggle("active", b.dataset.val === value);
      });
    }

    function apply() {
      const sp = new URLSearchParams(location.search);
      for (const f of fields) {
        const n = node(f);
        if (!n) continue;
        const raw = sp.get(f.param);
        if (isCheck(n)) {
          n.checked = raw === "1";
          continue;
        }
        n.value = raw ?? f.def ?? "";
        // A stale/unknown option (a genre that no longer exists) falls back.
        if (n.tagName === "SELECT" && n.value !== (raw ?? f.def ?? "")) n.value = f.def ?? "";
        syncPills(f.id, n.value);
      }
    }

    function write() {
      const sp = new URLSearchParams();
      for (const f of fields) {
        const n = node(f);
        if (!n) continue;
        if (isCheck(n)) {
          if (n.checked) sp.set(f.param, "1");
        } else {
          const v = n.value.trim();
          if (v && v !== (f.def ?? "")) sp.set(f.param, v);
        }
      }
      const qs = sp.toString();
      history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : ""));
      try {
        if (qs) localStorage.setItem(PREFIX + path, qs);
        else localStorage.removeItem(PREFIX + path);
      } catch { /* storage blocked: the URL still carries the state */ }
    }

    return { apply, write };
  }

  // The remembered query for `path` ("" if none or storage is unavailable).
  function saved(path) {
    try { return localStorage.getItem(PREFIX + path) || ""; } catch { return ""; }
  }

  return { create, saved };
})();
