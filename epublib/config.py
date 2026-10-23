"""The Config dataclass every module reads, and `load()`.

Which file is read, the defaults, the GUI overlay and the HONDANA_* environment
overrides all live in settings.py; this module holds the resolved result. Paths
are absolute by the time they get here, so the rest of the code never worries
about the working directory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# The checkout (parent of the epublib package): templates, static, and the
# config.toml / config.<env>.toml a git checkout may keep next to the code.
ROOT = Path(__file__).resolve().parent.parent
# `cli.py --config X serve` passes the file to the server process through this.
CONFIG_ENV = "HONDANA_CONFIG"


@dataclass
class Config:
    epub_dir: Path
    data_dir: Path
    audiobook_data: Path
    audiobook_dirs: list[Path]
    audiobook_natively: Path
    host: str
    port: int
    natively_delay: float
    natively_min_score: float
    pairing_min_score: float
    xref_min_score: float
    data_mirror_publish: bool
    data_mirror_path: Path | None  # the git clone `cli.py push` copies into; required to publish

    # Which profile's language-learning data is active. Only the three
    # per-user paths below (known_words/known_stats/read_books) depend on it;
    # every other index is shared across profiles. This module-level default
    # ("default", then overwritten once at startup by
    # profiles.ensure_initialized) only matters for the CLI and any code
    # without a request to resolve a per-device cookie from — the server
    # never reads this field directly per-request. Each HTTP request instead
    # gets its own Config via server.py's `_request_cfg`, built with
    # `dataclasses.replace(cfg, active_user=<slug resolved from the device's
    # cookie>)`, so two devices on different profiles never share this value.
    active_user: str = "default"

    # Google Books API key for the EPUB ISBN lookup; "" = lookup disabled.
    # Read from the GOOGLE_BOOKS_API_KEY environment variable (.env).
    google_books_api_key: str = ""

    # Where your Yomitan dictionary zips live, hosted to the iOS reader
    # (dicts.py). None = data_dir/dicts, filled in by __post_init__.
    dicts_dir: Path | None = None

    # Command template that aligns one audio file to the book's text (align_jobs.py),
    # with {audio}, {text} and {out} placeholders; "" = not configured.
    subplz_cmd: str = ""

    # Bonjour advertisement of the server for the iOS app (discovery.py).
    # `discovery_advertise_port` is the port to announce when the app sits behind a
    # proxy (host 127.0.0.1); None = the bind port, and no advert on a loopback bind.
    discovery_enabled: bool = True
    discovery_advertise_port: int | None = None
    # Host name the proxy answers to (TXT `host`), e.g. "books.local"; "" = not sent.
    discovery_advertise_host: str = ""
    # The name the phone lists ("" = the machine's own) and the addresses to announce
    # ([] = this machine's LAN addresses), for when the automatic choice is wrong.
    discovery_name: str = ""
    discovery_advertise_addresses: list[str] = field(default_factory=list)
    # The address phones are told to use (QR code, connect link); "" = the request's Host.
    public_url: str = ""
    # Origins (scheme://host[:port]) besides this server's own allowed to make
    # changes from a browser (auth.origin_ok).
    trusted_origins: list[str] = field(default_factory=list)

    # Where each value came from (default | file | settings | env), the file read
    # (None when there is none) and the home folder defaults sit under.
    sources: dict[str, str] = field(default_factory=dict, compare=False)
    config_path: Path | None = field(default=None, compare=False)
    home: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.dicts_dir is None:
            self.dicts_dir = self.data_dir / "dicts"

    # Derived index paths (under data_dir)
    @property
    def library_index(self) -> Path:
        return self.data_dir / "epub_library.json"

    @property
    def natively_index(self) -> Path:
        return self.data_dir / "natively_epub.json"

    @property
    def pairings_index(self) -> Path:
        return self.data_dir / "pairings.json"

    @property
    def vocab_index(self) -> Path:
        return self.data_dir / "vocab_epub.json"

    @property
    def wordlists_dir(self) -> Path:
        return self.data_dir / "wordlists"

    @property
    def sentences_dir(self) -> Path:
        return self.data_dir / "sentences"

    @property
    def annotations_dir(self) -> Path:
        """Reader packs (annotate.py), one gzipped JSON per JP book."""
        return self.data_dir / "annotations"

    @property
    def alignments_dir(self) -> Path:
        """Audio↔text sync (align.py): the raw SRT per book plus the derived map."""
        return self.data_dir / "alignments"

    # ── Per-user files: namespaced under data/users/<active_user>/ ──────────
    @property
    def profiles_index(self) -> Path:
        """Global registry of profiles + the active slug (not per-user)."""
        return self.data_dir / "profiles.json"

    @property
    def epub_catalog_extra(self) -> Path:
        """Genre/description pulled by ISBN for EPUBs."""
        return self.data_dir / "epub_catalog_extra.json"

    @property
    def audiobook_catalog_extra(self) -> Path:
        """Audiobooks pulled by ASIN since the `audiobook_data` scrape."""
        return self.data_dir / "audiobook_catalog_extra.json"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    @property
    def known_words(self) -> Path:
        return self.users_dir / self.active_user / "known_words.json"

    @property
    def known_stats(self) -> Path:
        return self.users_dir / self.active_user / "known_stats.json"

    @property
    def read_books(self) -> Path:
        return self.users_dir / self.active_user / "read_books.json"

    @property
    def reader_learning(self) -> Path:
        """Words the profile is learning in the Hondana iOS reader (reader.py)."""
        return self.users_dir / self.active_user / "learning.json"

    @property
    def reader_lookups(self) -> Path:
        """Append-only log of the reader's dictionary lookups (reader.py)."""
        return self.users_dir / self.active_user / "lookups.jsonl"

    @property
    def reader_progress(self) -> Path:
        """Reading positions synced from the iOS reader, by stable key (reader.py)."""
        return self.users_dir / self.active_user / "reader_progress.json"

    @property
    def reader_audio_progress(self) -> Path:
        """Audiobook positions synced from the iOS reader, by ab_id (reader.py)."""
        return self.users_dir / self.active_user / "audio_progress.json"

    @property
    def anki_note_type(self) -> Path:
        """The note type the iOS reader sends cards to, as the phone described it (anki_notetype.py)."""
        return self.users_dir / self.active_user / "anki_note_type.json"

    @property
    def pairing_overrides(self) -> Path:
        return self.data_dir / "pairing_overrides.json"

    @property
    def series_overrides(self) -> Path:
        return self.data_dir / "series_overrides.json"

    @property
    def genre_overrides(self) -> Path:
        return self.data_dir / "genre_overrides.json"

    @property
    def title_overrides(self) -> Path:
        return self.data_dir / "title_overrides.json"

    @property
    def author_overrides(self) -> Path:
        return self.data_dir / "author_overrides.json"

    @property
    def language_overrides(self) -> Path:
        return self.data_dir / "language_overrides.json"

    @property
    def cover_overrides(self) -> Path:
        """{book_id: {"ext": ".png"}} — books whose cover was replaced by hand."""
        return self.data_dir / "cover_overrides.json"

    @property
    def audiobook_hidden(self) -> Path:
        return self.data_dir / "audiobook_hidden.json"

    @property
    def audiobook_links(self) -> Path:
        return self.data_dir / "audiobook_links.json"

    @property
    def audiobook_link_overrides(self) -> Path:
        """{book_id: audible_isbn | null} — hand-corrected EPUB↔audiobook links."""
        return self.data_dir / "audiobook_link_overrides.json"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def custom_covers_dir(self) -> Path:
        """Hand-uploaded covers. A subdirectory so the scanner's `covers/{id}.*`
        sweep (and a rescan generally) can never touch them."""
        return self.covers_dir / "custom"

    @property
    def audiobook_root(self) -> Path:
        """Canonical audiobook directory (consolidation destination)."""
        if not self.audiobook_dirs:
            raise SystemExit("audiobook_dirs is empty; set it in Settings or config.toml")
        return self.audiobook_dirs[0]

    @property
    def audiobook_files_index(self) -> Path:
        return self.data_dir / "audiobook_files.json"

    @property
    def audiobook_index(self) -> Path:
        return self.data_dir / "audiobook_library.json"

    @property
    def audiobook_tags_cache(self) -> Path:
        """Each audio file's embedded tags, by (size, mtime_ns); rebuildable."""
        return self.data_dir / "audiobook_tags.json"

    @property
    def audiobook_covers_dir(self) -> Path:
        """Covers taken from audiobook tags or a folder's cover.jpg, by fid; rebuildable."""
        return self.covers_dir / "audiobooks"

    @property
    def audiobook_assignments(self) -> Path:
        """{fid: catalog key}: the book a file was matched to by hand."""
        return self.data_dir / "audiobook_assignments.json"

    @property
    def audiobook_metadata_overrides(self) -> Path:
        """{ab_id: {field: value}}: audiobook details typed by hand, over any catalog row."""
        return self.data_dir / "audiobook_metadata_overrides.json"

    @property
    def audiobook_aliases(self) -> Path:
        """{old ab_id: new ab_id}, kept when an audiobook gains a key (audiobook_rekey.py)."""
        return self.data_dir / "audiobook_aliases.json"

    @property
    def consolidation_plan(self) -> Path:
        return self.data_dir / "consolidation_plan.json"

    @property
    def search_db(self) -> Path:
        return self.data_dir / "search.sqlite3"


def load(path: Path | str | None = None) -> Config:
    """The effective configuration: see settings.py for where each value comes from."""
    from . import settings

    return settings.load(path)
