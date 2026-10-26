"""Carry an audiobook's state across an ab_id change.

An audiobook's ab_id is its catalog key, else its surrogate fid
(audiobook_scanner.py). The fid is sticky, so a move or rename never changes
the id; what does is a keyless file gaining a key — an `[ASIN]` added to its
name, an ASIN tag, or a hand Match. `rekey_audiobook(old, new)` then moves
everything kept under the old id:

- the prune list (`audiobook_hidden.json`),
- every profile's `audio_progress.json` (the newer position wins if both exist),
- the values of `audiobook_link_overrides.json` (an EPUB pinned to the old id),
- the `audiobook` field of each `alignments/*.json` map (otherwise
  `align.ensure` refuses it as `audiobook_changed`) and the `ab_id` of each fix in
  `alignments/*.anchors.jsonl` (otherwise `align.load_anchors` ignores it),
- every profile's `bookmarks.json`: a bookmark's audio place is a place in the file,
- the hand-typed details (`audiobook_metadata_overrides.json`, with a hand-set
  cover's image) and the Natively match (`natively_levels.json`), never over an
  entry the new id already has,

and records `old → new` in `audiobook_aliases.json`, which
`reader.merge_audio_progress` applies to phone events that still carry the old
id (and, for bookmarks and sync fixes, `reader.merge_bookmarks` and the server's
`_record_anchors`). Hand matches (`audiobook_assignments.json`) are keyed by fid and need nothing.

A file moving from one catalog key to another (a hand match corrected, or
removed) carries only what belongs to the *file*: the listening position, the
bookmarks, the alignment with its sync fixes and the alias (`whole_book=False`). The rest describes the book the old
key names — the Natively match, hand-typed details, the prune flag, an EPUB
pinned to it — and stays with that row.

The caller holds `server._index_lock` (or is a pipeline stage that does).
"""
from __future__ import annotations

from . import align
from .config import Config
from .index_store import load_json, save_json
from .reader import parse_ts

# {ab_id: value} stores under data_dir that follow an audiobook (Config attrs).
AB_INDEXES = ("audiobook_metadata_overrides", "audiobook_natively")


def _move_key(path, old: str, new: str) -> bool:
    data = load_json(path, {})
    if not isinstance(data, dict) or old not in data:
        return False
    value = data.pop(old)
    data.setdefault(new, value)
    save_json(path, data)
    return True


def _newer(a: dict, b: dict) -> dict:
    try:
        return a if parse_ts(a["updated_at"]) >= parse_ts(b["updated_at"]) else b
    except (KeyError, TypeError, ValueError):
        return b


def rekey_audiobook(cfg: Config, old: str, new: str, *, whole_book: bool = True) -> list[str]:
    """Move everything kept under `old` to `new`. Returns the stores touched.
    `whole_book=False` moves only what follows the file (see the module docs)."""
    if old == new:
        return []
    touched: list[str] = []
    if whole_book:
        for attr in AB_INDEXES:
            path = getattr(cfg, attr)
            if _move_key(path, old, new):
                touched.append(path.name)

        hidden = load_json(cfg.audiobook_hidden, {})
        ids = list(hidden.get("ab_ids") or [])
        if old in ids:
            ids = [i for i in ids if i != old]
            if new not in ids:
                ids.append(new)
            save_json(cfg.audiobook_hidden, {**hidden, "ab_ids": ids})
            touched.append(cfg.audiobook_hidden.name)

    for d in sorted(p for p in cfg.users_dir.glob("*") if p.is_dir()):
        path = d / "audio_progress.json"
        progress = load_json(path, {})
        if old in progress:
            moved = progress.pop(old)
            progress[new] = _newer(progress[new], moved) if new in progress else moved
            save_json(path, progress)
            touched.append(f"users/{d.name}/{path.name}")

        path = d / "bookmarks.json"
        marks = load_json(path, {})
        moved_marks = [k for k, v in marks.items() if isinstance(v, dict) and v.get("ab_id") == old]
        if moved_marks:
            for k in moved_marks:
                marks[k] = {**marks[k], "ab_id": new}
            save_json(path, marks)
            touched.append(f"users/{d.name}/{path.name}")

    links = load_json(cfg.audiobook_link_overrides, {})
    if whole_book and old in links.values():
        save_json(cfg.audiobook_link_overrides,
                  {bid: (new if key == old else key) for bid, key in links.items()})
        touched.append(cfg.audiobook_link_overrides.name)

    for path in sorted(cfg.alignments_dir.glob("*.json")):
        m = load_json(path, {})
        if isinstance(m, dict) and m.get("audiobook") == old:
            save_json(path, {**m, "audiobook": new})
            touched.append(f"alignments/{path.name}")
    touched += [f"alignments/{name}" for name in align.rekey_anchors(cfg, old, new)]

    # A hand-set cover's image goes with its flag (moved above, in the overrides).
    if whole_book:
        covers = cfg.audiobook_custom_covers_dir
        for src in covers.glob(f"{old}.*"):
            dst = covers / f"{new}{src.suffix}"
            if not dst.exists():
                src.rename(dst)
                touched.append(f"covers/audiobooks/custom/{dst.name}")
        (covers / "thumbs" / f"{old}.webp").unlink(missing_ok=True)

    aliases = load_json(cfg.audiobook_aliases, {})
    aliases = {k: (new if v == old else v) for k, v in aliases.items() if k != new}
    aliases[old] = new
    save_json(cfg.audiobook_aliases, aliases)
    touched.append(cfg.audiobook_aliases.name)
    return touched


def drop_live_aliases(cfg: Config, index: dict) -> None:
    """Forget an alias whose old id is in use again (a key removed by hand), so
    the phone's events for that book stay with it."""
    aliases = load_json(cfg.audiobook_aliases, {})
    live = {k: v for k, v in aliases.items() if k not in index}
    if live != aliases:
        save_json(cfg.audiobook_aliases, live)


def resolve(cfg: Config, ab_id: str) -> str:
    """The current id for `ab_id`: itself, or where an alias says it went."""
    return load_json(cfg.audiobook_aliases, {}).get(ab_id, ab_id)
