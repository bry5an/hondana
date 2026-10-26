"""Per-profile state for the Hondana iOS reader.

The phone resolves a word's status itself so a tap recolours the page instantly
and offline. For that it needs the profile's *effective* known set, which is
what the library scores against: the list's orth keys plus the set-aside
`not_words` (`known.load_sets`), the lemma keys, and exposure (words used at
least `known.EXPOSURE_MIN` times in books marked read). `snapshot()` is that
set; the phone applies the `known.exposure_for` exclusion of the scored book's
own contribution itself, from the pack's per-book counts.

The reader also keeps three things the library has no store for, all under
`data/users/<slug>/` (so `data_mirror` backs them up with the rest of a
profile's files; none of them is derived, so none belongs in `USER_DERIVED`):

- `learning.json`: `{schema, words: {orth: {lemma, reading, book_id, sentence,
  added_at}}}`. Learning words still count as *unknown* in every metric: they
  are not in the known list, so nothing here changes a Known %.
- `lookups.jsonl`: append-only `{id, ts, orth, lemma, book_id, sentence}`,
  de-duplicated by `id`.
- `reader_progress.json`: `{stable_key: {book_id, sha256, cfi, fraction,
  updated_at}}`. Keyed by stable key (`pairing.stable_key`) because `book_id`
  changes on rename and differs between machines.
- `audio_progress.json`: `{ab_id: {track, seconds, updated_at}}`, where the
  listener is in each audiobook. Keyed by `ab_id`, which is all an audiobook has
  (it is sticky across moves and renames; when it does change, the entry moves
  with it and `audiobook_aliases.json` maps late events, audiobook_rekey.py).

`POST /api/reader/events` is how they are written. The phone keeps an
outbox of events and replays it when it is back on the LAN, so every function
here is idempotent: the same batch twice leaves the same files. Callers hold
the locks (server.py): `_index_lock` around `apply_status` (it writes the known
list), `_reader_lock` around the rest.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone

from pydantic import ValidationError

from . import known, schemas
from .config import Config
from .index_store import load_json, save_json

SCHEMA_VERSION = 1


def load_learning(cfg: Config) -> dict[str, dict]:
    """orth key → {lemma, reading, book_id, sentence, added_at}."""
    return dict(load_json(cfg.reader_learning, {}).get("words") or {})


def load_progress(cfg: Config) -> dict[str, dict]:
    """stable key → {book_id, sha256, cfi, fraction, updated_at}."""
    return dict(load_json(cfg.reader_progress, {}))


def load_audio_progress(cfg: Config) -> dict[str, dict]:
    """ab_id → {track, seconds, updated_at}."""
    return dict(load_json(cfg.reader_audio_progress, {}))


def _stamp(path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "-"
    return f"{st.st_mtime_ns}:{st.st_size}"


def etag(cfg: Config) -> str:
    """Changes whenever the snapshot can: built from the (mtime_ns, size) of
    the three files it is read from, which every writer replaces atomically.

    Cheap enough to check before building anything, so a 304 never reads the
    wordlists. The profile is part of the hash, so two profiles can never
    share a tag. Exposure also depends on the wordlists, which a re-analyze
    rewrites without touching these files; the next mark or read toggle
    corrects the tag.
    """
    parts = [cfg.active_user, *(_stamp(p) for p in (
        cfg.known_words, cfg.read_books, cfg.reader_learning))]
    digest = hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
    return f'"{SCHEMA_VERSION}-{digest}"'


def snapshot(cfg: Config) -> dict:
    """The body of `GET /api/reader/vocab`."""
    orth, lemma = known.load_sets(cfg)
    not_words = load_json(cfg.known_words, {}).get("not_words") or []
    return {
        # Includes not_words (known.load_sets folds them in), so they colour as
        # known; not_words says which of those the phone shows as ignored.
        "orth": sorted(orth),
        "lemma": sorted(lemma),
        "not_words": sorted(not_words),
        "learning": load_learning(cfg),
        "exposure": known.exposure_counts(cfg),
        "exposure_min": known.EXPOSURE_MIN,
    }


# ── events ────────────────────────────────────────────────────────────

STATUSES = ("known", "learning", "unknown", "ignored")


def parse_ts(ts: float | int | str) -> float:
    """Unix seconds from what the phone sends: a number, or an ISO 8601 string
    (a missing zone is read as UTC). Raises ValueError on anything else."""
    if isinstance(ts, bool):
        raise ValueError(f"bad ts: {ts!r}")
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"bad ts: {ts!r}") from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def parse_events(raw: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Validate a batch event by event. Returns the good ones grouped by type,
    each as a plain dict with `ts` in Unix seconds, and the rejected ones as
    `{id, reason}`."""
    good: dict[str, list[dict]] = {"status": [], "lookup": [], "progress": [], "audio_progress": [], "sync_anchor": []}
    rejected: list[dict] = []
    for item in raw:
        eid = item.get("id") if isinstance(item.get("id"), str) else None
        try:
            event = schemas.READER_EVENT.validate_python(item).model_dump()
            event["ts"] = parse_ts(event["ts"])
            # Keys match the known list's, which is NFKC; a stable key is opaque.
            for key in ("orth", "lemma"):
                if key in event:
                    event[key] = unicodedata.normalize("NFKC", event[key].strip())
            if "orth" in event and not event["orth"]:
                raise ValueError("orth: empty")
        except ValidationError as e:
            err = e.errors()[0]
            rejected.append({"id": eid, "reason": f"{'.'.join(map(str, err['loc']))}: {err['msg']}"})
        except ValueError as e:
            rejected.append({"id": eid, "reason": str(e)})
        else:
            good[event["type"]].append(event)
    return good, rejected


