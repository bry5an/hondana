// The "Audio sync queue" card on /admin: the books `cli.py align-remote --missing --run`
// would work through, with the hours of audio each one means. Read-only; a book is synced from
// its own page or from that command. Loaded after admin.js (shared helpers come from ui.js).
"use strict";

const sqList = document.getElementById("sq-list");
const sqCount = document.getElementById("sq-count");
const sqCmd = document.getElementById("sq-cmd");

function sqHours(min) {
  if (!min) return "";
  return min < 90 ? `${Math.round(min)} min` : `${(min / 60).toFixed(1)} h`;
}

function sqRender(q) {
  const n = q.books.length;
  sqCount.textContent = n ? `${n}` : "";
  if (!n) {
    sqCmd.hidden = true;
    sqList.innerHTML = '<div class="prune-empty">Every Japanese book with an audiobook here is synced, or being synced now.</div>';
    return;
  }
  const total = q.total_min ? ` · ${sqHours(q.total_min)} of audio` : "";
  sqCount.textContent = `${n}${total}`;
  // A server with no GPU leaves the transcribing to another machine; one with `transcribe_cmd` (or `subplz_cmd`) can do
  // it from each book's page, and the command still works for the whole queue overnight.
  const cmd = `uv run cli.py align-remote --missing --run --server ${sqServer}`;
  sqCmd.hidden = false;
  sqCmd.innerHTML = `
    <p class="ab-note">${q.can_generate
      ? "Start one from its page (Audio sync › Generate), or run the lot from a machine with a GPU:"
      : "This server has no GPU to transcribe with. On a machine that has one, this works through the whole list, one book after another (add <code>--dry-run</code> to only list it):"}</p>
    <div class="sync-cmd"><code id="sq-cmd-text" tabindex="0">${esc(cmd)}</code>
      <button type="button" class="btn" id="sq-copy">Copy</button></div>`;
  sqList.innerHTML = q.books.map((b) => `
    <a class="prune-row sq-row" href="/book/${esc(b.book_id)}">
      <span class="t"${langAttr(b.title)}>${esc(b.title || "(untitled)")}</span>
      <span class="a"${langAttr(b.author)}>${esc(b.author || "")}</span>
      <span class="sq-h">${esc(sqHours(b.runtime_min))}</span>
    </a>`).join("");
}

let sqServer = location.origin;

async function sqLoad() {
  try {
    const r = await fetch("/api/connect");  // a localhost page gets the LAN address back
    if (r.ok) sqServer = (await r.json()).url;
  } catch { /* the page's own origin */ }
  sqList.innerHTML = '<div class="prune-empty" role="status">Loading…</div>';
  try {
    const r = await fetch("/api/admin/alignment-queue");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    sqRender(await r.json());
  } catch (e) {
    sqCmd.hidden = true;
    sqList.innerHTML = `<div class="status-err prune-failed">Couldn't load the queue: ${esc(e.message)}
      <button class="btn" data-retry="sq">Retry</button></div>`;
  }
}

sqList.addEventListener("click", (e) => { if (e.target.closest("[data-retry]")) sqLoad(); });
sqCmd.addEventListener("click", async (e) => {
  const t = e.target.closest("#sq-copy");
  if (!t) return;
  if (await copyText(document.getElementById("sq-cmd-text").textContent)) {
    t.textContent = "Copied";
    setTimeout(() => { if (t.isConnected) t.textContent = "Copy"; }, 2000);
  } else {
    const range = document.createRange();
    range.selectNodeContents(document.getElementById("sq-cmd-text"));
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    toast("Press Ctrl/⌘-C to copy it.", "ok");
  }
});

sqLoad();
