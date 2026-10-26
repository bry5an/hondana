// The Audiobook tab on /add. Choose audiobook files (or a .zip of one book's tracks) and each goes
// up in resumable chunks (epublib/uploads.py); the server then files and indexes it
// (epublib/intake.py) while this page watches. The tab switch lives here too.
//
// A multi-gigabyte file can't be one request: it would be spooled to a temp file, couldn't resume,
// and a proxy would cap it. So it is PUT in chunks of `chunk_size` bytes, each with a Content-Range,
// and the server's `offset` is the one truth about how much has arrived. Anything that goes wrong
// mid-way (a dropped connection, a 5xx, a chunk the server didn't expect) is settled by asking
// the server for the offset and carrying on from there. Closing the tab only pauses an upload:
// choosing the same file again finds it by name, size and date and resumes.
//
// Wrapped in a function: add.js's top-level names are page globals, and none of them are ours.
(() => {
  'use strict';

  const API = '/api/audiobooks/uploads';
  const EXTS = ['.m4b', '.m4a', '.mp3', '.flac', '.opus', '.ogg', '.zip'];
  const MIN_CHUNK = 256 * 1024;  // how small a proxy's body limit can push a chunk
  const RETRIES = 5;             // failures in a row before a row asks for a Retry
  const STALL_MS = 60000;        // a chunk that moves no bytes for this long is given up on
  const STORE = 'hondana-audio-uploads';
  const END_ICONS = { dup: 'library_books', cancelled: 'block', err: 'close' };
  const STEPS = {
    filing: 'Filing it', indexing: 'Updating the library', 'audio-scan': 'Reading its tags',
    catalog: 'Looking it up on Audible', xref: 'Linking it to your EPUBs', build: 'Updating the library',
  };
  const NETWORK_MSG = 'Lost the connection to the server';
  const PROXY_MSG = "A proxy in front of the server is turning the upload away (413). Raise its upload limit — see the networking notes in the docs";

  const $ = (id) => document.getElementById(id);
  const queueEl = $('audio-queue');
  const cardEl = $('audio-card');
  const rejectEl = $('audio-reject');
  const announceEl = $('audio-announce');
  const input = $('audio-input');
  const zone = $('audio-drop');

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const fmtBytes = (n) => (n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB`
    : n >= 1048576 ? `${(n / 1048576).toFixed(n >= 10485760 ? 0 : 1)} MB`
    : `${Math.max(1, Math.round(n / 1024))} KB`);

  // A refusal with words for the person. `retry`: worth another go as it is (a dropped connection),
  // as opposed to a file the server will never take.
  class Failure extends Error {
    constructor(message, retry = false) { super(message); this.retry = retry; }
  }

  // ── Tabs ────────────────────────────────────────────────────────────────────

  const TABS = { epub: ['tab-epub', 'panel-epub'], audio: ['tab-audio', 'panel-audio'] };

  function showTab(name, { remember = true } = {}) {
    for (const [key, [tabId, panelId]] of Object.entries(TABS)) {
      const on = key === name;
      const tab = $(tabId);
      tab.classList.toggle('active', on);
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      $(panelId).hidden = !on;
    }
    if (remember) history.replaceState(null, '', name === 'audio' ? '#audiobook' : location.pathname + location.search);
  }

  $('tab-epub').addEventListener('click', () => showTab('epub'));
  $('tab-audio').addEventListener('click', () => showTab('audio'));
  document.querySelector('.add-tabs').addEventListener('keydown', (e) => {
    const names = Object.keys(TABS);
    const at = names.findIndex((n) => $(TABS[n][0]) === document.activeElement);
    if (at < 0) return;
    const to = { ArrowRight: (at + 1) % names.length, ArrowLeft: (at + names.length - 1) % names.length, Home: 0, End: names.length - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    showTab(names[to]);
    $(TABS[names[to]][0]).focus();
  });
  if (location.hash === '#audiobook') showTab('audio', { remember: false });

  // ── Remembering an upload across a reload ───────────────────────────────────

  // Keyed by what identifies a file to the person: name, size and date. Per-browser convenience
  // only; the server holds the bytes and the truth.
  const fingerprint = (f) => `${f.name}|${f.size}|${f.lastModified}`;
  const store = {
    read() { try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; } },
    write(all) { try { localStorage.setItem(STORE, JSON.stringify(all)); } catch { /* storage may be off */ } },
    get(key) { return this.read()[key]; },
    set(key, value) { this.write({ ...this.read(), [key]: value }); },
    del(key) { const all = this.read(); delete all[key]; this.write(all); },
  };

  // ── Talking to the server ───────────────────────────────────────────────────

  // JSON calls go through the page's fetch (which signs in again if the password is needed).
  async function api(method, url, body) {
    let resp;
    try {
      resp = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new Failure(NETWORK_MSG, true);
    }
    const data = await resp.json().catch(() => ({}));
    return { status: resp.status, ok: resp.ok, data };
  }

  const detail = (r) => r.data.detail || (r.status === 413 ? PROXY_MSG : `The server answered ${r.status}`);

  // XHR for the chunk, because fetch() has no upload-progress events. Resolves {status, data}; rejects
  // with `aborted` when the person cancelled, any other rejection is the network.
  function putChunk(row, start, end) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      row.xhr = xhr;
      // A connection can stall without failing (no bytes, no error), and XHR has no inactivity
      // timeout: without this the whole queue would wait on it forever. A stalled chunk is
      // aborted and goes the way of a dropped connection (see sendChunks).
      let idle;
      const watch = () => { clearTimeout(idle); idle = setTimeout(() => xhr.abort(), STALL_MS); };
      xhr.open('PUT', `${API}/${row.id}`);
      xhr.setRequestHeader('Content-Range', `bytes ${start}-${end - 1}/${row.file.size}`);
      xhr.upload.onprogress = (e) => { watch(); progress(row, start + e.loaded); };
      xhr.upload.onload = watch;  // all sent: now the server has STALL_MS to answer
      xhr.onload = () => {
        clearTimeout(idle);
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch { /* a proxy's own page */ }
        resolve({ status: xhr.status, data });
      };
      xhr.onerror = () => { clearTimeout(idle); reject(new Error('network')); };
      xhr.onabort = () => { clearTimeout(idle); reject(Object.assign(new Error('aborted'), { aborted: true })); };
      watch();
      xhr.send(row.file.slice(start, end));
    });
  }

  // Where the server says the upload stands. A vanished upload (cleared, or the server's staging
  // was wiped) can't be resumed: start again from the file.
  async function resync(row) {
    const r = await api('GET', `${API}/${row.id}`);
    if (r.status === 404) {
      store.del(fingerprint(row.file));
      throw new Failure('The server no longer has this upload — choose the file again', false);
    }
    if (!r.ok) throw new Failure(detail(r), true);
    row.offset = r.data.offset;
  }

  async function sendChunks(row) {
    const size = row.file.size;
    let failures = 0;
    let mismatches = 0;
    // A failure worth another try: wait a little longer each time, then re-ask where the file stands.
    const again = async () => {
      if (++failures > RETRIES) throw new Failure(NETWORK_MSG, true);
      // A chunk that keeps failing is too big for the link, or for a proxy that cuts the connection
      // instead of answering 413: send less at a time.
      if (failures >= 2) row.chunk = Math.max(MIN_CHUNK, row.chunk >> 1);
      await sleep(Math.min(8000, 500 * 2 ** failures));
      if (!row.cancelled) await resync(row);
    };
    while (row.offset < size && !row.cancelled) {
      const end = Math.min(row.offset + row.chunk, size);
      let r;
      try {
        r = await putChunk(row, row.offset, end);
      } catch (err) {
        if (row.cancelled) return;  // the person pressed ×; any other abort is the network
        await again();
        continue;
      }
      if (r.status === 200) { failures = 0; row.offset = r.data.offset; progress(row, row.offset); continue; }
      if (r.status === 409 && r.data.reason === 'offset_mismatch') {
        if (++mismatches > 20) throw new Failure('The upload and the server disagree about where it is — try again', true);
        row.offset = r.data.offset;
        continue;
      }
      if (r.status === 409 && r.data.reason === 'already_complete') return;  // upload() asks for the job's state
      if (r.status === 409) { await again(); continue; }  // superseded: our own retry's predecessor, say
      if (r.status === 401) {
        if (await ensureAdmin()) continue;
        throw new Failure('Adding audiobooks needs the admin password', true);
      }
      if (r.status === 413 && row.chunk > MIN_CHUNK) {  // a proxy's body limit: smaller chunks fit under it
        row.chunk = Math.max(MIN_CHUNK, row.chunk >> 1);
        continue;
      }
      if (r.status >= 500 || r.status === 499 || r.status === 408 || r.status === 429) { await again(); continue; }
      throw new Failure(detail(r), r.status === 507);  // out of space: worth retrying once there is some
    }
  }

  // Find the upload this file left unfinished, or announce it. Returns the server's record.
  async function open(row, key) {
    const saved = store.get(key);
    if (saved) {
      const r = await api('GET', `${API}/${saved.id}`).catch(() => null);
      const d = r && r.data;
      if (r && r.ok && d.size === row.file.size && d.filename === row.file.name && d.state !== 'error') return d;
      store.del(key);
    }
    const r = await api('POST', API, { filename: row.file.name, size: row.file.size });
    if (!r.ok) throw new Failure(detail(r), r.status >= 500 || r.status === 507);
    store.set(key, { id: r.data.id });
    return r.data;
  }

  // Send the whole file and tell the server it is complete. True once the server has it and is
  // filing it (or already was); false if the person cancelled meanwhile.
  async function upload(row) {
    row.state = 'up'; row.cancelled = false; row.progress = 0; row.text = 'starting…';
    paint(row);
    if (!(await ensureAdmin())) throw new Failure('Adding audiobooks needs the admin password', true);
    const key = fingerprint(row.file);
    const up = await open(row, key);
    row.id = up.id;
    row.chunk = up.chunk_size || 16 * 1024 * 1024;
    row.offset = up.offset;
    if (['queued', 'running', 'done'].includes(up.state)) return true;  // an earlier visit got that far
    if (row.offset > 0) { progress(row, row.offset); row.text = `resuming at ${Math.round(row.progress * 100)}%`; paint(row); }
    await sendChunks(row);
    if (row.cancelled) return false;
    const c = await api('POST', `${API}/${row.id}/complete`);
    if (!c.ok) {
      if (c.status === 409 && c.data.reason === 'incomplete') throw new Failure('Part of the file didn\'t arrive — retry to send the rest', true);
      throw new Failure(detail(c), false);
    }
    return true;
  }

  // ── After the last byte: the server files and indexes it ────────────────────

  const seriesHref = (res) => `/audiobook-series?name=${encodeURIComponent(res.series || res.title || '')}` +
    `&author=${encodeURIComponent(res.author || '')}`;

  function settleDone(row, res) {
    store.del(fingerprint(row.file));
    row.progress = 1;
    row.title = res.title || '';
    row.href = res.ab_id ? seriesHref(res) : null;
    if (res.status === 'duplicate') {
      row.state = 'dup'; row.text = 'Already in your library';
      announce(`${row.file.name} is already in your library`);
    } else {
      row.state = 'done';
      row.text = res.needs_details && row.href
        ? `Added · it has no tags, so <a href="${esc(row.href)}">add its details</a>`
        : 'Added';
      announce(`${row.title || row.file.name} added`);
    }
    paint(row);
  }

  function settleError(row, st) {
    store.del(fingerprint(row.file));
    if (st.reason === 'index_failed') {  // the file is in the folder; only the index is behind
      row.state = 'warn'; row.progress = 1;
      row.text = 'Saved, but updating the library failed. Run <a href="/admin">Scan audiobooks</a> to finish.';
    } else {
      row.state = 'err'; row.text = esc(st.error || "This file couldn't be added");
    }
    announce(`${row.file.name} failed`);
    paint(row);
  }

  async function watch(row) {
    row.state = 'work'; row.progress = 1; row.text = 'Adding to your library…';
    paint(row);
    let misses = 0;
    for (;;) {
      await sleep(700);
      let r;
      try {
        r = await api('GET', `${API}/${row.id}`);
      } catch {
        if (++misses > 20) return settleLost(row);
        continue;
      }
      if (r.status === 404) return settleLost(row);
      if (!r.ok) { if (++misses > 20) return settleLost(row); continue; }
      misses = 0;
      if (r.data.state === 'done') return settleDone(row, r.data.result);
      if (r.data.state === 'error') return settleError(row, r.data);
      const text = STEPS[r.data.step] ? `${STEPS[r.data.step]}…` : 'Adding to your library…';
      if (text !== row.text) { row.text = text; paint(row); }
    }
  }

  // The server forgot the job (it restarted while the book was being added). The file is either
  // already in the library or gone with the restart; only the person can tell.
  function settleLost(row) {
    store.del(fingerprint(row.file));
    row.state = 'warn'; row.progress = 1;
    row.text = 'Sent, but the server restarted before saying whether it was added. Look in <a href="/audiobooks">Audiobooks</a>; if it isn\'t there, choose the file again.';
    paint(row);
  }

  // ── The queue ───────────────────────────────────────────────────────────────

  const rows = [];
  let pumping = false;

  function announce(text) {
    announceEl.textContent = '';
    setTimeout(() => { announceEl.textContent = text; }, 50);
  }

  // Redrawing a row destroys the button just pressed (Retry, ×); keep focus somewhere sensible.
  function keepFocus(el) {
    if (el.contains(document.activeElement)) cardEl.focus({ preventScroll: true });
  }

  function paint(row) {
    keepFocus(row.el);
    const pct = Math.round(row.progress * 100);
    row.el.className = `uq-row uq-${row.state}${END_ICONS[row.state] ? ' uq-end' : ''}`;
    const label = row.title || row.file.name;
    const name = row.href
      ? `<a href="${esc(row.href)}"${langAttr(label)}><span class="uq-t">${esc(label)}</span></a>`
      : `<span${langAttr(row.file.name)}>${esc(row.file.name)}</span>`;
    const head = `<span class="uq-name">${name}<span class="uq-size">${fmtBytes(row.file.size)}</span></span>`;
    const cancel = row.state === 'waiting' || row.state === 'up'
      ? `<button type="button" class="uq-x" data-act="cancel" aria-label="Cancel ${esc(row.file.name)}">&times;</button>`
      : '<span></span>';
    row.el.innerHTML = END_ICONS[row.state]
      ? `<span class="uq-ico" aria-hidden="true">${icon(END_ICONS[row.state])}</span>${head}<span class="uq-state">${row.text || ''}</span>${cancel}`
      : `${head}<span class="uq-bar"><span class="uq-fill" style="width:${pct}%"></span></span><span class="uq-state">${row.text || ''}</span>${cancel}`;
  }

  // The bar and its number move without redrawing the row, so a click on × lands on a stable button.
  function progress(row, loaded) {
    row.progress = Math.min(1, loaded / row.file.size);
    if (row.state !== 'up') return;
    row.text = `${Math.round(row.progress * 100)}%`;
    const fill = row.el.querySelector('.uq-fill');
    if (fill) fill.style.width = `${Math.round(row.progress * 100)}%`;
    const st = row.el.querySelector('.uq-state');
    if (st) st.textContent = row.text;
  }

  // Forget an upload the server will never finish: tell it, and drop the remembered id.
  function forget(row) {
    store.del(fingerprint(row.file));
    if (row.id) api('DELETE', `${API}/${row.id}`).catch(() => {});
  }

  function fail(row, err) {
    if (err.aborted) return;
    const failure = err instanceof Failure ? err : new Failure(String(err.message || err), false);
    row.state = 'err';
    row.text = esc(failure.message) + (failure.retry ? ' · <button type="button" class="link-btn" data-act="retry">Retry</button>' : '');
    if (!failure.retry) forget(row);
    announce(`${row.file.name} failed: ${failure.message}`);
    paint(row);
  }

  // One file at a time goes up. Once its last byte is in, the next may start while the server
  // files this one, so `watch` is not awaited.
  async function pump() {
    if (pumping) return;
    pumping = true;
    try {
      for (let row; (row = rows.find((r) => r.state === 'waiting'));) {
        try {
          if (await upload(row)) watch(row).catch((err) => fail(row, err));
        } catch (err) {
          fail(row, err);
        }
      }
    } finally {
      pumping = false;
    }
  }

  function cancel(row) {
    if (row.state === 'waiting') {
      row.state = 'cancelled'; row.text = 'cancelled';
    } else if (row.state === 'up') {
      row.cancelled = true;
      if (row.xhr) row.xhr.abort();
      row.state = 'cancelled'; row.text = 'cancelled'; row.progress = 0;
      forget(row);
    } else {
      return;
    }
    paint(row);
  }

  queueEl.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    const row = btn && rows.find((r) => r.el === btn.closest('.uq-row'));
    if (!row) return;
    if (btn.dataset.act === 'cancel') cancel(row);
    if (btn.dataset.act === 'retry') { row.state = 'waiting'; row.text = 'waiting'; paint(row); pump(); }
  });

  // ── Choosing files ──────────────────────────────────────────────────────────

  function addFiles(list) {
    const turnedAway = [];
    for (const file of list) {
      const ext = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
      if (!EXTS.includes(ext)) turnedAway.push(`${esc(file.name)} isn't an audiobook file — use ${EXTS.slice(0, -1).join(', ')}, or a .zip of one book's tracks`);
      else if (!file.size) turnedAway.push(`${esc(file.name)} is empty`);
      else newRow(file);
    }
    rejectEl.innerHTML = turnedAway.length
      ? statusHtml('err', turnedAway.length === 1 ? turnedAway[0] : `${turnedAway.length} files weren't added:<br>${turnedAway.join('<br>')}`, '!')
      : '';
    pump();
  }

  function newRow(file) {
    const el = document.createElement('div');
    queueEl.appendChild(el);
    cardEl.hidden = false;
    const row = { file, el, state: 'waiting', progress: 0, text: 'waiting', id: null, offset: 0, chunk: 0 };
    rows.push(row);
    paint(row);
    announce(`${file.name} queued`);
  }

  input.addEventListener('change', () => {
    addFiles([...input.files]);
    input.value = '';  // choosing the same file again must fire a change
  });

  // A file dropped anywhere on the page while this tab shows is an add (and never navigates away).
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  const showing = () => !$('panel-audio').hidden;
  window.addEventListener('dragover', (e) => {
    if (!hasFiles(e) || !showing()) return;
    e.preventDefault();
    zone.classList.add('drag-over');
  });
  window.addEventListener('dragleave', (e) => {
    if (!e.relatedTarget) zone.classList.remove('drag-over');
  });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e) || !showing()) return;
    e.preventDefault();
    zone.classList.remove('drag-over');
    addFiles([...e.dataTransfer.files]);
  });
})();
