"""Romanize Japanese text (offline) via pykakasi, for cross-script matching.

The one field that survives translation between an EN and JP edition is the
author — but in different scripts (住野よる ↔ Yoru Sumino). Romanizing the
Japanese side lets us compare authors directly.

Degrades gracefully: if pykakasi isn't installed, the helpers return None / a
best-effort fallback so the scan/build pipeline never hard-depends on it (only
pairing quality suffers).
"""
from __future__ import annotations

import functools
import re
import unicodedata

try:
    import pykakasi

    _KKS = pykakasi.kakasi()
except Exception:  # pragma: no cover - optional dependency
    _KKS = None


# Hiragana, Katakana, CJK — same ranges used in lang.py.
_JP_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾝ]")


_KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}   # ァ–ヶ → ぁ–ゖ (as textnorm.to_hiragana)


_LEADING_MARKS_RE = re.compile(r"^[^\w]+")


def available() -> bool:
    return _KKS is not None


def has_japanese(text: str) -> bool:
    return bool(_JP_RE.search(text or ""))


@functools.lru_cache(maxsize=4096)
def to_romaji(text: str) -> str | None:
    """Hepburn romanization, lowercased and whitespace-collapsed.

    Returns None when pykakasi is unavailable. Non-Japanese text passes through
    pykakasi unchanged (it just lowercases/normalizes spacing).
    """
    if not text:
        return None
    if _KKS is None:
        return None
    try:
        parts = _KKS.convert(text)
        out = " ".join(p.get("hepburn", "") for p in parts)
        return re.sub(r"\s+", " ", out).strip().lower() or None
    except Exception:  # pragma: no cover
        return None


def to_hiragana(text: str | None) -> str | None:
    """Hiragana reading of `text` (kanji read by pykakasi, katakana folded), no
    separators; None when pykakasi is unavailable or there is nothing to read."""
    if not text or _KKS is None:
        return None
    try:
        out = "".join(p.get("hira", "") for p in _KKS.convert(text))
        # No separators, and no ・ — nobody types the middle dot when searching.
        return re.sub(r"[\s・･]+", "", out) or None
    except Exception:  # pragma: no cover
        return None


def sort_reading(text: str | None, reading: str | None = None) -> str | None:
    """Key to put `text` in gojūon order: its hiragana reading, katakana
    folded and full-width Latin narrowed, so あ…ん run the way a bookshop shelf does
    and kanji sort by how they are read, not by code point.

    `reading` is a kana reading the file itself supplied (the OPF's `file-as`); it
    beats pykakasi's guess. Text with no Japanese in it is its own key — the page
    collates it with Intl.Collator("ja"), which puts Latin ahead of kana. None when
    there is nothing to sort on, or pykakasi is missing for Japanese text (the page
    then falls back to the plain text)."""
    if not text:
        return None
    if reading:
        key = unicodedata.normalize("NFKC", reading).translate(_KATA_TO_HIRA)
    elif not has_japanese(text):
        key = unicodedata.normalize("NFKC", text)
    else:
        key = to_hiragana(unicodedata.normalize("NFKC", text))
        key = key.translate(_KATA_TO_HIRA) if key else ""
    # 「あたま」 is filed under あ: brackets and marks up front would sort ahead of everything.
    return _LEADING_MARKS_RE.sub("", key.strip()) or None


@functools.lru_cache(maxsize=8192)
def reading_forms(text: str) -> tuple[str, ...]:
    """What a learner might type to find Japanese `text`: its hiragana reading, its
    romaji (spaced, and run together), with punctuation like ・ turned into spaces.
    Empty for text with no Japanese in it (that is already searchable as written)."""
    if not has_japanese(text):
        return ()
    forms = []
    hira = to_hiragana(text)
    if hira:
        forms.append(hira)
    rom = to_romaji(text)
    if rom:
        rom = re.sub(r"[^a-z0-9 ]+", " ", rom)
        rom = re.sub(r"\s+", " ", rom).strip()
        if rom:
            forms += [rom, rom.replace(" ", "")]
    return tuple(dict.fromkeys(forms))


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.split(s.lower()) if t}


def author_tokens(name: str | None) -> set[str]:
    """Order-independent token set for an author name.

    Japanese names are romanized first (so 住野よる → {sumino, yoru}); Latin
    names tokenize directly (Yoru Sumino → {yoru, sumino}). Returns an empty set
    when nothing usable is available.
    """
    if not name:
        return set()
    if has_japanese(name):
        rom = to_romaji(name)
        if rom:
            return _tokens(rom)
        # pykakasi missing: drop the Japanese, keep any Latin tokens present.
        return _tokens(_JP_RE.sub(" ", name))
    return _tokens(name)
