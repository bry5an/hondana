// The "Dictionaries" card on /admin: the Yomitan zips Hondana for iPhone imports from
// `GET /api/reader/dicts`, with upload (`POST /api/admin/dicts`) and delete. Loaded after admin.js
// (shared helpers come from ui.js).
"use strict";

const dcList = document.getElementById("dc-list");
const dcCount = document.getElementById("dc-count");
const dcStatus = document.getElementById("dc-status");
const dcInput = document.getElementById("dc-input");
const dcDrop = document.getElementById("dc-drop");

let dcRows = [];
let dcBusy = false;

function dcSize(n) {
  return n >= 1048576 ? `${(n / 1048576).toFixed(n >= 10485760 ? 0 : 1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

function dcSay(text, err = false) {
  dcStatus.textContent = text;
  dcStatus.classList.toggle("err", err);
}

function dcRender() {
  dcCount.textContent = dcRows.length ? `${dcRows.length}` : "";
  dcList.innerHTML = dcRows.length
    ? dcRows.map((d) => {
        const bits = [d.file, dcSize(d.size), d.revision && `rev ${d.revision}`].filter(Boolean);
        return `
        <div class="prune-row dc-row">
          <span class="t"${langAttr(d.title)}>${esc(d.title)}</span>
          <span class="dc-kind">${esc(d.kind)}</span>
          <button class="btn" type="button" data-file="${esc(d.file)}"
                  aria-label="Delete ${esc(d.title)}">Delete</button>
          <span class="a">${esc(bits.join(" · "))}</span>
        </div>`;
      }).join("")
    : '<div class="prune-empty">No dictionaries yet. Add the Yomitan zips you want on your iPhone.</div>';
}

async function dcLoad() {
  if (!dcRows.length) dcList.innerHTML = '<div class="prune-empty" role="status">Loading…</div>';
  try {
    const r = await fetch("/api/reader/dicts", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    dcRows = (await r.json()).dicts;
    dcRender();
  } catch (e) {
    if (dcRows.length) {
      toast(`Couldn't refresh the dictionaries: ${e.message}`, "error", { action: { label: "Retry", run: dcLoad } });
    } else {
      dcList.innerHTML = `<div class="status-err prune-failed">Couldn't load the dictionaries: ${esc(e.message)}
        <button class="btn" data-retry="dc">Retry</button></div>`;
    }
  }
}

// XHR rather than fetch: a 40 MB zip is the one request here worth a progress figure.
function dcSend(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/admin/dicts");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onerror = () => reject(new Error("the connection dropped"));
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* a proxy's HTML error page */ }
      if (xhr.status < 300) resolve(body);
      else if (xhr.status === 413) reject(new Error("the web server in front of Hondana turned it away as too large (nginx client_max_body_size)"));
      else reject(new Error(body.detail || `HTTP ${xhr.status}`));
    };
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

async function dcUpload(files) {
  if (dcBusy || !files.length) return;
  dcBusy = true;
  dcInput.disabled = true;
  const done = [];
  const failed = [];
  for (const [i, f] of files.entries()) {
    const of = files.length > 1 ? ` (${i + 1} of ${files.length})` : "";
    const say = (p) => dcSay(`Uploading ${f.name}${of}${p == null ? "" : ` — ${Math.round(p * 100)}%`}`);
    say(0);
    try {
      done.push(await dcSend(f, say));
    } catch (e) {
      failed.push(`${f.name}: ${e.message}`);
    }
  }
  dcBusy = false;
  dcInput.disabled = false;
  dcInput.value = "";
  if (failed.length) {
    dcSay(`Not added — ${failed.join("; ")}`, true);
    toast(failed.length === 1 ? `Not added — ${failed[0]}` : `${failed.length} files were not added`, "error");
  } else {
    dcSay("");
  }
  if (done.length) {
    toast(done.length === 1 ? `Added ${done[0].title}` : `Added ${done.length} dictionaries`, "ok");
    dcLoad();
  }
}

dcInput.addEventListener("change", () => dcUpload([...dcInput.files]));
dcDrop.addEventListener("dragover", (e) => { e.preventDefault(); dcDrop.classList.add("drag-over"); });
dcDrop.addEventListener("dragleave", () => dcDrop.classList.remove("drag-over"));
dcDrop.addEventListener("drop", (e) => {
  e.preventDefault();
  dcDrop.classList.remove("drag-over");
  dcUpload([...e.dataTransfer.files]);
});

dcList.addEventListener("click", async (e) => {
  if (e.target.closest("[data-retry]")) { dcLoad(); return; }
  const btn = e.target.closest("button[data-file]");
  if (!btn) return;
  const d = dcRows.find((r) => r.file === btn.dataset.file);
  if (!d) return;
  const ok = await confirmDialog({
    title: `Delete ${d.title}?`,
    message: `${d.file} is removed from this server and from the list Hondana for iPhone imports from. `
      + "Dictionaries already on a phone stay there. Upload the zip again to bring it back.",
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  btn.disabled = true;
  try {
    const r = await fetch(`/api/admin/dicts/${encodeURIComponent(d.file)}`, { method: "DELETE" });
    if (!r.ok && r.status !== 404) {
      const b = await r.json().catch(() => ({}));
      throw new Error(b.detail || `HTTP ${r.status}`);
    }
    dcRows = dcRows.filter((x) => x.file !== d.file);
    dcRender();
    toast(`Deleted ${d.title}`, "ok");
    dcList.focus();
  } catch (err) {
    btn.disabled = false;
    toast(`Couldn't delete ${d.title}: ${err.message}`, "error");
  }
});

dcLoad();
