"""Tokenize EPUB text and compute jpdb-style vocabulary statistics.

For every Japanese (fugashi/UniDic) and English (spaCy) book in
epub_library.json this stage extracts the full spine text (epub_text), runs it
through the language's analyzer, and writes:
- aggregate stats to data/vocab_epub.json (gitignored like the rest of data/ — rebuildable with `cli.py analyze`, but slow)
- the per-book word→count map to data/wordlists/{book_id}.json, which lets
  known-word percentages be recomputed instantly without re-tokenizing
- the per-sentence word index to data/sentences/{book_id}.json.gz, which lets
  the same be done for the sentence-level reading-load metrics (known.py)

Everything written here is user-independent: difficulty comes from a general
frequency corpus, not from what the reader knows. Metrics relative to the
user's vocabulary live in known_stats.json.

Word identity (JP) is UniDic's orthBase (書字形基本形 — the dictionary form as
actually written, e.g. する), NOT the canonical lemma (為る): orthBase lives in
the same spelling space as the wordfreq corpus and jpdb exports. For English it
is the lowercased surface, with token.lemma_ as the second matching space.

Incremental and resume-safe like scanner.py: entries whose file is unchanged
(mtime+filesize) at the current SCHEMA_VERSION are skipped.
"""
from __future__ import annotations

import gzip
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from collections.abc import Iterator
from functools import lru_cache
from typing import NamedTuple

from . import epub_text
from . import known as known_mod
from .config import Config
from .index_store import load_json, save_json

SAVE_EVERY = 10
# v3: added the English (spaCy) analysis path alongside the JP (fugashi) one.
# v4: sentence index carries each sentence's (start, end) character offsets
# into the extracted text, so study.py can slice the example directly instead
# of re-running split_sentences and indexing into it positionally — that
# re-split never matched spaCy's EN sentence boundaries.
SCHEMA_VERSION = 5

# Longest piece handed to MeCab at once. A sentence over this is tokenized in
# several passes that still accumulate into one sentence entry.
MAX_CHUNK = 4000

# UniDic pos1 classes.
PUNCT_POS = {"補助記号", "記号", "空白"}
FUNCTION_POS = {"助詞", "助動詞"}
# Bound morphemes (たち, くん, つ, 大-) are counted as words and scored for
# difficulty like any other, but they are never a lookup: they attach to a
# stem rather than carrying meaning to be looked up separately. Flagged
# "free" in the sentence index so known.py doesn't score them as unknown.
AFFIX_POS = {"接尾辞", "接頭辞"}

# English (Universal POS) analogue of FUNCTION_POS: pure grammatical words that
# jpdb-style counting keeps out of the content-word denominator (the/a, of/in,
# and/but, to, is/have as auxiliaries). Pronouns are kept as content, matching
# UniDic's 代名詞. Proper nouns and numbers are handled as "free" at token time,
# like JP 固有名詞/数詞. Calibratable — see the English calibration follow-up.
EN_FUNCTION_POS = {"DET", "ADP", "CCONJ", "SCONJ", "PART", "AUX"}

# Zipf → difficulty/100 mapping. zipf ≥ Z_EASY → 0 (は, する, 人…);
# zipf ≤ Z_HARD or out-of-vocabulary → 100. Tunable; calibrated against
# jpdb's published numbers for 夏へのトンネル (avg 26, p90 44).
Z_EASY = 6.0
Z_HARD = 1.5

# Below this many letter/number characters a book is "no_text" (image-only
# EPUB, cover-stub, extraction failure) rather than a garbage stats row.
MIN_WORD_CHARS = 500

_KANJI_RANGES = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # Extension A
    (0xF900, 0xFAFF),   # Compatibility Ideographs
    (0x20000, 0x2FA1F), # Extensions B+
)

_tagger = None
_en_nlp = None


def _get_tagger():
    # Lazy: cli.py/server.py import this module at startup; the dictionary
    # only loads when the stage actually runs.
    global _tagger
    if _tagger is None:
        import fugashi

        _tagger = fugashi.Tagger()
    return _tagger


