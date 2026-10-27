# Style guide

How the UI looks and why. The look is **Playful Shelf** (design direction 1b): a deep-purple
sidebar, a cream canvas, white rounded "shelf" cards with a hard drop shadow, sticker badges,
pastel chips and a coral gradient for anything primary. It has a light theme (default) and a
dark one.

**This doc is prescriptive.** Where the code predates a rule, the code is wrong, not the doc.
Existing deviations are tracked as issues from the style audit, not listed here. Before adding
UI, read the section you need; when you add a new pattern, add it here in the same PR.

Mechanics (where CSS lives, cache-busting, `ui.js` helpers, include partials) are in
`CLAUDE.md` → *Key files* → `static/` and `templates/`. This doc is about what things
should look like.

Contents: [1 Principles](#1-principles) · [2 Tokens](#2-tokens) · [3 Typography](#3-typography) ·
[4 Shape and elevation](#4-shape-and-elevation) · [5 Page layout](#5-page-layout) ·
[6 Cards](#6-cards) · [7 Buttons](#7-buttons) · [8 Chips and badges](#8-chips-and-badges) ·
[9 Forms](#9-form-controls) · [10 Reading-load language](#10-reading-load-colour-language) ·
[11 Dark theme](#11-dark-theme) · [12 Feedback and overlays](#12-feedback-and-overlays) ·
[13 Icons and motion](#13-icons-motion-accessibility) · [14 Checklist](#14-new-ui-checklist)

---

## 1. Principles

1. **Tokens, not hex.** Surfaces, text and borders come from the custom properties in §2 so one
   rule works in both themes. A literal colour is allowed only for the semantic palettes in §8/§10
   (and each needs a dark pair).
2. **One component per job.** Download buttons are `dlEpub()`/`dlAudio()`, dialogs are
   `confirmDialog()`/`promptDialog()`, a delete is behind the ⋯ menu. Never hand-roll a second
   version; extend the shared one (see `ui.js`).
3. **Coral means "do this".** The coral→pink gradient marks the primary action on a surface and
   active/selected states. Nothing else is coral-filled, so it stays a signal.
4. **Soft and tactile, not flat.** Generous radii, 2px borders, a hard (zero-blur) shadow under
   cards. Blurred shadows are only for things that float (menus, toasts, stickers).
   The cream canvas (`--bg` `#fbf6ec`) is the brand, not an accident: ignore the design
   detector's `cream-palette` finding. Brand marks use the hard ledge, never a coloured glow.
5. **A phone is a first-class screen.** One viewport breakpoint, `max-width: 639px`; design the
   narrow layout, don't just let the wide one wrap. A component that runs out of room *before* the
   phone breakpoint — because the sidebar and page padding take ~300px of a laptop window — adapts
   to its own width with a container query (`/next`'s hero, §5), never a second viewport breakpoint.
6. **The reader's data is the colour.** Green/amber/pink/red encode reading difficulty (§10);
   don't spend them on decoration.

## 2. Tokens

Defined in `app.css` `:root`; `body.dark` overrides the surface ones. Use these names.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#fbf6ec` cream | `#211d2b` | Page canvas, header |
| `--panel` | `#ffffff` | `#2b2637` | Cards, menus, inputs on the canvas |
| `--panel2` | `#f3ece0` | `#332c46` | Wells inside a card: table stripes, hover fills, tracks, readout panel |
| `--text` | `#2c2340` ink | `#f4eff6` | Body and headings |
| `--muted` | `#766752` | `#b6acc6` | Secondary text, labels, placeholder glyphs (≥4.5:1 on `--panel`, `--panel2` and `--bg`) |
| `--border` | `#efe4d2` | `#3b3450` | Every hairline and card border |
| `--card-shadow` | `#efe4d2` | `#17131f` | The hard shelf shadow colour |
| `--accent` | `#ff6d9e` pink | same | Focus rings, selected borders, fills, small accents — **not text** on a light surface (2.65:1) |
| `--accent-ink` | `#c2255c` | `#ff6d9e` | Accent as *text*: links in a callout or hint |
| `--on-accent` | `#2c2340` ink | same | Text and glyphs on the coral gradient or an `--accent` fill |
| `--accent1` → `--accent2` | `#ff9e6d` → `#ff6d9e` | same | The coral gradient (always `135deg`) |
| `--sidebar-bg` | `#2c2340` | same | Sidebar and the dark profile hero; purple in both themes |
| `--ja` / `--en` | amber `#a06a1f` / green `#2f7a55` | `#e0a94a` / `#7fbf8f` | Language ink |
| `--sidebar-w` | `238px` | | Sidebar width |

Rules:
- Text on `--panel`/`--bg` is `--text` or `--muted`. Never `#000`, never grey literals.
- **Text on the coral gradient (or an `--accent` fill) is `--on-accent` ink**, in both themes: white on
  coral is only 2.0–2.65:1, ink is 5.6–7.3:1. This covers primary buttons, the EPUB download, the
  brand and nav-icon tiles, the Known-words card, the count badge and avatars.
- Text on the sidebar or the dark hero is white (`#fff`, or `rgba(255,255,255,.55–.85)` for
  secondary).
- **Status colours are tokens.** `--ok` `#7ec699`, `--warn` `#e2b96f`, `--danger` `#f28b82` are for
  fills, borders, dots and bars (same in both themes). **Danger text** uses `--danger-ink`
  (`#b04a5c` light, `#f28b82` dark) so it stays readable on a light surface; **ok text or glyphs** use
  `--ok-ink` (`#2d7451` light, `#8fd6ac` dark, the §10 green ink). Never write the
  literals again. The pastel chip/tile pairs in §8/§10 are a separate palette and keep their own hex.
- Need a tint of a colour? `color-mix(in srgb, <colour> N%, var(--panel))`, not a new hex.
- **Increase Contrast** (the web doesn't read `prefers-contrast` yet; the iOS app,
  [hondana-ios](https://github.com/bry5an/hondana-ios), does). The status fills are soft: on a light card
  `--ok`, `--warn` and `--danger` are 2.0, 1.8 and 2.4:1, under the 3:1 a mark needs. With Increase Contrast
  on, the light theme swaps in the darker fills below (≥3:1 on `--panel` and `--panel2`); dark already clears
  3:1 and keeps its own. The iOS app's `scripts/gen_colors.py` reads this table.

  | Token | Light, Increase Contrast | on `--panel` / `--panel2` |
  |---|---|---|
  | `--ok` | `#2a8352` | 4.7 / 4.0:1 |
  | `--warn` | `#a56f08` | 4.3 / 3.7:1 |
  | `--danger` | `#cf3f3a` | 4.8 / 4.0:1 |

## 3. Typography

Fonts load from Google Fonts in `partials/head.html` (not a CSS `@import`).

| Role | Family | Used for |
|---|---|---|
| Display | **Baloo 2** (700/800) | `h1`, card headings, big numbers, button labels on `dl-btn`, brand |
| Body | **Hanken Grotesk** (400–800) | Everything else; falls back through Hiragino/Noto Sans JP |
| Japanese titles | **Noto Sans JP** | Book/series titles and any Japanese display text. A Baloo stack names it right after Baloo (`'Baloo 2', 'Noto Sans JP', 'Hanken Grotesk'`) so Han glyphs never fall back to a Chinese font; long titles get `text-wrap: balance` and, in Japanese, `word-break: auto-phrase` |
| Label | **Space Mono** (700) | Tiny uppercase labels, counts, readout captions, level/lang pills |

Scale (rem; the page root is 16px):

| Step | Size | Where |
|---|---|---|
| Page title `h1` | 1.6rem Baloo 700 | Header |
| Hero title | 1.5–1.7rem Baloo 800 (1.25rem on phones) | Night-hero cards |
| Card heading `h2` | 1.05rem Baloo | `.stats-card`, `.add-card`, `.admin-card` |
| Body | 0.85–0.9rem | Table rows, form values |
| Secondary | 0.78–0.82rem, `--muted` | Hints, author lines, captions |
| Micro / chip | 0.62–0.72rem | Chips, badges, stat labels |
| Mono label | 0.66–0.72rem Space Mono, `letter-spacing` .06–.08em, uppercase | Section captions inside cards (never smaller: 0.53rem was ~8.5px; the Edit-dialog field labels are migrated to 0.69rem, and so are the admin ones). On `/book` the reading-load labels and units are 0.7rem, clear of the 11px floor |

Rules: size in `rem`, never `px` (the few `px` in the sidebar are the exception). Weight 700 for
anything interactive, 600 for form values, 800 only in Baloo. Titles never use `Space Mono`.

## 4. Shape and elevation

**Radius scale — use only these:**

| Radius | For |
|---|---|
| `8px` | Dense/xs controls: table-row buttons, menu items, small tracks |
| `12px` | Inputs, selects, covers, inner tiles, toasts, menus, popovers |
| `16px` | Inner panels and large buttons: readout panel, `lg` buttons, toolbar search |
| `18px` | Cards and containers (the shelf radius) |
| `22px` | Night-hero cards |
| `20px` / `999px` | Pills: buttons, chips, segmented controls, count badge |
| `50%` | Dots and avatars |

Borders: containers and inputs are **2px** `--border`; list rows and table rules are **1px**.
Dashed borders mean "empty / not set / staged" (no audiobook, dropzone, unconfirmed level).

Elevation, from lowest:
1. **Shelf** — cards: `0 4px 0 var(--card-shadow)` (3px on small cards). Zero blur, solid colour.
2. **Hero** — `0 4px 0` light / `0 8px 0 #130e20` dark, plus two soft radial glows (`hero.css`).
3. **Sticker** — tilted badges over covers: `0 3px 8px -2px rgba(0,0,0,.35)`.
4. **Float** — menus, popovers, tooltip: `0 8px 24px rgba(0,0,0,.25)`; modals are the largest.

Hover on a clickable card: `translateY(-3px)`, `0.14s`. Don't change the shadow on hover.

## 5. Page layout

```
┌ sidebar 238px (purple) ┬ .main-col ─────────────────────────┐
│ brand                  │ <header> sticky: [☰] h1 [badge] …   │
│ nav tiles              ├─────────────────────────────────────┤
│                        │ .{page}-wrap  padding 24px,         │
│ profile / known / theme│   max-width per page, gap 20px      │
└────────────────────────┴─────────────────────────────────────┘
```

- A page is `head` + `shell` + `header` partials and one `PAGES` entry (see `CLAUDE.md`); never copy
  the shell. Page CSS is `static/<page>.css`, no `<style>` in templates.
- **Content width by page type:**

  | Type | `max-width` | Pages |
  |---|---|---|
  | Form / narrow | 760px | `/add`, `/known` |
  | Reading / detail | 860px | `/book`, `/profile` |
  | Dashboard / lists | 1100px | `/next`, `/series`, `/audiobook-series`, `/pair`, `/admin` (two columns: pipeline, audiobook catalog) |
  | Full-bleed grid | none (`.grid` auto-fill ≥178px) | `/`, `/audiobooks` |

- `/book` stays at 860px on wide screens: its hero, tables and example sentences read best at that
  measure, and the ~260px of empty canvas beside it at 1360px is deliberate, not a missed layout.
  Its chips sit under the title block (`.book-hero .chips { margin-top: 0 }`), never floating in
  the gap above the downloads. Order down the page: hero, synopsis, vocabulary, series, study list.
- Vertical rhythm: **20px** between cards, **22px** inside a card (`14px` on phones), **10–14px**
  between a card's own blocks, **7–8px** between chips/buttons.
- The header is sticky; on `/` and `/audiobooks` it hides on scroll-down on phones.
- **Phone (`≤639px`)**: the sidebar becomes an off-canvas drawer; the grid goes 2-up; heroes keep
  the cover beside the title and push the readout to a full-width row (`/book`'s hero is a grid:
  cover + title block, then chips, downloads and the Read toggle as full-width rows); button rows split equally;
  queue rows become an explicit grid. Breakpoint rules live **in the same file as the base rule
  they override** (a shared file loads before the page file).
- **Container queries** (principle 5): `/next`'s top row is `container: next-top / inline-size`;
  below 820px of container width the readout drops under cover + title, below 620px the phone
  hero applies; its queue (`next-queue`) drops row badges below 820px and stacks rows below 680px.
  Use this for any side-by-side component that collapses between 640 and ~1100px.
- Page-level empty/error states: a `--panel2` well with a dashed `--border` (`.no-vocab`), or the
  status banner (`statusHtml()`), never a bare red line.

## 6. Cards

| Card | Look | Use |
|---|---|---|
| **Shelf card** (`.stats-card`, `.add-card`, `.admin-card`, `.card`) | `--panel`, 2px `--border`, 18px radius, shelf shadow, 22px padding | Any self-contained block on a page |
| **Night hero** (`.book-hero`, `hero.css`) | Light: panel→blush gradient; dark: purple gradient (`#43306d→#2a2142`); 22px radius, glows, cover left, white-on-dark text in dark | The one headline object on a page: `/book`, `/next` top pick, `/series`. `/profile`'s hero is the same idea on `--sidebar-bg` (`.profile-hero`) |
| **Readout panel** (`.readout`) | `--panel2` well (translucent white in dark), 16px radius, 14px padding, inside a hero | `/book` and `/series`: stat tiles (§10 colours) + coverage meter or strip. `/next`: the strain verdict as a word in its bucket ink plus one plain sentence, the load bar with legend, and the new-word count — no tiles. `/book` and `/add` draw the same readout from `loadReadoutHtml(k)` (`ui.js`): verdict, load bar, To 98% and Known; a page needing it calls that, never a copy. The per-volume strain strip is `strainStrip(vols)` (`/series`, and `/add` when most of a batch is one series) |
| **Queue row** (`.queue-row`) | `--panel`, 1px border, tight, cover 40×54 | Ranked/volume lists. On `/next` a band is one shelf card and its rows are 1px-ruled lines inside it, not a card each |
| **Callout** | `--panel2` + dashed `--border`, 12px radius | Notes, empty states, suggestions |

- A card has an `h2` (Baloo 1.05rem, `margin: 0 0 6px`) and, under it, an optional `.hint`
  (0.78rem `--muted`, `margin: 0 0 14px`).
- A card whose destructive action is rare puts it behind a ⋯ (`moreBtn()` + `openMenu()`) at the
  end of its heading row (`.card-head`: h2 left, a 34px round ⋯ right), hidden while there is
  nothing to act on. `/known`'s "Manage list" card holds Clear this way.
- Covers are `2/3` (grid, `12px` radius) or `3/4` (hero, `168px`, big soft shadow). No cover → a
  Material "menu_book" glyph in `--muted` on `--panel2`. A cover that fails to load gets the same glyph (`coverGone()` in `add.js` for `/add`'s `onerror`), never an emptied box.
- Only one hero per page. Don't put a shelf card inside a shelf card; use a well (`--panel2`).
- Tables inside cards: `.stats-table` — label column `--muted`, value column right-aligned and
  `tabular-nums`, 1px `--border` row rules, section rows in accent small-caps.

## 7. Buttons

Four tiers. Pick by importance, not by what the neighbours look like.

| Tier | Look | Class(es) | Use |
|---|---|---|---|
| **Primary** | Coral gradient, `--on-accent` ink 700 text, no border, **pill** (`20px`) | `.submit-btn`, `.dl-btn--epub`, `.stage-btn.full`, `.bd-save`, `.profile-create` | The one action the page exists for. **One per surface.** |
| **Secondary** | `--panel` fill, 2px `--border`, `--text`, pill (`20px`); hover `--panel2` / border → `--accent` | `.btn`, `.dl-btn--audio`, `.ab-btn`, `.read-btn`, `.stage-btn` | Everything else clickable |
| **Quiet** | No border or fill, `--muted` → `--text` on hover; underline for in-text | `.ghost-btn`, `.link-btn`, `.bd-reset` | Cancel, reset, "show more" |
| **Danger** | Secondary shape, `--danger` ink and border, tinted fill on hover. Always behind confirmation | `.btn-danger`, `.danger-btn`, `.row-menu-item.danger` | Delete, remove, clear |

Sizes: **lg** — the hero's EPUB/audiobook buttons (`.dl-btn` default: 16px radius, Baloo 800,
primary adds a `0 4px 0 #d94f82` hard shadow); **md** — the default (`0.85rem`, `6px 12px`
padding, pill); **sm** — queue/runner-up rows (`0.72rem`); **xs** — table rows (`0.7rem`, 8px
radius, 1px border, icon-only allowed).

States: hover brightens the primary (`filter: brightness(1.06)`), tints secondary with `--panel2`;
`:disabled` is `opacity .45` with `cursor: default`; keyboard focus shows a 2px `--accent` ring (§13).
A busy button changes its label ("Saving…") and disables itself; it does not show a spinner.

Rules:
- **Download buttons come only from `dlEpub()` / `dlAudio()`.** Variants: `--epub` (filled), `--audio`
  (local M4B), `--audible` (external, trailing ↗ icon), `--none` (dashed, "No audiobook"). The
  audiobook slot is always rendered so rows line up. Over the dark hero, outlines go white: the hero
  carries `.on-hero`, which turns the audiobook buttons and the Mark as read toggle into white outlines
  (`app.css`).
- **A download repeated down a list is quiet** (`tone: "quiet"` → `.dl-btn--quiet`): secondary
  look (`--panel`, 2px `--border`, accent border on hover), the EPUB glyph kept in `--accent`, and
  "No audiobook" as plain muted text without the dashed outline. The filled coral EPUB is for the
  one book a page is about (a hero). Pass `label` (the book title) so every link has its own
  accessible name.
- A text button with an icon uses a leading Material glyph and a single space.
- Toggle buttons show state by fill: off = secondary, on = accent text/border (`.read-btn.on`) or
  coral fill for the page mode toggle (`.manage-btn.active`).
- **Edit mode controls** are one `.edit-group` pill of segmented buttons (`--panel`, `--border`,
  accent on hover) at a card's top-right or a row's end; destructive actions live behind one ⋯
  `popover` menu (`moreBtn()` / `openMenu()`), never next to an edit button.
- Segmented/toggle controls on the toolbars: `.seg` + `.seg-btn` (`.active` is coral gradient);
  dropdown pills `.sort-pill`; filter toggles `.chk`.
- `.seg` also makes **tabs** when a page shows one of several lists at a time: `/next`'s band
  switcher (`.band-tabs`, `role=tablist`, a `.band-count` chip per tab, sticky under the header).
  Prefer it to stacking long collapsible sections; paging inside a tab appends (`.ghost-btn` "Show
  10 more"), never folds back. `/add` uses it for *what you are adding* (Book | Audiobook,
  `.add-tabs`, `#audiobook` in the URL): each panel (`.add-panel`) is laid out as a page of its own,
  and the audiobook queue reuses the EPUB queue's rows (`.uq-*`) rather than defining another.

## 8. Chips and badges

Chips are small pastel pills (`999px`, 0.62–0.72rem, weight 700, 3×9px padding), background +
ink pair, with a **dark pair** for each. Every light ink is >= 4.5:1 on its tint (the strain
inks were 3.7–4.3:1 at this size; the green, amber and pink were darkened, keeping their hues). Meaning is fixed, so reuse the colour for the meaning:

| Meaning | Light bg / ink | Dark bg / ink | Chip |
|---|---|---|---|
| Neutral | `--panel2` / `--muted` | same | default `.chip` |
| Good / known / easy / English | `#dbeee2` / `#2d7451` | `#24402f` / `#8fd6a8` | `.known`, `.strain.s-low`, `.lang-en` |
| Caution / Japanese / moderate–notable | `#f6e3c9` / `#8b5c1b` | `#4a3a24` / `#e0b878` | `.lang-ja`, `.strain.s-some` |
| Hard | `#f7dfe2` / `#a54556` | `#46262d` / `#f0a6b0` | `.strain.s-high` |
| Heavy | `#f4d0cc` / `#a3342b` | `#4a2420` / `#f0b0a6` | `.strain.s-heavy` |
| Read / paired / genre | `#e6dcf2`·`#ece1f5`·`#e7e0f2` / purple | `#352a4a` / `#c9a6f0` | `.read`, `.pair`, `.genre` |
| Natively level | `#d9e8f2` / `#2f6a95` | `#23384a` / `#8fc0e0` | `.level` (dashed + faded when unconfirmed) |
| Needs details (an audiobook known only by its file name) | `#f6e3c9` / `#8b5c1b` | `#4a3a24` / `#e0b878` | `.needs-details` (the caution pair; `audiobooks.css`) |

- Chip budget: **four** per card (language, strain, ✓ known, ✓ Read) so the fixed card height
  holds; everything else goes on the detail page. On a *detail* page the language is implied by the
  title, so it is not shown.
- Strain is a **worded grade** (very low / low / moderate / notable / high / very high / heavy; seven
  words on the four colours below), never a bare percentage; the exact number is in the
  tooltip. Thresholds live in `strainBucket()` only.
- **Stickers** (over a cover): the Natively `Lv N` (tilted −5°…−7°, white, Baloo 800, top-left),
  the series volume count (bottom-left), the audiobook 音 badge. White fill and `--sidebar-bg`
  ink in both themes.
- The pink **count badge** (Space Mono on `--accent`) sits beside a page title.
- **/admin's own pills** (`admin.css`; not the card chips above): `.stage-chip` is the neutral
  0.7rem pill under a pipeline stage (`.warn` = the caution pair);
  `.stage-running` is the accent-tinted "Running" pill with a 10px `.spinner` that replaces a
  stage's "Last run" line while it is in flight; `.sel-chip` is a pill *button* (border, `--panel2`)
  that shows a count and clears it, e.g. the prune list's "Selected (N) ×".

## 9. Form controls

- Text input / select: `--panel` (or `--panel2` inside a card), 2px `--border`, **12px radius**,
  `10px 13px` padding, `0.85rem` weight 600, min-height 40px. Focus: border → `--accent`, no other
  ring. Placeholder: `--muted` (≥4.5:1; the old warm grey `#b9a98f` / `#8a80a0` was ~2:1). Below 640px a
  textarea or text input is `1rem`, so iOS doesn't zoom on focus.
- Toolbar search: the dotted `.search-box` (16px radius). Selects are native elements under
  a pill wrapper, never a custom listbox.
- Field label: Space Mono 0.69rem uppercase, `--muted`, above the control (`.bd-fld > label`). Helper
  text under it is 0.7–0.8rem `--muted`.
- Radios/checkboxes use `accent-color: var(--accent)`.
- A reset staged but unsaved shows a dashed border and `0.75` opacity. The one exception is the cover
  row (`.bd-cover`, 60px 2:3 thumbnail + a secondary `.btn` + a quiet `.bd-reset`): a file can't be staged
  with the JSON save, so it acts immediately and says "Saved" in `.bd-status`.
- Drop zone: **one shared component** (`.dropzone` + `.dz-*` in `app.css`; `/add`, `/known`). A `<label for>` around an
  `.sr-only` file input, so Tab reaches the picker and Enter/Space or a click opens it with no click handler;
  dashed 2.5px, 18px radius; coral tile with an ink (`--on-accent`) glyph; a dark `.dz-browse` button; border →
  `--accent` on drag; "Drop" only on hover devices (`.only-hover` / `.no-hover`). Put it in a `.dz-host` form so
  the button takes its own row below 540px.
- **Settings fields** (`.set-fld`, `settings.css`): label, control, then a row of small neutral chips saying where the
  value comes from (`.set-src`; "Saved here" in the accent tint, "Set by ENV" dashed and the control disabled,
  "Restart to apply" in the caution pair) with quiet `.link-btn` actions (Check folder, Reset), then helper text.
  One Save (primary) per card; a locked field still shows its value.
- Radio groups are a `<fieldset class="field">` with a `<legend>` styled as the field label, never a bare
  `<label>` with no control.

## 10. Reading-load colour language

One four-step palette, used identically for every strain/flow display (chips, strips, tiles, bars):

| Step | Light | Dark | Meaning |
|---|---|---|---|
| Green | `#2d7451` on `#dbeee2` | `#8fd6ac` | Very low / low strain, flow, easy |
| Amber | `#8b5c1b` on `#f6e3c9` | `#e0b878` | Moderate / notable strain |
| Pink | `#a54556` on `#f7dfe2` | `#f0a6b0` | High / very high strain |
| Red | `#a3342b` on `#f4d0cc` | `#f0b0a6` | Heavy strain |

- **Stat tiles** (To 98% / Known, and `/series`' Strain / Read / Known / Flow) are neutral ink on
  a neutral tile (`.readout--load` on `/book` and `/add`, `.readout--neutral` on `/series`): a fixed
  colour per metric could contradict the verdict beside it (green under "High strain").
  The one colour is the verdict word's bucket ink — on `/series`, the Strain tile's value
  (`.stat-val.s-*`). Don't give a tile a fixed colour per metric.
- **Load bar** (`loadBarHtml(k, {legend, size})`, styles in `hero.css`): stacked no-unknown
  `--ok` · one-unknown `--warn` · two-or-more `--danger`, 12px (8px `size: "sm"` in rows and
  runner-ups), rounded track on `--panel2`; the shares are in its accessible name and tooltip.
  It is how a book *feels* to read, so it leads wherever a book is being chosen.
- The 85–100% coverage meter is retired (on `/next` and `/book`): most of a library sits below
  85% known, where it drew an empty bar. Known % and words-to-98% are plain tiles/sentences now.
- A strain verdict word (the readout's `.load-verdict b`, `/next` and `/book`) is inked in its bucket's colour from the table above,
  so the word and its colour always agree.
- A small inline progress bar elsewhere (`.mini-meter`) is `--accent` on `--panel2`.
- Natively level is **not** part of this palette; it is blue (§8) because it is a human rating,
  not a measurement against your vocabulary.

## 11. Dark theme

- Toggled by `body.dark` (`nav.js`; follows `prefers-color-scheme` until the user chooses; the
  inline init in `shell.html` prevents a flash). Tokens flip; the sidebar, the white active-nav
  pill and sticker badges stay the same in both.
- Write the light rule with tokens; add a `body.dark` rule **only** where a literal colour is
  involved (a pastel fill, a gradient, a hard-coded shadow). If you find yourself writing a dark
  override for a token-based surface, the light rule used a literal.
- Dark surfaces are deep purple, not black. Pastel fills become dark tints with a light ink
  (see §8). Shadows get darker (`#17131f`, `#130e20`), never lighter.
- On the dark hero, text is white, secondary `rgba(255,255,255,.55–.7)`, wells
  `rgba(255,255,255,.07–.14)`, outline buttons `rgba(255,255,255,.6)`.
- Test every UI change in **both** themes before opening a PR.

## 12. Feedback and overlays

| Pattern | Use | Look |
|---|---|---|
| **Toast** `toast(msg, "ok"|"error", {action})` | Non-blocking result. Pass `action: {label: "Undo", run}` when the change is safely reversible: prefer it to a confirm (§7); also pass `anchor` (the element the reader just used) so the Undo is the next Tab stop, not the last one on the page | `--panel`, 2px border, 6px coloured left edge (green/red), 12px radius, bottom-right. With an action: one quiet underlined `--accent-ink` button at the end, 8 s, held while hovered or focused; a failing `run` shows its message as an error toast |
| **Dialog** `confirmDialog` / `promptDialog` | Confirm a destructive action; short text entry | Native `<dialog>`, `.modal-box` (shelf card), 65% black backdrop; focus starts on Cancel for destructive |
| **⋯ menu** `openMenu` | Row/card actions | Native `popover`, `--panel`, 1px border, 12px radius, float shadow; danger items last, in red |
| **Status banner** `statusHtml(type, html, glyph?)` | Page-level success/error/pending | Tinted well (10% colour into `--panel`), 1px 30% border |
| **Status banner, `warn` tone** (`/add`) | The thing was done, but with notes: a book added whose Natively lookup or genre fetch failed | Same well as ok/err in amber; red stays for "not done". A queue row takes the same tone (`.uq-warn`) |
| **Neutral well** (`/add` duplicate, `/book`'s other-edition prompt) | Something that needs a decision but isn't wrong: the book you already own, with "Open it" / "Add anyway"; or a candidate other edition, with Yes (secondary, never coral) / Not this one / No other edition. The heading is the question itself, no kicker | `--panel`, 2px `--border`, 12px radius; cover thumb + title + meta, `ghost-btn` actions |
| **Inline status** | Next to the control that caused it | 0.8rem, ok green / err red / pending muted; text only |

Never `alert()`, `confirm()` or `prompt()`. Destructive copy names the item and says whether the
file is deleted permanently; reversible actions don't use the red styling.

## 13. Icons, motion, accessibility

- **Icons**: Material Symbols Rounded via `icon("name")` (`.mi`, `1.25em`; `1.1–1.15em` inside
  chips/text buttons). No emoji, no flag emoji, no image icons. A new glyph means re-fetching the
  font subset (see `CLAUDE.md`). The nav tiles use Material icons on a 29px rounded-9px tile.
- **Motion**: `0.12–0.22s` ease on `background`, `border-color`, `transform`, `opacity`. One
  decorative transform: card hover lift.
  The sidebar adds the sticker family (§8): the nav tiles tilt −6° (the current page's at rest, the
  others on hover/focus). The **Known-words card** is the learner's badge, so it is the loudest thing
  in the sidebar: coral gradient with a top-left glow, the shelf's hard `0 4px 0 #d94f82` ledge, the
  count in Baloo 800 40px with a white offset lip, and a ghosted `spellcheck` glyph (no progress bar: the card is label + number). It **reacts when its count rises** (`nav.js`
  `showKnownCount`): the number rolls up, the card bumps, and a white "+N" sticker is pressed onto its
  corner for ~2s (quick marks add onto one sticker); each ten passed by hand-marking (a rise of ≤100 words,
  so an import stays silent) is named on the label ("Passed 3,110") and a band of light sweeps once across the
  card; each hundred does it louder: "Milestone: 3,100", the light sweeps twice, and the card swells
  past a plain bump while a ring of light spreads off its ledge. The card reacts on the *click*, not the save (`mark.js`
  sends a `known-words-delta` event; the server's total settles any difference afterwards, and an
  un-mark or failed save takes the count and the live sticker back). The first load, a drop and a no-change never animate. The
  theme toggle's new icon turns into place. All of it is off under `prefers-reduced-motion` (the "+N"
  sticker still appears, still). No page-load animation, no autoplay. Never animate
  `width`/`padding`/`flex-basis` (the desktop sidebar collapse is instant for this reason).
- **Focus**: every interactive element needs a visible `:focus-visible`: a 2px `--accent-ink` ring (coral
  is only ~2.5:1 on the cream canvas). Native `<dialog>`/`popover` supply focus management; keep it.
- **Hiding**: toggle the `hidden` attribute (`el.hidden = true`). `app.css` makes `[hidden]` always win
  (`display: none !important`), so a component's own `display: flex` can't keep it on screen. Never
  restate `.thing[hidden] { display: none }` per selector; that patch is what let `/known`'s empty
  "Set aside" card show.
- **Names and state**: a control repeated down a list carries its row in its accessible name
  (`markButton(word)`), a toggle says its state with `aria-pressed`, a disclosure with `aria-expanded`,
  and a status line that changes is `role="status"`. Every form control has a `<label for>`. Text in
  another language than the page's gets `lang` (`langAttr(text)` in `ui.js`).
- **Targets**: ≥ 32px tall on phones for anything tappable; icon-only buttons need `title`/`aria-label`. Inline links in a hero are
  padded to it (`display: inline-block` with block padding and an equal negative margin).
- **Contrast**: body text is `--text` on `--panel`/`--bg`; `--muted` is for secondary text only
  (≥ 0.72rem) and never for something the user must read to act.

## 14. New-UI checklist

- [ ] Colours are tokens (or a §8/§10 pair with a dark counterpart).
- [ ] Radii are from the §4 scale; borders 2px (containers) / 1px (rows).
- [ ] Exactly one primary button per surface; secondary/quiet/danger tiers used correctly.
- [ ] Download/edit/delete use the shared components.
- [ ] Page uses the standard shell, a §5 width, 20px card gap.
- [ ] Checked at ≤639px and ≥1100px, in light **and** dark.
- [ ] Phone rules live next to the base rules they override.
- [ ] Icons from `icon()`, no emoji; interactive elements have focus states and labels.
- [ ] New pattern documented here.
