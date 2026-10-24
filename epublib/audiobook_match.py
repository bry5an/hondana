"""Match a local audiobook to an Audible book by hand.

A file the scanner can't identify (no `[ASIN]` in its name, no ASIN tag) is a
guess from its tags or file name. Match says which Audible book it is: the
choice is kept in `audiobook_assignments.json` (`{fid: asin}`, which the scanner
reads), the book's details are pulled into `audiobook_catalog_extra.json`
unless a catalog has them already, and the next scan gives the file the ASIN as
its `ab_id`, moving everything kept under the old id (`audiobook_rekey`).

- `candidates()` — Audible search results for the dialog, marked with what is
  already a file here.
- `state()` — what the dialog needs to know about one audiobook.
- `prepare()` — the checks, then the one network call (no lock held).
- `match()` / `unmatch()` — the writes, then scan → index → links → library.
  `rename=True` also adds ` [ASIN]` to the file (or folder) name, so the match
  survives losing the data folder and other tools read it too.

A file whose name already has a `[KEY]` can't be matched: the name wins over
every other source, so the name is what to change.

The caller holds `server._index_lock` around `match` and `unmatch`.
"""
from __future__ import annotations

from pathlib import Path

from . import audible, audiobook_catalog, audiobook_rekey, audiobook_scanner
from .audiobook_details import rebuild
from .config import Config
from .index_store import load_json, save_json