def _get_en_nlp():
    # Lazy, like _get_tagger. Tagger + attribute_ruler + lemmatizer supply POS
    # (content/free split) and the lemma (second matching space); the parser
    # and NER are the slow, unused components, so they are dropped and a
    # sentencizer supplies sentence boundaries instead.
    global _en_nlp
    if _en_nlp is None:
        import spacy

        nlp = spacy.load("en_core_web_sm", disable=["parser", "ner"])
        nlp.add_pipe("sentencizer")
        _en_nlp = nlp
    return _en_nlp


@lru_cache(maxsize=200_000)
def _zipf(word: str, lang: str = "ja") -> float:
    from wordfreq import zipf_frequency

    return zipf_frequency(word, lang)


def _difficulty(zipf: float) -> int:
    if zipf >= Z_EASY:
        return 0
    if zipf <= Z_HARD:
        return 100
    return round(100 * (Z_EASY - zipf) / (Z_EASY - Z_HARD))


def _is_kanji(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _KANJI_RANGES)


# Characters that stand in for a kanji inside a word: 々 repeats the one before
# (人々), ヶ is a counter (ヶ月). The pack's ruby treats them as kanji so those
# words align; the stats keep the narrow test, because they count (kanji,
# reading) pairs and neither is a kanji.
_KANJI_LIKE = "々ヶ"


def _hira_to_kata(s: str) -> str:
    return "".join(
        chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in s
    )


@lru_cache(maxsize=100_000)
def _align_readings(
    surface: str, kana: str, kanji_like: bool = False
) -> tuple[tuple[str, str], ...]:
    """Align a token's surface with its katakana reading, yielding
    (kanji_run, reading) pairs. Kana characters in the surface anchor the
    match; each maximal kanji run becomes a lazy capture group. `kanji_like`
    also counts 々 and ヶ as kanji (the reader pack's ruby)."""
    def kanji(ch: str) -> bool:
        return _is_kanji(ch) or (kanji_like and ch in _KANJI_LIKE)

    parts: list[str] = []
    runs: list[str] = []
    i = 0
    while i < len(surface):
        ch = surface[i]
        if kanji(ch):
            j = i
            while j < len(surface) and kanji(surface[j]):
                j += 1
            runs.append(surface[i:j])
            parts.append("(.+?)")
            i = j
        else:
            parts.append(re.escape(_hira_to_kata(ch)))
            i += 1
    if not runs:
        return ()
    m = re.fullmatch("".join(parts), kana)
    if not m:
        return ()
    return tuple(zip(runs, m.groups()))


# Sound changes at reading boundaries inside compounds: gemination of a
# leading kanji's reading (学 ガク → ガッ in 学校) and rendaku voicing of a
# trailing one (口 クチ → グチ in 出口). The changed form is what this word
# actually uses, so it counts as its own reading — as jpdb does.
_GEMINATE_FINALS = "キクチツ"
_VOICED = {
    "カ": "ガ", "キ": "ギ", "ク": "グ", "ケ": "ゲ", "コ": "ゴ",
    "サ": "ザ", "シ": "ジ", "ス": "ズ", "セ": "ゼ", "ソ": "ゾ",
    "タ": "ダ", "チ": "ヂ", "ツ": "ヅ", "テ": "デ", "ト": "ド",
    "ハ": "バ", "ヒ": "ビ", "フ": "ブ", "ヘ": "ベ", "ホ": "ボ",
}
_HANDAKU = {"ハ": "パ", "ヒ": "ピ", "フ": "プ", "ヘ": "ペ", "ホ": "ポ"}


def _prefix_variants(r: str):
    yield r
    if len(r) > 1 and r[-1] in _GEMINATE_FINALS:
        yield r[:-1] + "ッ"


def _suffix_variants(r: str):
    yield r
    if r:
        if r[0] in _VOICED:
            yield _VOICED[r[0]] + r[1:]
        if r[0] in _HANDAKU:
            yield _HANDAKU[r[0]] + r[1:]


