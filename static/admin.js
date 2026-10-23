// Admin panel: fire a pipeline stage, then poll /api/admin/status until done.
"use strict";

const groupsEl = document.getElementById("stage-groups");
const runAllEl = document.getElementById("run-all");
const statusEl = document.getElementById("run-status");
const announceEl = document.getElementById("run-announce");
let buttons = [];
let polling = null;
let ticker = null;
let runView = null;   // {key, elapsed, at} while a run is on screen
let clockSkew = 0;    // server clock minus ours, from /api/admin/status `now`
const stageBtn = {};  // stage id -> its run button, so "Run again" goes through the same confirm
const lastRun = {};   // stage id -> when it last finished (epoch seconds), from the run logs

// Sections of the stage list, in the order they run. The server decides which
// section a stage belongs to (pipeline.Stage.group).
const GROUPS = [
  ["index", "Index your files"],
  ["audiobooks", "Audiobooks"],
  ["finish", "Finish"],
  ["maintenance", "Maintenance"],
];

// Only the two stages with a consequence worth stopping for get a confirm.
const CONFIRMS = {
  all: (chain) => ({
    title: "Run the full pipeline?",
    message:
      `Runs every step in order (${chain}). It can take several minutes, and ` +
      "while it runs nothing else can save: uploads, pairing and edits wait until it finishes.",
    confirmLabel: "Run full pipeline",
  }),
  "enrich-audiobooks": () => ({
    title: "Look up audiobook levels?",
    message:
      "Fetches Natively levels for audiobooks that don't have one yet. " +
      "It makes one request per title, so a large catalog takes a while.",
    confirmLabel: "Run",
  }),
};

