"""Local full-text search index (SQLite FTS5, trigram tokenizer).

Replaces the Supabase/PGroonga mirror (see docs/dev/05-gotchas.md for why
that was abandoned): the trigram tokenizer gives PGroonga's word-boundary-free
bigram search the same way — no dictionary, no language config — entirely
locally, no server process, no leak risk.

build() runs on every build.build(cfg) call, but only a table whose source
file's (mtime_ns, size) changed since the last build is actually dropped and
re-tokenized — tracked in a `meta` table inside the index itself. `catalog`'s
source (data.json) is immutable per CLAUDE.md, so in steady state it is the
only one of the three that's expensive, and after the first build it never
needs to move again. An unchanged table still has to get from the old file
into the new one, which is done with sqlite3's backup() (raw page copy) rather
than a row-by-row INSERT...SELECT, since the latter would re-tokenize through
FTS5's insert trigger and defeat the whole point. Written to a temp file and
swapped in with Path.replace(), same atomicity pattern as index_store.save_json,
so a reader mid-request never sees a half-written index.

A table's signature also folds in its indexed-columns tuple, not just the
source stamp, so a code change to _SOURCES (e.g. adding an indexed column)
forces a rebuild even though it doesn't touch the source file's mtime.

FTS5's trigram tokenizer only indexes runs of 3+ characters, so a MATCH query
shorter than that can't find anything — search() raises rather than returning
an empty match set, so callers (server.py) fall back to substring search
instead of reporting a false "no matches".
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import audiobook_catalog, romaji, textnorm
from .config import Config
from .index_store import load_json

# Bump when how `readings` is computed changes: it is folded into each table's
# signature, so the tables rebuild even though no source file did.
READINGS_VERSION = 1

# kind -> (source path getter, indexed columns). The id is always the JSON key.
# `readings` is not a field of the entry: it is computed (see _readings) so that
# romaji (murakami), hiragana (かぜがつよく) and the EPUB's own kana readings find
# Japanese titles, authors and series.
_SOURCES = {
    "catalog": (lambda cfg: (cfg.audiobook_data, cfg.audiobook_catalog_extra),
                ("title", "series", "author", "narrators", "description_full", "readings")),
    "audiobooks": (lambda cfg: (cfg.audiobook_index,),
                   ("title", "author", "series", "readings")),
    "epub": (lambda cfg: (cfg.library_index,),
             ("title", "author", "series_name", "readings")),
}
# What the readings are made from, per kind: fields to romanize/read, and fields
# that already hold a kana reading (the OPF's `file-as`).
_READ_FROM = {
    "catalog": (("title", "series", "author", "narrators"), ()),
    "audiobooks": (("title", "author", "series"), ()),
    "epub": (("title", "author", "series_name"), ("title_reading", "author_reading")),
}


def _readings(kind: str, entry: dict) -> str:
    """Space-joined search forms of an entry's Japanese text: hiragana, romaji
    (spaced and compact), the text without its spaces, and the OPF's own readings, normalized to hiragana."""
    fields, kana_fields = _READ_FROM[kind]
    forms: list[str] = []
    for f in fields:
        text = str(entry.get(f) or "")
        forms += romaji.reading_forms(text)
        if romaji.has_japanese(text):
            # 村上春樹 should find the author stored as 村上 春樹.
            forms.append("".join(text.split()))
    for f in kana_fields:
        if entry.get(f):
            forms.append(textnorm.to_hiragana(str(entry[f])))
    return " ".join(dict.fromkeys(forms))


def _stamp(paths: tuple[Path, ...]) -> tuple[int, int] | None:
    """(mtime_ns, size) of the source file(s): the newest mtime and the summed
    size, so a change to any one of them changes the stamp. None if none exist."""
    stats = []
    for path in paths:
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        stats.append((st.st_mtime_ns, st.st_size))
    if not stats:
        return None
    return max(s[0] for s in stats), sum(s[1] for s in stats)


def _rebuild_table(con: sqlite3.Connection, kind: str, columns: tuple[str, ...],
                    cfg: Config, source_paths: tuple[Path, ...]) -> int:
    cols_sql = ", ".join(columns)
    con.execute(f"drop table if exists {kind}")
    con.execute(
        f"create virtual table {kind} using fts5"
        f"(id UNINDEXED, {cols_sql}, tokenize='trigram')"
    )
    data = audiobook_catalog.load(cfg) if kind == "catalog" else load_json(source_paths[0], {})
    rows = [
        (key, *(_readings(kind, entry) if c == "readings" else str(entry.get(c) or "")
                for c in columns))
        for key, entry in data.items()
    ]
    placeholders = ", ".join(["?"] * (len(columns) + 1))
    con.executemany(f"insert into {kind} values ({placeholders})", rows)
    return len(rows)


