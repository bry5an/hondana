// /settings: every setting the web app may change, built from GET /api/settings (settings.py
// defines them), plus the admin password, script tokens and where things live. Each card saves
// its own fields with one PUT; a field set by an environment variable or only allowed in the
// config file is shown but locked. A saved library folder starts a scan; host/port/discovery
// changes wait for a restart, which the banner offers when something will restart the server.
"use strict";

const SET_GROUPS = [
  { id: "library", title: "Library", hint: "Where your EPUBs are." },
  { id: "audiobooks", title: "Audiobooks", hint: "Folders scanned for .m4b files and folders of .mp3s." },
  { id: "network", title: "Network and phone", hint: "How other devices reach this server. See docs/networking.md." },
  { id: "metadata", title: "Online lookups", hint: "Optional services that fill in genres and descriptions." },
  { id: "advanced", title: "Advanced", hint: "Matching thresholds. The defaults suit most libraries." },
];
const SOURCE_WORDS = {
  default: "Default",
  file: "config.toml",
  settings: "Saved here",
  env: "Environment",
};

let setDoc = null;

function fieldsOf(group) {
  return setDoc.fields.filter((f) => f.group === group);
}

function inputHtml(f) {
  const id = `set-${f.key.replace(/\./g, "-")}`;
  const off = f.editable ? "" : " disabled";
  const desc = `aria-describedby="${id}-help"`;
  if (f.kind === "bool") {
    return `<label class="set-check"><input type="checkbox" id="${id}" data-key="${esc(f.key)}"
      ${f.value ? "checked" : ""}${off} ${desc}> ${esc(f.label)}</label>`;
  }
  if (f.kind === "paths") {
    const lines = (f.value || []).join("\n");
    return `<textarea id="${id}" data-key="${esc(f.key)}" rows="${Math.max(2, (f.value || []).length)}"
      spellcheck="false"${off} ${desc}>${esc(lines)}</textarea>`;
  }
  if (f.kind === "secret") {
    const ph = f.is_set ? "Saved. Type a new one to replace it" : "Not set";
    return `<input type="password" id="${id}" data-key="${esc(f.key)}" autocomplete="off"
      placeholder="${esc(ph)}"${off} ${desc}>`;
  }
  if (f.kind === "choice") {
    const opts = (f.choices || []).map(([v, label]) =>
      `<option value="${esc(v)}"${v === f.value ? " selected" : ""}>${esc(label)}</option>`).join("");
    return `<select id="${id}" data-key="${esc(f.key)}"${off} ${desc}>${opts}</select>`;
  }
  if (f.kind === "list") {
    return `<input type="text" id="${id}" data-key="${esc(f.key)}" value="${esc((f.value || []).join(", "))}"
      spellcheck="false"${off} ${desc}>`;
  }
  const numeric = ["int", "optint", "float"].includes(f.kind);
  const type = numeric ? "number" : "text";
  const step = f.kind === "float" ? ' step="any"' : "";
  let value = f.value === null || f.value === undefined ? "" : f.value;
  if (Array.isArray(value)) value = JSON.stringify(value);  // a command given as an argv array
  return `<input type="${type}" id="${id}" data-key="${esc(f.key)}" value="${esc(value)}"${step}
    spellcheck="false"${off} ${desc}>`;
}

function fieldHtml(f) {
  const id = `set-${f.key.replace(/\./g, "-")}`;
  const src = f.env_locked ? `Set by ${f.env}` : SOURCE_WORDS[f.source] || f.source;
  const chips = [`<span class="set-src set-src--${esc(f.source)}">${esc(src)}</span>`];
  if (f.restart_pending) chips.push(`<span class="set-src set-src--restart">Restart to apply</span>`);
  else if (f.restart) chips.push(`<span class="set-src">Applies after a restart</span>`);
  const reset = f.source === "settings" && f.editable
    ? `<button type="button" class="link-btn set-reset" data-reset="${esc(f.key)}">Reset</button>` : "";
  const check = (f.kind === "path" || f.kind === "paths") && f.editable
    ? `<button type="button" class="link-btn set-checkbtn" data-check="${esc(f.key)}">Check folder</button>` : "";
  const lock = f.file_only ? " Only the config file can change it." :
    f.env_locked ? ` Remove ${f.env} to change it here.` : "";
  const label = f.kind === "bool" ? "" : `<label for="${id}">${esc(f.label)}</label>`;
  return `
    <div class="set-fld" data-field="${esc(f.key)}">
      ${label}
      ${inputHtml(f)}
      <div class="set-meta">${chips.join("")}${check}${reset}</div>
      <p class="set-help" id="${id}-help">${esc(f.help || "")}${esc(lock)}</p>
      <p class="set-check-result" data-result="${esc(f.key)}" role="status"></p>
    </div>`;
}