function fmtDuration(start, end) {
  if (!start || !end) return "";
  const s = Math.max(0, Math.round(end - start));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function fmtClock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const serverNow = () => Date.now() / 1000 + clockSkew;

// "2 hours ago" for a past epoch time; a date once it is a few weeks old.
function fmtAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.round(serverNow() - ts));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${plural(m, "minute", "minutes")} ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${plural(h, "hour", "hours")} ago`;
  const d = Math.round(h / 24);
  if (d < 35) return `${plural(d, "day", "days")} ago`;
  return "on " + new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// The age as a <time>, whose tooltip carries the exact moment.
function agoHtml(ts) {
  const exact = new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  return `<time datetime="${new Date(ts * 1000).toISOString()}" title="${esc(exact)}">${esc(fmtAgo(ts))}</time>`;
}

function paintLastRuns() {
  document.querySelectorAll(".stage-last").forEach((el) => {
    const ts = lastRun[el.dataset.stage];
    el.innerHTML = ts ? `Last run ${agoHtml(ts)}` : "Not run yet";
  });
}

// Before anything has run since the server started, say how recently the
// pipeline last did anything, so the page opens on state and not only verbs.
function renderLastRun(stages) {
  const latest = stages.filter((s) => lastRun[s.id]).sort((a, b) => lastRun[b.id] - lastRun[a.id])[0];
  statusEl.innerHTML = latest
    ? `<p class="run-last">Last run: <strong>${esc(latest.label)}</strong>, ${agoHtml(lastRun[latest.id])}.</p>`
    : "";
}

function setButtonsDisabled(disabled) {
  buttons.forEach((b) => { b.disabled = disabled; });
}

// Eleven buttons dim together while a stage runs, so the one in flight says so itself:
// a "Running" chip in place of its "Last run" line, and its button stays at full strength.
function markRunning(id) {
  document.querySelectorAll(".stage-running").forEach((el) => { el.hidden = el.dataset.stage !== id; });
  document.querySelectorAll(".stage-last").forEach((el) => { el.hidden = el.dataset.stage === id; });
  for (const [sid, b] of Object.entries(stageBtn)) b.classList.toggle("is-running", sid === id);
}

function announce(text) {
  announceEl.textContent = text;
}

function stopTicker() {
  if (ticker) { clearInterval(ticker); ticker = null; }
  runView = null;
}

function paintElapsed() {
  const el = document.getElementById("run-elapsed");
  if (!el || !runView) return;
  el.textContent = fmtClock(runView.elapsed + (Date.now() - runView.at) / 1000);
}

function renderRunning(st) {
  setButtonsDisabled(true);
  markRunning(st.stage);
  const key = `${st.stage}@${st.started_at}`;
  if (!runView || runView.key !== key) {
    // Built once per run; later polls only touch the clock and the log tail, so a
    // screen reader isn't re-reading a rebuilt panel every two seconds.
    statusEl.innerHTML =
      `<div class="status-pending"><span class="spinner"></span>Running <strong>${esc(st.label)}</strong> · ` +
      `<span id="run-elapsed">0:00</span></div>` +
      `<pre class="run-tail" id="run-tail" aria-hidden="true"></pre>`;
    announce(`Running ${st.label}`);
  }
  runView = {
    key,
    elapsed: st.now && st.started_at ? st.now - st.started_at : 0,
    at: Date.now(),
  };
  if (!ticker) ticker = setInterval(paintElapsed, 1000);
  paintElapsed();
  const tail = document.getElementById("run-tail");
  if (tail) {
    tail.textContent = st.tail || "";
    tail.hidden = !st.tail;
  }
}

function reportBlock(st) {
  if (!st.output) return "";
  const file = st.log_file ? ` <span class="report-file">(${esc(st.log_file)})</span>` : "";
  return (
    `<details class="run-report"${st.ok ? "" : " open"}>` +
    `<summary>Report${file}</summary>` +
    `<pre>${esc(st.output)}</pre>` +
    `</details>`
  );
}

// ── what a finished run changed ──────────────────────────────────────────────
//
// Read off the closing lines each stage prints (the report is the only record
// of a run), so it is best-effort: a stage whose wording changes just falls
// back to "finished in Ns". Counts that nothing changed say so, because "did
// anything happen?" is the question after a run.

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

const SUMMARIES = {
  scan(o) {
    const m = o.match(/Scan complete: (\d+) books \((\d+) added, (\d+) updated, (\d+) removed\)/);
    if (!m) return null;
    const [added, updated, removed] = [+m[2], +m[3], +m[4]];
    const parts = [];
    if (added) parts.push(`${added} new`);
    if (updated) parts.push(`${updated} updated`);
    if (removed) parts.push(`${removed} removed`);
    return parts.length ? parts.join(", ") : `no changes (${m[1]} books)`;
  },
  analyze(o) {
    const m = o.match(/Analyze complete: (\d+) books \((\d+) analyzed, (\d+) removed\)/);
    if (!m) return null;
    return +m[2] ? `${plural(+m[2], "book", "books")} analyzed` : "nothing new to analyze";
  },
  enrich(o) {
    const m = o.match(/Enrichment complete: (\d+)\/(\d+) matched/);
    if (m) return +m[2] ? `${m[1]} of ${plural(+m[2], "lookup", "lookups")} matched on Natively` : null;
    return /Natively enrichment: \d+ JP books, 0 to fetch/.test(o) ? "nothing to look up" : null;
  },
  pair(o) {
    const m = o.match(/Pairing complete: (\d+) pairs/);
    return m ? `${plural(+m[1], "pair", "pairs")} in total` : null;
  },
  "audio-scan"(o) {
    const m = o.match(/Audiobook index built: (\d+) entries \((\d+) local/);
    return m ? `${m[1]} audiobooks indexed, ${m[2]} downloaded` : null;
  },
  catalog(o) {
    if (/Audiobook catalog: nothing missing/.test(o)) return "no new audiobooks to look up";
    const m = o.match(/Audiobook catalog: (\d+) added/);
    return m ? `${m[1]} added to the catalog` : null;
  },
  xref(o) {
    const m = o.match(/Audiobook xref complete: (\d+) EPUBs linked/);
    return m ? `${plural(+m[1], "EPUB", "EPUBs")} linked to audiobooks` : null;
  },
  build(o) {
    const m = o.match(/Build complete: (\d+) books[^(]*\((\d+) in series\)/);
    return m ? `${m[1]} books indexed, ${m[2]} in series` : null;
  },
  "epub-catalog"(o) {
    if (/no GOOGLE_BOOKS_API_KEY/.test(o)) return "skipped: no Google Books key set";
    if (/EPUB catalog: nothing missing/.test(o)) return "nothing missing";
    const m = o.match(/EPUB catalog: (\d+) found/);
    return m ? `${m[1]} found on Google Books` : null;
  },
};
SUMMARIES["enrich-unmatched"] = SUMMARIES.enrich;
SUMMARIES["enrich-audiobooks"] = (o) => {
  const m = o.match(/Enrichment complete: (\d+)\/(\d+) matched/);
  return m && +m[2] ? `${m[1]} of ${plural(+m[2], "level", "levels")} matched` : null;
};

// "all" reports the changes that matter to a reader, not eleven counts.
function summarize(st) {
  const o = st.output || "";
  if (st.stage !== "all") return (SUMMARIES[st.stage] || (() => null))(o);
  return ["scan", "pair", "catalog"].map((id) => SUMMARIES[id](o)).filter(Boolean).join(" · ") || null;
}

const LINKS = {
  scan: [["/", "Library"], ["/next", "Read Next"]],
  analyze: [["/next", "Read Next"], ["/", "Library"]],
  enrich: [["/", "Library"]],
  "enrich-unmatched": [["/", "Library"]],
  "enrich-audiobooks": [["/audiobooks", "Audiobooks"]],
  pair: [["/pair", "Editions"]],
  "audio-scan": [["/audiobooks", "Audiobooks"]],
  catalog: [["/audiobooks", "Audiobooks"]],
  xref: [["/audiobooks", "Audiobooks"]],
  build: [["/", "Library"]],
  "epub-catalog": [["/", "Library"]],
  all: [["/", "Library"], ["/pair", "Editions"], ["/audiobooks", "Audiobooks"]],
};

// Pairing runs leave books waiting on a human; say how many, since that is the
// next thing to do (the queue on /pair).
async function pendingDecisions(stage) {
  if (stage !== "pair" && stage !== "all") return 0;
  try {
    const d = await (await fetch("/api/pairing/suggestions")).json();
    return (d.suggestions || []).filter((s) => s.candidates.some((c) => c.signal === "author")).length;
  } catch (e) {
    return 0;
  }
}

async function renderDone(st, quiet = false) {
  stopTicker();
  markRunning(null);
  setButtonsDisabled(false);
  // A result restored on reload may be days old; one that just finished is "now".
  if (st.finished_at) {
    lastRun[st.stage] = st.finished_at;
    paintLastRuns();
  }
  const when = quiet && st.finished_at ? ` ${agoHtml(st.finished_at)}` : "";
  if (st.ok) {
    const dur = fmtDuration(st.started_at, st.finished_at);
    const changed = summarize(st);
    const waiting = await pendingDecisions(st.stage);
    const bits = [changed, waiting ? `${plural(waiting, "book needs", "books need")} a decision` : null]
      .filter(Boolean).join(" · ");
    const links = (LINKS[st.stage] || LINKS.all)
      .map(([href, label]) => `<a href="${href}">${esc(label)} ${icon("arrow_forward")}</a>`).join(" · ");
    statusEl.innerHTML =
      `<div class="status-ok">${icon("check")} <strong>${esc(st.label)}</strong> finished${when}${dur ? ` in ${dur}` : ""}` +
      `${bits ? `: ${esc(bits)}.` : "."} ${links}</div>` +
      reportBlock(st);
    if (!quiet) announce(`${st.label} finished${dur ? ` in ${dur}` : ""}${bits ? `: ${bits}` : ""}`);
  } else {
    const msg = esc(st.error || "Unknown error");
    statusEl.innerHTML =
      `<div class="status-err">${icon("close")} <strong>${esc(st.label)}</strong> failed${when}:\n${msg}` +
      nextStepHtml(st.stage, failureHint(st), !!st.output) + `</div>` +
      reportBlock(st);
    if (!quiet) announce(`${st.label} failed`);
  }
}

// Where the cause of a failure is known, say what to do about it. The error text is all
// there is to go on (a stage fails with an exception message), so this is best-effort too.
const FAILURE_HINTS = [
  [/epub_dir does not exist|not mounted|audiobook_dirs is empty/i,
    "The library folder isn't reachable. Check the drive is mounted and the config paths are right, then run it again."],
  [/ConnectionError|Max retries|Timeout|timed out|Name or service not known|URLError|HTTP 5\d\d/i,
    "Couldn't reach the lookup service. Check the connection, then run it again."],
  [/Install requests first/i, "A Python dependency is missing on the server. Run uv sync there, then run it again."],
];

function failureHint(st) {
  const text = `${st.error || ""}\n${st.output || ""}`;
  const hit = FAILURE_HINTS.find(([re]) => re.test(text));
  return hit ? hit[1] : "";
}

// The failure's way out: one line on what to do (the report's own details when nothing
// more specific is known) and a Run again button.
function nextStepHtml(stage, hint, hasReport) {
  const line = hint || (hasReport ? "The report below has the details." : "");
  return (
    `<div class="run-next">` +
    (line ? `<span>${esc(line)}</span>` : "") +
    (stageBtn[stage] ? `<button type="button" class="btn run-again" data-stage="${esc(stage)}">Run again</button>` : "") +
    `</div>`
  );
}

statusEl.addEventListener("click", (e) => {
  const again = e.target.closest(".run-again");
  if (again && stageBtn[again.dataset.stage]) stageBtn[again.dataset.stage].click();
});

function stopPolling() {
  if (polling) { clearInterval(polling); polling = null; }
}

async function poll() {
  let st;
  try {
    st = await (await fetch("/api/admin/status")).json();
  } catch (e) {
    return; // transient — try again next tick
  }
  if (st.now) clockSkew = st.now - Date.now() / 1000;
  if (st.running) {
    renderRunning(st);
  } else {
    stopPolling();
    if (st.stage) await renderDone(st);
    else setButtonsDisabled(false);
  }
}

function startPolling() {
  if (polling) return;
  poll();
  polling = setInterval(poll, 2000);
}

async function run(stage, label) {
  setButtonsDisabled(true);
  markRunning(stage);
  stopTicker();
  statusEl.innerHTML =
    `<div class="status-pending"><span class="spinner"></span>Starting <strong>${esc(label)}</strong>…</div>`;
  statusEl.scrollIntoView({ block: "nearest" });
  try {
    const resp = await fetch(`/api/admin/run/${stage}`, { method: "POST" });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      statusEl.innerHTML = `<div class="status-err">${esc(data.detail || `HTTP ${resp.status}`)}</div>`;
      markRunning(null);
      setButtonsDisabled(false);
      return;
    }
    startPolling();
  } catch (e) {
    statusEl.innerHTML =
      `<div class="status-err">Request failed: ${esc(e.message)}` +
      nextStepHtml(stage, "The server didn't answer. Check it is up, then try again.", false) + `</div>`;
    markRunning(null);
    setButtonsDisabled(false);
  }
}

// A stage's description is long (two to four lines, eleven times over), so the list
// shows its first sentence and tucks the rest behind "More".
function splitHint(text) {
  const m = text.match(/^.*?(?<!e\.g|i\.e)[.!?](?=\s)/);
  return m ? [m[0], text.slice(m[0].length).trim()] : [text, ""];
}
const more = (text) => splitHint(text)[1];

function hintHtml(id, text) {
  const [first, rest] = splitHint(text);
  if (!rest) return `<p class="hint" id="hint-${id}">${esc(first)}</p>`;
  return (
    `<p class="hint" id="hint-${id}">${esc(first)} ` +
    `<button type="button" class="more-link" aria-expanded="false" aria-controls="hint-${id}-more">More</button></p>` +
    `<p class="hint" id="hint-${id}-more" hidden>${esc(rest)}</p>`
  );
}

groupsEl.addEventListener("click", (e) => {
  const btn = e.target.closest(".more-link");
  if (!btn) return;
  const open = btn.getAttribute("aria-expanded") !== "true";
  btn.setAttribute("aria-expanded", String(open));
  btn.textContent = open ? "Less" : "More";
  document.getElementById(btn.getAttribute("aria-controls")).hidden = !open;
});

function stageRow(s, n) {
  const li = document.createElement("li");
  li.className = "stage";
  const hintId = `hint-${s.id}`;
  const chips = [
    ...(s.in_chain ? [] : [["Not part of Run full pipeline", ""]]),
    ...s.chips.map((c) => [c === "Writes a repo file" ? c : `Uses ${c}`, c === "Writes a repo file" ? "warn" : ""]),
  ];
  li.innerHTML =
    `<span class="stage-n" aria-hidden="true">${n || ""}</span>` +
    `<div class="stage-body">` +
    `<div class="stage-head">` +
    `<button class="stage-btn" aria-describedby="${hintId}${more(s.description) ? ` ${hintId}-more` : ""}">${esc(s.label)}</button>` +
    `<span class="stage-last" data-stage="${esc(s.id)}"></span>` +
    `<span class="stage-running" data-stage="${esc(s.id)}" hidden><span class="spinner"></span>Running</span>` +
    `</div>` +
    hintHtml(s.id, s.description) +
    (chips.length
      ? `<div class="stage-chips">${chips.map(([t, tone]) => `<span class="stage-chip ${tone}">${esc(t)}</span>`).join("")}</div>`
      : "") +
    `</div>`;
  return [li, li.querySelector("button")];
}

function bindStage(btn, s, chain) {
  stageBtn[s.id] = btn;
  btn.addEventListener("click", async () => {
    const ask = CONFIRMS[s.id];
    if (ask && !(await confirmDialog(ask(chain)))) return;
    run(s.id, s.label);
  });
  return btn;
}

async function init() {
  let stages;
  try {
    stages = await (await fetch("/api/admin/stages")).json();
  } catch (e) {
    statusEl.innerHTML = `<div class="status-err">Failed to load stages: ${esc(e.message)}</div>`;
    return;
  }

  for (const s of stages) if (s.last_run) lastRun[s.id] = s.last_run;
  const chainStages = stages.filter((s) => s.in_chain && s.id !== "all");
  const chainIds = chainStages.map((s) => s.id);
  const chain = chainStages.map((s) => s.label).join(" → ");
  buttons = [];

  // The whole chain sits apart from the stages: it is the slowest action and
  // blocks writes everywhere, so it shouldn't read as the default one.
  const all = stages.find((s) => s.id === "all");
  if (all) {
    runAllEl.innerHTML =
      `<div class="run-all-row">` +
      `<button class="btn run-all-btn" aria-describedby="hint-all">${icon("play_arrow")} ${esc(all.label)}</button>` +
      `<span class="stage-running" data-stage="all" hidden><span class="spinner"></span>Running</span></div>` +
      `<p class="hint" id="hint-all">Runs the ${chainStages.length} numbered stages below, in order.</p>`;
    buttons.push(bindStage(runAllEl.querySelector("button"), all, chain));
  }

  groupsEl.innerHTML = "";
  for (const [id, title] of GROUPS) {
    // The stages that run in "all" first, in run order; standalone ones after.
    const inGroup = stages
      .filter((s) => s.group === id)
      .sort((a, b) => Number(b.in_chain) - Number(a.in_chain));
    if (!inGroup.length) continue;
    // Rarely-needed actions stay out of the way: Maintenance is a closed disclosure whose
    // summary still holds a real heading.
    const folded = id === "maintenance";
    const section = document.createElement(folded ? "details" : "section");
    section.className = "stage-group";
    const h = document.createElement("h3");
    h.textContent = title;
    const ul = document.createElement("ul");
    ul.className = "stage-list";
    for (const s of inGroup) {
      const n = s.in_chain ? chainIds.indexOf(s.id) + 1 : 0;
      const [li, btn] = stageRow(s, n);
      buttons.push(bindStage(btn, s, chain));
      ul.appendChild(li);
    }
    if (folded) {
      const sum = document.createElement("summary");
      sum.append(h);
      sum.insertAdjacentHTML("beforeend", `<span class="mi chev" aria-hidden="true">expand_more</span>`);
      section.append(sum, ul);
    } else {
      section.append(h, ul);
    }
    groupsEl.appendChild(section);
  }

  // Resume if a stage is already running (e.g. the page was reloaded mid-run).
  try {
    const st = await (await fetch("/api/admin/status")).json();
    if (st.now) clockSkew = st.now - Date.now() / 1000;
    paintLastRuns();
    if (st.running) { renderRunning(st); startPolling(); }
    else if (st.stage) { await renderDone(st, true); }
    else renderLastRun(stages);
  } catch (e) { /* ignore */ }
}

init();

// ── prune audiobooks ─────────────────────────────────────────────────────────
//
// The catalog carries every book the account ever owned. Pruning is a
// suppression list, not a delete: no file is touched and every removal is
// reversible from the second card. Batched deliberately — each hide/unhide
// call triggers a full audiobook rebuild chain, so ticking twenty boxes and
// removing once costs one rebuild instead of twenty.

const pruneList  = document.getElementById("prune-list");
const pruneQ     = document.getElementById("prune-q");
const pruneLocal = document.getElementById("prune-local");
const pruneCount = document.getElementById("prune-count");
const pruneAll   = document.getElementById("prune-all");
const pruneSel   = document.getElementById("prune-sel");
const pruneBtn   = document.getElementById("prune-btn");
const pruneDock  = document.getElementById("prune-dock");
const pruneDockN = document.getElementById("prune-dock-n");
const pruneDockBtn = document.getElementById("prune-dock-btn");
const pruneMore  = document.getElementById("prune-more");
const hiddenList = document.getElementById("hidden-list");
const hiddenCount = document.getElementById("hidden-count");

const PAGE_ROWS = 100;          // rows drawn at a time; "Show more" appends a page

let ALL = [];                   // the catalog rows still listed
let HIDDEN = [];                // the pruned ones
// "loading" until the first answer, then "ready"; "failed" only while there is
// no earlier list to keep showing.
const loadState = { all: "loading", hidden: "loading" };
const selected = new Set();     // global on purpose: it survives a retyped filter
const rowCache = new Map();     // full rows of titles pruned this visit, for a cheap restore
let limit = PAGE_ROWS;
let lastIdx = null;             // last-ticked row, the anchor of a shift-click range
let shownIds = [];              // ids of the rows currently drawn, in order

const titleOf = (e) => e.title || "(untitled)";
const byAuthor = (a, b) =>
  readingCmp(a.author_reading || a.author, b.author_reading || b.author) ||
  readingCmp(a.series_reading || a.series, b.series_reading || b.series) ||
  orderCmp(a.order, b.order) ||
  (a.title || "").localeCompare(b.title || "");

function candidates() {
  const q = pruneQ.value.trim().toLowerCase();
  return ALL.filter((e) => {
    if (!pruneLocal.checked && e.local) return false;
    if (!q) return true;
    return [e.title, e.author, e.series].some(
      (f) => f && f.toLowerCase().includes(q));
  });
}

async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function failedWell(what, retryKey, msg) {
  return `<div class="status-err prune-failed">Couldn't load ${what}: ${esc(msg)}
    <button class="btn" data-retry="${retryKey}">Retry</button></div>`;
}

// The toolbar: one count line, kept in step by every change to the list, the
// filter or the selection (it used to be written in two places that disagreed).
function paintBar(matching, drawn) {
  const n = selected.size;
  pruneCount.textContent = loadState.all !== "ready" && !ALL.length ? ""
    : matching === 0 ? "Nothing matches"
    : drawn < matching ? `Showing ${drawn} of ${matching} matching`
    : `${matching} matching`;
  pruneAll.hidden = matching === 0;
  // Once it has been used, the button says what it did instead of offering it again.
  const everyMatch = matching > 0 && candidates().every((e) => selected.has(e.ab_id));
  pruneAll.textContent = everyMatch ? `All ${matching.toLocaleString()} selected`
    : `Select all ${matching.toLocaleString()} matching`;
  pruneSel.hidden = n === 0;
  pruneSel.innerHTML = `Selected (${n}) ${icon("close")}`;
  pruneSel.setAttribute("aria-label", `Clear selection, ${n} selected`);
  pruneBtn.disabled = n === 0;
  pruneDock.hidden = n === 0;
  pruneDockN.textContent = `${n} selected`;
  pruneDockBtn.disabled = n === 0;
}

function renderPrune() {
  if (loadState.all === "loading" && !ALL.length) {
    shownIds = [];
    pruneList.innerHTML = '<div class="prune-empty" role="status">Loading catalog…</div>';
    paintBar(0, 0);
    pruneMore.hidden = true;
    return;
  }
  if (loadState.all === "failed" && !ALL.length) {
    shownIds = [];
    pruneList.innerHTML = failedWell("the catalog", "catalog", loadState.allError);
    paintBar(0, 0);
    pruneMore.hidden = true;
    return;
  }
  const rows = candidates();
  // Cap the DOM: 2,900 rows of checkboxes janks the page and nobody scrolls
  // that far — the search box is the way through a list this size.
  const shown = rows.slice(0, limit);
  shownIds = shown.map((e) => e.ab_id);
  paintBar(rows.length, shown.length);
  pruneMore.hidden = shown.length >= rows.length;
  pruneMore.textContent = `Show ${Math.min(PAGE_ROWS, rows.length - shown.length)} more`;

  pruneList.innerHTML = shown.length
    ? shown.map((e) => `
      <label class="prune-row">
        <input type="checkbox" data-ab-id="${esc(e.ab_id)}"${selected.has(e.ab_id) ? " checked" : ""}>
        <span class="t"${langAttr(e.title)}>${esc(titleOf(e))}</span>
        ${e.local ? '<span class="lc">local</span>' : ""}
        <span class="a"${langAttr(e.author)}>${esc(e.author || "")}</span>
      </label>`).join("")
    : '<div class="prune-empty">Nothing matches.</div>';
}

function renderHidden() {
  hiddenCount.textContent = HIDDEN.length ? `${HIDDEN.length}` : "";
  if (loadState.hidden === "loading" && !HIDDEN.length) {
    hiddenList.innerHTML = '<div class="prune-empty" role="status">Loading…</div>';
  } else if (loadState.hidden === "failed" && !HIDDEN.length) {
    hiddenList.innerHTML = failedWell("the pruned list", "hidden", loadState.hiddenError);
  } else {
    hiddenList.innerHTML = HIDDEN.length
      ? HIDDEN.map((e) => `
        <div class="prune-row" data-row="${esc(e.ab_id)}">
          <span class="t"${langAttr(e.title)}>${esc(titleOf(e))}</span>
          <span class="a"${langAttr(e.author)}>${esc(e.author || "")}</span>
          <button class="btn restore" data-ab-id="${esc(e.ab_id)}"
                  aria-label="Restore ${esc(titleOf(e))}">Restore</button>
        </div>`).join("")
      : '<div class="prune-empty">Nothing pruned yet.</div>';
  }
}

// Keep the reader's place in a list that is redrawn under them.
function keepScroll(el, draw) {
  const top = el.scrollTop;
  draw();
  el.scrollTop = top;
}

async function loadCatalog() {
  if (!ALL.length) { loadState.all = "loading"; renderPrune(); }
  try {
    ALL = (await getJson("/api/audiobooks")).sort(byAuthor);
    loadState.all = "ready";
  } catch (e) {
    if (ALL.length) {
      // Keep what is on screen; say the refresh didn't happen.
      toast(`Couldn't refresh the catalog: ${e.message}`, "error",
        { action: { label: "Retry", run: loadCatalog }, anchor: pruneBtn });
    } else {
      loadState.all = "failed";
      loadState.allError = e.message;
    }
  }
  keepScroll(pruneList, renderPrune);
}