class MatchError(Exception):
    """Something the person can fix; `reason` is for the page, the message is for them."""

    def __init__(self, reason: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.reason, self.status = reason, status


# ── reading ──────────────────────────────────────────────────────────────────

def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def candidates(cfg: Config, title: str = "", author: str = "", keywords: str = "") -> list[dict]:
    """Audible books matching the search, each with `local` (a file here is
    already that book) and `in_catalog` (a catalog row exists, so choosing it
    needs no download). Raises ValueError for an empty search, AudibleError
    when Audible can't be reached."""
    products = audible.search(title, author, keywords, marketplace=cfg.audible_marketplace)
    held = {e.get("catalog_key") for e in load_json(cfg.audiobook_files_index, {}).values()}
    known = audiobook_catalog.load(cfg, local=False, edits=False)
    out = []
    for product in products:
        asin = product.get("asin")
        if not asin:
            continue
        row = audiobook_catalog.entry_from_product(product)
        out.append({
            "asin": asin, "title": row["title"], "author": row["author"],
            "narrators": row["narrators"], "series": row["series"] if row["order"] else None,
            "order": row["order"], "runtime_min": _int(row["runtime_length_min"]),
            "release_date": row["release_date"], "cover": row["cover"],
            "url": audible.pd_url(asin, cfg.audible_marketplace),
            "local": asin in held, "in_catalog": asin in known,
        })
    return out


def _why_not(entry: dict | None) -> str | None:
    if entry is None:
        return "There's no audiobook file on this computer to match."
    if audiobook_scanner._key_source(entry) == "name":
        return (f"This file's name already says which book it is ([{entry['catalog_key']}]). "
                "Rename the file to change it.")
    return None


def _rename_blocked(entry: dict) -> str | None:
    if entry.get("group"):
        return "This folder holds more than one album, so it can't be renamed as one book."
    if entry["format"] in ("mp3-folder", "tracks") and entry["path"] == ".":
        return "This book is the audiobook folder itself, which can't be renamed from here."
    return None


def state(cfg: Config, ab_id: str) -> dict | None:
    """What the Match dialog shows, or None for an id no catalog row has."""
    row = audiobook_catalog.load(cfg).get(ab_id)
    if row is None:
        return None
    entry = load_json(cfg.audiobook_files_index, {}).get(ab_id)
    key_source = audiobook_scanner._key_source(entry)
    mp = audible.market(cfg.audible_marketplace)
    reason = _why_not(entry)
    return {
        "ab_id": ab_id,
        "local": entry is not None,
        "mode": key_source or "none",   # name | assigned | tag | none
        "asin": entry.get("catalog_key") if entry else None,
        "matchable": reason is None,
        "reason": reason,
        "can_rename": entry is not None and reason is None and _rename_blocked(entry) is None,
        "query": {"title": row.get("title") or "", "author": row.get("author") or ""},
        "marketplace": {"code": cfg.audible_marketplace, "label": mp.label},
    }


# ── matching ─────────────────────────────────────────────────────────────────

def _local(cfg: Config, ab_id: str) -> dict:
    entry = load_json(cfg.audiobook_files_index, {}).get(ab_id)
    if entry is None:
        if audiobook_catalog.load(cfg).get(ab_id) is None:
            raise MatchError("unknown_audiobook", "Unknown audiobook", 404)
        raise MatchError("not_local", _why_not(None))
    return entry


def _check(cfg: Config, entry: dict, asin: str) -> None:
    problem = _why_not(entry)
    if problem:
        raise MatchError("key_in_name", problem)
    if entry["ab_id"] == asin:
        raise MatchError("same", "This file is already that book.")
    for other in load_json(cfg.audiobook_files_index, {}).values():
        if other["ab_id"] != entry["ab_id"] and other.get("catalog_key") == asin:
            raise MatchError("taken", f"Another file in your library is already that book "
                                      f"({other['path']}).")
    for fid, key in load_json(cfg.audiobook_assignments, {}).items():
        if key == asin and fid != entry["fid"]:
            raise MatchError("taken", "Another file in your library is already matched to that book.")


def prepare(cfg: Config, ab_id: str, asin: str) -> dict | None:
    """Check the match is possible and fetch what it needs from Audible: the
    catalog row to store, or None when a catalog already has the book. Takes no
    lock (it waits on the network); `match` checks again under the lock."""
    _check(cfg, _local(cfg, ab_id), asin)
    if asin in audiobook_catalog.load(cfg, local=False, edits=False):
        return None
    label = audible.market(cfg.audible_marketplace).label
    product = audible.fetch_product(asin, cfg.audible_marketplace)
    if not product:
        raise MatchError("not_found", f"Audible {label} has no book with the ASIN {asin}. "
                                      "Check the store in Settings.", 404)
    if not (product.get("title") and product.get("runtime_length_min")):
        raise MatchError("not_a_book", f"{asin} is a series page, not a book. "
                                       "Pick one of its volumes.")
    return audiobook_catalog.entry_from_product(product)


def _fid_index(index: dict, fid: str) -> str | None:
    return next((k for k, e in index.items() if e.get("fid") == fid), None)


def _carry_file_state(cfg: Config, entry: dict, new_id: str | None) -> None:
    """A file leaving one catalog key for another takes its listening position
    and alignment along (the first key of a surrogate is moved by the scan)."""
    if new_id and entry.get("catalog_key") and new_id != entry["ab_id"]:
        audiobook_rekey.rekey_audiobook(cfg, entry["ab_id"], new_id, whole_book=False)


def _add_key_to_name(entry: dict, asin: str) -> str | None:
    """Rename the file (or the book's folder) to end in ` [ASIN]`. None when it
    worked, else why it didn't."""
    problem = _rename_blocked(entry)
    if problem:
        return problem
    root = Path(entry["root"]).resolve()
    src = (root / entry["path"]).resolve()
    if root not in src.parents:
        return "That file is outside the audiobook folders."
    if entry["format"] in ("mp3-folder", "tracks"):
        dst = src.with_name(f"{src.name} [{asin}]")
    else:
        dst = src.with_name(f"{src.stem} [{asin}]{src.suffix}")
    if dst.exists():
        return f"There is already a file named {dst.name}."
    try:
        src.rename(dst)
    except OSError as exc:
        return f"Couldn't rename it: {exc.strerror or exc}"
    return None


def match(cfg: Config, ab_id: str, asin: str, row: dict | None, *, rename: bool = False) -> dict:
    """Make the file Audible book `asin`. `row` is `prepare`'s catalog row (None
    when a catalog has the book). Returns `{ab_id, old_ab_id, renamed, rename_error}`."""
    entry = _local(cfg, ab_id)
    _check(cfg, entry, asin)
    if row is not None:
        extra = load_json(cfg.audiobook_catalog_extra, {})
        extra[asin] = row
        save_json(cfg.audiobook_catalog_extra, extra)
    assignments = load_json(cfg.audiobook_assignments, {})
    assignments[entry["fid"]] = asin
    save_json(cfg.audiobook_assignments, assignments)

    index = audiobook_scanner.scan(cfg)
    new_id = _fid_index(index, entry["fid"])
    _carry_file_state(cfg, entry, new_id)

    renamed, rename_error = False, None
    if rename and new_id:
        rename_error = _add_key_to_name(index[new_id], asin)
        renamed = rename_error is None
        if renamed:  # the scan finds the file again by its tags or contents
            index = audiobook_scanner.scan(cfg)
            new_id = _fid_index(index, entry["fid"]) or new_id
    rebuild(cfg)
    return {"ab_id": new_id or asin, "old_ab_id": ab_id, "renamed": renamed,
            "rename_error": rename_error}


def unmatch(cfg: Config, ab_id: str) -> dict:
    """Take back a hand match: the file is whatever its tags and name say again.
    Returns `{ab_id, old_ab_id}`."""
    entry = _local(cfg, ab_id)
    if entry.get("key_source") != "assigned":
        raise MatchError("not_matched", "This file wasn't matched by hand.")
    assignments = load_json(cfg.audiobook_assignments, {})
    if assignments.pop(entry["fid"], None) is not None:
        save_json(cfg.audiobook_assignments, assignments)
    index = audiobook_scanner.scan(cfg)
    new_id = _fid_index(index, entry["fid"])
    _carry_file_state(cfg, entry, new_id)
    rebuild(cfg)
    return {"ab_id": new_id or ab_id, "old_ab_id": ab_id}