def _peel(run: str, reading: str, singles: dict[str, set[str]]):
    """Attribute `reading` across the kanji of `run`, anchoring on known
    readings of the edge kanji (either direction, with sound-change
    variants). None when unresolvable or ambiguous."""
    if len(run) == 1:
        return [(run, reading)] if reading else None
    results: list[list[tuple[str, str]]] = []
    for base_r in sorted(singles.get(run[0], ())):
        for r in _prefix_variants(base_r):
            if reading.startswith(r) and len(reading) - len(r) >= len(run) - 1:
                rest = _peel(run[1:], reading[len(r):], singles)
                if rest is not None:
                    cand = [(run[0], r)] + rest
                    if cand not in results:
                        results.append(cand)
    for base_r in sorted(singles.get(run[-1], ())):
        for r in _suffix_variants(base_r):
            if reading.endswith(r) and len(reading) - len(r) >= len(run) - 1:
                rest = _peel(run[:-1], reading[: len(reading) - len(r)], singles)
                if rest is not None:
                    cand = rest + [(run[-1], r)]
                    if cand not in results:
                        results.append(cand)
    return results[0] if len(results) == 1 else None


def _count_readings(pairs: set[tuple[str, str]]) -> int:
    """Approximate count of distinct (kanji, reading) pairs. Pass 1 collects
    unambiguous single-kanji readings; then multi-kanji compounds are peeled
    apart using those as anchors, feeding newly inferred readings back in
    until a fixpoint. Unresolvable/ambiguous compounds are skipped."""
    # Sorted iteration keeps the resolved set (and thus the count)
    # deterministic across runs despite hash randomization.
    singles: dict[str, set[str]] = {}
    pending: list[tuple[str, str]] = []
    for surface, kana in sorted(pairs):
        for run, reading in _align_readings(surface, kana):
            if not reading:
                continue
            if len(run) == 1:
                singles.setdefault(run, set()).add(reading)
            else:
                pending.append((run, reading))
    resolved = {(k, r) for k, rs in singles.items() for r in rs}
    grew = True
    while grew and pending:
        grew = False
        remaining = []
        for run, reading in pending:
            parts = _peel(run, reading, singles)
            if parts:
                for k, r in parts:
                    resolved.add((k, r))
                    if r not in singles.setdefault(k, set()):
                        singles[k].add(r)
                        grew = True
            else:
                remaining.append((run, reading))
        pending = remaining
    return len(resolved)


def _tokenize_pieces(seg: str):
    """Tokenize one sentence, splitting only for MeCab's input buffer. The
    caller sees a single stream, so a pathological unpunctuated run stays one
    sentence rather than becoming several."""
    tagger = _get_tagger()
    while len(seg) > MAX_CHUNK:
        yield from tagger(seg[:MAX_CHUNK])
        seg = seg[MAX_CHUNK:]
    yield from tagger(seg)


def iter_tokens(text: str) -> Iterator[tuple[int, int, object]]:
    """Every MeCab token of `text` as `(start, end, tok)`, with character
    offsets into `text` (so `text[start:end] == tok.surface`)."""
    cursor = 0
    for tok in _tokenize_pieces(text):
        surface = tok.surface
        # MeCab drops the whitespace before a token and reports it apart.
        cursor += len(tok.white_space)
        if text[cursor:cursor + len(surface)] != surface:
            found = text.find(surface, cursor)
            if found < 0:
                continue
            cursor = found
        yield cursor, cursor + len(surface), tok
        cursor += len(surface)


class WordDef(NamedTuple):
    """What one token contributes to a word index. The first five are the
    sentence index's word definition (minus difficulty, which needs wordfreq)."""
    base: str      # NFKC orthBase: the key the library counts and matches on
    lemma: str     # UniDic lemma: the second matching space
    free: bool     # proper noun, numeral or bound affix: never a lookup
    reading: str   # dictionary-form reading (kanaBase), falling back to kana
    goshu: str     # 語種: 外 loanword, 固 proper, 和/漢/混
    kind: str      # "content" or "function" (助詞/助動詞)
    kana: str      # reading of the surface as inflected
    proper: bool   # 固有名詞/数詞 only, without the affixes `free` adds


def word_def(tok) -> WordDef | None:
    """The word a token stands for, or None for punctuation and spaces."""
    feature = tok.feature
    pos1 = feature.pos1
    if pos1 in PUNCT_POS:
        return None
    base = unicodedata.normalize("NFKC", feature.orthBase or tok.surface)
    kana = feature.kana or ""
    # Proper nouns and numerals are counted as words but excluded from the
    # difficulty distribution (names are OOV in any frequency corpus and would
    # inflate every book's score). They are flagged so known.py can treat them
    # as free too.
    proper = feature.pos2 in ("固有名詞", "数詞")
    return WordDef(
        base=base,
        lemma=feature.lemma or base,
        free=proper or pos1 in AFFIX_POS,
        # kanaBase is the dictionary-form reading (モツ), not the inflected one
        # (モッ) that kana gives for 持った.
        reading=feature.kanaBase or kana,
        # 語種: 外 = loanword, 固 = proper, 和/漢/混.
        goshu=getattr(feature, "goshu", "") or "",
        kind="function" if pos1 in FUNCTION_POS else "content",
        kana=kana,
        proper=proper,
    )