async function loadHidden() {
  if (!HIDDEN.length) { loadState.hidden = "loading"; renderHidden(); }
  try {
    HIDDEN = (await getJson("/api/audiobooks/hidden")).hidden || [];
    loadState.hidden = "ready";
  } catch (e) {
    if (HIDDEN.length) {
      toast(`Couldn't refresh the pruned list: ${e.message}`, "error",
        { action: { label: "Retry", run: loadHidden }, anchor: hiddenList });
    } else {
      loadState.hidden = "failed";
      loadState.hiddenError = e.message;
    }
  }
  keepScroll(hiddenList, renderHidden);
}

// Throws a readable Error; callers decide how to say it (a toast, an Undo's own).
async function postIds(url, ids, verb) {
  let resp;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ab_ids: ids }),
    });
  } catch (e) {
    throw new Error(`${verb} failed: ${e.message}`);
  }
  if (!resp.ok) {
    const d = await resp.json().catch(() => ({}));
    throw new Error(`${verb} failed: ${d.detail || `HTTP ${resp.status}`}`);
  }
}

function setSelected(ids, on) {
  for (const id of ids) (on ? selected.add(id) : selected.delete(id));
}

// What a confirm should say about a batch: the first three by name, then a count.
function namesOf(ids) {
  const names = ids.slice(0, 3).map((id) => {
    const e = ALL.find((r) => r.ab_id === id);
    return `“${e ? titleOf(e) : id}”`;
  });
  const more = ids.length - names.length;
  return more > 0 ? `${names.join(", ")} and ${more} more` : names.join(", ");
}

