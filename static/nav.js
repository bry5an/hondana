(function () {
  var sidebar = document.getElementById('sidebar');
  var overlay = document.getElementById('overlay');
  var toggle  = document.getElementById('nav-toggle');
  if (!sidebar || !toggle) return;

  function isMobile() { return window.innerWidth < 640; }

  var mainCol = document.querySelector('.main-col');

  // Phone drawer. While open, the page behind it is inert so Tab stays inside the
  // drawer; closed, the drawer is out of the tab order via CSS (visibility).
  function openDrawer() {
    sidebar.classList.add('open');
    if (overlay) overlay.classList.add('visible');
    toggle.setAttribute('aria-expanded', 'true');
    if (mainCol) mainCol.inert = true;
    var first = sidebar.querySelector('a[href], button');
    if (first) first.focus();
  }

  // returnFocus: put focus back on the toggle (Escape, overlay tap). Skipped when
  // closing because a nav link was tapped — the page is about to navigate.
  function closeDrawer(returnFocus) {
    sidebar.classList.remove('open');
    if (overlay) overlay.classList.remove('visible');
    toggle.setAttribute('aria-expanded', 'false');
    if (mainCol) mainCol.inert = false;
    if (returnFocus === true) toggle.focus();
  }

  function collapseDesktop() {
    sidebar.classList.add('collapsed');
    toggle.setAttribute('aria-expanded', 'false');
    localStorage.setItem('nav-collapsed', '1');
  }

  function expandDesktop() {
    sidebar.classList.remove('collapsed');
    toggle.setAttribute('aria-expanded', 'true');
    localStorage.setItem('nav-collapsed', '0');
  }

  // aria-expanded says whether the sidebar is showing: the drawer on a phone, not-collapsed
  // on desktop. The markup starts "true", so a phone load needs setting right away, and
  // crossing the breakpoint switches which state it reads.
  function syncToggle() {
    var shown = isMobile() ? sidebar.classList.contains('open') : !sidebar.classList.contains('collapsed');
    toggle.setAttribute('aria-expanded', shown ? 'true' : 'false');
  }

  toggle.addEventListener('click', function () {
    if (isMobile()) {
      sidebar.classList.contains('open') ? closeDrawer() : openDrawer();
    } else {
      sidebar.classList.contains('collapsed') ? expandDesktop() : collapseDesktop();
    }
  });

  if (overlay) {
    overlay.addEventListener('click', function () { closeDrawer(true); });
  }

  // Escape closes the profile menu first (its own handler, below); the drawer
  // stays up until a second press.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !isMobile() || !sidebar.classList.contains('open')) return;
    if (sidebar.querySelector('.profile-switcher.open')) return;
    closeDrawer(true);
  });

  // For controls outside the sidebar that need something inside it (the profile
  // page's "Switch reader"): on a phone the sidebar is an off-canvas drawer, and
  // on desktop it may be collapsed to nothing, so bring it into view first.
  window.revealSidebar = function () {
    if (isMobile()) {
      if (!sidebar.classList.contains('open')) openDrawer();
    } else if (sidebar.classList.contains('collapsed')) {
      expandDesktop();
    }
  };

  // Rotating or resizing past the breakpoint with the drawer open: drop the phone state.
  window.addEventListener('resize', function () {
    if (!isMobile() && sidebar.classList.contains('open')) closeDrawer();
    syncToggle();
  });

  // Close drawer on mobile when a nav link is tapped
  sidebar.querySelectorAll('.nav-link').forEach(function (a) {
    a.addEventListener('click', function () { if (isMobile()) closeDrawer(); });
  });

  // Restore desktop collapsed state across page loads
  if (!isMobile() && localStorage.getItem('nav-collapsed') === '1') {
    sidebar.classList.add('collapsed');
  }
  syncToggle();
})();