function cardHtml(g) {
  const fields = fieldsOf(g.id);
  if (!fields.length) return "";
  const editable = fields.some((f) => f.editable);
  return `
    <section class="add-card set-card" id="${g.id}" aria-labelledby="${g.id}-h">
      <h2 id="${g.id}-h">${esc(g.title)}</h2>
      <p class="hint">${esc(g.hint)}</p>
      <form class="set-form" data-group="${g.id}" novalidate>
        ${fields.map(fieldHtml).join("")}
        ${editable ? `<div class="set-actions">
          <button type="submit" class="submit-btn">Save</button>
          <span class="set-status" role="status"></span>
        </div>` : ""}
      </form>
    </section>`;
}

function readField(f, form) {
  const el = form.querySelector(`[data-key="${CSS.escape(f.key)}"]`);
  if (!el || el.disabled) return undefined;
  if (f.kind === "bool") return el.checked;
  if (f.kind === "paths") return el.value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (f.kind === "list") return el.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
  if (f.kind === "secret") return el.value.trim() || undefined;  // blank = keep the saved one
  if (f.kind === "optint") return el.value.trim() === "" ? null : Number(el.value);
  if (f.kind === "int" || f.kind === "float") return Number(el.value);
  return el.value.trim();
}

function same(a, b) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

async function saveValues(values, statusEl) {
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const errors = data.errors || {};
      for (const [key, msg] of Object.entries(errors)) {
        const out = document.querySelector(`[data-result="${CSS.escape(key)}"]`);
        if (out) { out.className = "set-check-result err"; out.textContent = msg; }
      }
      throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    }
    setDoc = data;
    render();
    const notes = [];
    if (data.started === "scan") notes.push("Scanning your books now (see Admin).");
    if (data.started === "audio-scan") notes.push("Scanning your audiobooks now (see Admin).");
    toast(["Saved.", ...notes].join(" "), "ok");
  } catch (err) {
    if (statusEl) { statusEl.className = "set-status err"; statusEl.textContent = err.message; }
    else toast(err.message, "error");
  }
}

async function onSubmit(e) {
  e.preventDefault();
  const form = e.target;
  const status = form.querySelector(".set-status");
  status.className = "set-status";
  status.textContent = "";
  form.querySelectorAll(".set-check-result").forEach((el) => { el.textContent = ""; });
  const values = {};
  for (const f of fieldsOf(form.dataset.group)) {
    if (!f.editable) continue;
    const v = readField(f, form);
    if (v === undefined) continue;
    if (f.kind !== "secret" && same(v, f.value)) continue;
    values[f.key] = v;
  }
  if (!Object.keys(values).length) {
    status.textContent = "Nothing changed.";
    return;
  }
  const btn = form.querySelector(".submit-btn");
  btn.disabled = true;
  btn.textContent = "Saving…";
  await saveValues(values, status);
  btn.disabled = false;
  btn.textContent = "Save";
}

async function checkFolder(key) {
  const f = setDoc.fields.find((x) => x.key === key);
  const form = document.querySelector(`[data-field="${CSS.escape(key)}"]`).closest("form");
  const value = readField(f, form);
  const paths = Array.isArray(value) ? value : [value];
  const out = document.querySelector(`[data-result="${CSS.escape(key)}"]`);
  out.className = "set-check-result";
  out.textContent = "Checking…";
  const lines = [];
  let bad = false;
  for (const path of paths.filter(Boolean)) {
    try {
      const r = await postJson("/api/settings/check-path", { path });
      if (r.problem) { bad = true; lines.push(`${r.path}: ${r.problem}`); continue; }
      const what = key === "epub_dir" ? `${fmt(r.epubs)} EPUBs` : `${fmt(r.audio)} audio files`;
      lines.push(`${r.path}: ${what}${r.writable ? "" : ", read-only"}`);
    } catch (err) {
      bad = true;
      lines.push(err.message);
    }
  }
  out.className = `set-check-result ${bad ? "err" : "ok"}`;
  out.textContent = lines.join(" · ") || "Type a folder first.";
}

