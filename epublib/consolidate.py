"""Plan and execute physical consolidation of the audiobook collection.

Every audio file across cfg.audiobook_dirs is matched against the catalog
(read-only) and assigned a destination under the canonical root
(cfg.audiobook_root, the first configured dir). A row's `new_path` IS the
canonical relative path; a row without one (anything pulled by ASIN, or a
catalog that never had the field) gets the same shape derived by `_dest_rel`.

Matching tiers:
  1. Audible ISBN embedded in the filename.
  2. Exact normalized-title match (unique catalog hit only).
  3. Fuzzy title match (>= FUZZY_MIN with FUZZY_MARGIN over the runner-up);
     reported separately, disable with --no-fuzzy.
  4. Unmatched: files already under an "[Author] ..." directory keep their
     relative layout; flat files land in _unsorted/.

`build_plan` is a pure dry-run (writes data/consolidation_plan.json).
`execute` performs the moves: same-volume rename, or cross-volume
copy-to-.part → size-verify → replace → delete-source-last, so an
interruption never loses data and a re-run converges to all-noop.
Ambiguous, conflict, and duplicate entries are never executed; duplicate
sources (canonical copy already present) are left in place.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import audiobook_catalog
from .audiobook_scanner import CATALOG_KEY_RE, AUTHOR_DIR_RE, _is_junk
from .config import Config
from .index_store import save_json
from .textnorm import file_title_key

FUZZY_MIN = 0.90
FUZZY_MARGIN = 0.05
FREE_SPACE_MARGIN = 5 * 1024**3
AUDIO_EXTS = {".m4b"}
STRAY_EXTS = {".pdf", ".zip"}


def _file_title(path: Path) -> str:
    return file_title_key(path.stem if path.is_file() else path.name)


def _match(stem_norm: str, key: str | None, catalog: dict,
           by_title: dict, use_fuzzy: bool):
    """Return (catalog_key, action) or (None, reason)."""
    if key and key in catalog:
        return key, "move-isbn"
    hits = by_title.get(stem_norm, [])
    if len(hits) == 1:
        return hits[0], "move-title"
    if len(hits) > 1:
        return None, "ambiguous"
    if use_fuzzy and stem_norm:
        best, second, best_isbn = 0.0, 0.0, None
        for cand_isbn, cand_norm in ((k, v) for v, ks in by_title.items() for k in ks):
            r = difflib.SequenceMatcher(None, stem_norm, cand_norm).ratio()
            if r > best:
                best, second, best_isbn = r, best, cand_isbn
            elif r > second:
                second = r
        if best >= FUZZY_MIN and best - second >= FUZZY_MARGIN:
            return best_isbn, "move-fuzzy"
    return None, "unmatched"


_UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _part(text: str) -> str:
    return re.sub(r"\s+", " ", _UNSAFE_PATH_CHARS.sub(" ", text)).strip()


def _dest_rel(key: str, entry: dict) -> str:
    """The canonical relative path for a catalog row: its `new_path` if it has
    one, else `[Author] Series/[NN] Title [key].m4b` — the layout `new_path` uses."""
    if entry.get("new_path"):
        return entry["new_path"]
    title = _part(entry.get("title") or "") or key
    series = _part(entry.get("series") or "") or title
    author = _part(entry.get("author") or "")
    folder = f"[{author}] {series}" if author else series
    order = f"[{entry['order']}] " if entry.get("order") else ""
    return f"{folder}/{order}{title} [{key}].m4b"


def _dest_for_unmatched(root: Path, path: Path) -> str:
    """Keep '[Author] …' layout; everything else goes to _unsorted/."""
    rel = path.relative_to(root)
    if len(rel.parts) > 1 and AUTHOR_DIR_RE.match(rel.parts[0]):
        return rel.as_posix()
    return f"_unsorted/{path.name}"


def build_plan(cfg: Config, use_fuzzy: bool = True) -> dict:
    # The catalog proper: a local file's own row would only name it after itself.
    catalog: dict = audiobook_catalog.load(cfg, local=False)
    canonical = cfg.audiobook_root
    if not canonical.exists():
        raise SystemExit(f"Canonical root not mounted: {canonical}")

    by_title: dict[str, list[str]] = {}
    for isbn, ab in catalog.items():
        by_title.setdefault(file_title_key(ab.get("title") or ""), []).append(isbn)

    moves: list[dict] = []
    ambiguous: list[dict] = []
    junk: list[str] = []
    claimed_dests: dict[str, str] = {}  # dest rel → src, to catch two-sources-one-dest

    def plan_one(src: Path, root: Path, dest_rel: str, action: str,
                 isbn: str | None, is_dir: bool = False) -> None:
        dest = canonical / dest_rel
        size = (sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
                if is_dir else src.stat().st_size)
        # samefile, not string equality: APFS resolves NFC/NFD-variant names
        # to the same file, so an in-place file can have a "different" path.
        if dest.exists() and os.path.samefile(src, dest):
            action = "noop"
        elif dest_rel in claimed_dests:
            action = "conflict"
        elif dest.exists():
            dsize = (sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
                     if dest.is_dir() else dest.stat().st_size)
            action = "duplicate" if dsize == size else "conflict"
        if action not in ("noop", "conflict", "duplicate"):
            claimed_dests[dest_rel] = str(src)
        moves.append({
            "src": str(src),
            "dest": str(canonical / f"_duplicates/{src.name}") if action == "duplicate"
                    else str(dest),
            "action": action,
            "isbn": isbn,
            "is_dir": is_dir,
            "cross_volume": src.stat().st_dev != canonical.stat().st_dev,
            "filesize": size,
        })

    for root in cfg.audiobook_dirs:
        if not root.exists():
            print(f"  ! root not mounted, skipped in plan: {root}")
            continue
        mp3_dirs: set[Path] = set()
        m4b_stems: dict[Path, dict[str, str]] = {}  # dir → {stem: dest_rel}

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if _is_junk(path):
                junk.append(str(path))
                continue
            ext = path.suffix.lower()
            if ext == ".m4b":
                m = CATALOG_KEY_RE.search(path.name)
                isbn, action = _match(_file_title(path), m.group(1) if m else None,
                                      catalog, by_title, use_fuzzy)
                if action == "ambiguous":
                    ambiguous.append({"src": str(path),
                                      "candidates": by_title[_file_title(path)]})
                    continue
                if isbn:
                    dest_rel = _dest_rel(isbn, catalog[isbn])
                else:
                    action = ("move-keep-layout"
                              if AUTHOR_DIR_RE.match(path.parent.name)
                              else "move-unsorted")
                    dest_rel = _dest_for_unmatched(root, path)
                m4b_stems.setdefault(path.parent, {})[path.stem] = dest_rel
                plan_one(path, root, dest_rel, action, isbn)
            elif ext == ".mp3":
                mp3_dirs.add(path.parent)

        # One book per mp3 directory (no sibling m4b).
        for d in sorted(mp3_dirs):
            if any(p.suffix.lower() == ".m4b" for p in d.iterdir() if p.is_file()):
                continue
            isbn, action = _match(file_title_key(d.name), None,
                                  catalog, by_title, use_fuzzy)
            if action == "ambiguous":
                ambiguous.append({"src": str(d),
                                  "candidates": by_title[file_title_key(d.name)]})
                continue
            if isbn:
                dest_rel = re.sub(r"\.m4b$", "", _dest_rel(isbn, catalog[isbn]))
            else:
                action, dest_rel = "move-unsorted", f"_unsorted/{d.name}"
            plan_one(d, root, dest_rel, action, isbn, is_dir=True)

        # Strays (pdf/zip) follow a sibling m4b with the same stem, else _unsorted.
        for path in sorted(root.rglob("*")):
            if not path.is_file() or _is_junk(path):
                continue
            if path.suffix.lower() not in STRAY_EXTS:
                continue
            if root == canonical:
                continue  # already-in-place strays stay put
            sibling = m4b_stems.get(path.parent, {}).get(path.stem)
            if sibling:
                dest_rel = str(Path(sibling).parent / path.name)
                plan_one(path, root, dest_rel, "move-stray", None)
            else:
                plan_one(path, root, f"_unsorted/{path.name}", "move-unsorted", None)

    by_action: dict[str, int] = {}
    for mv in moves:
        by_action[mv["action"]] = by_action.get(mv["action"], 0) + 1

    plan = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canonical_root": str(canonical),
        "moves": moves,
        "ambiguous": ambiguous,
        "junk": junk,
        "summary": {
            "by_action": by_action,
            "ambiguous": len(ambiguous),
            "junk": len(junk),
            "bytes_cross_volume": sum(
                m["filesize"] for m in moves
                if m["cross_volume"] and m["action"].startswith("move")
            ),
        },
    }
    save_json(cfg.consolidation_plan, plan)
    return plan


def print_report(cfg: Config, plan: dict) -> None:
    s = plan["summary"]
    print(f"\nConsolidation plan → {cfg.consolidation_plan}")
    print(f"  canonical root: {plan['canonical_root']}")
    for action, n in sorted(s["by_action"].items()):
        print(f"  {action:18} {n}")
    print(f"  {'ambiguous':18} {s['ambiguous']}  (never executed)")
    print(f"  {'junk files':18} {s['junk']}  (deleted only with --clean-junk)")

    free = shutil.disk_usage(plan["canonical_root"]).free
    need = s["bytes_cross_volume"]
    print(f"  cross-volume bytes: {need / 1e9:.1f} GB "
          f"(free on target: {free / 1e9:.1f} GB)")

    fuzzy = [m for m in plan["moves"] if m["action"] == "move-fuzzy"]
    if fuzzy:
        print(f"\nFuzzy matches ({len(fuzzy)}) — REVIEW BEFORE --execute:")
        for m in fuzzy:
            print(f"  {Path(m['src']).name}\n    → {m['dest']}")
    if plan["ambiguous"]:
        print(f"\nAmbiguous ({len(plan['ambiguous'])}):")
        for a in plan["ambiguous"]:
            print(f"  {a['src']}  candidates: {a['candidates']}")
    conflicts = [m for m in plan["moves"] if m["action"] == "conflict"]
    if conflicts:
        print(f"\nConflicts ({len(conflicts)}) — resolve manually:")
        for m in conflicts:
            print(f"  {m['src']}\n    vs {m['dest']}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _move_file(src: Path, dest: Path, cross: bool, verify_hash: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not cross:
        try:
            os.rename(src, dest)
            return
        except OSError:
            pass  # EXDEV etc. → fall through to copy path
    part = dest.with_suffix(dest.suffix + ".part")
    shutil.copy2(src, part)
    if part.stat().st_size != src.stat().st_size:
        part.unlink()
        raise OSError(f"size mismatch copying {src}")
    if verify_hash and _sha256(part) != _sha256(src):
        part.unlink()
        raise OSError(f"hash mismatch copying {src}")
    os.replace(part, dest)
    src.unlink()


def _move_dir(src: Path, dest: Path, cross: bool, verify_hash: bool) -> None:
    if not cross:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(src, dest)
            return
        except OSError:
            pass
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        if _is_junk(f):
            continue
        _move_file(f, dest / f.relative_to(src), cross=True, verify_hash=verify_hash)
    shutil.rmtree(src, ignore_errors=True)


def execute(cfg: Config, plan: dict, clean_junk: bool = False,
            verify_hash: bool = False) -> None:
    canonical = Path(plan["canonical_root"])
    todo = [m for m in plan["moves"] if m["action"].startswith("move")]
    dup_skipped = sum(1 for m in plan["moves"] if m["action"] == "duplicate")
    if dup_skipped:
        print(f"  skipping {dup_skipped} duplicate(s); sources left in place")

    need = sum(m["filesize"] for m in todo if m["cross_volume"])
    free = shutil.disk_usage(canonical).free
    if free < need + FREE_SPACE_MARGIN:
        raise SystemExit(
            f"Not enough space on {canonical}: need {need / 1e9:.1f} GB "
            f"+ margin, free {free / 1e9:.1f} GB"
        )

    done = skipped = failed = 0
    for i, mv in enumerate(todo, 1):
        src, dest = Path(mv["src"]), Path(mv["dest"])
        if not src.exists():
            skipped += 1  # already moved by a previous (interrupted) run
            continue
        if dest.exists():
            print(f"  ! dest appeared since planning, skipping: {dest}")
            skipped += 1
            continue
        try:
            if mv["is_dir"]:
                _move_dir(src, dest, mv["cross_volume"], verify_hash)
            else:
                _move_file(src, dest, mv["cross_volume"], verify_hash)
            done += 1
        except OSError as exc:
            print(f"  ! failed: {src}: {exc}")
            failed += 1
        if i % 25 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] moved={done} skipped={skipped} failed={failed}")

    if clean_junk:
        removed = 0
        for j in plan["junk"]:
            try:
                Path(j).unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        print(f"  junk removed: {removed}")

    # Prune directories the moves emptied (rmdir refuses non-empty ones).
    for root in cfg.audiobook_dirs[1:]:
        if not root.exists():
            continue
        for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass

    print(f"\nConsolidation done: {done} moved, {skipped} skipped, {failed} failed.")
    if failed:
        print("Re-run `consolidate` to re-plan and retry failures.")
