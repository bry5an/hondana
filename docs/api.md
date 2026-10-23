# HTTP API

Everything the web pages and Hondana for iPhone do goes through a JSON API on the same server.
The running server documents it itself:

- **`/docs`**: interactive Swagger UI, with every route and its request body.
- **`/redoc`**: the same as a reference page.

Request bodies are fully typed there. Responses built from the book and audiobook indices are
shown as generic JSON, because those shapes gain fields over time. `CLAUDE.md` ("API routes")
describes each route's behaviour in detail.

There is no authentication. The reader profile is chosen per client by a `profile` cookie
(`Cookie: profile=<slug>`), falling back to the household default. A request without one acts
for that default profile.

## Overview

| Area | Routes |
|---|---|
| Health | `GET /healthz` |
| Books | `GET /api/books` (all, `?series=` to filter), `GET /api/books/{id}`, `GET /api/books/{id}/study`, `POST /api/books/{id}/read`, `PATCH …/read-date`, `DELETE /api/books/{id}` (deletes the file) |
| Files | `GET /download/{id}` (EPUB), `GET /covers/{id}`, `GET /download/audiobook/{ab_id}[?part=<track>]` (Range supported; a book of tracks without `part` lists its `files`), `GET /covers/audiobook/{ab_id}[?full=1]` (a local audiobook's own cover). An old `ab_id` (before the book gained a key) redirects to the new one |
| Adding books | `POST /api/upload` (multipart) and `POST /api/acquire` (from a URL), both 409 with `existing` for a book you already have; `POST /api/books/{id}/analyze`; `GET /api/books/{id}/status` (the book's pipeline progress) |
| Editing | `GET`/`POST /api/books/{id}/details` (title, author, language, genre, series), `POST`/`DELETE …/cover`, `GET`/`POST`/`DELETE …/audiobook-link`, `GET /api/genres`, series-wide `POST /api/books/genre`, `/api/books/series`, `/api/series/rename` |
| Editions | `GET /api/books/{id}/edition`, `POST`/`DELETE …/edition/none`, `GET /api/pairing/suggestions`, `POST /api/pairing/link` / `unlink` / `block` / `unblock` |
| Audiobooks | `GET /api/audiobooks`, `GET /api/audiobook-catalog/search?q=`, `POST /api/audiobooks/hide` / `unhide`, `GET /api/audiobooks/hidden`, `DELETE /api/audiobooks/{ab_id}` (deletes the file) |
| Search | `GET /api/search?q=&kind=epub\|audiobooks\|catalog` → `{source, ids}` |
| Profiles | `GET`/`POST /api/profiles`, `POST /api/profiles/active`, `GET /api/profile/read-books`, `DELETE /api/profile/orphan-reads` |
| Known words | `GET`/`POST`/`DELETE /api/known-words` (import: multipart `file` or `text`, `format`, `mode`), `POST …/mark` / `unmark` / `skip` / `unskip`, `GET …/frequent`, `POST …/seed`, `GET …/snapshots`, `POST …/restore` |
| Admin | `GET /api/admin/stages`, `GET /api/admin/status`, `POST /api/admin/run/{stage}` |

## Reader API (Hondana for iPhone)

The phone does no language processing. The server tokenizes each book once and the phone places
the result on the page. These routes exist for it:

| Route | What it returns |
|---|---|
| `GET /api/setup` | The first-run checklist behind the **Getting started** card on `/`: `{steps: [{id, label, required, done, detail, href, link}], complete}` for the requesting profile (EPUB folder, library built, known words; optional audiobooks, iPhone, dictionaries). `complete` is true once every required step is done. Reads existing indices only. |
| `GET /api/connect`, `GET /api/connect/qr.svg` | How a phone connects: `{url, profile, link, lan_substituted}`, where `url` is this request's own `Host` (a `localhost` host becomes the machine's LAN address), and `link` is `hondana://connect?server=…&profile=…`; the second route is `link` as a QR code (SVG). Shown on **Profile**. |
| `GET /api/reader/books/{id}/pack` | The book's **annotation pack**: every token's position (UTF-16 offsets into each spine document's text), its dictionary form, reading and furigana, and the sentence spans. Gzip, with an ETag. 409 `{reason: drm\|not_ja\|no_text}` when there can't be one. |
| `GET /api/reader/vocab` | The profile's known, learning and ignored words plus exposure counts, which colour the words (ETag). |
| `POST /api/reader/events` | The phone's outbox: word status changes, lookups and reading positions, replayed in batches of up to 1000. Idempotent. |
| `GET /api/reader/progress` | The profile's reading positions, by the book's stable key (`progress`), and where it is in each audiobook, by `ab_id` (`audio`). |
| `GET /api/reader/dicts`, `GET /api/reader/dicts/{file}` | The Yomitan dictionary zips in `dicts_dir`, with a manifest. |
| `POST /api/admin/dicts`, `DELETE /api/admin/dicts/{file}` | Add a Yomitan zip (multipart `file`, up to 1 GB; 400 unless it is a `.zip` with an `index.json`; a zip of the same name is replaced) or delete one from the manifest. Behind the Admin page's Dictionaries card. |
| `GET /api/reader/books/{id}/alignment` | The sentence → time map for audiobook sync ([audio-sync.md](audio-sync.md)). |
| `PUT /api/reader/anki-note-type` | The Anki note type the phone uses, which Profile then offers as an `.apkg` for desktop Anki. |

The pack format, the vocab snapshot and the event rules are specified in `CLAUDE.md`
("Reader API"). Bump `annotate.ANNOTATE_SCHEMA` when the pack's shape changes.

## Audio sync

`GET /api/books/{id}/alignment/text`, `POST`/`DELETE /api/books/{id}/alignment` (SRT upload),
`POST`/`DELETE /api/books/{id}/alignment/generate` and `GET /api/reader/books/{id}/alignment.srt`.
`GET /api/books/{id}/status` carries `alignment: {state, coverage, ok, step, error, eligible, can_generate}`:
`eligible` is a Japanese book whose audiobook is on the server, `can_generate` that `subplz_cmd` can run
there; the book page's Audio sync card reads both. `GET /api/admin/alignment-queue` lists the eligible books
with no sync yet (`{books: [{book_id, title, author, runtime_min, files}], total_min, can_generate}`), which
Admin shows. See [audio-sync.md](audio-sync.md).