function renderRestart() {
  const banner = document.getElementById("restart-banner");
  const pending = setDoc.restart_pending || [];
  if (!pending.length) { banner.hidden = true; banner.innerHTML = ""; return; }
  const action = setDoc.supervised
    ? ` <button type="button" class="btn" id="restart-btn">Restart now</button>`
    : " Restart Hondana to apply them.";
  banner.innerHTML = statusHtml("pending",
    `Saved, and waiting for a restart: ${esc(pending.join(", "))}.${action}`);
  banner.hidden = false;
}

function renderAbout() {
  const rows = [
    ["Version", setDoc.version],
    ["Config file", setDoc.config_path || "None (defaults, settings saved here and the environment)"],
    ["Home folder", setDoc.home || ""],
    ["Data folder", setDoc.data_dir],
  ];
  document.getElementById("about-body").innerHTML = `
    <table class="stats-table set-about">
      ${rows.map(([k, v]) => `<tr><th scope="row">${esc(k)}</th><td><code>${esc(v)}</code></td></tr>`).join("")}
    </table>
    <p class="hint">The data folder holds your profiles, known words and corrections: back it up.
      A few settings (the SubPlz command, backups, the data folder itself) can only be set in
      config.toml or the environment. <code>cli.py config</code> lists every setting.</p>`;
}

function render() {
  document.getElementById("settings-cards").innerHTML = SET_GROUPS.map(cardHtml).join("");
  renderRestart();
  renderAbout();
}

// ── admin password and tokens (auth.py) ───────────────────────────────────────

async function renderSecurity() {
  const body = document.getElementById("security-body");
  const st = await (await fetch("/api/auth/state")).json();
  if (!st.password_set) {
    body.innerHTML = `
      <p class="hint">No password: anyone on your network can change settings, run the pipeline
        and delete books. Set one to keep those to you. Browsing, reading and the iPhone app stay
        open to everyone.</p>
      ${passwordFormHtml("Set password")}`;
  } else {
    body.innerHTML = `
      <p class="hint">Set. Settings, Admin and changes to the library need it; browsing,
        reading and the iPhone app don't.</p>
      ${passwordFormHtml("Change password")}
      <div class="set-actions set-actions--quiet">
        <button type="button" class="btn" id="signout-btn">Sign out</button>
        <button type="button" class="btn btn-danger" id="clearpw-btn">Remove password…</button>
      </div>`;
  }
}

function passwordFormHtml(label) {
  return `
    <form class="set-form" id="pw-form">
      <div class="set-fld">
        <label for="pw-new">New password</label>
        <input type="password" id="pw-new" name="password" autocomplete="new-password" minlength="8" required>
      </div>
      <div class="set-fld">
        <label for="pw-again">Type it again</label>
        <input type="password" id="pw-again" name="again" autocomplete="new-password" required>
      </div>
      <div class="set-actions">
        <button type="submit" class="submit-btn">${esc(label)}</button>
        <span class="set-status" role="status"></span>
      </div>
    </form>`;
}

async function renderTokens() {
  const body = document.getElementById("tokens-body");
  let tokens = [];
  try {
    tokens = await (await fetch("/api/auth/tokens")).json();
  } catch { /* shown as empty */ }
  const rows = (Array.isArray(tokens) ? tokens : []).map((t) => `
    <li class="set-token-row">
      <span><b>${esc(t.name)}</b> <span class="set-help">made ${esc(new Date(t.created_at * 1000).toLocaleDateString())}${
        t.last_used ? `, last used ${esc(new Date(t.last_used * 1000).toLocaleDateString())}` : ", never used"}</span></span>
      <button type="button" class="btn btn-danger" data-revoke="${esc(t.id)}" data-name="${esc(t.name)}">Revoke…</button>
    </li>`).join("");
  body.innerHTML = `
    ${rows ? `<ul class="set-tokens">${rows}</ul>` : `<p class="set-help">No tokens.</p>`}
    <div id="token-new"></div>
    <div class="set-actions"><button type="button" class="btn" id="token-create">Make a token…</button></div>`;
}