def build(cfg: Config) -> dict[str, int]:
    """Rebuild whichever tables' sources changed since the last build; carry
    the rest forward unchanged. Returns row counts per kind."""
    path = cfg.search_db
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)

    dst = sqlite3.connect(tmp)
    dst.isolation_level = None
    try:
        if path.exists():
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                src.backup(dst)
            finally:
                src.close()

        dst.execute(
            "create table if not exists meta ("
            " kind text primary key, mtime_ns integer, size integer,"
            " columns text, rows integer)"
        )
        existing_tables = {
            row[0] for row in dst.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        prev_meta = {
            row[0]: row[1:] for row in
            dst.execute("select kind, mtime_ns, size, columns, rows from meta")
        }

        counts: dict[str, int] = {}
        dst.execute("begin")
        for kind, (source_fn, columns) in _SOURCES.items():
            source_paths = source_fn(cfg)
            stamp = _stamp(source_paths)
            columns_sig = ",".join(columns) + f"|readings v{READINGS_VERSION}"
            prev = prev_meta.get(kind)
            unchanged = (
                kind in existing_tables
                and stamp is not None
                and prev is not None
                and prev[:2] == stamp
                and prev[2] == columns_sig
            )
            if unchanged:
                counts[kind] = prev[3]
                continue

            counts[kind] = _rebuild_table(dst, kind, columns, cfg, source_paths)
            mtime_ns, size = stamp or (0, 0)
            dst.execute(
                "insert into meta (kind, mtime_ns, size, columns, rows)"
                " values (?, ?, ?, ?, ?)"
                " on conflict(kind) do update set"
                "  mtime_ns=excluded.mtime_ns, size=excluded.size,"
                "  columns=excluded.columns, rows=excluded.rows",
                (kind, mtime_ns, size, columns_sig, counts[kind]),
            )
        dst.execute("commit")
    finally:
        dst.close()

    tmp.replace(path)
    return counts


def search(cfg: Config, q: str, kind: str, limit: int) -> list[str]:
    """Matching ids for `kind` ("catalog" -> isbn, "audiobooks" -> ab_id,
    "epub" -> book_id). Raises if the index doesn't exist yet or the query is
    too short to tokenize — callers should fall back to substring search."""
    table = {"catalog": "catalog", "audiobooks": "audiobooks", "epub": "epub"}.get(kind)
    if table is None:
        raise ValueError(f"unknown search kind: {kind!r}")
    if len(q) < 3:
        raise ValueError("query too short for trigram search")
    if not cfg.search_db.exists():
        raise FileNotFoundError("search index not built yet")

    con = sqlite3.connect(f"file:{cfg.search_db}?mode=ro", uri=True)
    try:
        # Phrase-quoted so MATCH requires the trigrams in order (substring
        # semantics) rather than an unordered AND of the query's trigrams.
        # Doubling embedded quotes is FTS5's own phrase-escaping rule. A query in
        # katakana is also tried as hiragana, the script `readings` is stored in.
        match = " OR ".join('"' + v.replace('"', '""') + '"' for v in query_variants(q))
        cur = con.execute(
            f"select id from {table} where {table} match ? limit ?", (match, limit)
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        con.close()


def query_variants(q: str) -> list[str]:
    """The strings to look for: the query as typed, and its hiragana form (so
    カゼ finds かぜ, and ｶｾﾞ finds both)."""
    return list(dict.fromkeys([q, textnorm.to_hiragana(q)]))


def _like_escape(s: str) -> str:
    """Make `s` literal inside a LIKE pattern that uses `escape '\\'`."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def readings_like(cfg: Config, q: str, kind: str, limit: int) -> list[str]:
    """Ids whose `readings` contain `q` — a plain scan, for the queries too short
    for the trigram index (one or two kana). [] if the index isn't there."""
    if kind not in _SOURCES or not cfg.search_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{cfg.search_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        likes = [f"%{_like_escape(v.lower())}%" for v in query_variants(q)]
        where = " or ".join("readings like ? escape '\\'" for _ in likes)
        return [r[0] for r in con.execute(f"select id from {kind} where {where} limit ?", (*likes, limit))]
    except sqlite3.Error:
        return []
    finally:
        con.close()