def _analyze_text(text: str) -> tuple[dict, Counter, dict]:
    words: Counter = Counter()  # orthBase over content tokens
    words_total = 0
    content_tokens = 0
    diff_hist = [0] * 101
    diff_n = 0
    reading_pairs: set[tuple[str, str]] = set()

    # Per-sentence index: word types interned to ids, one id list per
    # sentence. Lets known.py recompute reading-load metrics for a changed
    # known-word list without re-tokenizing.
    word_ids: dict[tuple[str, str, bool], int] = {}
    word_defs: list[list] = []
    sents: list[list[int]] = []
    sent_spans: list[list[int]] = []

    spans = epub_text.split_sentences_with_spans(text)
    sentences_text = [text[start:end] for start, end in spans]
    for (start, end), seg in zip(spans, sentences_text):
        ids: list[int] = []
        for _, _, tok in iter_tokens(seg):
            d = word_def(tok)
            if d is None:
                continue
            pos1 = tok.feature.pos1
            # jpdb's word units merge the morphemes bound to a predicate —
            # aux verbs (た/られる), conjunctive particles (て/ば/と) and the
            # nominalizing の — into the word, so they don't add to Length.
            merged = pos1 == "助動詞" or (
                pos1 == "助詞" and tok.feature.pos2 in ("接続助詞", "準体助詞")
            )
            if not merged:
                words_total += 1
            if d.kind == "function":
                continue
            content_tokens += 1

            surface = tok.surface
            words[d.base] += 1

            difficulty = -1
            if not d.proper:
                z = max(_zipf(d.base), _zipf(surface), _zipf(d.lemma))
                difficulty = _difficulty(z)
                diff_hist[difficulty] += 1
                diff_n += 1

            # Reading and difficulty are carried in the index so the study
            # list can show them without the server importing fugashi or
            # wordfreq (see the laziness note in the module docstring).
            key = (d.base, d.lemma, d.free)
            wid = word_ids.get(key)
            if wid is None:
                wid = word_ids[key] = len(word_defs)
                # goshu is appended last so positional readers of d[0..4] are
                # unaffected.
                word_defs.append([d.base, d.lemma, 1 if d.free else 0,
                                  d.reading, difficulty, d.goshu])
            ids.append(wid)

            if d.kana and any(_is_kanji(c) for c in surface):
                reading_pairs.add((surface, d.kana))
        sents.append(ids)
        sent_spans.append([start, end])

    characters = epub_text.count_chars(text)
    sentences = len(sentences_text)
    kanji_counts = Counter(ch for ch in text if _is_kanji(ch))

    if diff_n:
        difficulty_avg = round(sum(d * n for d, n in enumerate(diff_hist)) / diff_n)
        cum = 0
        difficulty_p90 = 100
        for d, n in enumerate(diff_hist):
            cum += n
            if cum >= 0.9 * diff_n:
                difficulty_p90 = d
                break
    else:
        difficulty_avg = difficulty_p90 = 0

    stats = {
        "words_total": words_total,
        "content_tokens": content_tokens,
        "unique_words": len(words),
        "unique_words_once": sum(1 for c in words.values() if c == 1),
        "unique_words_once_pct": round(
            100 * sum(1 for c in words.values() if c == 1) / len(words), 1
        )
        if words
        else 0.0,
        "unique_kanji": len(kanji_counts),
        "unique_kanji_once": sum(1 for c in kanji_counts.values() if c == 1),
        "unique_kanji_readings": _count_readings(reading_pairs),
        "difficulty_avg": difficulty_avg,
        "difficulty_p90": difficulty_p90,
        "characters": characters,
        "sentences": sentences,
        "avg_sentence_chars": round(characters / sentences, 1) if sentences else 0.0,
        "avg_sentence_words": round(words_total / sentences, 1) if sentences else 0.0,
    }
    return stats, words, {"words": word_defs, "sents": sents, "spans": sent_spans}


