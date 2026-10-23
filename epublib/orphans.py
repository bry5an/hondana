"""Find and remove data keyed by a book_id that is no longer in the library.

`purge.purge_book` and `rekey` stop new orphans from forming, but
anything orphaned before them is still on disk. `find()` lists it, per store;
`apply()` re-attaches what `rekey.recover_orphans` can, then purges the rest.

The stores swept are the ones `purge_book` clears: the derived indexes, the
curated overrides, per-profile read status and known_stats, and the files named
by book_id (covers, thumbnails, hand-set covers, wordlists, sentence indexes, reader packs, audiobook alignments).
`pairing_overrides.json` is keyed by stable key, not book_id, and is never
touched.

The caller holds `server._index_lock` if this is ever run from the server.
"""
from __future__ import annotations

from pathlib import Path

from . import known, purge, rekey
from .config import Config
from .index_store import load_json


def _ids_in_dict(path: Path, nested: str | None = None) -> set[str]:
    data = load_json(path, {})
    target = data.get(nested) if nested else data
    return set(target) if isinstance(target, dict) else set()


def _stems(directory: Path, suffix: str = "") -> set[str]:
    """book_ids of the files in `directory` (files only — `covers/` also holds
    the `thumbs/` and `custom/` subdirectories). `suffix` is stripped, so the
    compound `.json.gz` yields the bare id."""
    if not directory.is_dir():
        return set()
    out = set()
    for p in directory.iterdir():
        if not p.is_file():
            continue
        if suffix:
            if p.name.endswith(suffix):
                out.add(p.name[: -len(suffix)])
        else:
            out.add(p.stem)
    return out


def collect(cfg: Config) -> dict[str, set[str]]:
    """{store: every book_id it holds}, including ids that are still live."""
    stores: dict[str, set[str]] = {}
    for attr in (*purge._DERIVED_INDEXES, *purge._CURATED_INDEXES):
        path = getattr(cfg, attr)
        stores[path.name] = _ids_in_dict(path)
    for d in sorted(p for p in cfg.users_dir.glob("*") if p.is_dir()):
        stores[f"users/{d.name}/known_stats.json"] = _ids_in_dict(d / "known_stats.json")
        stores[f"users/{d.name}/read_books.json"] = _ids_in_dict(d / "read_books.json", "books")
    stores["wordlists/"] = _stems(cfg.wordlists_dir, ".json")
    stores["sentences/"] = _stems(cfg.sentences_dir, ".json.gz")
    stores["annotations/"] = _stems(cfg.annotations_dir, ".json.gz")
    stores["alignments/"] = (
        {p.name.split(".")[0] for p in cfg.alignments_dir.iterdir() if p.is_file()}
        if cfg.alignments_dir.is_dir() else set())
    stores["covers/"] = _stems(cfg.covers_dir)
    stores["covers/thumbs/"] = _stems(cfg.covers_dir / "thumbs", ".webp")
    stores["covers/custom/"] = _stems(cfg.custom_covers_dir)
    return stores


def find(cfg: Config, library: dict) -> dict[str, list[str]]:
    """{store: sorted orphaned ids}, omitting stores with none."""
    live = set(library)
    found = {store: sorted(ids - live) for store, ids in collect(cfg).items()}
    return {store: ids for store, ids in found.items() if ids}


def titles(cfg: Config, ids: set[str]) -> dict[str, str]:
    """A human label for each orphan any store still names — the title override
    or the Natively match. Most orphans have neither."""
    out: dict[str, str] = {}
    natively = load_json(cfg.natively_index, {})
    for bid in ids:
        if bid in natively and natively[bid].get("matched_title"):
            out[bid] = natively[bid]["matched_title"]
    for bid, title in load_json(cfg.title_overrides, {}).items():
        if bid in ids and isinstance(title, str) and title:
            out[bid] = title
    return out


def apply(cfg: Config, library: dict, orphan_ids: set[str]) -> dict[str, list[str]]:
    """Purge every orphan (derived and curated). Returns {id: stores touched}.
    Recompute `known` afterwards, once."""
    removed: dict[str, list[str]] = {}
    for bid in sorted(orphan_ids - set(library)):
        # purge_book takes the extracted cover as the library entry's path, and
        # an orphan has no entry — so sweep covers/{id}.* here.
        covers = [p for p in cfg.covers_dir.glob(f"{bid}.*") if p.is_file()]
        for p in covers:
            p.unlink(missing_ok=True)
        removed[bid] = purge.purge_book(cfg, bid, curated=True) + (["covers/"] if covers else [])
    return removed


def run(cfg: Config, library: dict, *, do_apply: bool, out=print) -> int:
    """The `cli.py gc` body. Returns the number of orphaned ids."""
    if not library:
        out("error: the library index is empty or missing, so every id would "
            "look orphaned. Run `cli.py scan` first.")
        raise SystemExit(1)

    recovered = rekey.recover_orphans(cfg, library)
    for old, new in recovered.items():
        out(f"  ⇄ recovered {old} → {new}  {library[new].get('path')}")
    if recovered:
        out(f"Re-attached curated state for {len(recovered)} moved book(s) first.")

    found = find(cfg, library)
    ids = {bid for ids in found.values() for bid in ids}
    if not ids:
        out("No orphaned data.")
        if recovered:
            known.recompute_all(cfg)
        return 0

    out("Orphaned entries per store:")
    for store, store_ids in found.items():
        out(f"  {len(store_ids):4d}  {store}")
    labels = titles(cfg, ids)
    out(f"\n{len(ids)} orphaned book id(s):")
    for bid in sorted(ids):
        out(f"  {bid}  {labels.get(bid, '')}".rstrip())

    if not do_apply:
        out("\nDry run — nothing removed. Re-run with --apply to delete the above.")
        if recovered:
            known.recompute_all(cfg)
        return len(ids)

    removed = apply(cfg, library, ids)
    known.recompute_all(cfg)
    out(f"\nRemoved data for {len(removed)} orphaned book(s).")
    return len(ids)
