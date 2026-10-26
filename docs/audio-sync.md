# Syncing an audiobook with its text

Hondana for iPhone can shade the sentence being narrated, follow along as the audiobook plays,
and start the audio from any sentence you tap. To do that it needs a **sentence → time map** of
the book, which the server builds from a transcript of the audio.

The transcript comes from [SubPlz](https://github.com/kanjieater/SubPlz) (Whisper). That takes
hours on a CPU and a fraction of that on a GPU. So there are three ways to get one:

| Your setup | Use |
|---|---|
| The server has no GPU, but a Mac with Apple Silicon (or another GPU machine) is around | [`align-remote` from the Mac](#from-a-mac-align-remote), the usual case |
| The server itself has a GPU | [Run SubPlz on the server](#on-the-server) |
| You made an SRT some other way | [Upload it](#uploading-an-srt-you-already-have) |

Every path ends the same way: the server matches the transcript against the book's sentences and
reports **coverage**, the share of sentences it could place in the audio. At 90% or more the phone
follows along; below that the map is stored but the phone doesn't highlight.

Only Japanese books with a **local** audiobook linked to them can be synced. A book that is
DRM-protected or has no text can't be.

## From a Mac: `align-remote`

```
server ──sentences + audio──▶ Mac (SubPlz) ──SRT──▶ server matches it to the book
```

One command does the whole trip, from a clone of this repo on the Mac.

### One-time setup

1. Install [subplz-mac](https://github.com/helica1/subplz-mac) (the Apple Silicon fork of SubPlz)
   somewhere outside this repo, following its README, and check that its `subplz` command runs.
2. Tell Hondana how to call it and where your server is, in `~/.config/hondana/align.toml`:

   ```toml
   server = "http://my-server.local"   # your Hondana server

   [alignment]
   subplz_cmd = "/path/to/subplz-mac/.venv/bin/subplz sync --audio {audio} --text {text} --output-dir {out} --lang ja --model turbo --mlx --respect-grouping"
   ```

   `{audio}`, `{text}` and `{out}` are filled in for every audio file; keep them as written.
   On Windows, or anywhere a path has spaces or backslashes, write the command as an array of
   arguments instead of one string: `subplz_cmd = ["C:\\Tools\\subplz.exe", "sync", "--audio", "{audio}", "--text", "{text}", "--output-dir", "{out}"]`.
   `align-remote` reads this file whenever you don't pass `--config`. `--server URL` still
   overrides `server`; with neither, it uses the one Hondana server it finds on the network over
   Bonjour (and asks for `--server` if it finds none, or several).

Nothing needs setting up on the server.

### Aligning a book

See what is waiting, without starting anything:

```bash
uv run cli.py align-remote --missing --dry-run
```

This lists every Japanese book on the server that has a local audiobook and no alignment yet,
with its hours of audio and the download size.

Align one book, by a `book_id` from that list or part of its title. Start with a short one to
check the setup:

```bash
uv run cli.py align-remote 明日をくれた君に
```

It then:
1. asks the server for the book's sentences (a book that can't be synced is refused here, before
   any audio is downloaded);
2. downloads the audiobook into `~/.cache/hondana/align/<host>/<book_id>/`;
3. runs SubPlz on each audio file (once per mp3 for an mp3 folder);
4. uploads every SRT in one request, and prints the coverage. Below 90% it exits non-zero and
   keeps the transcripts.

The Mac stays awake while it runs (`caffeinate`). When the upload succeeds the cache is deleted;
`--keep-audio` keeps the download.

### Doing many books

`align-remote --missing --run` works through the whole list, for example overnight. A book that
fails is reported and the next one still runs. The exit code is non-zero if any failed.

### Good to know

- **Interrupting is safe.** Ctrl-C stops SubPlz and everything it started. Run the same command
  again to resume: downloaded audio and finished files are skipped, and nothing reaches the
  server until every file of the book has a transcript. A failed run never changes an alignment
  the server already has.
- **Audio already on the Mac?** Use `--audio PATH` (repeatable) or `--audio-dir DIR`, matched by
  file name. The copy must be the server's file; an m4b of a different size is refused.
- **Redoing** a book that is already synced needs `--force`.
- **If the EPUB changed** on the server between download and upload, the server refuses the
  upload (`book_changed`), because the transcripts were made from the old text. Run the command
  again; it discards them and starts from the new text.
- `--config PATH` uses a full Hondana config instead of `align.toml` (its
  `[alignment] subplz_cmd`).

## On the server

If the server has a GPU, set `[alignment] subplz_cmd` in its own config (same template as
above). Then queue a run per book:

```bash
curl -X POST http://my-server.local/api/books/<book_id>/alignment/generate
```

It runs in the background, one book at a time. Progress (`alignment.state`, `step`, `error`)
is in `GET /api/books/<book_id>/status`, and `DELETE` on the same `…/generate` path cancels it. A
failed or cancelled run keeps the files already transcribed, so a retry continues where it
stopped.

## Uploading an SRT you already have

```bash
curl -F file=@book.srt http://my-server.local/api/books/<book_id>/alignment
```

For an mp3-folder audiobook, send one SRT per mp3, each followed by its file name:
`-F file=@01.srt -F part=01.mp3 -F file=@02.srt -F part=02.mp3`. Several parts in one request
are stored together or not at all. The response includes the coverage.

## From the web pages

You don't need `curl` for any of this.

- **The book page** has an **Audio sync** card for a Japanese book whose audiobook is on the server. It
  says whether the book is synced (and how much of it matched), shows the step while a run is in
  progress with a Cancel button, and has Remove behind its ⋯ menu. For a book that isn't synced it
  offers **Generate** when the server has `subplz_cmd`, and otherwise the exact `align-remote` command
  to copy and run on a machine with a GPU. A failed run's error and a "Try again" appear there too.
- **Admin** has an **Audio sync queue** card: every Japanese book with a local audiobook and no sync yet,
  with the hours of audio, and the `align-remote --missing --run` command for the overnight run.

## Checking the result

- `GET /api/books/<book_id>/status` reports `alignment.state: "ready"` and the coverage.
- The map itself is at `GET /api/reader/books/<book_id>/alignment`.
- In Hondana for iPhone, a synced book's reader shows two extra buttons in its top bar: follow
  (scope) and tap-to-jump (hand). If they are missing, the coverage was below 90% or the map was
  made for a different version of the book.

Transcripts are kept in `data/alignments/` and backed up by `cli.py push`. The map is rebuilt
from them when the book changes.