async function onClick(e) {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.reset) {
    const f = setDoc.fields.find((x) => x.key === t.dataset.reset);
    await saveValues({ [t.dataset.reset]: null }, null);
    toast(`${f ? f.label : "Setting"} is back to ${f && f.source === "settings" ? "its file or default value" : "default"}.`, "ok");
  } else if (t.dataset.check) {
    await checkFolder(t.dataset.check);
  } else if (t.id === "restart-btn") {
    t.disabled = true;
    t.textContent = "Restarting…";
    try {
      await postJson("/api/admin/restart", {});
      setTimeout(waitForRestart, 1500);
    } catch (err) {
      toast(err.message, "error");
      t.disabled = false;
      t.textContent = "Restart now";
    }
  } else if (t.id === "signout-btn") {
    await postJson("/api/auth/logout", {});
    location.href = "/";
  } else if (t.id === "clearpw-btn") {
    const ok = await confirmDialog({
      title: "Remove the admin password?",
      message: "Anyone on your network will be able to change settings and delete books again.",
      confirmLabel: "Remove password", danger: true,
    });
    if (!ok) return;
    try {
      await postJson("/api/auth/password", undefined, "DELETE");
      toast("Password removed.", "ok");
      await renderSecurity();
    } catch (err) { toast(err.message, "error"); }
  } else if (t.id === "token-create") {
    const got = await promptDialog({
      title: "Make a script token",
      fields: [{ name: "name", label: "What it's for", placeholder: "align-remote on my Mac", required: true }],
      confirmLabel: "Make token",
    });
    if (!got) return;
    try {
      const made = await postJson("/api/auth/tokens", { name: got.name });
      await renderTokens();
      document.getElementById("token-new").innerHTML = `
        <div class="set-token-new">
          <p><b>${esc(made.name)}</b>: copy it now, it won't be shown again.</p>
          <code class="set-token-value">${esc(made.token)}</code>
          <button type="button" class="btn" id="token-copy" data-token="${esc(made.token)}">Copy</button>
        </div>`;
    } catch (err) { toast(err.message, "error"); }
  } else if (t.id === "token-copy") {
    const ok = await copyText(t.dataset.token);
    toast(ok ? "Copied." : "Couldn't copy: select it and copy by hand.", ok ? "ok" : "error");
  } else if (t.dataset.revoke) {
    const ok = await confirmDialog({
      title: `Revoke “${t.dataset.name}”?`,
      message: "Scripts using it stop working until you give them a new token.",
      confirmLabel: "Revoke", danger: true,
    });
    if (!ok) return;
    try {
      await postJson(`/api/auth/tokens/${encodeURIComponent(t.dataset.revoke)}`, undefined, "DELETE");
      await renderTokens();
    } catch (err) { toast(err.message, "error"); }
  }
}

async function onPasswordSubmit(e) {
  if (e.target.id !== "pw-form") return;
  e.preventDefault();
  const form = e.target;
  const status = form.querySelector(".set-status");
  status.className = "set-status";
  if (form.password.value !== form.again.value) {
    status.className = "set-status err";
    status.textContent = "The two don't match.";
    return;
  }
  try {
    await postJson("/api/auth/password", { password: form.password.value });
    toast("Password saved. Other browsers will need it next time.", "ok");
    await renderSecurity();
    await renderTokens();
  } catch (err) {
    status.className = "set-status err";
    status.textContent = err.message;
  }
}

async function waitForRestart() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch("/healthz", { cache: "no-store" });
      if (r.ok) { location.reload(); return; }
    } catch { /* still down */ }
    await new Promise((res) => setTimeout(res, 1000));
  }
  toast("Hondana hasn't come back yet. Check the server's log.", "error");
}

async function initSettings() {
  const main = document.getElementById("settings-main");
  main.addEventListener("click", onClick);
  main.addEventListener("submit", (e) => {
    if (e.target.matches(".set-form[data-group]")) onSubmit(e);
    else onPasswordSubmit(e);
  });
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setDoc = await res.json();
    render();
  } catch (err) {
    document.getElementById("settings-cards").innerHTML =
      statusHtml("err", `Couldn't load the settings (${esc(err.message)}). Reload to try again.`);
  }
  await renderSecurity();
  await renderTokens();
  if (location.hash) document.querySelector(location.hash)?.scrollIntoView();
}

initSettings();
