"""Text normalization shared across stages: author names today; the title
normalizers belong here too.

Two different jobs, kept apart on purpose:

* `clean_author()` — the *display* form: only what is unambiguously noise
  (OPF list punctuation like the trailing ";" in "James Nestor;", and an explicit
  "Last, First" written out in the string).
* `author_key()` — the *comparison* key. Two spellings of one person get one key,
  so grouping, sorting and duplicate checks stop splitting them. Never shown.
"""
from __future__ import annotations

import html
import re
import unicodedata

# Kana, kanji (incl. extension A and compatibility ideographs) and half-width kana.
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]")
_TRAILING_PUNCT = re.compile(r"[\s;,、，；]+$")
_LAST_FIRST = re.compile(r"^([^,;]+),\s*([^,;]+)$")
# Everything that is not a letter/number: spaces, dots, commas, middle dots, brackets…
_SEPARATORS = re.compile(r"[\W_]+")


def clean_author(author: str | None) -> str | None:
    """Display form of an OPF author: no trailing list punctuation, and a written-out
    Latin "Last, First" flipped to "First Last". Anything else is left as the file
    wrote it — in particular a Japanese name keeps its space (or lack of one)."""
    if not author:
        return author
    s = _TRAILING_PUNCT.sub("", author.strip())
    m = _LAST_FIRST.match(s)
    if m and not _CJK.search(s):
        s = f"{m.group(2).strip()} {m.group(1).strip()}"
    return s or None


def author_key(author: str | None) -> str | None:
    """Comparison key: NFKC, case-folded, and independent of spacing/order.

    * Japanese (any CJK character): all separators dropped, so 廣嶋 玲子 == 廣嶋玲子
      and はやみね かおる == はやみねかおる.
    * Everything else: the set of words, sorted, so Bill Bryson == Bryson Bill ==
      Bryson, Bill and J.K. Rowling == J. K. Rowling == Rowling, J.K.
    """
    if not author:
        return None
    s = unicodedata.normalize("NFKC", _TRAILING_PUNCT.sub("", author.strip())).casefold()
    if _CJK.search(s):
        return _SEPARATORS.sub("", s) or None
    words = [w for w in _SEPARATORS.split(s) if w]
    return " ".join(sorted(words)) or None


# ── titles ─────────────────────────────────────────────────────────────
# Six named variants, one per job — each fix lands here once instead of drifting
# across copies:
#
#   peel_title        trailing publisher/edition brackets + appended English translation.
#                     What series derivation reads (it must not shift series names).
#   display_title     peel_title + leading labels/imprints/edition markers, spaced-out
#                     kana, full-width digits/letters. What a book is *shown* as.
#   natively_query    display_title + the name-only form Natively is searched with.
#   match_key         EPUB↔audiobook comparison form (audiobook_xref).
#   compact_key       whitespace-insensitive equality (the /add duplicate check).
#   file_title_key    audiobook file/folder ↔ catalog matching (consolidate).

from .audiobook_scanner import CATALOG_KEY_RE, VOL_PREFIX_RE  # noqa: E402  (filename conventions)

# Trailing bracketed publisher/edition notes: (ガガガ文庫), 【電子特別版】 …
_TRAILING_BRACKETS = re.compile(r"[\s　]*[（(【\[][^（）()【】\[\]]*[）)】\]][\s　]*$")
# An appended English translation of the JP title, e.g. "…賢者の石 - Harry Potter
# and the Philosopher's Stone". Gated on CJK appearing before the match so a
# genuine English book's real " - Subtitle" is never touched.
_ENGLISH_SUFFIX = re.compile(r"[\s　]+[-–—][\s　]+[A-Za-z].*$")
_JP_CHAR = re.compile(r"[぀-ヿ一-鿿]")

# Leading noise. Each strip is only taken when something substantial is left.
_LEADING_LABEL = re.compile(r"^【[^】]*】[\s　]*")                           # 【大活字シリーズ】星の王子さま
_LEADING_IMPRINT = re.compile(                                               # 双葉社ジュニア文庫 …
    r"^[^\s　【】（）()\[\]]{1,15}?(?:文庫|新書|ノベルス|ブックス|ライブラリー)[\s　]+")
_LEADING_EDITION = re.compile(                                               # 新装版　魔女の宅急便
    r"^(?:新装改訂版|新装版|改訂新版|改訂版|新版|電子特別版|電子版|文庫版|愛蔵版|普及版|増補版|完全版)[\s　]+")
_TRAILING_EDITION = re.compile(                                              # …　全６冊合本版
    r"[\s　]+(?:全[0-9０-９]+[冊巻](?:合本版|合本|セット|収録)?|合本版)$")