def collapse(events: list[dict], key: str) -> dict[str, dict]:
    """The newest event per `event[key]` by `ts`; of equal timestamps, the one
    that came later in the batch."""
    out: dict[str, dict] = {}
    for e in events:
        k = e[key]
        if k not in out or e["ts"] >= out[k]["ts"]:
            out[k] = e
    return out


def apply_status(cfg: Config, events: list[dict]) -> tuple[dict, dict]:
    """Apply status events to the known list with one `known.apply`.

    Collapses to the last status per orth, then maps each onto the known list.
    A word has one status, so each mapping also clears the others it could be
    sitting in:

    - known: mark, and take it out of the set-aside words
    - ignored: set it aside, and drop a hand-marked entry
    - unknown / learning: drop a hand-marked entry and any set-aside entry

    `learning` is held in learning.json (see `record_status`), not here: a
    learning word is still unknown to every metric. Returns the collapsed
    events and `known.apply`'s result; the caller recomputes once if
    `written`. Holds no lock itself.
    """
    final = collapse(events, "orth")
    by = {s: [o for o, e in final.items() if e["status"] == s] for s in STATUSES}
    res = known.apply(
        cfg,
        mark=by["known"],
        unmark=by["ignored"] + by["unknown"] + by["learning"],
        skip=by["ignored"],
        unskip=by["known"] + by["unknown"] + by["learning"],
    )
    return final, res


def unsettable(cfg: Config, final: dict[str, dict]) -> list[str]:
    """The orth keys of `final` that were asked to be unknown or learning and
    are still known after `apply_status`: they came from an import (by orth or
    by lemma), which this API will not edit."""
    orth, lemma = known.load_sets(cfg)
    return [
        o for o, e in final.items()
        if e["status"] in ("unknown", "learning")
        and (o in orth or (e.get("lemma") and e["lemma"] in lemma))
    ]


def record_status(cfg: Config, final: dict[str, dict], skipped: list[str]) -> None:
    """Keep learning.json in step with the final statuses. A learning word
    leaves it on any other status; one the server could not make unknown
    (`skipped`) is not added, because it is known. An existing entry keeps its
    `added_at`, so a replay changes nothing."""
    skipped_set = set(skipped)
    data = load_json(cfg.reader_learning, {})
    words = dict(data.get("words") or {})
    before = json.dumps(words, sort_keys=True)
    for orth, e in final.items():
        if e["status"] != "learning" or orth in skipped_set:
            words.pop(orth, None)
            continue
        old = words.get(orth) or {}
        words[orth] = {
            "lemma": e.get("lemma", ""), "reading": e.get("reading", ""),
            "book_id": e.get("book_id", ""), "sentence": e.get("sentence", ""),
            "added_at": old.get("added_at") or iso(e["ts"]),
        }
    if json.dumps(words, sort_keys=True) != before:
        save_json(cfg.reader_learning, {"schema": SCHEMA_VERSION, "words": words})


def append_lookups(cfg: Config, events: list[dict]) -> int:
    """Append lookups to lookups.jsonl, skipping any `id` already in it (and
    repeats within the batch). Returns how many lines were added."""
    path = cfg.reader_lookups
    seen: set[str] = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["id"])
                except (ValueError, KeyError, TypeError):
                    continue  # a torn last line from a crash; ignore it
    fresh = []
    for e in events:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        fresh.append({
            "id": e["id"], "ts": iso(e["ts"]), "orth": e["orth"], "lemma": e.get("lemma", ""),
            "book_id": e.get("book_id", ""), "sentence": e.get("sentence", ""),
        })
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fresh))
    return len(fresh)


def merge_progress(cfg: Config, events: list[dict]) -> int:
    """Last write wins by `ts`, per stable key: an event no newer than the one
    on file changes nothing, so out-of-order arrival and replays are safe.
    Returns how many entries were updated."""
    newest = collapse(events, "stable_key")
    stored = load_progress(cfg)
    changed = 0
    for key, e in newest.items():
        have = stored.get(key)
        if have and parse_ts(have["updated_at"]) >= e["ts"]:
            continue
        stored[key] = {
            "book_id": e.get("book_id", ""), "sha256": e.get("sha256", ""),
            "cfi": e["cfi"], "fraction": e.get("fraction", 0.0), "updated_at": iso(e["ts"]),
        }
        changed += 1
    if changed:
        save_json(cfg.reader_progress, stored)
    return changed


def merge_audio_progress(cfg: Config, events: list[dict]) -> int:
    """Last write wins by `ts`, per `ab_id`, as `merge_progress` does for books.
    An id the audiobook has since left (it gained a key, audiobook_rekey.py) is
    read as the one it went to. Returns how many entries were updated."""
    aliases: dict = load_json(cfg.audiobook_aliases, {})
    newest = collapse([{**e, "ab_id": aliases.get(e["ab_id"], e["ab_id"])} for e in events], "ab_id")
    stored = load_audio_progress(cfg)
    changed = 0
    for key, e in newest.items():
        have = stored.get(key)
        if have and parse_ts(have["updated_at"]) >= e["ts"]:
            continue
        stored[key] = {"track": e["track"], "seconds": e["seconds"], "updated_at": iso(e["ts"])}
        changed += 1
    if changed:
        save_json(cfg.reader_audio_progress, stored)
    return changed
