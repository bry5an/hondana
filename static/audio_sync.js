// The "Audio sync" card on /book/{id}: whether Hondana for iPhone can follow the audiobook
// in this book, and how to make it so. Loaded before book.js, which calls mountAudioSync(). The
// map is made by epublib/align*.py; this only reads GET /api/books/{id}/status (`alignment`) and
// starts, cancels or removes a run.
"use strict";

const SYNC_DOCS = "https://github.com/bry5an/hondana/blob/main/docs/audio-sync.md";
const SYNC_POLL_MS = 3000;

// Why the server refused to start a run, in words (the 409 `reason`s of …/alignment/generate).
const SYNC_REFUSALS = {
  subplz_not_configured: "This server can't run the sync itself.",
  no_audiobook: "The book's audiobook isn't on this server.",
  not_ja: "Only Japanese books can be synced.",
  drm: "This book is DRM-protected, so there is no text to sync.",
  no_text: "This book has no text to sync (image-only).",
};

let syncState = null;
// The address another machine reaches this server at: /api/connect swaps a localhost page for the
// LAN address; the page's own origin is the fallback.
let syncServer = location.origin;
let syncTimer = null;

function syncUrl(suffix = "") {
  return `/api/books/${encodeURIComponent(bookId)}${suffix}`;
}

// The command for a machine with a GPU. `--server` names this page's own address, which is the one
// that machine can reach (cli.py would otherwise look for a server over Bonjour).
function syncCommand(st) {
  const force = st.state === "ready" ? " --force" : "";
  return `uv run cli.py align-remote ${bookId} --server ${syncServer}${force}`;
}

function syncPct(st) {
  return st.coverage == null ? "" : `${Math.round(st.coverage * 100)}%`;
}

// What to do when the server can't run SubPlz itself: the command, a copy button, the docs.
function syncCommandHtml(st) {
  return `
    <p class="sync-how">This server has no GPU to transcribe with. On a Mac or PC that has one and is set up for it, run:</p>
    <div class="sync-cmd">
      <code id="sync-cmd-text" tabindex="0">${esc(syncCommand(st))}</code>
      <button type="button" class="ab-btn" id="sync-copy">Copy</button>
    </div>
    <p class="ab-note">It downloads the audiobook, transcribes it there and sends the result back; this page then shows it as synced.
      <a href="${SYNC_DOCS}" target="_blank" rel="noopener">How to set that up</a>.</p>`;
}

function syncActionHtml(st, label) {
  return st.can_generate
    ? `<div class="ab-actions"><button type="button" class="ab-btn" id="sync-generate">${esc(label)}</button></div>
       <p class="ab-note">Runs on this server and can take hours for a long audiobook; you can leave this page.</p>`
    : syncCommandHtml(st);
}

// What the map is worth beyond its coverage: gaps the narrator talks through, a map made
// without a transcript (it can drift), and fixes listeners made in the app.
function syncQualityHtml(st) {
  const notes = [];
  if (st.speech_gaps) {
    const min = Math.round((st.speech_gap_s || 0) / 6) / 10;
    notes.push(`${st.speech_gaps} ${st.speech_gaps === 1 ? "stretch" : "stretches"} (${min} min) where the narrator speaks but the sync has nothing`);
  }
  if (st.source === "synced") notes.push("made without a transcript, so it can drift; redoing it from one fixes that");
  if (st.anchors) notes.push(`${st.anchors} ${st.anchors === 1 ? "fix" : "fixes"} from the app`);
  return notes.length ? `<p class="ab-note">${notes.map(esc).join(" · ")}</p>` : "";
}

function syncBodyHtml(st) {
  const err = st.error
    ? `<p class="sync-err" role="alert">The last run failed: ${esc(st.error)}</p>`
    : "";
  if (st.state === "queued" || st.state === "running") {
    const what = st.state === "queued" ? "Waiting its turn…" : `Working… ${esc(st.step || "")}`;
    return `<p class="sync-line">${what}</p>
      <div class="ab-actions"><button type="button" class="ab-btn" id="sync-cancel">Cancel</button></div>`;
  }
  const menu = moreBtn(`id="sync-more"`, "More actions for the audio sync");
  if (st.state === "ready") {
    const line = st.ok
      ? `<span class="sync-ok">${icon("check")}</span> Synced with the audiobook · ${esc(syncPct(st))}`
      : `<span class="sync-warn">${icon("warning")}</span> Only ${esc(syncPct(st))} matched; the app won't highlight`;
    const redo = st.source === "synced" || st.speech_gaps > 0;
    return `<div class="sync-head"><p class="sync-line">${line}</p>${menu}</div>${syncQualityHtml(st)}${err}
      ${st.ok && !st.error && !redo ? "" : syncActionHtml(st, !st.ok ? "Sync again" : redo ? "Redo from a transcript" : "Try again")}`;
  }
  if (st.state === "stale") {
    return `<div class="sync-head"><p class="sync-line"><span class="sync-warn">${icon("warning")}</span>
        Out of date: the book or its audiobook changed since this was made, so the app won't highlight</p>${menu}</div>${err}
      ${syncActionHtml(st, "Sync again")}`;
  }
  return `<p class="sync-line">Not synced. Hondana for iPhone can't follow the audiobook or jump to a sentence in this book.</p>${err}
    ${syncActionHtml(st, err ? "Try again" : "Generate")}`;
}