// ── Theme toggle: light / dark Playful Shelf, remembered across pages ──
(function () {
  var body = document.body;
  var sidebar = document.getElementById('sidebar');
  var KEY = 'epub-theme';

  // Same rule as the inline script in partials/shell.html: an explicit choice
  // wins, otherwise follow the OS setting.
  var stored = localStorage.getItem(KEY);
  if (stored === 'dark' || (stored === null && window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)) body.classList.add('dark');
  if (!sidebar) return;

  var foot = document.createElement('div');
  foot.className = 'sidebar-foot';

  // Known-words stat card — real count from /api/known-words, links to /known.
  // Hidden until the count loads so an offline/failed fetch shows nothing.
  var known = document.createElement('a');
  known.className = 'known-card';
  known.href = '/known';
  known.hidden = true;
  // The card is the only way to /known, so it takes the nav's "you are here".
  if (location.pathname === '/known') known.setAttribute('aria-current', 'page');
  known.innerHTML =
    '<span class="known-card-art" aria-hidden="true"><span class="mi">spellcheck</span></span>' +
    '<span class="known-card-label">Known words</span>' +
    '<span class="known-card-num"></span>';
  foot.appendChild(known);

  var btn = document.createElement('button');
  btn.className = 'theme-toggle';
  btn.type = 'button';
  foot.appendChild(btn);
  sidebar.appendChild(foot);

  // The card notices progress. A count that goes *up* after the first load (words
  // marked known, a bigger import) rolls to its new value, bumps the card and
  // sticks a "+N" sticker on it; every ten passed by hand-marking is named
  // on the label for a moment, and every hundred does it louder. The first load, a drop and a no-change are silent:
  // the same page-load number should never perform, and a loss is not a party.
  var numEl = known.querySelector('.known-card-num');
  var labelEl = known.querySelector('.known-card-label');
  var LABEL = labelEl.textContent;
  var shown = null;       // the count on screen (or rolling to); null until the first load
  var current = 0;        // the number the roll has reached so far
  var milestoneAt = 0;
  var rollFrame = 0, gainTimer = 0, labelTimer = 0, gain = 0;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');

  function restart(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth;            // re-trigger the animation
    el.classList.add(cls);
  }

  // Every STEP words the card names the round number it just passed; every BIG_STEP it
  // celebrates. A rise of more than BIG_STEP (an import) is not hand-marking: silent.
  var STEP = 10;
  var BIG_STEP = 100;
  var celebrateTimer = 0;

  function put(n) {
    current = n;
    numEl.textContent = n.toLocaleString();
  }

  function roll(from, to) {
    cancelAnimationFrame(rollFrame);
    from = current;                 // a second mark mid-roll carries on from where it is
    if (reduced && reduced.matches) { put(to); return; }
    var t0 = performance.now();
    var ms = Math.min(900, 380 + 120 * Math.log10(to - from + 1));
    (function step(now) {
      var k = Math.min(1, (now - t0) / ms);
      var e = k === 1 ? 1 : 1 - Math.pow(2, -10 * k);   // expo ease-out
      put(Math.round(from + (to - from) * e));
      if (k < 1) rollFrame = requestAnimationFrame(step);
    })(t0);
  }

  function sticker(delta) {
    var tag = known.querySelector('.known-card-gain');
    if (!tag) {
      tag = document.createElement('span');
      tag.className = 'known-card-gain';
      tag.setAttribute('aria-hidden', 'true');
      known.appendChild(tag);
    }
    gain = gainTimer ? gain + delta : delta;   // quick marks add up on one sticker
    tag.textContent = '+' + gain.toLocaleString();
    restart(tag, 'on');
    clearTimeout(gainTimer);
    gainTimer = setTimeout(function () { tag.classList.remove('on'); gainTimer = 0; }, 2300);
  }

  function milestone(n) {
    milestoneAt = Math.floor(n / STEP) * STEP;
    var big = milestoneAt % BIG_STEP === 0;
    labelEl.textContent = (big ? 'Milestone: ' : 'Passed ') + milestoneAt.toLocaleString();
    known.classList.toggle('big', big);
    restart(known, 'milestone');     // the light sweeps again for each round number
    if (big) {
      // The louder card reaction replaces the plain bump (swapping them cleanly, so
      // taking one off never replays the other).
      known.classList.remove('bump');
      restart(known, 'celebrate');
      clearTimeout(celebrateTimer);
      celebrateTimer = setTimeout(function () { known.classList.remove('celebrate'); }, 1400);
    }
    clearTimeout(labelTimer);
    labelTimer = setTimeout(endMilestone, big ? 4200 : 2800);
  }

  function endMilestone() {
    labelEl.textContent = LABEL;
    known.classList.remove('milestone', 'big');
    milestoneAt = 0;
  }

  // The count went down (an unmark, or the server's total came in lower). Quiet, but
  // it takes back what it announced: an un-marked word comes off the live "+N"
  // sticker, and a round number that is no longer passed stops being named.
  function lower(by, n) {
    cancelAnimationFrame(rollFrame);
    put(n);
    var tag = known.querySelector('.known-card-gain');
    if (gainTimer && tag) {
      gain = Math.max(0, gain - by);
      if (gain) tag.textContent = '+' + gain.toLocaleString();
      else { clearTimeout(gainTimer); gainTimer = 0; tag.classList.remove('on'); }
    }
    if (milestoneAt && n < milestoneAt) { clearTimeout(labelTimer); endMilestone(); }
  }

  function showKnownCount(n) {
    if (typeof n !== 'number') return;
    var from = shown;
    shown = n;
    known.hidden = false;
    if (from === null) { put(n); return; }
    if (n === from) return;         // already showing it (an optimistic mark the server just confirmed)
    if (n < from) { lower(from - n, n); return; }
    roll(from, n);
    sticker(n - from);
    var named = n - from <= BIG_STEP && Math.floor(n / STEP) > Math.floor(from / STEP);
    if (named) milestone(n);
    // A plain rise bumps the card; a named hundred celebrates instead (milestone()).
    if (!named || milestoneAt % BIG_STEP !== 0) {
      known.classList.remove('celebrate');
      restart(known, 'bump');
    }
  }

  // A mark or unmark is shown the moment it is clicked, before the server has
  // saved it (mark.js; the save takes a second or two). The server's own total
  // arrives later as 'known-words-changed' and settles any difference.
  window.addEventListener('known-words-delta', function (e) {
    var d = e.detail && e.detail.delta;
    if (typeof d === 'number' && d && shown !== null) showKnownCount(shown + d);
  });

  fetch('/api/known-words').then(function (r) { return r.json(); }).then(function (d) {
    showKnownCount(d && d.count);
  }).catch(function () {});

  // Pages that change the list (import / clear / restore / mark) announce the
  // new count so the card doesn't sit on the page-load value.
  window.addEventListener('known-words-changed', function (e) {
    showKnownCount(e.detail && e.detail.count);
  });

  function paint(swap) {
    var isDark = body.classList.contains('dark');
    btn.innerHTML = isDark
      ? '<span class="ic mi">light_mode</span><span class="lbl">Light mode</span>'
      : '<span class="ic mi">dark_mode</span><span class="lbl">Dark mode</span>';
    if (swap) btn.firstChild.classList.add('swap');   // the new icon turns into place
  }
  paint();

  btn.addEventListener('click', function () {
    var isDark = body.classList.toggle('dark');
    localStorage.setItem(KEY, isDark ? 'dark' : 'light');
    paint(true);
  });
})();