def _analyze_text_en(text: str) -> tuple[dict, Counter, dict]:
    """English counterpart of _analyze_text, using spaCy. Emits the identical
    (stats, wordlist, sentence-index) shape so every downstream stage
    (known.recompute, corpus, study, build) stays language-agnostic.

    Word identity is the lowercased surface (the orthBase analogue); the second
    matching space is spaCy's token.lemma_. Proper nouns and numbers are "free"
    — counted as words but never a lookup and out of the difficulty
    distribution — exactly as JP treats 固有名詞/数詞. English has no reading,
    so that slot in the sentence index is left empty.
    """
    nlp = _get_en_nlp()

    words: Counter = Counter()  # lowercased surface over content tokens
    words_total = 0
    content_tokens = 0
    diff_hist = [0] * 101
    diff_n = 0

    word_ids: dict[tuple[str, str, bool], int] = {}
    word_defs: list[list] = []
    sents: list[list[int]] = []
    sent_spans: list[list[int]] = []

    # epub_text inserts a newline at every block tag (<p>/<div>/…), so splitting
    # on newlines yields paragraph-sized pieces — small enough that no single
    # doc is huge, and a natural boundary the sentencizer then splits into real
    # sentences. (The JP splitter keys off 。！？ and would swallow a whole
    # English paragraph as one sentence, inflating strain.)
    para_starts: list[int] = []
    para_texts: list[str] = []
    offset = 0
    for line in text.split("\n"):
        if line.strip():
            para_starts.append(offset)
            para_texts.append(line)
        offset += len(line) + 1  # +1 for the '\n' split() consumed

    for para_start, doc in zip(para_starts, nlp.pipe(para_texts, batch_size=64)):
        for sent in doc.sents:
            ids: list[int] = []
            for tok in sent:
                if tok.is_space or tok.is_punct or not any(
                    ch.isalnum() for ch in tok.text
                ):
                    continue
                words_total += 1
                if tok.pos_ in EN_FUNCTION_POS:
                    continue
                content_tokens += 1

                surface = unicodedata.normalize("NFKC", tok.text.lower())
                lemma = unicodedata.normalize(
                    "NFKC", (tok.lemma_ or tok.text).lower()
                )
                words[surface] += 1

                free = tok.pos_ == "PROPN" or tok.like_num
                difficulty = -1
                if not free:
                    z = max(_zipf(surface, "en"), _zipf(lemma, "en"))
                    difficulty = _difficulty(z)
                    diff_hist[difficulty] += 1
                    diff_n += 1

                key = (surface, lemma, free)
                wid = word_ids.get(key)
                if wid is None:
                    wid = word_ids[key] = len(word_defs)
                    word_defs.append(
                        [surface, lemma, 1 if free else 0, "", difficulty]
                    )
                ids.append(wid)
            if ids:
                sents.append(ids)
                sent_spans.append(
                    [para_start + sent.start_char, para_start + sent.end_char]
                )

    characters = epub_text.count_chars(text)
    sentences = len(sents)

    if diff_n:
        difficulty_avg = round(sum(d * n for d, n in enumerate(diff_hist)) / diff_n)
        cum = 0
        difficulty_p90 = 100
        for d, n in enumerate(diff_hist):
            cum += n
            if cum >= 0.9 * diff_n:
                difficulty_p90 = d
                break
    else:
        difficulty_avg = difficulty_p90 = 0

    stats = {
        "words_total": words_total,
        "content_tokens": content_tokens,
        "unique_words": len(words),
        "unique_words_once": sum(1 for c in words.values() if c == 1),
        "unique_words_once_pct": round(
            100 * sum(1 for c in words.values() if c == 1) / len(words), 1
        )
        if words
        else 0.0,
        # No kanji in English; keep the keys so the stats row shape is uniform.
        "unique_kanji": 0,
        "unique_kanji_once": 0,
        "unique_kanji_readings": 0,
        "difficulty_avg": difficulty_avg,
        "difficulty_p90": difficulty_p90,
        "characters": characters,
        "sentences": sentences,
        "avg_sentence_chars": round(characters / sentences, 1) if sentences else 0.0,
        "avg_sentence_words": round(words_total / sentences, 1) if sentences else 0.0,
    }
    return stats, words, {"words": word_defs, "sents": sents, "spans": sent_spans}


