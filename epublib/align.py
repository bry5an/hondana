"""Audio↔text sync: match an audiobook's SRT cues to a book's sentences.

The phone highlights the sentence being spoken and jumps the player to a tapped
one. It has the reader pack's `sents` spans but not the sentence text, so the
matching happens here: an SRT produced elsewhere (SubPlz / Whisper) is
ingested, its cues are located in the book's text, and the result is stored
against the pack's own anchors:

    {"schema": 5, "book_id": …, "sha256": …, "audiobook": "<ab_id>",
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
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from . import annotate, speech
from .config import Config
from .index_store import write_bytes_atomic

ALIGN_SCHEMA = 5  # 5: a transcript's spellings are read off its own cues, holes are filled from the cues in
# them, kanji digits match digits
# 4: a plain transcript is matched globally; `source`, per-doc `len` and `est`;
# listeners' fixes (`anchors`) change the map through `anchors_sha`, not the schema
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


_KANJI_DIGITS = str.maketrans("〇一二三四五六七八九", "0123456789")


def norm(s: str) -> str:
    """The comparison form: NFKC, katakana as hiragana, kanji digits as digits (the book
    says 第二話, Whisper writes 第2話), letters and numbers only. Every character stays
    one character, so a sentence's length is the same with or without the fold."""
    out = []
    for ch in unicodedata.normalize("NFKC", s).translate(_KANJI_DIGITS):
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
    only between the chained cues either side of them.

    Whisper's spelling can differ from the book's (にしな for 仁科, こと for 事): once
    the chain is made, `_respellings` reads those pairs off it, and the cues are looked
    for again with them applied. A line still unplaced that sits alone between two
    placed ones takes the one cue left between them (`_fill`)."""
    norms, b, bounds = _book(sentences)
    index = _bigrams(b)
    texts = [cue_norm(cue.text) for cue in cues]
    found = [_candidates(b, index, t, 0, len(b), _MIN_RATIO) if len(t) >= 4 else [] for t in texts]
    chained = _chain(found, texts)
    spelled = _respellings(b, texts, chained)
    if spelled:
        again = re.compile("|".join(re.escape(h) for h in sorted(spelled, key=len, reverse=True)))
        for k, t in enumerate(texts):
            respelled = again.sub(lambda m: spelled[m.group()], t)
            if respelled == t:
                continue
            hits = _candidates(b, index, respelled, 0, len(b), _MIN_RATIO) if len(respelled) >= 4 else []
            if _fit(hits, respelled) >= _fit(found[k], t):  # the book's own spelling, unless the cue fitted it better
                texts[k], found[k] = respelled, hits
        chained = _chain(found, texts)
    anchors: list[tuple[int, int, Cue]] = []
    anchored: set[int] = set()  # the cues that have a place in the book
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
            anchored.add(k)
            lo = hit[1]
    rows = _rows(bounds, anchors, cues)
    sizes = [len(n) for n in norms]
    _fill(rows, sizes, cues, [k for k in range(len(cues)) if texts[k] and k not in anchored])
    return rows, _estimate(rows, sizes)


_CHAIN_SLACK = 8         # characters of the book two neighbouring cues' places may share
_EST_PACE = (3.0, 15.0)  # chars/s an unheard run between two heard sentences must make to be estimated
_SPELL_VOTES = 3         # cues that must show a respelling before it is believed ...
_SPELL_SHARE = 0.75      # ... in this share of the places that text is written some other way
_SPELL_PLAIN = 3         # ... and the cues' kana must be this many times as common as the book's own (else it is a plain word)
_FILL_MAX = 4            # most sentences a hole may have to be filled from the cues in it


def _fit(hits: list[tuple[int, int, float]], text: str) -> float:
    """How many of `text`'s characters its best place in the book matched."""
    return max((ratio for _, _, ratio in hits), default=0.0) * len(text)


def _kana(s: str) -> bool:
    return bool(s) and all("ぁ" <= ch <= "ゖ" or ch == "ー" for ch in s)