function paintSync(st) {
  const slot = document.getElementById("sync-slot");
  if (!slot) return;
  // The hero's "Synced audio" row follows the same rule as the server's audio_synced.
  if (typeof renderSyncRow === "function") renderSyncRow(st.state === "ready" && st.ok === true);
  const html = syncBodyHtml(st);
  const body = slot.querySelector(".sync-body");
  if (!body) return;  // not mounted yet: the first paint is mountAudioSync's
  // A poll that changed nothing leaves the card (and focus inside it) alone.
  if (body.dataset.sig === html) return;
  const had = body.contains(document.activeElement) ? document.activeElement.id : null;
  body.dataset.sig = html;
  body.innerHTML = html;
  if (had) document.getElementById(had)?.focus();
}

async function pollSync() {
  clearTimeout(syncTimer);
  try {
    const r = await fetch(syncUrl("/status"));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    syncState = (await r.json()).alignment;
  } catch {
    if (syncState) syncTimer = setTimeout(pollSync, SYNC_POLL_MS);  // keep what we showed; try again
    return;
  }
  if (!syncState || !syncState.eligible) return;
  paintSync(syncState);
  if (syncState.state === "queued" || syncState.state === "running") {
    syncTimer = setTimeout(pollSync, SYNC_POLL_MS);
  } else if (slotWasRunning) {
    // A run just ended: say how it went, once.
    slotWasRunning = false;
    if (syncState.error) toast("The audio sync failed.", "error");
    else if (syncState.state === "ready") toast(syncState.ok ? "Audio sync is ready." : "Audio sync finished, but too little matched.", syncState.ok ? "ok" : "error");
  }
  if (syncState.state === "queued" || syncState.state === "running") slotWasRunning = true;
}
let slotWasRunning = false;

async function syncGenerate(btn) {
  btn.disabled = true;
  try {
    const r = await fetch(syncUrl("/alignment/generate"), { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(SYNC_REFUSALS[data.reason] || data.detail || `HTTP ${r.status}`);
    syncState = data.alignment;
    slotWasRunning = true;
    paintSync(syncState);
    syncTimer = setTimeout(pollSync, SYNC_POLL_MS);
  } catch (e) {
    btn.disabled = false;
    toast(`Couldn't start the sync: ${e.message}`);
  }
}

async function syncCancel(btn) {
  btn.disabled = true;
  try {
    const r = await fetch(syncUrl("/alignment/generate"), { method: "DELETE" });
    if (!r.ok && r.status !== 404) throw new Error(`HTTP ${r.status}`);
    slotWasRunning = false;
    // The worker stops its process in the background; the next poll reads where that landed.
    await pollSync();
  } catch (e) {
    btn.disabled = false;
    toast(`Couldn't cancel: ${failReason(e)}.`);
  }
}

async function syncRemove() {
  const ok = await confirmDialog({
    title: "Remove the audio sync?",
    message: "The app will stop highlighting the narrated sentence in this book. You can make it again later.",
    confirmLabel: "Remove",
    danger: true,
  });
  if (!ok) return;
  try {
    await postJson(syncUrl("/alignment"), undefined, "DELETE");
    toast("Audio sync removed.", "ok");
    await pollSync();
    document.getElementById("sync-card-title")?.focus();
  } catch (e) {
    toast(`Couldn't remove it: ${failReason(e)}.`);
  }
}

// Fills #sync-slot (a hidden placeholder book.js leaves under the vocabulary card) for a Japanese
// book whose audiobook is on this server; any other book never shows the card.
async function mountAudioSync() {
  const slot = document.getElementById("sync-slot");
  if (!slot) return;
  try {
    const r = await fetch("/api/connect");
    if (r.ok) syncServer = (await r.json()).url;
  } catch { /* the page's own origin */ }
  await pollSync();
  if (!syncState || !syncState.eligible) return;
  slot.innerHTML = `<h2 id="sync-card-title" tabindex="-1">Audio sync</h2><div class="sync-body" aria-live="polite"></div>`;
  slot.hidden = false;
  paintSync(syncState);
  slot.addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.id === "sync-generate") syncGenerate(t);
    else if (t.id === "sync-cancel") syncCancel(t);
    else if (t.id === "sync-more") {
      openMenu(t, [{ label: "Remove audio sync…", danger: true, run: syncRemove }]);
    } else if (t.id === "sync-copy") {
      const ok = await copyText(syncCommand(syncState));
      if (ok) {
        t.textContent = "Copied";
        setTimeout(() => { if (t.isConnected) t.textContent = "Copy"; }, 2000);
      } else {
        // Nothing to copy with: select the command so Ctrl/⌘-C does it.
        const code = document.getElementById("sync-cmd-text");
        const range = document.createRange();
        range.selectNodeContents(code);
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        toast("Press Ctrl/⌘-C to copy it.", "ok");
      }
    }
  });
}