_SPACED_KANA = re.compile(r"(?:[ぁ-んァ-ヶー][\s　])+[ぁ-んァ-ヶー]")           # モ　モ
# Full-width digits and Latin letters (ＣＡＳＥ１ → CASE1). Punctuation is left alone:
# ！ 、 ＆ are part of how a Japanese title is written.
_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_NUMERIC_BRACKET = re.compile(r"[\s　]*[（(][0-9０-９]{1,4}[）)]$")           # 都会のトム＆ソーヤ(1)


def peel_title(title: str) -> str:
    """Peel trailing bracket groups and appended English translations off a
    title — but never a bare bracketed number like (1), which is a volume
    marker, not a publisher note."""
    t = title.strip()
    while True:
        m = _TRAILING_BRACKETS.search(t)
        if m:
            inner = re.sub(r"[（(【\[）)】\]\s　]", "", m.group(0))
            if re.fullmatch(r"[0-9０-９]{1,4}", inner):
                break
            t = t[: m.start()].strip()
            continue
        m = _ENGLISH_SUFFIX.search(t)
        if m and _JP_CHAR.search(t[: m.start()]):
            t = t[: m.start()].strip()
            continue
        break
    return t


def _substantial(s: str) -> bool:
    """Is `s` still a title — two or more characters, at least one a letter/kana/kanji
    — rather than the empty or numeric stub a too-greedy strip would leave?"""
    return len(s) >= 2 and bool(re.search(r"[^\W\d_]", s))


def display_title(title: str) -> str:
    """The title as shown. Idempotent."""
    # A title that is nothing but a bracket group (【推しの子】) peels to "": keep it.
    t = peel_title(title) or title.strip()
    for _ in range(6):
        for pat, leading in ((_LEADING_LABEL, True), (_LEADING_IMPRINT, True),
                             (_LEADING_EDITION, True), (_TRAILING_EDITION, False)):
            m = pat.search(t)
            if not m:
                continue
            rest = (t[m.end():] if leading else t[: m.start()]).strip()
            if _substantial(rest):
                t = peel_title(rest)
                break
        else:
            break
    if _SPACED_KANA.fullmatch(t):
        t = re.sub(r"[\s　]", "", t)
    return t.translate(_FULLWIDTH)


def natively_query(title: str) -> str:
    """What to search Natively for: the display title, name only — no volume
    number in brackets, and "[N巻] Name" turned into "Name N"."""
    m = re.match(r"^\[(\d+)巻\]\s*(.*)", title)
    if m:
        vol, rest = m.group(1), m.group(2).strip()
        if not re.search(rf"{re.escape(vol)}\s*$", rest):
            rest = f"{rest} {vol}"
        title = rest
    t = _NUMERIC_BRACKET.sub("", display_title(title))
    return re.sub(r"\s+", " ", t).strip()


def strip_volume_prefix(s: str) -> str:
    """The catalog prefixes some titles with the volume: "[5巻] リアデイルの大地にて5"."""
    return _VOL_PREFIX.sub("", s)


_VOL_PREFIX = re.compile(r"^\s*[\[［【]\s*[0-9０-９]+\s*巻\s*[\]］】]\s*")


def match_key(s: str | None) -> str:
    """Comparison form: NFKC (full-width digits/latin → ASCII), lowercase,
    punctuation and whitespace dropped. Digits are kept — they are the volume."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[\W_]+", "", s)


def compact_key(s: str | None) -> str:
    """NFKC, case-folded, whitespace removed — equality that ignores spacing only."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def file_title_key(s: str) -> str:
    """Audiobook folder/file name or catalog title → matching key: drops the
    `[01]` volume prefix, the `[B0…]` catalog key and Shogakukan noise."""
    s = unicodedata.normalize("NFKC", s)
    s = VOL_PREFIX_RE.sub("", s)
    s = CATALOG_KEY_RE.sub("", s)
    # Publisher noise, optionally preceded by a (modified) colon.
    s = re.sub(r"[꞉:：]?\s*[（(]小学館[^）)]*[）)]", "", s)
    s = re.sub(r"[\s　]+", "", s)
    return s.lower()


# ── kana ─────────────────────────────────────────────────────────────────────

_KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}   # ァ–ヶ → ぁ–ゖ (ー and ・ stay)


def to_hiragana(s: str | None) -> str:
    """NFKC (so half-width ｶﾅ becomes full-width) with katakana folded to hiragana —
    the one script every reading is compared in."""
    return unicodedata.normalize("NFKC", s or "").translate(_KATA_TO_HIRA)


# ── descriptions ─────────────────────────────────────────────────────────────

def plain_text(value: str | None) -> str | None:
    """A description written as HTML (the Audible catalog's `<br />`, Google Books'
    `<p>`) as plain text: each break or paragraph end a newline, every other tag
    dropped, entities decoded. The book page shows it with `white-space: pre-line`."""
    if not value:
        return None
    value = re.sub(r"<br\s*/?>|</p>", "\n", value, flags=re.I)
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", "\n", value)).strip() or None