function focusAfterRemove() {
  const first = pruneList.querySelector("input[type=checkbox]");
  (first || pruneQ).focus({ preventScroll: true });
}

async function removeIds(ids) {
  await postIds("/api/audiobooks/hide", ids, "Remove");
  const gone = new Set(ids);
  const rows = ALL.filter((e) => gone.has(e.ab_id));
  rows.forEach((e) => rowCache.set(e.ab_id, e));
  ALL = ALL.filter((e) => !gone.has(e.ab_id));
  HIDDEN = [...rows, ...HIDDEN];
  ids.forEach((id) => selected.delete(id));
  keepScroll(pruneList, renderPrune);
  renderHidden();
  focusAfterRemove();
  const n = ids.length;
  toast(`Removed ${n} title${n === 1 ? "" : "s"}`, "ok", {
    anchor: pruneBtn,
    action: { label: "Undo", busyLabel: "Undoing…", run: () => restoreIds(ids, rows) },
  });
}

// Put titles back (Undo after a remove, and the Restore button). `rows` are the
// entries to put back into the lists without asking the server for the catalog again.
async function restoreIds(ids, rows) {
  await postIds("/api/audiobooks/unhide", ids, "Restore");
  const back = new Set(ids);
  HIDDEN = HIDDEN.filter((e) => !back.has(e.ab_id));
  const known = rows.map((e) => rowCache.get(e.ab_id)).filter(Boolean);
  ALL = [...ALL, ...known].sort(byAuthor);
  keepScroll(pruneList, renderPrune);
  renderHidden();
  // Entries pruned in an earlier visit have no full row here: fetch the catalog once, quietly.
  if (known.length < ids.length) await loadCatalog();
}

