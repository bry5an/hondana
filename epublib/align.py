"""Audio↔text sync: match an audiobook's SRT cues to a book's sentences.

The phone highlights the sentence being spoken and jumps the player to a tapped
one. It has the reader pack's `sents` spans but not the sentence text, so the
matching happens here: an SRT produced elsewhere (SubPlz / Whisper) is
ingested, its cues are located in the book's text, and the result is stored
against the pack's own anchors:

    {"schema": 4, "book_id": …, "sha256": …, "audiobook": "<ab_id>",
     "srt_sha": …, "source": "transcript", "coverage": 0.96, "ok": true,
     "matched": 812, "estimated": 20, "total": 846,
     "docs": [{"index": 3, "fnv": "9a3f01bc", "len": [14, 6, …], "est": [7, 8],
               "sents": [[startMs, endMs, part?] | null, …]}, …]}

`docs[].sents[i]` lines up with the pack's `docs[].sents[i]` (both come from
`annotate._sentence_spans`); `null` marks a sentence no cue matched. `fnv` is
the pack document's, so the phone can refuse a map built against another text.
`part` is the file name of the track a sentence is in, for an audiobook that
is a folder of tracks (time is per part there); it is absent for a single file.
`len` is each sentence's length in base text, for the phone to judge a pace by;
`est` lists the sentences whose time is estimated rather than heard (interpolated
between heard neighbours, or moved by `retime`). `matched` counts the heard
ones, `estimated` the others; `coverage` is both over `total`.
`ok` is `coverage >= OK_COVERAGE`: below it the map is still served, so the
phone can decline to highlight rather than show one that drifts.

`source` says which SRT the map came from. `transcript` is a plain
Whisper transcript (SubPlz `gen`), matched by `match_transcript`; `synced` is
SubPlz `sync` output, whose cues are the book's own lines with times SubPlz
chose, matched by `match_cues`. A transcript wins when the book has both: SubPlz
`sync` interpolates the lines it can't place, which drifted minutes behind the
voice, while a transcript's times are what was heard.

Matching works on the base text: NFKC, katakana folded to hiragana, and
everything that isn't a letter or number dropped (ruby readings are already
absent from `dom_text`). A cue is located by voting with its character
bigrams, in a window ahead of the previous match (a wide search for a long cue
the narrator jumped to), then verified with difflib. Time is linear in the
book's text across a matched cue, so a cue that spans two sentences, or a
sentence split over two cues, gets a boundary inside the cue.

Storage (data/alignments/): the raw SRT, `{book_id}.srt` (or one
`{book_id}.{NNN}.srt` per mp3, by position in the audiobook's file list), is
the hand-supplied part and is kept so the map can be re-matched without
re-transcribing; a transcript is `{book_id}.transcript.srt` (or
`{book_id}.transcript.{NNN}.srt`). `{book_id}.json` is the derived map, rebuilt
from the SRT when the schema or the book's sha256 changes.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import unicodedata
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from . import annotate
from .config import Config
from .index_store import write_bytes_atomic

ALIGN_SCHEMA = 4  # 4: a plain transcript is matched globally; `source`, per-doc `len` and `est`
# 3: squeezed runs of cues are spread back out over the time they took
# 2: the matcher no longer lets a repeated line skip ahead
OK_COVERAGE = 0.90
KINDS = ("synced", "transcript")  # what an uploaded SRT is: SubPlz `sync` (the book's lines) or `gen` (what was heard)

_BIN = 8            # offset votes are pooled in bins this wide (insertions shift offsets)
_PAD = 24           # slack around the voted position when verifying a cue
_NEAR_MIN = 1500    # how far ahead of the last match a cue is looked for first
_WIDE_MIN_LEN = 8   # only a cue this long is distinctive enough to jump for
_MIN_RATIO = 0.6    # share of a cue's characters difflib must find in the book
_WIDE_RATIO = 0.75  # ... when the match is far from where the narrator was
_MIN_SENT_COVER = 0.5  # share of a sentence's characters cues must cover
_TRUST_GAP = 120    # a hit further than this past the cursor must be confirmed: a next cue just behind it
_CONFIRM_CUES = 2   # how many following cues are tried to confirm one
_CANDIDATES = 4     # voted places verified per cue

# retime(): Whisper sometimes loses the audio for a stretch and writes a run of cues packed into
# a few seconds after a long hole, which the phone shows as a highlight that blanks and then races
# ahead of the voice. Rates are characters of base text per second.
_RETIME_MIN_SENTENCES = 30  # a book with fewer cues has no usual pace to judge a run by
_DENSE = 2.4        # a sentence read faster than this many times the book's median is squeezed
_DENSE_GAP = 3      # ordinary sentences allowed between two squeezed ones in one run
_DENSE_RUN = 4      # squeezed sentences a run needs to be repaired
_DENSE_JOINT = 1.6  # ... and its pace as a whole, over this many times the median
_PACED = 1.4        # the run is given the time ahead of it until it reads at most this fast
_LOOKBACK = 12      # sentences ahead of the run that may lend their time
_LEAN = 0.7         # time borrowed from a hole that makes the run slower than this is too much


class AlignError(Exception):
    """The alignment can't be produced or served. `reason`: no_audiobook |
    audiobook_changed | bad_srt | bad_part."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass(frozen=True)
