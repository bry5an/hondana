"""The pipeline's one definition.

    inbox → scan → analyze → enrich → pair → audio-scan → catalog → xref → build → epub-catalog

`STAGES` is the ordered registry; `cli.py` (one subcommand per stage, plus `all`) and
the `/admin` panel (buttons, tooltips, the "all" run) are both generated from it, so
adding or reordering a stage is one edit here. `BOOK_CHAIN` is the per-book variant
that `book_jobs` runs for an uploaded/acquired book: the same stages, minus the
library-wide scans, with `xref` narrowed to that book (`xref_one`). `AUDIO_CHAIN` is what
`intake` runs after an audiobook is filed.

Stage bodies look their module functions up at call time (`analyze_mod.analyze`, not
a bound reference) so tests can monkeypatch the module attribute.
"""
from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass

from . import analyze as analyze_mod
from . import annotate as annotate_mod
from . import audiobook_catalog as audiobook_catalog_mod
from . import audiobook_scanner as audiobook_scanner_mod
from . import audiobook_xref as xref_mod
from . import build as build_mod
from . import enrich as enrich_mod
from . import epub_catalog as epub_catalog_mod
from . import pairing as pairing_mod
from . import scanner as scanner_mod
from .config import Config
from .index_store import load_json

# Languages with a vocabulary pipeline (fugashi for ja, spaCy for en).
ANALYZABLE = ("ja", "en")


@dataclass(frozen=True)
class Stage:
    id: str
    label: str          # admin button
    help: str           # one line, for `cli.py --help`
    description: str    # admin tooltip
    run: Callable[..., None]  # run(cfg, refresh=False)
    refresh_help: str | None = None  # set → the stage takes `--refresh`
    refresh_in_all: bool = False     # does `all --refresh` pass it on?
    in_chain: bool = True            # False: a standalone action, not a step of the pipeline
    group: str = "index"             # /admin section: index | audiobooks | finish | maintenance
    chips: tuple[str, ...] = ()      # /admin flags: what the stage touches beyond data/


def _inbox(cfg: Config, refresh: bool = False) -> None:
    from . import inbox as inbox_mod  # intake (which inbox uses) imports this module

    report = inbox_mod.run_once(cfg)
    # Filed into the library, so make them visible now: a full run indexes them again right
    # after (an unchanged file is skipped), but `inbox` on its own would otherwise leave them
    # unseen until the next scan.
    if report.filed_epubs:
        _scan(cfg)
    if report.filed_audio:
        _audio_scan(cfg)


def _scan(cfg: Config, refresh: bool = False) -> None:
    scanner_mod.scan(cfg)
    # scan rewrites every changed entry from scratch, so everything build derives
    # (series, cleaned title, genre, pair, vocab) is gone until build runs again.
    build_mod.build(cfg)


def _analyze(cfg: Config, refresh: bool = False) -> None:
    analyze_mod.analyze(cfg, refresh=refresh)


def _annotate(cfg: Config, refresh: bool = False) -> None:
    annotate_mod.build_all(cfg, refresh=refresh)


def _enrich(cfg: Config, refresh: bool = False) -> None:
    enrich_mod.enrich(cfg, refresh=refresh)


def _enrich_unmatched(cfg: Config, refresh: bool = False) -> None:
    enrich_mod.enrich(cfg, retry_unmatched=True)


def _enrich_audiobooks(cfg: Config, refresh: bool = False) -> None:
    enrich_mod.enrich_audiobooks(cfg, refresh=refresh)


def _pair(cfg: Config, refresh: bool = False) -> None:
    pairing_mod.pair(cfg)


def _audio_scan(cfg: Config, refresh: bool = False) -> None:
    audiobook_scanner_mod.scan(cfg)
    audiobook_scanner_mod.build_index(cfg)


def _catalog(cfg: Config, refresh: bool = False) -> None:
    audiobook_catalog_mod.fetch_missing(cfg)


def _epub_catalog(cfg: Config, refresh: bool = False) -> None:
    epub_catalog_mod.fetch_missing(cfg)


def _xref(cfg: Config, refresh: bool = False) -> None:
    xref_mod.xref(cfg)


def _build(cfg: Config, refresh: bool = False) -> None:
    build_mod.build(cfg)


