"""Load and resolve configuration from config.toml.

Paths in the config may be absolute or relative to the repo root
(the parent of this package). They are resolved to absolute Path objects here
so the rest of the code never worries about the working directory.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Repo root (parent of the epublib package directory)
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"
EXAMPLE_CONFIG = ROOT / "config.example.toml"
# The server process picks up the active config via this env var (set by
# `cli.py --config X serve`), so --config works for the web server too.
CONFIG_ENV = "EPUB_LIBRARY_CONFIG"
# Selects config.<env>.toml (if it exists) when no explicit path is given, so
# one checkout can serve two setups; defaults to "dev". Otherwise config.toml.
ENV_ENV = "EPUB_LIBRARY_ENV"
DEFAULT_ENV = "dev"


def _select_config_path() -> Path:
    """Pick the active config file, in priority order.

    1. EPUB_LIBRARY_CONFIG  — explicit path (set by `cli.py --config X`)
    2. config.<EPUB_LIBRARY_ENV>.toml  — env-based (default env: "dev")
    3. config.toml  — legacy single-file fallback
    """
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit)
    env = (os.environ.get(ENV_ENV) or DEFAULT_ENV).strip().lower()
    candidate = ROOT / f"config.{env}.toml"
    if candidate.exists():
        return candidate
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    return candidate  # not found → load() raises a helpful error


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from .env into os.environ.

    Optional, gitignored; holds secrets (e.g. GOOGLE_BOOKS_API_KEY).
    Kept as ~a dozen lines of stdlib rather than a python-dotenv dependency.
    Existing environment variables always win.
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
            raise SystemExit("audiobook_dirs is empty; set it in config.toml")
        return self.audiobook_dirs[0]

    @property
    def audiobook_files_index(self) -> Path:
        return self.data_dir / "audiobook_files.json"

    @property
    def audiobook_index(self) -> Path:
        return self.data_dir / "audiobook_library.json"

    @property
    def consolidation_plan(self) -> Path:
        return self.data_dir / "consolidation_plan.json"

    @property
    def search_db(self) -> Path:
        return self.data_dir / "search.sqlite3"


def load(path: Path | str | None = None) -> Config:
    cfg_path = Path(path) if path else _select_config_path()
    if not cfg_path.exists():
        env = (os.environ.get(ENV_ENV) or DEFAULT_ENV).strip().lower()
        raise SystemExit(
            f"Config not found: {cfg_path}\n"
            f"Create it for the '{env}' environment, e.g.:\n"
            f"  cp {EXAMPLE_CONFIG.name} config.{env}.toml"
        )

    _load_dotenv()

    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)

    nat = raw.get("natively", {})
    pair = raw.get("pairing", {})
    xref = raw.get("xref", {})
    mirror = raw.get("data_mirror", {})

    return Config(
        epub_dir=_resolve(ROOT, raw.get("epub_dir", "./books")),
        data_dir=_resolve(ROOT, raw.get("data_dir", "./data")),
        audiobook_data=_resolve(ROOT, raw.get("audiobook_data", "./data/audiobook_catalog_base.json")),
        audiobook_dirs=[_resolve(ROOT, d) for d in raw.get("audiobook_dirs", [])],
        audiobook_natively=_resolve(
            ROOT, raw.get("audiobook_natively", "./data/natively_levels.json")
        ),
        host=raw.get("host", "0.0.0.0"),
        port=int(raw.get("port", 8000)),
        natively_delay=float(nat.get("delay", 0.4)),
        natively_min_score=float(nat.get("min_score", 0.55)),
        pairing_min_score=float(pair.get("min_score", 0.5)),
        xref_min_score=float(xref.get("min_score", 0.62)),
        data_mirror_publish=bool(mirror.get("publish", False)),
        data_mirror_path=_resolve(ROOT, mirror["path"]) if mirror.get("path") else None,
        google_books_api_key=os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip(),
        dicts_dir=_resolve(ROOT, raw["dicts_dir"]) if raw.get("dicts_dir") else None,
        subplz_cmd=str(raw.get("alignment", {}).get("subplz_cmd", "")).strip(),
        discovery_enabled=bool(raw.get("discovery", {}).get("enabled", True)),
        discovery_advertise_port=(
            int(raw["discovery"]["advertise_port"])
            if raw.get("discovery", {}).get("advertise_port") else None),
        discovery_advertise_host=str(raw.get("discovery", {}).get("advertise_host", "")).strip(),
    )