class Cue:
    start: int  # ms
    end: int
    text: str
    part: str | None = None


# ── SRT ──────────────────────────────────────────────────────────────────────

_TIME = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _ms(h: str, m: str, s: str, frac: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))


def parse_srt(text: str, part: str | None = None) -> list[Cue]:
    """The cues of an SRT, in file order. Lenient: BOM, CRLF, `.` for `,`, a
    missing index line, a cue with no text (dropped)."""
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            m = _TIME.search(ln)
            if m:
                body = " ".join(x.strip() for x in lines[i + 1:]).strip()
                if body:
                    g = m.groups()
                    cues.append(Cue(_ms(*g[:4]), _ms(*g[4:]), body, part))
                break
    return cues


# ── matching ─────────────────────────────────────────────────────────────────

_READING = re.compile(r"[（(][ぁ-ゖァ-ヺー]+[）)]")


def norm(s: str) -> str:
    """The comparison form: NFKC, katakana as hiragana, letters and numbers only."""
    out = []
    for ch in unicodedata.normalize("NFKC", s):
        if unicodedata.category(ch)[0] in ("L", "N"):
            if "ァ" <= ch <= "ヶ":
                ch = chr(ord(ch) - 0x60)
            out.append(ch.lower())
    return "".join(out)


def cue_norm(text: str) -> str:
    """A cue's comparison form; kana readings in brackets (漢字（かんじ）) go."""
    return norm(_READING.sub("", unicodedata.normalize("NFKC", text)))


def _bigrams(b: str) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for i in range(len(b) - 1):
        index.setdefault(b[i:i + 2], []).append(i)
    return index


def _locate(b: str, index: dict, c: str, lo: int, hi: int, min_ratio: float):
    """`(start, end)` in `b` of cue text `c` searched for within `[lo, hi)`, or None."""
    m = len(c)
    if m < 4:  # too short to vote: an exact hit close behind the cursor or nothing
        i = b.find(c, lo, min(hi, lo + 60))
        return (i, i + m) if i >= 0 else None
    # Of the places that fit equally well the nearest wins: a short line is most likely the next one.
    hits = _candidates(b, index, c, lo, hi, min_ratio)
    best = max(hits, key=lambda h: (round(h[2], 1), -h[0]), default=None)
    return best[:2] if best else None