def _written(s: str) -> bool:
    """A run the book writes in kanji (or Latin, digits): no kana, at least one ideograph or letter."""
    return (bool(s) and len(s) <= 6 and not any("ぁ" <= ch <= "ゖ" or ch == "ー" for ch in s)
            and any(ord(ch) >= 0x2E80 or ch.isascii() and ch.isalpha() for ch in s))


def _respellings(b: str, texts: list[str], chained: dict[int, tuple[int, int]]) -> dict[str, str]:
    """How Whisper spells what the book writes in kanji, `{heard: written}`, read off the cues
    already placed: a name (にしな for 仁科), 怪訝, 事. Where a placed
    cue and its place in the book differ by kana against kanji, that is a vote. A pair counts once
    several cues agree and the kana is not just a word the book itself writes in kana (かし, とき),
    so it works for any name in any book, with no dictionary."""
    votes: dict[str, Counter] = {}
    for k, (s, e) in chained.items():
        place = b[s:e]
        if texts[k] == place:
            continue
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, texts[k], place, autojunk=False).get_opcodes():
            if tag == "replace" and i2 - i1 >= 2 and _kana(texts[k][i1:i2]) and _written(place[j1:j2]):
                votes.setdefault(texts[k][i1:i2], Counter())[place[j1:j2]] += 1
    out = {}
    for heard, writings in votes.items():
        written, n = writings.most_common(1)[0]
        if n >= _SPELL_VOTES and n >= _SPELL_SHARE * sum(writings.values()) and n >= _SPELL_PLAIN * b.count(heard):
            out[heard] = written
    return out


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


def _fill(rows: list[list | None], sizes: list[int], cues: list[Cue], free: list[int]) -> None:
    """Times the sentences no cue was found in from the cues no sentence was found for,
    where both sit between the same two timed sentences and there is one cue for each (or
    one cue for the lot). The book writes 「なに？」, 「お義姉さん」, 「どーも」 and Whisper 何?,
    お姉さん, どうも, so no text matches and neither is placed, but a cue with nowhere
    else to belong is that sentence. Changes `rows` in place; `free` are the cue indices
    that have no place in the book. Time is per part, so a hole is only filled by cues of
    the part its neighbours are in."""
    by_part: dict[str | None, list[tuple[float, int]]] = {}
    for i in free:
        by_part.setdefault(cues[i].part, []).append(((cues[i].start + cues[i].end) / 2, i))
    mids = {part: sorted(items) for part, items in by_part.items()}
    keys = {part: [m for m, _ in items] for part, items in mids.items()}
    k = 0
    while k < len(rows):
        if rows[k] is not None:
            k += 1
            continue
        j = k
        while j < len(rows) and rows[j] is None:
            j += 1
        if k > 0 and j < len(rows):
            a, z = rows[k - 1], rows[j]
            part = a[2] if len(a) > 2 else None
            ids = [x for x in range(k, j) if sizes[x]]
            if part == (z[2] if len(z) > 2 else None) and 0 < len(ids) <= _FILL_MAX and part in mids:
                lo, hi = bisect_left(keys[part], a[1]), bisect_right(keys[part], z[0])
                near = [cues[i] for _, i in mids[part][lo:hi]]
                if len(near) == len(ids):
                    spans = [(c.start, c.end) for c in near]
                elif len(near) == 1:
                    chars, at, spans = sum(sizes[x] for x in ids), near[0].start, []
                    for x in ids:
                        took = (near[0].end - near[0].start) * sizes[x] / chars
                        spans.append((round(at), round(at + took)))
                        at += took
                else:
                    spans = []
                for x, (start, end) in zip(ids, spans):
                    start, end = max(start, a[1]), min(end, z[0])  # between its neighbours
                    if end > start:
                        rows[x] = [start, end] + ([part] if part else [])
        k = j


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


# ── listeners' fixes ───────────────────────────────────────────────────
# The phone's "tap the line you hear": sentence i starts at `ms` into `part`. From a
# fix, the sentences after it are timed by the book's pace until the map agrees again, and the ones
# before it back to where the map last agreed, the rows between spread by length. The phone applies
# the same rule (HondanaKit `AudioAlignment.applying`) until the server's map has the fix in it.