// ── Reader profile switcher (injected into the sidebar footer) ──
// A profile is a name + the language being learned; the active profile is
// per-device (a cookie, set when switchTo() posts to /api/profiles/active —
// the browser carries it automatically from then on, no client state here).
// Built client-side like the theme toggle so no template needs editing.
// Hidden until /api/profiles loads, so a failed fetch shows nothing.
(function () {
  var foot = document.querySelector('.sidebar-foot');
  if (!foot) return;

  var LANG = { ja: 'JA', en: 'EN' };
  var errEl = null;

  var wrap = document.createElement('div');
  wrap.className = 'profile-switcher';
  wrap.hidden = true;
  foot.insertBefore(wrap, foot.firstChild); // above the Known-words card

  function langTag(l) { return LANG[l] || esc(l || ''); }
  function items() {
    return Array.prototype.slice.call(wrap.querySelectorAll('[role="menuitem"]:not([hidden])'));
  }

  function setOpen(open, returnFocus) {
    var caretBtn = wrap.querySelector('.profile-caret-btn');
    if (!caretBtn) return;
    wrap.classList.toggle('open', open);
    caretBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      var first = wrap.querySelector('.profile-item.is-active') || items()[0];
      if (first) first.focus();
    } else if (returnFocus) {
      caretBtn.focus();
    }
  }
  function close(returnFocus) { setOpen(false, returnFocus === true); }

  // Open the menu from outside the sidebar. The sidebar is brought into view
  // first, or the menu would open where nobody can see it.
  window.openProfileMenu = function () {
    if (wrap.hidden) return;
    if (window.revealSidebar) window.revealSidebar();
    setOpen(true);
  };

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (r.ok) return r.json();
      return r.json().catch(function () { return {}; }).then(function (d) {
        throw new Error(d.detail || ('HTTP ' + r.status));
      });
    });
  }

  function showError(e) {
    if (!errEl) return;
    errEl.textContent = (e && e.message) || 'Something went wrong';
    errEl.hidden = false;
  }

  function switchTo(slug) {
    postJSON('/api/profiles/active', { slug: slug })
      .then(function () { location.reload(); })
      .catch(showError);
  }

  function render(data) {
    var profiles = (data && data.profiles) || [];
    if (!profiles.length) { wrap.hidden = true; return; }
    var active = profiles[0];
    profiles.forEach(function (p) { if (p.slug === data.active) active = p; });

    var rows = profiles.map(function (p) {
      var on = p.slug === active.slug ? ' is-active' : '';
      return '<button type="button" role="menuitem" class="profile-item' + on + '" data-slug="' +
        esc(p.slug) + '">' +
        '<span class="profile-avatar">' + esc(initial(p.name)) + '</span>' +
        '<span class="profile-item-name">' + esc(p.name) + '</span>' +
        '<span class="profile-lang">' + langTag(p.language) + '</span>' +
        (on ? '<span class="profile-check mi">check</span>' : '') +
        '</button>';
    }).join('');

    wrap.innerHTML =
      '<div class="profile-current">' +
        '<a class="profile-view" href="/profile">' +
          '<span class="profile-avatar">' + esc(initial(active.name)) + '</span>' +
          '<span class="profile-current-name">' + esc(active.name) + '</span>' +
          '<span class="profile-lang">' + langTag(active.language) + '</span>' +
        '</a>' +
        '<button type="button" class="profile-caret-btn" id="profile-menu-btn" aria-haspopup="menu" aria-controls="profile-menu" aria-expanded="false" aria-label="Switch profile">' +
          '<span class="profile-caret mi">expand_more</span>' +
        '</button>' +
      '</div>' +
      '<div class="profile-menu" id="profile-menu" role="menu" aria-labelledby="profile-menu-btn">' +
        '<div class="profile-list">' + rows + '</div>' +
        '<button type="button" role="menuitem" class="profile-add">＋ Add profile</button>' +
        '<form class="profile-add-form" hidden>' +
          '<input type="text" class="profile-name-input" placeholder="Name" maxlength="40" required>' +
          '<select class="profile-lang-select">' +
            '<option value="ja">Learning Japanese</option>' +
            '<option value="en">Learning English</option>' +
          '</select>' +
          '<div class="profile-form-row">' +
            '<button type="submit" class="profile-create">Create</button>' +
            '<button type="button" class="profile-cancel">Cancel</button>' +
          '</div>' +
          '<p class="profile-error" hidden></p>' +
        '</form>' +
      '</div>';
    wrap.hidden = false;
    errEl = wrap.querySelector('.profile-error');
    wire();
  }

  function wire() {
    var caretBtn = wrap.querySelector('.profile-caret-btn');
    caretBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      setOpen(!wrap.classList.contains('open'));
    });

    wrap.querySelectorAll('.profile-item').forEach(function (b) {
      b.addEventListener('click', function () {
        close();
        if (b.classList.contains('is-active')) return;
        switchTo(b.dataset.slug);
      });
    });

    var addBtn = wrap.querySelector('.profile-add');
    var form = wrap.querySelector('.profile-add-form');
    addBtn.addEventListener('click', function () {
      addBtn.hidden = true;
      form.hidden = false;
      form.querySelector('.profile-name-input').focus();
    });
    wrap.querySelector('.profile-cancel').addEventListener('click', function () {
      form.hidden = true;
      addBtn.hidden = false;
      addBtn.focus();
      if (errEl) errEl.hidden = true;
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var name = form.querySelector('.profile-name-input').value.trim();
      var language = form.querySelector('.profile-lang-select').value;
      if (!name) return;
      if (errEl) errEl.hidden = true;
      postJSON('/api/profiles', { name: name, language: language })
        .then(function (d) { switchTo(d.profile.slug); })
        .catch(showError);
    });

    // Keep clicks inside the open menu from reaching the document closer.
    wrap.querySelector('.profile-menu').addEventListener('click', function (e) {
      e.stopPropagation();
    });
  }

  document.addEventListener('click', function () { close(); });

  // Escape closes and hands focus back to the caret; arrows walk the items.
  document.addEventListener('keydown', function (e) {
    if (!wrap.classList.contains('open')) return;
    if (e.key === 'Escape') {
      close(true);
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      var list = items();
      var i = list.indexOf(document.activeElement);
      if (!list.length || (i < 0 && !wrap.contains(document.activeElement))) return;
      e.preventDefault();
      var step = e.key === 'ArrowDown' ? 1 : -1;
      list[(i + step + list.length) % list.length].focus();
    }
  });

  fetch('/api/profiles')
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function () {});
})();