def _candidates(b: str, index: dict, c: str, lo: int, hi: int, min_ratio: float) -> list[tuple[int, int, float]]:
    """The few places in `b[lo:hi]` where cue text `c` (4 characters or more) fits, as
    `(start, end, ratio)`: voted by its bigrams, each verified with difflib."""
    m = len(c)
    votes: Counter = Counter()
    for k in range(m - 1):
        pos = index.get(c[k:k + 2])
        if not pos:
            continue
        i, j = bisect_left(pos, lo), bisect_left(pos, hi)
        if j - i > 30:  # a bigram everywhere says nothing about where
            continue
        for p in pos[i:j]:
            votes[(p - k) // _BIN] += 1
    if not votes:
        return []
    # Vote counts alone mislead (a sentence that repeats a phrase out-votes the exact
    # line), so the few best places are each verified.
    scored = sorted(((votes[bn] + votes[bn + 1], bn) for bn in votes), key=lambda t: (-t[0], t[1]))
    need = max(3, int((m - 1) * 0.2))
    hits, tried = [], []
    for n, bn in scored:
        if n < need or len(tried) == _CANDIDATES:
            break
        if any(abs(bn - t) <= 2 for t in tried):  # the same place, one bin over
            continue
        tried.append(bn)
        r0 = max(lo, bn * _BIN - _PAD)
        region = b[r0:bn * _BIN + 2 * _BIN + m + _PAD]
        blocks = [x for x in SequenceMatcher(None, c, region, autojunk=False).get_matching_blocks() if x.size]
        if not blocks:
            continue
        ratio = sum(x.size for x in blocks) / m
        if ratio >= min_ratio:
            hits.append((r0 + blocks[0].b, r0 + blocks[-1].b + blocks[-1].size, ratio))
    return hits


def match_cues(sentences: list[str], cues: list[Cue]) -> list[list | None]:
    """One `[startMs, endMs]` (+ part name when the cues have one) or None per
    sentence. `sentences` are the sentences' plain text, in book order. For an SRT
    whose cues are the book's own lines (SubPlz `sync`); `match_transcript` is for
    what Whisper heard."""
    return match_synced(sentences, cues)[0]


def match_synced(sentences: list[str], cues: list[Cue]) -> tuple[list[list | None], set[int]]:
    """`match_cues`, with the indices of the rows `retime` moved (their time is a guess)."""
    norms, b, bounds = _book(sentences)
    index = _bigrams(b)
    cue_texts = [cue_norm(cue.text) for cue in cues]
    anchors: list[tuple[int, int, Cue]] = []  # (b start, b end, cue), increasing

    def confirmed(k: int, end: int) -> bool:
        """Whether one of the next cues after `k` is found just past `end`. A short
        or common line ("よろしくね", "ありがとうございます") turns up many times in a
        book; a hit far from where the narrator was is only believed if what is
        said next follows it, or the cursor would skip everything between."""
        rest = [t for t in cue_texts[k + 1:k + 1 + 4 * _CONFIRM_CUES] if t][:_CONFIRM_CUES]
        if not rest:
            return True
        return any(_locate(b, index, t, end, end + 2 * _TRUST_GAP + 6 * len(t), _MIN_RATIO)
                   for t in rest)

    cursor = 0
    for k, cue in enumerate(cues):
        c = cue_texts[k]
        if not c or cursor >= len(b):
            continue
        hit = _locate(b, index, c, cursor, cursor + max(_NEAR_MIN, 10 * len(c)), _MIN_RATIO)
        if hit is None and len(c) >= _WIDE_MIN_LEN:
            hit = _locate(b, index, c, cursor, len(b), _WIDE_RATIO)
        if hit and hit[0] - cursor > _TRUST_GAP and not confirmed(k, hit[1]):
            hit = None
        if hit and hit[1] > hit[0]:
            anchors.append((hit[0], hit[1], cue))
            cursor = hit[1]
    rows = _rows(bounds, anchors, cues)
    before = [list(r) if r else None for r in rows]
    retime(rows, [len(n) for n in norms])
    return rows, {i for i, (r, was) in enumerate(zip(rows, before)) if r != was}


def _book(sentences: list[str]) -> tuple[list[str], str, list[tuple[int, int]]]:
    """The sentences' comparison forms, the book's base text they make, and where
    each sentence sits in it."""
    norms = [norm(s) for s in sentences]
    bounds, at = [], 0
    for n in norms:
        bounds.append((at, at + len(n)))
        at += len(n)
    return norms, "".join(norms), bounds


def _rows(bounds: list[tuple[int, int]], anchors: list[tuple[int, int, Cue]], cues: list[Cue]) -> list[list | None]:
    """Each sentence's time from the cues found in its text (`anchors`, in book order):
    linear in the text across a cue, None when cues cover under half of it."""
    part_end: dict[str | None, int] = {}
    for cue in cues:
        part_end[cue.part] = max(part_end.get(cue.part, 0), cue.end)

    def at_time(pos: int, a: tuple[int, int, Cue]) -> int:
        bs, be, cue = a
        return round(cue.start + (pos - bs) / (be - bs) * (cue.end - cue.start))

    out: list[list | None] = []
    ai = 0
    for s, e in bounds:
        while ai < len(anchors) and anchors[ai][1] <= s:
            ai += 1
        hits = []
        j = ai
        while j < len(anchors) and anchors[j][0] < e:
            hits.append(anchors[j])
            j += 1
        covered = sum(min(e, be) - max(s, bs) for bs, be, _ in hits)
        if not hits or e <= s or covered / (e - s) < _MIN_SENT_COVER:
            out.append(None)
            continue
        first, last = hits[0], hits[-1]
        start = at_time(max(s, first[0]), first)
        part = first[2].part
        if last[2].part == part:
            end = at_time(min(e, last[1]), last)
        else:  # time is per part: a sentence can't run across the join
            end = part_end[part]
        row: list = [start, max(end, start + 1)]
        if part is not None:
            row.append(part)
        out.append(row)
    return out


def match_transcript(sentences: list[str], cues: list[Cue]) -> tuple[list[list | None], set[int]]:
    """`match_cues` for a plain transcript (SubPlz `gen`): what Whisper heard, at the
    times it heard it. Returns the rows and the indices of the rows whose time is
    estimated (between two heard neighbours) rather than heard.

    A cursor that only moves forward is what let SubPlz's `sync` drift for minutes:
    one short line placed on the wrong speech drags every line after it along. Here
    each cue of four or more characters is looked for in the whole book on its own,
    and the places kept are the heaviest chain that moves
    forward in the book as it moves forward in time. A wrong place is then simply
    left out, because keeping it would mean dropping every cue it jumps over. Short
    lines (「はい」) and cues the whole-book search missed are looked for afterwards,
    only between the chained cues either side of them."""
    norms, b, bounds = _book(sentences)
    index = _bigrams(b)
    texts = [cue_norm(cue.text) for cue in cues]
    chained = _chain([_candidates(b, index, t, 0, len(b), _MIN_RATIO) if len(t) >= 4 else [] for t in texts], texts)
    anchors: list[tuple[int, int, Cue]] = []
    ahead = sorted(chained)  # cue indices with a chained place, to bound each gap from above
    nxt = 0
    lo = 0
    for k, cue in enumerate(cues):
        while nxt < len(ahead) and ahead[nxt] <= k:
            nxt += 1
        if k in chained:
            hit = chained[k]
        elif texts[k]:
            hi = chained[ahead[nxt]][0] + _CHAIN_SLACK if nxt < len(ahead) else len(b)
            hit = _locate(b, index, texts[k], lo, hi, _MIN_RATIO)
            if hit and not (lo - _CHAIN_SLACK <= hit[0] and hit[1] <= hi + _CHAIN_SLACK):
                hit = None
        else:
            hit = None
        if hit and hit[1] > hit[0] and (not anchors or hit[1] > anchors[-1][1]):
            anchors.append((hit[0], hit[1], cue))
            lo = hit[1]
    rows = _rows(bounds, anchors, cues)
    return rows, _estimate(rows, [len(n) for n in norms])


_CHAIN_SLACK = 8         # characters of the book two neighbouring cues' places may share
_EST_PACE = (3.0, 15.0)  # chars/s an unheard run between two heard sentences must make to be estimated


def _chain(candidates: list[list[tuple[int, int, float]]], texts: list[str]) -> dict[int, tuple[int, int]]:
    """The heaviest set of places, at most one per cue, that moves forward in the book
    as the cues move forward in time: a weighted longest increasing subsequence, a
    place weighing the characters it matched. `{cue index: (start, end)}`."""
    size = max((e for hits in candidates for _, e, _ in hits), default=0) + 2
    tree = [(0.0, -1)] * (size + 1)  # Fenwick tree of (best weight, item) over place ends

    def best_before(pos: int) -> tuple[float, int]:
        got, i = (0.0, -1), min(pos, size)
        while i > 0:
            if tree[i][0] > got[0]:
                got = tree[i]
            i -= i & -i
        return got

    def put(pos: int, value: tuple[float, int]) -> None:
        i = pos
        while i <= size:
            if value[0] > tree[i][0]:
                tree[i] = value
            i += i & -i

    items: list[tuple[int, int, int, int]] = []  # (cue, start, end, previous item)
    top = (0.0, -1)
    for k, hits in enumerate(candidates):
        scored = []
        for s, e, ratio in hits:
            # Ends are kept strictly increasing; a place may overlap the one before by a little.
            weight, prev = best_before(min(s + _CHAIN_SLACK, e - 1) + 1)
            scored.append((weight + ratio * len(texts[k]), s, e, prev))
        for weight, s, e, prev in scored:  # after all of them, so one cue can't chain to itself
            items.append((k, s, e, prev))
            put(e + 1, (weight, len(items) - 1))
            if weight > top[0]:
                top = (weight, len(items) - 1)
    out: dict[int, tuple[int, int]] = {}
    i = top[1]
    while i >= 0:
        k, s, e, prev = items[i]
        out[k] = (s, e)
        i = prev
    return out


def _estimate(rows: list[list | None], sizes: list[int]) -> set[int]:
    """Times the sentences no cue covered between two heard ones, by their length,
    when that reads at a speaking pace; the rest stay None. Changes `rows` in place
    and returns the indices it filled. Time is per part, so a run never spans a join."""
    filled: set[int] = set()
    prev, k = None, 0
    while k < len(rows):
        if rows[k] is not None:
            prev, k = k, k + 1
            continue
        j = k
        while j < len(rows) and rows[j] is None:
            j += 1
        if prev is not None and j < len(rows):
            a, z = rows[prev], rows[j]
            part = a[2] if len(a) > 2 else None
            chars = sum(sizes[k:j])
            span = (z[0] - a[1]) / 1000
            if part == (z[2] if len(z) > 2 else None) and chars and span > 0 \
                    and _EST_PACE[0] <= chars / span <= _EST_PACE[1]:
                at = float(a[1])
                for x in range(k, j):
                    if not sizes[x]:
                        continue
                    took = (z[0] - a[1]) * sizes[x] / chars
                    rows[x] = [round(at), max(round(at + took), round(at) + 1)] + ([part] if part else [])
                    at += took
                    filled.add(x)
        k = j
    return filled


def looks_synced(sentences: list[str], cues: list[Cue]) -> bool:
    """Whether an SRT's cues are the book's own lines, as SubPlz `sync` writes them,
    rather than what Whisper heard: most of them equal a sentence exactly."""
    own = {norm(s) for s in sentences} - {""}
    texts = [t for t in (cue_norm(c.text) for c in cues) if t]
    return bool(texts) and sum(t in own for t in texts) >= 0.8 * len(texts)


def retime(rows: list[list | None], sizes: list[int]) -> list[list | None]:
    """Spreads out runs of sentences whose cues are packed too tightly to be spoken.

    `rows` are `match_cues`' rows and `sizes` the length of each sentence's base text. A run of
    four or more sentences read at over 2.4 times the book's median pace, with ordinary pace
    nowhere near it, is where the transcript lost the audio: the voice is somewhere ahead of
    the run (in the hole before it, or in the stretched cues of the sentences before the hole).
    The run takes the time from the nearest sentence before it that makes its pace human again,
    divided between the sentences by their length. Where that borrows far more time than the
    run needs, the run is read at the book's usual pace back from its end instead and the rest of the
    hole stays silent. Rows are changed in place. Time is per part, so a part is judged alone."""
    by_part: dict = {}
    for i, row in enumerate(rows):
        if row and sizes[i] > 0:
            by_part.setdefault(row[2] if len(row) > 2 else None, []).append(i)
    pace = sorted(sizes[i] / max((rows[i][1] - rows[i][0]) / 1000, 0.05) for ids in by_part.values() for i in ids)
    if len(pace) < _RETIME_MIN_SENTENCES:
        return rows
    median = pace[len(pace) // 2]
    # The pace with the pauses between sentences in it, over stretches without a hole: what a run
    # that has to be placed afresh takes per character.
    chars = seconds = 0.0
    for ids in by_part.values():
        for a, b in zip(ids, ids[1:]):
            step = (rows[b][0] - rows[a][0]) / 1000
            if 0 < step < 5 and sizes[a] / step <= _DENSE * median:  # not a hole, not squeezed
                chars += sizes[a]
                seconds += step
    usual = chars / seconds if seconds else median
    for ids in by_part.values():
        _retime_part(rows, sizes, ids, median, usual)
    return rows


def _retime_part(rows: list, sizes: list[int], ids: list[int], median: float, usual: float) -> None:
    def joint(a: int, b: int) -> float:
        span = max((rows[ids[b]][1] - rows[ids[a]][0]) / 1000, 0.05)
        return sum(sizes[ids[k]] for k in range(a, b + 1)) / span

    fast = [k for k, i in enumerate(ids) if sizes[i] / max((rows[i][1] - rows[i][0]) / 1000, 0.05) > _DENSE * median]
    runs: list[list[int]] = []  # [first, last, how many fast]
    for k in fast:
        if runs and k - runs[-1][1] <= _DENSE_GAP + 1:
            runs[-1][1] = k
            runs[-1][2] += 1
        else:
            runs.append([k, k, 1])
    quick = set(fast)
    for run in runs:
        # A lone fast sentence just after ordinary ones is the next passage's, not this run's tail:
        # cutting the run there keeps the well-timed sentences after the run where they were.
        while run[2] > 1 and run[1] - 1 not in quick and run[1] - 2 not in quick:
            run[1] = max(k for k in fast if k < run[1] and k >= run[0])
            run[2] -= 1
    spans: list[list[int]] = []
    for first, last, count in runs:
        if count < _DENSE_RUN or joint(first, last) <= _DENSE_JOINT * median:
            continue
        start = next((first - back for back in range(1, min(_LOOKBACK, first) + 1)
                      if joint(first - back, last) <= _PACED * median), None)
        if start is None:
            continue
        if spans and start <= spans[-1][1]:  # two runs sharing the time before them are one
            spans[-1][1] = last
            spans[-1][0] = min(spans[-1][0], start)
        else:
            spans.append([start, last])
    for start, last in spans:
        end = rows[ids[last]][1]
        if joint(start, last) < _LEAN * median and start < last:
            # The sentence ahead only brought a hole with it: leave it, and read the run at the usual
            # pace back from its end, but not into the sentence before it or past its own first cue.
            start += 1
            own = rows[ids[start]][0]
            total = sum(sizes[ids[k]] for k in range(start, last + 1))
            begin = max(rows[ids[start - 1]][1], end - round(1000 * total / usual))
            begin = min(begin, own)
        else:
            begin = rows[ids[start]][0]
        total = sum(sizes[ids[k]] for k in range(start, last + 1))
        done = 0
        for k in range(start, last + 1):
            row = rows[ids[k]]
            row[0] = round(begin + (end - begin) * done / total)
            done += sizes[ids[k]]
            row[1] = round(begin + (end - begin) * done / total)


# ── storage ──────────────────────────────────────────────────────────────────

def _srt_path(cfg: Config, book_id: str, index: int | None = None, kind: str = "synced") -> Path:
    stem = book_id if kind == "synced" else f"{book_id}.{kind}"
    return cfg.alignments_dir / (f"{stem}.srt" if index is None else f"{stem}.{index:03d}.srt")


def work_dir(cfg: Config, book_id: str) -> Path:
    """Scratch space of a running or interrupted `align_jobs` run. It sits
    in a dot-folder, which the `{book_id}.*` sweep of `delete` and the purge never match."""
    return cfg.alignments_dir / ".work" / book_id


def book_text(cfg: Config, book: dict) -> str:
    """The book's sentences, one per line: what SubPlz is given as `{text}`, so
    the cues it returns are the sentences `match_cues` compares against. Written
    by `align_jobs` for a run here and served by `GET …/alignment/text` for one
    on another machine. A line break inside a sentence would split it.
    Raises annotate.NoPack for a book that can't have sentences."""
    sentences = [" ".join(t.split()) for d in annotate.sentence_texts(cfg, book) for t in d["texts"]]
    return "\n".join(s for s in sentences if s) + "\n"


def numbered_text(cfg: Config, book: dict) -> str:
    """Every pack sentence as `doc:sentence<TAB>text`, the empty ones too: `book_text`
    leaves those out, so its line numbers aren't always sentence numbers. For reading
    a line against a map."""
    return "".join(f"{d['index']}:{i}\t{' '.join(t.split())}\n"
                   for d in annotate.sentence_texts(cfg, book) for i, t in enumerate(d["texts"]))


def map_path(cfg: Config, book_id: str) -> Path:
    return cfg.alignments_dir / f"{book_id}.json"


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _book_lock(book_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(book_id, threading.Lock())


def _sources(cfg: Config, book_id: str, files: list[str] | None,
             kind: str | None = None) -> list[tuple[str | None, Path]]:
    """The stored SRTs of a book, in play order, as `(part name, path)`: of `kind`,
    or when it is None the kind the map is made from (`source_kind`)."""
    kind = kind or source_kind(cfg, book_id, files)
    if kind is None:
        return []
    if files:
        found = [(f, _srt_path(cfg, book_id, i, kind)) for i, f in enumerate(files)]
        return [(f, p) for f, p in found if p.exists()]
    p = _srt_path(cfg, book_id, kind=kind)
    return [(None, p)] if p.exists() else []


def source_kind(cfg: Config, book_id: str, files: list[str] | None) -> str | None:
    """Which stored SRTs the map is made from: a transcript when there is one, else
    the synced SRT; None when the book has neither."""
    for kind in ("transcript", "synced"):
        indices = range(len(files)) if files else [None]
        if any(_srt_path(cfg, book_id, i, kind).exists() for i in indices):
            return kind
    return None


def srt_bytes(cfg: Config, book_id: str, files: list[str] | None, part: str | None,
              kind: str | None = None) -> bytes | None:
    """The raw SRT: the whole file, or one mp3's when `part` names it; of `kind`, or
    the one the map is made from."""
    kind = kind or source_kind(cfg, book_id, files) or "synced"
    if files:
        if part not in files:
            raise AlignError("bad_part", "this audiobook has several files; pass ?part=<file name>")
        path = _srt_path(cfg, book_id, files.index(part), kind)
    else:
        path = _srt_path(cfg, book_id, kind=kind)
    return path.read_bytes() if path.exists() else None


def _srt_sha(sources: list[tuple[str | None, Path]]) -> str:
    h = hashlib.sha256()
    for name, path in sources:
        h.update(f"{name}\0".encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def etag(m: dict) -> str:
    return f'"{m["schema"]}-{m["sha256"][:16]}-{m["srt_sha"][:8]}"'


def _load_map(cfg: Config, book_id: str) -> dict | None:
    try:
        data = json.loads(map_path(cfg, book_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "schema" in data else None


def _rebuild(cfg: Config, book: dict, ab_id: str, files: list[str] | None, sha: str) -> dict:
    kind = source_kind(cfg, book["book_id"], files) or "synced"
    sources = _sources(cfg, book["book_id"], files, kind)
    cues: list[Cue] = []
    for name, path in sources:
        cues += parse_srt(path.read_text("utf-8-sig", errors="replace"), name)
    if not cues:
        raise AlignError("bad_srt", "no cues found in the SRT")
    docs = annotate.sentence_texts(cfg, book)
    sentences = [t for d in docs for t in d["texts"]]
    rows, est = (match_transcript if kind == "transcript" else match_synced)(sentences, cues)
    out_docs, at, timed = [], 0, 0
    for d in docs:
        n = len(d["texts"])
        sents = rows[at:at + n]
        timed += sum(1 for s in sents if s)
        out_docs.append({"index": d["index"], "fnv": d["fnv"], "len": [len(norm(t)) for t in d["texts"]],
                         "est": [i for i in range(n) if at + i in est], "sents": sents})
        at += n
    total = len(rows)
    coverage = round(timed / total, 4) if total else 0.0
    result = {
        "schema": ALIGN_SCHEMA, "book_id": book["book_id"], "sha256": sha, "audiobook": ab_id,
        "srt_sha": _srt_sha(sources), "source": kind, "coverage": coverage, "ok": coverage >= OK_COVERAGE,
        "matched": timed - len(est), "estimated": len(est), "total": total, "docs": out_docs,
    }
    write_bytes_atomic(map_path(cfg, book["book_id"]),
           json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return result


def ingest(cfg: Config, book: dict, ab_id: str | None, files: list[str] | None,
           srt_text: str, part: str | None = None, kind: str = "synced") -> dict:
    """Store an SRT for the book's linked audiobook and match it. Returns the map.
    `files` is the audiobook's mp3 list (None for a single m4b); `part` then
    names the mp3 this SRT is for. `kind` is one of `KINDS`."""
    return ingest_parts(cfg, book, ab_id, files, {part: srt_text}, kind)


def ingest_parts(cfg: Config, book: dict, ab_id: str | None, files: list[str] | None,
                 srts: dict[str | None, str], kind: str = "synced") -> dict:
    """`ingest` for several SRTs at once, keyed by mp3 file name (None for a
    single m4b): all are stored, then matched in one rebuild. If that fails,
    every SRT goes back to what it was, so a rejected run leaves the previous
    alignment alone. A `transcript` whose cues are the book's own lines is
    refused (`bad_srt`): that is SubPlz `sync` output, and matching it as heard
    would keep the times SubPlz made up."""
    if not ab_id:
        raise AlignError("no_audiobook", "link a local audiobook first")
    if kind not in KINDS:
        raise AlignError("bad_kind", f"kind is one of {', '.join(KINDS)}")
    paths: dict[str | None, Path] = {}
    for part, text in srts.items():
        if files:
            if part not in files:
                raise AlignError("bad_part", "this audiobook has several files; pass part=<file name>")
            paths[part] = _srt_path(cfg, book["book_id"], files.index(part), kind)
        else:
            paths[part] = _srt_path(cfg, book["book_id"], kind=kind)
        if not parse_srt(text):
            raise AlignError("bad_srt", "no cues found in the SRT")
    if kind == "transcript":
        sentences = [t for d in annotate.sentence_texts(cfg, book) for t in d["texts"]]
        if looks_synced(sentences, [c for text in srts.values() for c in parse_srt(text)]):
            raise AlignError("bad_srt", "these cues are the book's own lines (SubPlz sync output); "
                                        "send them as kind=synced, or make a transcript with subplz gen")
    sha = annotate.book_sha(cfg, book)
    with _book_lock(book["book_id"]):
        before = {part: path.read_bytes() if path.exists() else None for part, path in paths.items()}
        try:
            for part, path in paths.items():
                write_bytes_atomic(path, srts[part].encode("utf-8"))
            return _rebuild(cfg, book, ab_id, files, sha)
        except BaseException:  # a book that can't have sentences keeps no orphan SRT
            for part, path in paths.items():
                if before[part] is None:
                    path.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(path, before[part])
            raise


def ensure(cfg: Config, book: dict, ab_id: str | None, files: list[str] | None) -> dict | None:
    """The book's current map, re-matched from the stored SRT if the schema or
    the book changed; None when it has no alignment. Raises AlignError
    `audiobook_changed` when the SRT belongs to a different audiobook than the
    one linked now, `no_audiobook` when none is local, and annotate.NoPack for
    a book that can't have sentences."""
    book_id = book["book_id"]
    m = _load_map(cfg, book_id)
    if m is None and not _sources(cfg, book_id, files):
        return None
    if not ab_id:
        raise AlignError("no_audiobook", "the linked audiobook is not on this machine")
    if m is not None and m.get("audiobook") != ab_id:
        raise AlignError("audiobook_changed", "the book is linked to a different audiobook than the SRT was made for")
    sha = annotate.book_sha(cfg, book)
    with _book_lock(book_id):
        m = _load_map(cfg, book_id)
        if m is not None and m.get("schema") == ALIGN_SCHEMA and m.get("sha256") == sha:
            return m
        return _rebuild(cfg, book, ab_id, files, sha)


def state(cfg: Config, book: dict, ab_id: str | None, job: dict | None = None) -> dict:
    """What `GET /api/books/{id}/status` reports; reads only the stored map, never
    rebuilds. `state`: none | ready | stale (the book, the schema or the linked
    audiobook changed since it was made — a GET re-matches or says why not) |
    queued | running (an `align_jobs` run, which replaces the stored
    state while it lasts). A failed run keeps the stored state and adds `error`,
    since it never touches the stored alignment."""
    m = _load_map(cfg, book["book_id"])
    if m is None:
        out = {"state": "none", "coverage": None, "ok": None, "source": None, "estimated": None}
    else:
        fresh = (m.get("schema") == ALIGN_SCHEMA and m.get("audiobook") == ab_id
                 and m.get("sha256") == (book.get("sha256") or m.get("sha256")))
        out = {"state": "ready" if fresh else "stale", "coverage": m.get("coverage"), "ok": m.get("ok"),
               "source": m.get("source", "synced"), "estimated": m.get("estimated")}
    if job and job["state"] in ("queued", "running"):
        out.update(state=job["state"], step=job.get("step"))
    elif job and job["state"] == "error":
        out["error"] = job.get("error")
    return out


def delete(cfg: Config, book_id: str, kind: str | None = None) -> bool:
    """Remove the book's SRT(s) and map. Whether anything was there. With `kind`,
    only that kind's SRTs go (and the map, which the next GET rebuilds from what
    is left): removing a transcript goes back to the synced SRT."""
    found = False
    if kind is None:
        shutil.rmtree(work_dir(cfg, book_id), ignore_errors=True)
    if cfg.alignments_dir.is_dir():
        if kind is None:
            doomed = list(cfg.alignments_dir.glob(f"{book_id}.*"))
        else:
            stem = book_id if kind == "synced" else f"{book_id}.{kind}"
            doomed = [f for f in cfg.alignments_dir.glob(f"{stem}.*srt")
                      if kind != "synced" or not f.name.startswith(f"{book_id}.transcript.")]
            if doomed:
                doomed.append(map_path(cfg, book_id))
        for f in doomed:
            if f.exists():
                f.unlink(missing_ok=True)
                found = True
    return found
