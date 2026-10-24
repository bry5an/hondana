# Hondana 本棚

**A self-hosted library for reading Japanese.** Point it at your folder of EPUBs (and, if you
have them, your audiobooks). Hondana works out how hard each book will be **for you**, by
measuring it against the words you already know. It tells you what you can read next, and gets
the book onto your phone.

- **Your own difficulty, not a generic score.** Every Japanese book is tokenized in full and
  compared with your known-words list (imported from jpdb or Anki, then grown as you read). Each
  book gets a **strain** rating, the share of sentences with two or more words you don't know,
  plus your Known % and how many new words would take you to 98% coverage.
- **Read Next** ranks your unread books from comfortable to "study first", one book per series.
- **Study lists**: a book's most useful unknown words, with example sentences from it. Tick off
  the ones you already know.
- **Editions and audiobooks are linked**: a Japanese book and its English translation, and each
  book with its audiobook (local file or Audible).
- **[Hondana for iPhone](https://github.com/bry5an/hondana-ios)** reads your books offline. Words
  you don't know are underlined and get furigana, you can tap any word to look it up in your own
  dictionaries, and the audiobook can follow along with the text. Every word you mark syncs back
  here.
- **A household, not just you**: each reader has a profile with their own known words and
  numbers, chosen per device, with no passwords. English learners work too.

It runs on your own computer or home server and is reached from any browser on your network.
There is no account, no cloud, and nothing to pay for.

## What's in it

| Page | What you do there |
|---|---|
| **Books** | Your library as covers or a table: filter, sort by strain, Known % or Natively level, search titles in kana or romaji |
| **Read Next** | The book to pick up now, then the rest in bands: *Comfortable now*, *A stretch*, *Study first* |
| Book page | The reading-load verdict, study list, downloads, mark as read, fix the details |
| Series page | Every volume with its strain, and which volume to read next |
| **Audiobooks** | Your audiobook collection by series, with downloads or Audible links |
| **Editions** | Japanese/English pairs, and the books waiting for you to confirm one |
| **Add Book** | Drop EPUBs in; each is filed, analyzed, and given its verdict in seconds |
| **Known words** (sidebar) | Import your deck, review the library's most common unknown words |
| **Profile** (sidebar) | Your counts, the books you've read, and the profile switcher |
| **Admin** | Run the pipeline, prune the audiobook list |

## What you need

- A Mac or Linux machine that stays on (a home server or an old laptop works), with
  [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed. `uv` installs the
  right Python for you.
- **DRM-free EPUBs.** Books with DRM are listed, but they can't be analyzed or read in the app.
- Optional:
  - **Audiobooks**: `.m4b` files or folders of `.mp3`s, with the Audible ASIN in the name
    (`… [B0CCN9M612].m4b`).
  - **Yomitan dictionaries**, for looking words up in the iPhone app.
  - **An iPhone** on iOS 26 or later, and a Mac with Xcode to install the app on it.

## Quick start

Two ways to run it:

- **Docker** (a home server or NAS): copy [`docker/compose.yaml`](docker/compose.yaml), set your
  folders, `docker compose up -d`. See [docs/docker.md](docs/docker.md).
- **From source** (any Mac or Linux machine), below.

```bash
git clone https://github.com/bry5an/hondana.git
cd hondana
uv sync
cp config.example.toml config.toml
```

Open `config.toml` and set at least:

```toml
epub_dir = "/path/to/your/EPUBs"                 # scanned recursively
audiobook_dirs = ["/path/to/your/Audiobooks"]    # or [] if you have none
```

Then start it:

```bash
uv run cli.py serve
```

Open **http://localhost:8000**, or `http://<this-computer>.local:8000` from another device.

1. **Build your library.** Go to **Admin** and press **Run full pipeline**. The first run reads
   every book, which takes a second or two per Japanese book. Re-runs only do new work.
2. **Tell it what you know.** Open **Known words** in the sidebar and import a jpdb CSV or an
   Anki export (or paste a list). No deck? **Seed common words** starts the list from the most
   frequent words, and you correct it from there.
3. **Pick a book.** **Read Next** now ranks your library against your own words.

Your first profile is called "Reader". Add your own (and one for each person in the household)
from the profile menu in the sidebar.

## Everyday use

- **Add books** on **Add Book**: drop files or paste a URL. Or copy them into your EPUB folder
  and run **Scan EPUBs** on Admin. Either way each new book is analyzed automatically.
- **Choose** on Read Next, or browse Books and a series' page. A book's page tells you what
  reading it will feel like and which words to learn first.
- **Read** in Hondana for iPhone, or download the EPUB (tap **EPUB** on your phone to save it to
  Files or Books).
- **Mark books read** when you finish. Words you've met often in finished books then count as
  known everywhere else ("exposed vocabulary").
- **Mark words known** from a study list, from **Known words › Review frequent words**, or by
  tapping words in the iPhone app. Every Known % and strain updates right away.
- **Fix what's wrong** with **Edit** on a book page (title, author, series and volume, genre,
  language, cover, audiobook, other edition). Your corrections survive rescans and file renames.

## Hondana for iPhone

[hondana-ios](https://github.com/bry5an/hondana-ios) is the reader that goes with this server. It
downloads books for offline reading and shows:

- your unknown words underlined, with furigana on just those words;
- tap-to-look-up in your own Yomitan dictionaries, with pitch accent and pronunciation;
- one-tap Anki cards;
- the audiobook, with the spoken sentence highlighted.

To connect it:

1. Make sure the server is reachable from the phone: same Wi-Fi, `host = "0.0.0.0"` in the config
   (the default), and your firewall allowing the port.
2. In the app, open **Settings › Server**. The server announces itself on your network, so it should
   appear in the list. If it doesn't, scan the QR code on this server's **Profile** page with the phone's
   camera (it opens the app on your profile), or type the address shown there, e.g. `my-mac.local:8000`
   (not `localhost`). Tap **Test connection** and pick your profile.
3. Allow **Local Network** access when iOS asks.

**Dictionaries.** Hondana doesn't ship any (most aren't redistributable). Add your own Yomitan
zips (Jitendex or JMdict, a monolingual dictionary, Kanjium pitch accents, a frequency list) from
the **Dictionaries** card on the Admin page, or copy them into `data/dicts/` on the server. The
app offers to import them the first time it connects.

## Optional extras

- **Audiobook ↔ text sync.** Makes the iPhone app highlight the sentence being read. It needs a
  transcript of the audiobook, made with Whisper (SubPlz) on a machine with a GPU, usually a Mac
  with Apple Silicon. One command does it: `uv run cli.py align-remote <book title>`. See
  [docs/audio-sync.md](docs/audio-sync.md).
- **Natively levels for audiobooks.** Japanese books get their
  [learnnatively.com](https://learnnatively.com) level automatically. For the audiobook catalog,
  run `uv run cli.py enrich-audiobooks` once (about an hour for a big catalog).
- **Genres and descriptions from Google Books.** Add a free `GOOGLE_BOOKS_API_KEY` to a `.env`
  file (copy `.env.example`) and EPUBs with an ISBN fill in what their files lack.
- **Scattered audiobook folders.** `uv run cli.py consolidate` plans (and with `--execute`
  performs) moving them into one tidy tree.
- **Backups.** Your corrections, profiles and known words are the only things you can't
  rebuild. `uv run cli.py push` commits them to a private git repo. See
  [docs/running-a-server.md](docs/running-a-server.md#backups).

To keep it running all the time (systemd, a reverse proxy, updates), see
[docs/running-a-server.md](docs/running-a-server.md).

## Privacy and security

- Everything stays on your machine. The only outside services it calls are:
  - learnnatively.com, for difficulty levels;
  - Audible Japan's public catalog, for metadata about new audiobooks;
  - Google Books, only if you add a key.

  Nothing you read or mark leaves your network.
- **There is no login.** Anyone on your network can browse, download, edit and delete. Keep it on
  your home network; don't forward its port to the internet.

## Troubleshooting

| Problem | Try |
|---|---|
| A book shows no strain or Known % | It hasn't been analyzed yet (open it and press **Analyze now**), it's DRM-protected or image-only, or you haven't imported known words. The book page says which. |
| The Audiobooks page is empty | A wrong path fails silently. Check `audiobook_dirs` and that the drive is mounted, then run **Scan audiobooks** on Admin. |
| An audiobook isn't linked to its EPUB | Open the book, **Edit › Audiobook**, and pick it. |
| The phone can't connect | Use the computer's name or IP, not `localhost`; check `host = "0.0.0.0"`, the firewall, that both are on the same Wi-Fi, and iOS **Settings › Privacy & Security › Local Network › Hondana**. |
| `address already in use` on start | Another process holds the port. Find it with `lsof -i :8000`, or change `port`. |
| Uploads fail with a 413 behind nginx | Raise `client_max_body_size` (see [docs/running-a-server.md](docs/running-a-server.md)). |

## Documentation

- [docs/](docs/README.md): configuration, running a server, the pipeline, audiobooks, audio
  sync, the API.
- [docs/development.md](docs/development.md) and [CLAUDE.md](CLAUDE.md): working on the code.

## License

Hondana is free software under the [GNU General Public License v3.0 or later](LICENSE). It
bundles or downloads third-party dictionaries, word-frequency data and fonts under their own
licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
