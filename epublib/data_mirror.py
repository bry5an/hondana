"""Back up the hand-curated data/ files that have no other copy to a private git
repo (`cli.py push`). Off unless `[data_mirror] publish = true`.

Everything else under data/ is rebuildable from the live EPUB/audiobook
collection via `cli.py all`. These files aren't: the web UI writes
pairing_overrides.json, series_overrides.json, genre_overrides.json,
title_overrides.json, cover_overrides.json (plus the images under
covers/custom/) and audiobook_hidden.json (hand-picked, keyed to this
machine's book_ids, and hundreds of ids in the prune list's case); the
audiobook matches, hand-typed audiobook details and the aliases left when an
audiobook gained a key (audiobook_rekey.py);
audiobook_catalog_base.json (an optional hand-curated audiobook catalog) and
natively_levels.json (its difficulty ratings, ~1 h to re-fetch and not
guaranteed to match the same books);
profiles.json is the reader registry; and every file in each profile's folder
(read_books.json, known_words.json, ...) is hand-curated (marking books read, plus CSV/Anki imports
and words marked known one click at a time on /known and the book pages).
known_stats.json is derived (rebuildable from the above), so it is deliberately
not mirrored. Meant for a timer or cron job, not part of the
scan/analyze/build pipeline (see docs/running-a-server.md).
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

# Global hand-curated files, mirrored from the data/ root.
ROOT_FILES = (
    "pairing_overrides.json",
    "series_overrides.json",
    "genre_overrides.json",
    "title_overrides.json",
    "author_overrides.json",
    "language_overrides.json",
    "cover_overrides.json",
    "audiobook_hidden.json",
    "audiobook_catalog_base.json",
    "natively_levels.json",
    "audiobook_link_overrides.json",
    "audiobook_assignments.json",
    "audiobook_metadata_overrides.json",
    "audiobook_aliases.json",
    "profiles.json",
    "settings.json",
)
# Per-user files, mirrored from each data/users/<slug>/ preserving the subpath.
# Everything in a user folder is copied *except* these derived, rebuildable
# files — so a new profile, or a new per-user file added later, is backed up by
# default instead of silently missed (the failure mode that left four root
# overrides unmirrored). known_stats.json is rebuilt by known.recompute().
USER_DERIVED = frozenset({"known_stats.json"})


def _copy(src: Path, dst: Path) -> None:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def push(cfg: Config) -> None:
    if not cfg.data_mirror_publish:
        print("data_mirror: publish is false, skipping (dev no-op)")
        return

    mirror_dir = cfg.data_mirror_path
    if mirror_dir is None:
        raise SystemExit("data_mirror: publish is true but [data_mirror] path is not set")
    if not (mirror_dir / ".git").is_dir():
        raise SystemExit(
            f"data_mirror.path ({mirror_dir}) is not a git clone of the "
            "mirror repo. Clone it once by hand first — see "
            "docs/running-a-server.md."
        )

    for name in ROOT_FILES:
        _copy(cfg.data_dir / name, mirror_dir / name)

    # Hand-uploaded cover images: the one binary artifact nothing can rebuild.
    if cfg.custom_covers_dir.is_dir():
        for src in sorted(p for p in cfg.custom_covers_dir.iterdir() if p.is_file()):
            _copy(src, mirror_dir / "covers" / "custom" / src.name)

    # Hand-uploaded audiobook covers, likewise (not their thumbnails).
    if cfg.audiobook_custom_covers_dir.is_dir():
        for src in sorted(p for p in cfg.audiobook_custom_covers_dir.iterdir() if p.is_file()):
            _copy(src, mirror_dir / "covers" / "audiobooks" / "custom" / src.name)

    # Audiobook alignment SRTs: a Whisper run to make again. The map beside
    # them is derived and left out.
    if cfg.alignments_dir.is_dir():
        for src in sorted(cfg.alignments_dir.glob("*.srt")):
            _copy(src, mirror_dir / "alignments" / src.name)

    if cfg.users_dir.is_dir():
        for user_dir in sorted(p for p in cfg.users_dir.iterdir() if p.is_dir()):
            for src in sorted(p for p in user_dir.iterdir() if p.is_file()):
                if src.name not in USER_DERIVED and not src.name.startswith("."):
                    _copy(src, mirror_dir / "users" / user_dir.name / src.name)

    subprocess.run(["git", "-C", str(mirror_dir), "add", "-A"], check=True)
    unchanged = subprocess.run(
        ["git", "-C", str(mirror_dir), "diff", "--cached", "--quiet"]
    )
    if unchanged.returncode == 0:
        print("data_mirror: no changes")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subprocess.run(
        ["git", "-C", str(mirror_dir), "commit", "-m", f"data mirror: {stamp}"],
        check=True,
    )
    subprocess.run(["git", "-C", str(mirror_dir), "push"], check=True)
    print("data_mirror: pushed")