// ── Back to the last filtered view ──
// /, /audiobooks remember their filter/sort/search query (viewstate.js). A plain
// link to one of them — the sidebar entry, a series page's "← Library" — goes
// back to that view instead of resetting it; a link that already carries a
// query (an author's /?q=Name) is left alone. Delegated, so links a page
// renders later are covered too; modified clicks keep the browser's default.
(function () {
  if (!window.ViewState) return;
  function restored(a) {
    var path = a.getAttribute('href');
    if (path !== '/' && path !== '/audiobooks') return null;
    var qs = ViewState.saved(path);
    return qs ? path + '?' + qs : null;
  }
  document.querySelectorAll('#sidebar a[href]').forEach(function (a) {
    var to = restored(a);
    if (to) a.setAttribute('href', to);   // also right for open-in-new-tab
  });
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var to = restored(a);
    if (to) { e.preventDefault(); location.href = to; }
  });
})();

// ── Segmented pill controls (language, view, availability) ──
// Each .seg[data-for="x"] mirrors a hidden <select id="x"> — the value source
// the page's own filter/render code already reads. Clicking a pill sets that
// select and fires its input event, so no page needs bespoke wiring. No-op on
// pages that have no .seg[data-for].
(function () {
  document.querySelectorAll('.seg[data-for]').forEach(function (seg) {
    var target = document.getElementById(seg.dataset.for);
    if (!target) return;
    seg.addEventListener('click', function (e) {
      var b = e.target.closest('.seg-btn');
      if (!b) return;
      seg.querySelectorAll('.seg-btn').forEach(function (x) {
        x.classList.toggle('active', x === b);
      });
      target.value = b.dataset.val;
      target.dispatchEvent(new Event('input', { bubbles: true }));
    });
  });
})();

