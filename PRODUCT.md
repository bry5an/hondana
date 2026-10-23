# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: a Japanese learner who runs it for themselves**, reading native Japanese books
(largely light novels and other fiction) and listening to Japanese audiobooks they own. They open
the library to decide what they can read *right now* and to get that book onto a device. They
installed it themselves (Docker, a desktop app, or a checkout), so first-run setup has to work
without reading the source.

**Secondary: other household readers**, each on their own profile, e.g. a partner learning
English (`language: "en"`). A profile is a name and a learning language with no password, chosen
per device. Design for the Japanese learner first, but never hard-code the assumption that the
reader is learning Japanese.

## Product Purpose

A self-hosted library for one household's EPUB and audiobook collection. It joins the files on
disk with the Audible catalog, Natively difficulty levels, and a full-text vocabulary analysis
of every book, then scores each book against the reader's own known-words list.

Three jobs are core:

1. **Pick what to read next.** Find a book the reader can handle now, measured against their own
   known words (Read Next, strain, coverage).
2. **Get it onto a device.** Browse, then download the EPUB or the M4B/MP3 audiobook, usually to
   an iPhone through Safari into Files/Books.
3. **Curate the collection.** Fix metadata, pair EN/JP editions, link audiobooks to EPUBs, add
   books, prune the catalog, and run the pipeline.

Vocabulary work (study lists, marking words known, deck imports, the profile page) supports
those jobs by feeding the measure. It is not a core destination in its own right.

Success: the reader opens the app, sees a book they can actually read, and has it on their phone
within a couple of taps. The collection stays correct without hand-editing JSON.

## Positioning

The difficulty here is **personal**, not generic. It is computed from the book's full text
against the reader's own known words, e.g. strain is the share of sentences with two or more
unknown content words, and books are ranked by it. It sits next to the files the household
actually owns, with each book's audiobook and translated edition linked. jpdb, Natively,
Calibre and Audible each cover one slice; none scores your own files against your own deck.

## Operating Context

- Runs on the reader's own machine: a home server, a NAS (Docker) or a desktop computer. It is
  reachable only on the LAN (or a private VPN such as Tailscale), sometimes behind a reverse
  proxy. There is no public access and no multi-tenant concern.
- Tasks split by device. A **phone** is for browsing, choosing and downloading. A **laptop** is
  for curation, admin, bulk edits and pipeline runs.
- The web app does not read or play books itself. EPUBs can open in the phone's Books app,
  audiobooks in a separate player, and books that aren't local link out to Audible. Reading is
  the job of **Hondana iOS** (bry5an/hondana-ios), a native reader that talks to this server over
  the LAN. The server stays the brain: it tokenizes each book once into an annotation pack, holds
  the profile's known, learning and ignored words and reading positions, and hosts the reader's
  own Yomitan dictionaries. The phone only places that on the page, and keeps working offline by
  replaying an outbox of changes when it is back on the network. See "Reader API" in CLAUDE.md.
- Known words come from the reader's jpdb/Anki deck exports, plus words marked known one at a time
  in the app.
- Difficulty levels come from learnnatively.com. Audiobook metadata comes from Audible's public
  catalog by ASIN, plus an optional hand-curated catalog file kept per machine.

## Capabilities and Constraints

- **Pages:** Library (`/`), Read Next (`/next`), book detail, series detail, Audiobooks and
  audiobook series, EN/JP pairing review (`/pair`), Add (`/add`), Known Words (`/known`), Profile
  (`/profile`), Admin (`/admin`).
- **Stack (existing):** FastAPI with plain HTML templates expanded from partials, and vanilla JS +
  CSS with no build step. Shared UI helpers live in `static/ui.js`.
  `docs/STYLE.md` is the prescriptive guide for the existing UI.
- **Bilingual content:** titles, authors and series are mixed Japanese and English. Search matches
  kana and romaji readings, not just the surface text.
- **Terminology the UI already uses:** *strain* (shown as low/some/high/heavy buckets; the exact %
  goes in a tooltip), *flow*, *known %*, *words to 98%*, *coverage*, *exposed vocabulary*,
  *Natively level* (a quick human-judged read; you can sort by it but not filter, and it never
  drives Read Next), *Edit* mode (code name "manage").
- **Curated state is precious.** Overrides, read status and hand-set covers survive rescans and
  file renames. Reversible actions (hide, reset to automatic) are kept separate from destructive
  ones (delete file), and every destructive action needs a confirm step.
- **Scale:** from a handful of books up to a few hundred EPUBs and a few thousand audiobook
  catalog entries, so pages that list the whole catalog have to stay usable at that size.

## Evidence on Hand

- Your own collection under `data/` (the per-book vocabulary indices, Natively levels, the
  audiobook catalog) once the pipeline has run over it.
- The vocabulary metrics were calibrated against jpdb's published numbers for a sample novel
  (see "Vocab analysis" in CLAUDE.md).
- No testimonials, press or usage metrics. Future work must not invent any.

## Product Principles

1. **The reader's own words are the measure.** Personalized reading load outranks generic
   difficulty scores. A generic score can be shown or sorted by, but it doesn't decide what to read.
2. **Every book ends in a file on a device.** A download path (EPUB, local audiobook, or Audible
   link) is always one obvious step away. If none exists, the UI says so instead of hiding the slot.
3. **Design each task for the device it happens on.** Choosing and downloading are phone-first.
   Curation and admin can assume a laptop, but must still work on a phone.
4. **Curation is safe by default.** Prefer reversible actions, keep hand-made corrections across
   every rebuild, and put anything destructive behind a deliberate confirm step.
5. **Many readers, one at a time.** Each profile sees its own numbers in its own language. Nothing
   assumes the reader is whoever installed it, or a Japanese learner.