def save_sentences(path, book_id: str, index: dict) -> None:
    """Write the per-sentence index gzipped — it is an order of magnitude
    larger than the wordlist and compresses ~3x."""
    blob = json.dumps(
        {"book_id": book_id, "schema": SCHEMA_VERSION, **index},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(gzip.compress(blob, 6))
    tmp.replace(path)


def load_sentences(path) -> dict:
    """Read a per-sentence index, or {} when missing/corrupt."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def analyze(cfg: Config, refresh: bool = False) -> dict:
    library: dict = load_json(cfg.library_index, {})
    index: dict = load_json(cfg.vocab_index, {})
    cfg.wordlists_dir.mkdir(parents=True, exist_ok=True)
    cfg.sentences_dir.mkdir(parents=True, exist_ok=True)

    books = sorted(
        (b for b in library.values() if b.get("language") in ("ja", "en")),
        key=lambda b: b.get("path", ""),
    )

    processed = 0
    analyzed = 0
    for book in books:
        bid = book["book_id"]
        existing = index.get(bid)
        wordlist_path = cfg.wordlists_dir / f"{bid}.json"
        sentences_path = cfg.sentences_dir / f"{bid}.json.gz"
        # Like scanner.py: an "ok" entry additionally needs its side files
        # (gitignored environments may have the index but not the lists).
        if (
            not refresh
            and existing
            and existing.get("mtime") == book.get("mtime")
            and existing.get("filesize") == book.get("filesize")
            and existing.get("schema") == SCHEMA_VERSION
            # A hand-corrected language changes which tokenizer applies.
            # Entries analyzed before the stamp existed count as matching.
            and existing.get("language", book.get("language")) == book.get("language")
            and (
                existing.get("status") != "ok"
                or (wordlist_path.exists() and sentences_path.exists())
            )
        ):
            continue

        entry = {
            "book_id": bid,
            "schema": SCHEMA_VERSION,
            "mtime": book.get("mtime"),
            "filesize": book.get("filesize"),
            "language": book.get("language"),
            "status": "error",
            "error": None,
            "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        title = (book.get("title") or bid)[:60]
        try:
            text = epub_text.extract_text(cfg.epub_dir / book["path"])
            if text is None or epub_text.count_chars(text) < MIN_WORD_CHARS:
                entry["status"] = "no_text"
                print(f"  ∅ {title} (no extractable text)")
            else:
                if book.get("language") == "en":
                    stats, words, sent_index = _analyze_text_en(text)
                else:
                    stats, words, sent_index = _analyze_text(text)
                entry.update(stats)
                entry["status"] = "ok"
                save_json(
                    wordlist_path,
                    {
                        "book_id": bid,
                        "schema": SCHEMA_VERSION,
                        "total": stats["content_tokens"],
                        "words": dict(words.most_common()),
                    },
                )
                save_sentences(sentences_path, bid, sent_index)
                analyzed += 1
                print(
                    f"  + {title} — {stats['words_total']} words, "
                    f"{stats['unique_words']} unique, diff {stats['difficulty_avg']}"
                )
        except Exception as exc:  # one broken book never aborts the run
            entry["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  ✗ {title}: {entry['error']}")

        index[bid] = entry
        processed += 1
        if processed % SAVE_EVERY == 0:
            save_json(cfg.vocab_index, index)

    # Drop entries whose books vanished (or stopped being analyzable).
    valid = {b["book_id"] for b in books}
    removed = [bid for bid in index if bid not in valid]
    for bid in removed:
        index.pop(bid)
        (cfg.wordlists_dir / f"{bid}.json").unlink(missing_ok=True)
        (cfg.sentences_dir / f"{bid}.json.gz").unlink(missing_ok=True)
        (cfg.annotations_dir / f"{bid}.json.gz").unlink(missing_ok=True)

    save_json(cfg.vocab_index, index)
    # Every profile, not just the active one: a pipeline run changes the shared
    # library, and a profile whose known_stats.json isn't refreshed would show
    # new books with no strain/known numbers until its next import.
    known_mod.recompute_all(cfg)
    print(
        f"\nAnalyze complete: {len(index)} books "
        f"({analyzed} analyzed, {len(removed)} removed)."
    )
    return index