// ── Auto-hide the Library/Audiobooks toolbar on mobile scroll ──
// Only pages with a search-box (index.html, audiobooks.html) have a toolbar
// tall enough for this to matter; no-op everywhere else.
(function () {
  var searchBox = document.querySelector('.search-box');
  var header = searchBox && searchBox.closest('header');
  if (!header) return;

  var MIN_DELTA = 8; // ignore momentum/rubber-band jitter
  var lastY = window.scrollY;
  var ticking = false;

  function update() {
    ticking = false;
    if (window.innerWidth >= 640) { header.classList.remove('hide'); return; }
    var y = window.scrollY;
    var delta = y - lastY;
    if (Math.abs(delta) < MIN_DELTA) return;
    if (y <= header.offsetHeight) {
      header.classList.remove('hide');       // always visible near the top
    } else if (delta > 0) {
      header.classList.add('hide');          // scrolling down → hide
    } else {
      header.classList.remove('hide');       // scrolling up → reveal
    }
    lastY = y;
  }

  window.addEventListener('scroll', function () {
    if (!ticking) {
      ticking = true;
      window.requestAnimationFrame(update);
    }
  }, { passive: true });
})();

// ── Mobile toolbar collapse: sort/filter/view behind a button ──
// Search stays visible; .controls (sort, filters, grid/list, language/
// availability) starts collapsed on mobile and opens on tap. No-op on
// pages without .toolbar-toggle (CSS also no-ops above 639px).
(function () {
  var btn = document.getElementById('toolbar-toggle');
  var controls = document.getElementById('controls');
  if (!btn || !controls) return;

  btn.addEventListener('click', function () {
    var open = controls.classList.toggle('open');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
})();