STAGES: tuple[Stage, ...] = (
    Stage(
        "inbox", "File the inbox", "File new EPUBs and audiobooks from the inbox folder",
        "File what is in the inbox folder into your library. Moves new EPUBs and audiobooks "
        "out of it once they have stopped changing for a minute; anything it can't file, or "
        "already has, goes to the inbox's _failed folder with a note. Does nothing when no "
        "inbox folder is set. It runs first so the stages after it index what it filed.",
        _inbox,
    ),
    Stage(
        "scan", "Scan EPUBs", "Extract OPF metadata + covers, then rebuild",
        "Update the library from your EPUB folder, then rebuild the index. Reads OPF "
        "metadata + covers into epub_library.json. Resume-safe: only new or changed "
        "files are reparsed.",
        _scan,
    ),
    Stage(
        "analyze", "Analyze vocab", "Tokenize JP books → vocab stats",
        "Tokenize each Japanese EPUB and compute its vocabulary stats. Writes "
        "jpdb-style stats (unique words, kanji, difficulty) to vocab_epub.json + "
        "wordlists/. Resume-safe; first run takes minutes.",
        _analyze,
        refresh_help="Re-analyze unchanged books too",
    ),
    Stage(
        "enrich", "Enrich (Natively)", "Natively levels/ratings for JP books",
        "Fetch Natively levels and ratings for Japanese books. Asks learnnatively.com, "
        "rate-limited; can take a while on a full refresh.",
        _enrich,
        refresh_help="Re-fetch all JP books", refresh_in_all=True,
        chips=("Natively",),
    ),
    Stage(
        "enrich-unmatched", "Retry unmatched Natively lookups",
        "Retry Natively misses now, whatever their age",
        "Look up again every Japanese book whose Natively lookup found nothing, now "
        "rather than after the usual two-week wait — e.g. after fixing a title or "
        "series name. A retry never replaces an existing match with a miss.",
        _enrich_unmatched, in_chain=False,
        group="maintenance", chips=("Natively",),
    ),
    Stage(
        "enrich-audiobooks", "Enrich audiobooks (Natively)",
        "Natively levels/ratings for the audiobook catalog",
        "Fetch Natively levels for the audiobook catalog into data/natively_levels.json. "
        "Looks up only entries with no result and retries old misses; a match is "
        "never replaced. Run it after the catalog stage; re-fetching everything "
        "takes about an hour.",
        _enrich_audiobooks, in_chain=False,
        refresh_help="Re-fetch every catalog entry",
        group="audiobooks", chips=("Natively",),
    ),
    Stage(
        "annotate", "Build reader packs", "Pre-build the iOS reader's annotation packs",
        "Tokenize every Japanese book into its reader pack (data/annotations/), so the "
        "iOS reader's first open of a book is instant. A pack is otherwise built on "
        "first request. Skips books whose pack is current; about 1-2 s per book.",
        _annotate, in_chain=False,
        refresh_help="Rebuild every pack",
        group="maintenance",
    ),
    Stage(
        "pair", "Pair EN/JP", "Pair EN/JP editions (+ overrides)",
        "Match English and Japanese editions of the same book into paired "
        "entries, applying any manual overrides.",
        _pair,
    ),
    Stage(
        "audio-scan", "Scan audiobooks", "Scan audiobook dirs + build index",
        "Scan the configured audiobook folders and rebuild the audiobook index.",
        _audio_scan, group="audiobooks",
    ),
    Stage(
        "catalog", "Fetch audiobook metadata", "Pull catalog data for new audiobooks by ASIN",
        "Look up audiobook files whose ASIN is not in the catalog yet. Asks Audible's "
        "public catalog for title, series, volume, runtime, narrators, cover and genre, "
        "then re-indexes. Does nothing, and makes no requests, when every ASIN on disk "
        "is already known.",
        _catalog, group="audiobooks", chips=("Audible",),
    ),
    Stage(
        "xref", "Cross-ref audiobooks", "Cross-reference audiobook availability",
        "Cross-reference each book against the audiobook collection to flag "
        "which titles have an audiobook available.",
        _xref, group="audiobooks",
    ),
    Stage(
        "build", "Build index", "Join caches into the served index",
        "Join all caches into the index the library page serves. Writes the "
        "denormalized epub_library.json; run this last so the UI reflects other stages.",
        _build, group="finish",
    ),
    Stage(
        "epub-catalog", "Fetch EPUB metadata", "Pull genre/description for EPUBs by ISBN",
        "Look up a genre and description in Google Books for EPUBs with an ISBN, then "
        "rebuild. Only books still missing them after the OPF and any linked audiobook. "
        "Needs GOOGLE_BOOKS_API_KEY in .env; without it, does nothing.",
        _epub_catalog, group="finish", chips=("Google Books",),
    ),
)