_ANCHOR_REACH = 400  # sentences a fix reaches before it gives up looking for the map to agree
_RACING = 2.4        # a row read faster than this many times the book's pace is the map catching up
_AGREE_MS = 2000     # how close the map must be to a fix's prediction, at least ...
_AGREE_SHARE = 0.12  # ... or this share of the time walked from the fix


def _part(row: list) -> str | None:
    return row[2] if len(row) > 2 else None


def _pace(rows: list[list | None], sizes: list[int], est: set[int]) -> float:
    """Characters a millisecond, pauses included, over steps between neighbours that are neither a
    hole (5 s or more) nor racing (over `_RACING` times the median step)."""
    steps, last = [], None
    for i, row in enumerate(rows):
        if not row:
            continue
        if last is not None and _part(rows[last]) == _part(row) and last not in est and i not in est:
            ms = row[0] - rows[last][0]
            if 0 < ms < 5000:
                steps.append((sizes[last], ms))
        last = i
    rates = sorted(c / ms for c, ms in steps)
    if len(rates) < 10:
        return 7 / 1000
    median = rates[len(rates) // 2]
    usual = [(c, ms) for c, ms in steps if c / ms <= _RACING * median]
    total = sum(ms for _, ms in usual)
    return sum(c for c, _ in usual) / total if total else 7 / 1000


def apply_anchors(rows: list[list | None], sizes: list[int], anchors: dict[int, tuple[str | None, int]],
                  est: set[int]) -> set[int]:
    """Puts each fix (`{row index: (part, ms)}`) into `rows` in place. Returns the indices re-timed
    around them, whose times are now estimates."""
    if not anchors:
        return set()
    pace = _pace(rows, sizes, est)
    for i, (part, ms) in anchors.items():
        rows[i] = [ms, ms + round(max(sizes[i], 1) / pace)] + ([part] if part else [])
    points = sorted(anchors)
    moved: set[int] = set()
    for n, at in enumerate(points):
        ahead = points[n + 1] if n + 1 < len(points) else len(rows)
        behind = points[n - 1] if n > 0 else -1
        part = _part(rows[at])
        moved |= _mend(rows, sizes, at, ahead, part, rows[at][1], pace, 1, est | moved)
        moved |= _mend(rows, sizes, at, behind, part, rows[at][0], pace, -1, est | moved)
    _keep_in_order(rows)
    return moved - set(points)


def _mend(rows: list, sizes: list[int], at: int, limit: int, part: str | None, origin: int, pace: float,
          step: int, guessed: set[int]) -> set[int]:
    """Re-times the rows on one side of a fix (`step` 1 forward, -1 back): see the section comment."""
    def agrees(k: int, chars: float) -> bool:
        row = rows[k]
        if not row or _part(row) != part or k in guessed:
            return False
        if (row[1] - row[0]) * pace * _RACING < max(sizes[k], 1):
            return False
        predicted = origin + step * chars / pace
        edge = row[0] if step > 0 else row[1]
        return abs(edge - predicted) <= max(_AGREE_MS, _AGREE_SHARE * chars / pace)

    walked, j, far = 0.0, at + step, None
    while 0 <= j < len(rows) and abs(j - at) <= _ANCHOR_REACH:
        if j == limit:
            if rows[j] and _part(rows[j]) == part:
                far = rows[j][0] if step > 0 else rows[j][1]
            break
        if rows[j] and _part(rows[j]) != part:  # time is per part
            break
        nxt = j + step
        last = not 0 <= nxt < len(rows) or nxt == limit
        if agrees(j, walked) and (last or agrees(nxt, walked + max(sizes[j], 1))):
            far = rows[j][0] if step > 0 else rows[j][1]
            break
        walked += max(sizes[j], 1)
        j += step
    members = list(range(at + step, j, step))
    if not members:
        return set()
    total = sum(max(sizes[k], 1) for k in members)
    reach = (far if far is not None else origin + step * total / pace) - origin
    t = float(origin)
    for k in members:
        nt = t + reach * max(sizes[k], 1) / total
        a, b = (t, nt) if step > 0 else (nt, t)
        rows[k] = [round(a), max(round(b), round(a) + 1)] + ([part] if part else [])
        t = nt
    return set(members)


def _keep_in_order(rows: list[list | None]) -> None:
    """Drops rows that start before the row ahead of them in the same part: what a fix leaves of the
    stretch it replaced, which would send the highlight back."""
    last: dict = {}
    for i, row in enumerate(rows):
        if not row:
            continue
        part = _part(row)
        if part in last and row[0] < last[part]:
            rows[i] = None
            continue
        last[part] = row[0]


def anchors_path(cfg: Config, book_id: str) -> Path:
    """The listeners' fixes, one JSON object a line, appended. Not `.json`: a purge keeps them with the
    SRT (they are people's work, not derived), and the mirror and a rekey carry them like the SRT."""
    return cfg.alignments_dir / f"{book_id}.anchors.jsonl"


def add_anchors(cfg: Config, book_id: str, ab_id: str, events: list[dict], who: str | None = None) -> None:
    """Appends fixes (`sync_anchor` events: doc, sent, part, ms, op, ts) for the book's audiobook."""
    lines = "".join(json.dumps({"ts": e["ts"], "ab_id": ab_id, "doc": e["doc"], "sentence": e["sent"],
                                "part": e.get("part"), "ms": e["ms"], "op": e.get("op", "set"), "by": who},
                               ensure_ascii=False) + "\n" for e in events)
    path = anchors_path(cfg, book_id)
    with _book_lock(book_id):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(lines)


def load_anchors(cfg: Config, book_id: str, ab_id: str | None) -> dict[tuple[int, int], tuple[str | None, int]]:
    """The fixes in force for the book's audiobook: the newest per sentence, a `clear` withdrawing it."""
    path = anchors_path(cfg, book_id)
    if not path.exists():
        return {}
    newest: dict[tuple[int, int], dict] = {}
    for line in path.read_text("utf-8").splitlines():
        try:
            e = json.loads(line)
            key = (int(e["doc"]), int(e["sentence"]))
        except (ValueError, KeyError, TypeError):
            continue
        if e.get("ab_id") != ab_id:
            continue
        if key not in newest or e.get("ts", 0) >= newest[key].get("ts", 0):
            newest[key] = e
    return {k: (e.get("part"), int(e["ms"])) for k, e in newest.items() if e.get("op", "set") == "set"}


def rekey_anchors(cfg: Config, old: str, new: str) -> list[str]:
    """Points every fix made against audiobook `old` at `new` (audiobook_rekey: the audiobook gained
    a key). `load_anchors` ignores a fix made against another audiobook, so without this the
    listeners' fixes would stop applying. Returns the anchors files rewritten."""
    changed: list[str] = []
    for path in sorted(cfg.alignments_dir.glob("*.anchors.jsonl")):
        with _book_lock(path.name[:-len(".anchors.jsonl")]):
            try:
                lines = path.read_text("utf-8").splitlines()
            except OSError:
                continue
            out, hit = [], False
            for line in lines:
                try:
                    e = json.loads(line)
                except ValueError:
                    e = None
                if isinstance(e, dict) and e.get("ab_id") == old:
                    line, hit = json.dumps({**e, "ab_id": new}, ensure_ascii=False), True
                out.append(line)
            if hit:
                write_bytes_atomic(path, ("\n".join(out) + "\n").encode("utf-8"))
                changed.append(path.name)
    return changed


def _anchors_sha(cfg: Config, book_id: str) -> str:
    path = anchors_path(cfg, book_id)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


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
    fixes = m.get("anchors_sha") or ""
    return f'"{m["schema"]}-{m["sha256"][:16]}-{m["srt_sha"][:8]}{"-" + fixes[:8] if fixes else ""}"'


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
    fixes = load_anchors(cfg, book["book_id"], ab_id)
    where = {(d["index"], i): at for at, (d, i) in enumerate((d, i) for d in docs for i in range(len(d["texts"])))}
    placed = {where[k]: v for k, v in fixes.items() if k in where}
    est |= apply_anchors(rows, [len(norm(t)) for t in sentences], placed, est)
    est -= set(placed)
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
        "matched": timed - len(est), "estimated": len(est), "total": total,
        # The fixes in it: the phone stops applying its own once the server has them, and a
        # map with this key is one from a server that takes them.
        "anchors": [[d, s, part, ms] for (d, s), (part, ms) in sorted(fixes.items()) if (d, s) in where],
        "anchors_sha": _anchors_sha(cfg, book["book_id"]),
        # Gaps the narrator talks through, once `audit` has listened.
        "quality": speech.quality(out_docs, speech.load(cfg, book["book_id"])), "docs": out_docs,
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
        if (m is not None and m.get("schema") == ALIGN_SCHEMA and m.get("sha256") == sha
                and m.get("anchors_sha", "") == _anchors_sha(cfg, book_id)):
            return m
        return _rebuild(cfg, book, ab_id, files, sha)


def track_files(entry: dict) -> list[str] | None:
    """The file names of a multi-file audiobook's index entry, in play order; None for a
    single file. An `mp3-folder` and a `tracks` book both list them in `files`, and the stored
    SRTs and the map's `part` are keyed by these names."""
    return entry.get("files") or None


def audio_files(cfg: Config, book: dict) -> tuple[str | None, list[str] | None, list[tuple[str | None, Path]]]:
    """`(ab_id, track names, [(part, path)])` of the book's linked audiobook on this machine, as the
    server's routes find it; `(None, None, [])` when it isn't here."""
    link = book.get("audiobook") or {}
    ab_id = link.get("audible_isbn")
    try:
        entry = json.loads(cfg.audiobook_index.read_text("utf-8")).get(ab_id) if ab_id else None
    except (OSError, ValueError):
        entry = None
    if not entry or not entry.get("local") or not link.get("audiobook_local"):
        return None, None, []
    base = next((d.resolve() for d in cfg.audiobook_dirs if str(d) == entry.get("root")), None)
    if base is None:
        return None, None, []
    root = (base / entry["path"]).resolve()
    if base != root and base not in root.parents:
        return None, None, []
    files = track_files(entry)
    return ab_id, files, [(f, root / f) for f in files] if files else [(None, root)]


def audit(cfg: Config, book: dict, ffmpeg: str = "ffmpeg") -> dict | None:
    """Listens to the book's audio for the gaps its map leaves and rebuilds the map with a
    `quality` block. Minutes of ffmpeg for a long book: a CLI job, never a request. Returns the map,
    or None when the book has no alignment or its audiobook isn't on this machine."""
    ab_id, files, audio = audio_files(cfg, book)
    if not ab_id or not _sources(cfg, book["book_id"], files):
        return None
    speech.check(cfg, book["book_id"], audio, ffmpeg)
    with _book_lock(book["book_id"]):
        map_path(cfg, book["book_id"]).unlink(missing_ok=True)
    return ensure(cfg, book, ab_id, files)


def state(cfg: Config, book: dict, ab_id: str | None, job: dict | None = None) -> dict:
    """What `GET /api/books/{id}/status` reports; reads only the stored map, never
    rebuilds. `state`: none | ready | stale (the book, the schema or the linked
    audiobook changed since it was made — a GET re-matches or says why not) |
    queued | running (an `align_jobs` run, which replaces the stored
    state while it lasts). A failed run keeps the stored state and adds `error`,
    since it never touches the stored alignment."""
    m = _load_map(cfg, book["book_id"])
    if m is None:
        out = {"state": "none", "coverage": None, "ok": None, "source": None, "estimated": None, "anchors": 0,
               "speech_gaps": None, "speech_gap_s": None}
    else:
        fresh = (m.get("schema") == ALIGN_SCHEMA and m.get("audiobook") == ab_id
                 and m.get("sha256") == (book.get("sha256") or m.get("sha256")))
        q = m.get("quality") or {}
        out = {"state": "ready" if fresh else "stale", "coverage": m.get("coverage"), "ok": m.get("ok"),
               "source": m.get("source", "synced"), "estimated": m.get("estimated"),
               "anchors": len(m.get("anchors") or []),
               "speech_gaps": q.get("speech_gaps"), "speech_gap_s": q.get("speech_gap_s")}
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
