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

There are two kinds of SRT, and the first is the one to make:

- **A transcript** (`subplz gen`): what Whisper heard, at the times it heard it. The server finds
  each line in the book itself. This is the reliable kind.
- **A synced SRT** (`subplz sync --text …`): the book's own lines with times SubPlz chose. SubPlz
  gives a made-up time to every line it can't place, and a short line placed on the wrong speech
  pulls every line after it along. On real books that left the highlight up to four minutes behind
  the voice for pages at a time, while the coverage still read 99.9%. It still
  works, and books aligned this way keep their map until they are redone with a transcript.

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
   transcribe_cmd = "/path/to/subplz-mac/.venv/bin/subplz gen --audio {audio} --output-dir {out} --lang ja --model turbo --mlx"
   ```

   `{audio}` and `{out}` are filled in for every audio file (`{text}` too, for `subplz_cmd`); keep
   them as written. With `transcribe_cmd` set, `align-remote` makes transcripts. The older
   `subplz_cmd` (a `subplz sync --audio {audio} --text {text} --output-dir {out} …` template) is
   used only with `--kind synced`, or when `transcribe_cmd` isn't set.
   On Windows, or anywhere a path has spaces or backslashes, write either command as an array of
   arguments instead of one string: `transcribe_cmd = ["C:\\Tools\\subplz.exe", "gen", "--audio", "{audio}", "--output-dir", "{out}"]`.
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
4. uploads every SRT in one request, and prints the coverage: how many sentences were heard, and
   how many are estimated (timed between two heard ones, for a line Whisper didn't catch). Below
   90% it exits non-zero and keeps the transcripts.

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
- **Redoing** a book that is already synced needs `--force`, except that a book synced from a
  synced SRT is redone by a transcript without it. The server refuses a transcript upload until it
  is new enough to take one (`alignment.accepts` in its status lists `transcript`).
- **If the EPUB changed** on the server between download and upload, the server refuses the
  upload (`book_changed`), because the transcripts were made from the old text. Run the command
  again; it discards them and starts from the new text.
- `--config PATH` uses a full Hondana config instead of `align.toml` (its
  `[alignment] transcribe_cmd`, else `subplz_cmd`). `--kind transcript|synced` picks the command.

## On the server

If the server has a GPU, set `[alignment] transcribe_cmd` in its own config (same template as
above; `subplz_cmd` is used when it isn't set). Then queue a run per book:

```bash
curl -X POST http://my-server.local/api/books/<book_id>/alignment/generate
```

It runs in the background, one book at a time. Progress (`alignment.state`, `step`, `error`)
is in `GET /api/books/<book_id>/status`, and `DELETE` on the same `…/generate` path cancels it. A
failed or cancelled run keeps the files already transcribed, so a retry continues where it
stopped.

## Uploading an SRT you already have

```bash
curl -F file=@book.srt -F kind=transcript http://my-server.local/api/books/<book_id>/alignment
```

`kind` is `transcript` for what Whisper heard, or `synced` (the default) for SubPlz `sync` output.
A "transcript" whose cues are the book's own lines is refused (`bad_srt`): matching those as heard
would keep the times SubPlz made up. A transcript is stored beside a synced SRT and the map is made
from it; `DELETE …/alignment?kind=transcript` goes back to the synced one.

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

- `GET /api/books/<book_id>/status` reports `alignment.state: "ready"`, the coverage, the `source`
  (`transcript` or `synced`) and how many sentences are `estimated`.
- The map itself is at `GET /api/reader/books/<book_id>/alignment`. `GET …/alignment/text?numbered=1`
  lists every sentence as `doc:sentence` and its text, to read a map's rows against.
- In Hondana for iPhone, a synced book's reader shows two extra buttons in its top bar: follow
  (scope) and tap-to-jump (hand). If they are missing, the coverage was below 90% or the map was
  made for a different version of the book.

Transcripts are kept in `data/alignments/` and backed up by `cli.py push`. The map is rebuilt
from them when the book changes, or when the matcher does (`ALIGN_SCHEMA`): deploying a matcher
change re-matches every stored transcript the next time its map is asked for, without re-transcribing.

### How a transcript is matched

Each line of the transcript is looked for in the whole book, on its own, with no idea where the
last one was. Of all the places found, the server keeps the heaviest chain that moves forward in
the book as it moves forward in time. A line that lands in the wrong place then simply drops out,
because keeping it would mean dropping every line it jumps over. Short lines (「はい」) are placed
afterwards, between the lines either side of them.

Whisper and the book often spell the same thing differently. A name is the usual case: the book
writes 仁科, Whisper にしな, and a line that is mostly the name has too little left in common to
find. The server reads these spellings off the lines it did place (where a line and its place in
the book differ by kana against kanji, over and over, that is how Whisper writes it), then looks
for every line again with them applied and keeps whichever text fits the book better. It needs
no name list, so it works for any book. Kanji digits match digits (第二話 and 第2話).

What is still unplaced is mostly one-word lines (「なに？」 is 何? to Whisper, with nothing in
common). When a stretch of up to four sentences sits between two placed ones and has as many
unplaced lines as sentences (or one line for all of them), those lines are that stretch, since
they have nowhere else to go; the time is the line's own, so these sentences count as heard.
A sentence no line covers gets a time between its heard neighbours when that reads at a speaking
pace, and is listed in the map's `est`; otherwise it has none (front matter nobody reads, or music).

### Checking a map against the audio

Coverage only says how many sentences got a time, not whether the times are right. To check them,
run this on the server (it needs the audio, and takes a minute or two of ffmpeg per book):

```bash
uv run cli.py align-audit            # every synced book
uv run cli.py align-audit <book_id>  # one
```

It finds the gaps of five seconds or more between two sentences of a chapter where the narrator is
talking. A good map has few and short ones; a map that lost the voice has dozens. The book page's
Audio sync card shows the count, and offers to redo a map made without a transcript.

### Fixes from the phone

Where a map is still wrong, Hondana for iPhone lets you long-press the follow button, choose Fix
sync and tap the line you are hearing. The phone puts that line at that moment and re-times the
sentences around it until the map agrees again; it also sends the fix to the server, which keeps it
for the book and its audiobook (`data/alignments/<book_id>.anchors.jsonl`, for everyone using the
server) and builds it into the map, so every device gets it. `GET /api/books/<book_id>/status`
counts them in `alignment.anchors`.

### Squeezed stretches (synced SRTs)

Whisper sometimes loses the audio for a minute or two and writes the cues of that stretch packed
into a few seconds, after a hole with no cue at all. On the phone the highlight blanks for the hole
and then races ahead of the voice until the transcript catches up. The matcher repairs this
(`align.retime`): a run of four or more sentences read at over 2.4 times the book's median pace is
spread out, by length, over the time ahead of it that makes its pace human again; where the hole is
longer than the run needs, the run is read at the book's usual pace back from its end and the rest of
the hole stays silent. Sentences with a normal pace are not touched. This is a heuristic, not a new
transcript, so a repaired stretch can still be a second or two off; a run it can't place (no
sentence before it brings the pace back to normal) is left as transcribed. The hondana-ios reader
also stops turning the page by itself while the highlight moves faster than speech can.
