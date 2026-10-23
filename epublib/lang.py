"""Language detection for a book.

Per the user's convention, Japanese books have Japanese filenames and English
books have English filenames, so the *filename* is the primary signal. The OPF
dc:language tag is a corroborating signal. When they disagree, the filename
script wins.

Returns one of: "ja", "en", "other".
"""
from __future__ import annotations

# Hiragana, Katakana, and CJK Unified Ideographs.
_JP_RANGES = (
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs (Kanji)
    (0x3400, 0x4DBF),  # CJK Extension A
    (0xFF66, 0xFF9D),  # Half-width Katakana
)


def _has_japanese(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        for lo, hi in _JP_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _norm_opf_lang(lang: str | None) -> str | None:
    if not lang:
        return None
    code = lang.strip().lower().replace("_", "-").split("-", 1)[0]
    if code in ("ja", "jpn", "jp"):
        return "ja"
    if code in ("en", "eng"):
        return "en"
    return "other"


def detect(filename: str, opf_language: str | None) -> tuple[str, str]:
    """Return (language, source) where language is ja/en/other.

    source is one of: "filename", "opf", "both", "default".
    """
    fn_jp = _has_japanese(filename)
    opf = _norm_opf_lang(opf_language)

    # Filename is the primary signal.
    if fn_jp:
        return "ja", ("both" if opf == "ja" else "filename")

    # No Japanese in the filename → English/Latin filename.
    if opf == "ja":
        # OPF says Japanese but filename is Latin (e.g. romaji filename). Trust OPF.
        return "ja", "opf"
    if opf == "en":
        return "en", ("both" if not fn_jp else "opf")
    if opf == "other":
        return "other", "opf"

    # No usable OPF language and a Latin filename → assume English.
    return "en", "default"
