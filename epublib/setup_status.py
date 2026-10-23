"""First-run checklist: where a fresh install stands, for `/`'s "Getting started" card.

Every check reads what a pipeline stage or the app already wrote, so it is cheap
and never starts anything. A step is `done`, or not; `required` ones gate the card
(it goes away once they are all done), the optional ones are only offered.
"""
from __future__ import annotations

from pathlib import Path

from . import dicts
from .config import Config
from .index_store import load_json


def _has_epub(root: Path) -> bool:
    return root.is_dir() and next((p for p in root.rglob("*.epub") if p.is_file()), None) is not None


def _step(id: str, label: str, done: bool, detail: str, href: str, *, required: bool = True,
          link: str = "") -> dict:
    return {"id": id, "label": label, "required": required, "done": done, "detail": detail,
            "href": href, "link": link}


def _library_step(cfg: Config, library: dict) -> dict:
    # Japanese books are the ones the pipeline analyzes; English ones have no vocab entry by design.
    ja = [bid for bid, b in library.items() if b.get("language") == "ja"]
    vocab = load_json(cfg.vocab_index, {}) if ja else {}
    waiting = [bid for bid in ja if bid not in vocab]
    if not library:
        return _step("library", "Build the library", False,
                     "Nothing is indexed yet. Run the full pipeline to scan, analyze and link your books.",
                     "/admin", link="Run full pipeline")
    if waiting:
        return _step("library", "Build the library", False,
                     f"{len(library)} books scanned, {len(ja) - len(waiting)} of {len(ja)} Japanese "
                     "books analyzed. Run the full pipeline to finish.", "/admin", link="Run full pipeline")
    return _step("library", "Build the library", True,
                 f"{len(library)} books scanned"
                 + (f", {len(ja)} Japanese books analyzed." if ja else "."), "/admin", link="Admin")


def steps(cfg: Config, library: dict) -> list[dict]:
    """The checklist for the requesting profile, in the order to do it."""
    has_files = _has_epub(cfg.epub_dir)
    known = int(load_json(cfg.known_words, {}).get("count") or 0)

    audio_dirs = [d for d in cfg.audiobook_dirs if d.is_dir()]
    audio_files = load_json(cfg.audiobook_files_index, {}) if audio_dirs else {}

    phone = any(p.is_file() and p.stat().st_size > 2 for p in (cfg.reader_progress, cfg.reader_lookups))
    n_dicts = len(dicts.manifest(cfg))

    return [
        _step("epub_dir", "Point Hondana at your EPUBs", has_files,
              f"Found books in {cfg.epub_dir}." if has_files else
              f"No EPUBs in {cfg.epub_dir}. Add some, or set epub_dir in the server's config.",
              "/add", link="Add Book"),
        _library_step(cfg, library),
        _step("known", "Tell it which words you know", known > 0,
              f"{known:,} known words." if known else
              "No list for this profile yet. Import one, or start from the most common words.",
              "/known", link="Known words"),
        _step("audiobooks", "Add audiobooks", bool(audio_files),
              f"{len(audio_files)} audiobooks scanned." if audio_files else
              ("Folders are set but nothing is scanned yet: run the full pipeline." if audio_dirs else
               "Set audiobook_dirs in the server's config, then scan."),
              "/admin", required=False, link="Admin"),
        _step("phone", "Connect Hondana for iPhone", phone,
              "Your phone has synced." if phone else
              "Scan the QR code on your profile page to read on your phone.",
              "/profile", required=False, link="Profile"),
        _step("dicts", "Add dictionaries", n_dicts > 0,
              f"{n_dicts} dictionaries ready for the phone." if n_dicts else
              "Upload your Yomitan dictionary zips for the phone's lookups.",
              "/admin", required=False, link="Admin"),
    ]


def summary(cfg: Config, library: dict) -> dict:
    s = steps(cfg, library)
    return {"steps": s, "complete": all(x["done"] for x in s if x["required"])}