// A batch this big is restated in the confirm's title, since three names say little about it.
const BIG_BATCH = 25;

async function onRemove() {
  const ids = [...selected];
  const n = ids.length;
  const big = n >= BIG_BATCH;
  const count = `${n.toLocaleString()} title${n === 1 ? "" : "s"}`;
  const q = pruneQ.value.trim();
  const inFilter = new Set(candidates().map((e) => e.ab_id));
  const filterNote = big && q && ids.every((id) => inFilter.has(id)) ? ` Filter: “${q}”.` : "";
  const ok = await confirmDialog({
    title: !big ? `Remove ${count} from your library?`
      : n === ALL.length ? `Remove all ${count}?`
      : `Remove ${count} from your library?`,
    message: `${namesOf(ids)}.${filterNote} No files are deleted; you can restore them below.`,
    confirmLabel: "Remove",
  });
  if (!ok) return;
  pruneBtn.disabled = pruneDockBtn.disabled = true;
  try {
    await removeIds(ids);
  } catch (e) {
    paintBar(candidates().length, shownIds.length);   // selection is kept; re-enable
    toast(e.message, "error", { anchor: pruneBtn, action: { label: "Retry", run: () => removeIds(ids) } });
  }
}

if (pruneList) {
  pruneList.addEventListener("click", (e) => {
    const retry = e.target.closest("[data-retry]");
    if (retry) { loadCatalog(); return; }
    const cb = e.target.closest("input[type=checkbox][data-ab-id]");
    if (!cb) return;
    const idx = shownIds.indexOf(cb.dataset.abId);
    if (e.shiftKey && lastIdx !== null && idx >= 0) {
      const [a, b] = [Math.min(lastIdx, idx), Math.max(lastIdx, idx)];
      setSelected(shownIds.slice(a, b + 1), cb.checked);
      keepScroll(pruneList, renderPrune);
    } else {
      setSelected([cb.dataset.abId], cb.checked);
      paintBar(candidates().length, shownIds.length);
    }
    lastIdx = idx >= 0 ? idx : lastIdx;
  });

  const refilter = () => { limit = PAGE_ROWS; lastIdx = null; pruneList.scrollTop = 0; renderPrune(); };
  pruneQ.addEventListener("input", refilter);
  pruneLocal.addEventListener("change", refilter);

  pruneMore.addEventListener("click", () => {
    limit += PAGE_ROWS;
    keepScroll(pruneList, renderPrune);
  });

  pruneAll.addEventListener("click", () => {
    setSelected(candidates().map((e) => e.ab_id), true);
    keepScroll(pruneList, renderPrune);
  });
  pruneSel.addEventListener("click", () => {
    selected.clear();
    lastIdx = null;
    keepScroll(pruneList, renderPrune);
    pruneQ.focus({ preventScroll: true });
  });

  pruneBtn.addEventListener("click", onRemove);
  pruneDockBtn.addEventListener("click", onRemove);

  hiddenList.addEventListener("click", async (e) => {
    if (e.target.closest("[data-retry]")) { loadHidden(); return; }
    const btn = e.target.closest(".restore[data-ab-id]");
    if (!btn) return;
    const id = btn.dataset.abId;
    const entry = HIDDEN.find((h) => h.ab_id === id);
    const row = btn.closest(".prune-row");
    // Where focus goes once the list is redrawn: the row that takes this one's place.
    const neighbour = row.nextElementSibling || row.previousElementSibling;
    const nextId = neighbour?.dataset.row;
    btn.disabled = true;
    try {
      await restoreIds([id], [entry]);
    } catch (err) {
      btn.disabled = false;
      toast(err.message, "error", { anchor: hiddenList });
      return;
    }
    // keepScroll holds the list's place; focus follows to the neighbouring Restore.
    const nextBtn = nextId && hiddenList.querySelector(`.restore[data-ab-id="${CSS.escape(nextId)}"]`);
    (nextBtn || hiddenList).focus();
    toast(`Restored “${titleOf(entry)}”`, "ok", {
      anchor: hiddenList,
      action: {
        label: "Undo", busyLabel: "Undoing…",
        run: async () => {
          await postIds("/api/audiobooks/hide", [id], "Remove");
          ALL = ALL.filter((r) => r.ab_id !== id);
          HIDDEN = [entry, ...HIDDEN];
          keepScroll(pruneList, renderPrune);
          renderHidden();
        },
      },
    });
  });

  renderPrune();
  renderHidden();
  loadCatalog();
  loadHidden();
}
