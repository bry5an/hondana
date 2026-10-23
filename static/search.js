// Shared server-side search: hits /api/search (a substring scan over the JSON
// indices, including descriptions on `catalog`) and returns the set of
// matching primary-key ids, which each page uses to filter the data it
// already loaded. Callers only need to handle a hard network error.
"use strict";

window.LibrarySearch = (function () {
  async function fetchIds(kind, q, limit = 5000) {
    const url = `/api/search?kind=${encodeURIComponent(kind)}` +
      `&limit=${limit}&q=${encodeURIComponent(q)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`search HTTP ${res.status}`);
    const data = await res.json();
    const ids = new Set((data.ids || []).filter((x) => x != null));
    return { source: data.source, ids };
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  return { fetchIds, debounce };
})();