STAGE_BY_ID: dict[str, Stage] = {s.id: s for s in STAGES}
CHAIN = " → ".join(s.id for s in STAGES if s.in_chain)


def run_all(cfg: Config, refresh: bool = False) -> None:
    """Every stage in order. `refresh` reaches only the stages marked
    `refresh_in_all` (a Natively re-fetch is minutes; a re-analysis is far more)."""
    for stage in STAGES:
        if stage.in_chain:
            stage.run(cfg, refresh and stage.refresh_in_all)


ALL = Stage(
    "all", "Run full pipeline", "Run the full pipeline",
    f"Run every stage in order: {CHAIN}.",
    run_all,
    refresh_help="Re-fetch all JP books",
)


# ── per-book chain ───────────────────────────────────────────────────────────

BOOK_CHAIN = ("analyze", "enrich", "pair", "link", "build", "epub-catalog")
# Skipped for a language with no vocabulary pipeline.
_VOCAB_STEPS = ("analyze", "enrich")


def unanalyzed_ids(cfg: Config) -> list[str]:
    """Books in a vocabulary language with no `vocab_epub.json` entry at all:
    files that reached `epub_dir` without going through /add. A `no_text` or `error`
    entry counts as analyzed, so image-only/DRM books aren't re-tokenized on every
    sweep; "Analyze now" on the book page is the retry for an error."""
    library = load_json(cfg.library_index, {})
    vocab = load_json(cfg.vocab_index, {})
    return sorted(b for b, e in library.items()
                  if e.get("language") in ANALYZABLE and b not in vocab)


def run_book_chain(cfg: Config, book_ids: str | list[str],
                   progress: Callable[[str], None] = lambda step: None) -> list[str]:
    """Run `BOOK_CHAIN` for one book, or once for a batch of them. Each step is
    attempted even if an earlier one failed (a book whose analysis blew up should
    still be paired, linked and built). Returns the error messages, empty on
    success. Network stages swallow their own request errors (natively.lookup), so
    an outage costs the level, not the strain numbers."""
    ids = [book_ids] if isinstance(book_ids, str) else list(book_ids)
    library = load_json(cfg.library_index, {})
    ids = [b for b in ids if b in library]
    if not ids:
        return ["book is no longer in the library"]

    def link() -> None:
        # The one narrowed stage: ~20 ms per book against the full pass's ~1.5 s.
        for bid in ids:
            xref_mod.xref_one(cfg, bid)

    vocab = any(library[b].get("language") in ANALYZABLE for b in ids)
    steps: list[tuple[str, Callable[[], object]]] = []
    for name in BOOK_CHAIN:
        if name in _VOCAB_STEPS and not vocab:
            continue
        if name == "link":
            fn = link
        elif name == "enrich":
            # Only books with no result yet: an upload must not wait on retrying old misses.
            fn = lambda: enrich_mod.enrich(cfg, retry=False)  # noqa: E731
        else:
            fn = lambda s=STAGE_BY_ID[name]: s.run(cfg)  # noqa: E731
        steps.append((name, fn))

    return _run_steps(steps, progress)


def _run_steps(steps: list[tuple[str, Callable[[], object]]],
               progress: Callable[[str], None]) -> list[str]:
    """Attempt every step even if an earlier one raised; the error messages, empty on success."""
    errors: list[str] = []
    for name, fn in steps:
        progress(name)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — report, keep going
            traceback.print_exc()
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return errors


# What follows a file landing in the audiobook library (`intake`): the same stages as a full
# run, minus the EPUB ones. `catalog` is the one that asks Audible, and only about ASINs no
# catalog knows; it swallows its own request errors, so an outage costs the metadata, not the file.
AUDIO_CHAIN = ("audio-scan", "catalog", "xref", "build")


def run_audio_chain(cfg: Config, progress: Callable[[str], None] = lambda step: None) -> list[str]:
    """Run `AUDIO_CHAIN` once (however many files arrived). Returns the error messages."""
    steps = [(name, lambda s=STAGE_BY_ID[name]: s.run(cfg)) for name in AUDIO_CHAIN]
    return _run_steps(steps, progress)
