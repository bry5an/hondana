"""What an audiobook file says about itself: its embedded tags, read with mutagen.

`read(path)` gives one file's tags, `read_tracks(paths)` a folder of tracks taken
as one book. Both return a plain dict (or None for a file mutagen can't read):

    title, album, author, narrator, series, part, asin, description, year,
    genre, track, duration (seconds), cover (bytes or None), cover_mime

Tag names differ by container, and taggers disagree on where series and
narrator go, so each field is a list of places to look, first hit wins:

- MP4 (m4b/m4a): ©nam ©alb ©ART aART ©wrt ©nrt desc ©day ©gen trkn covr, plus
  freeform `----:com.apple.iTunes:<NAME>` atoms (ASIN, SERIES, SERIES-PART,
  NARRATOR) and Audible's own `CDEK` (the ASIN).
- ID3 (mp3): TIT2 TALB TPE1 TPE2 TCOM TDRC TCON TRCK COMM APIC, TXXX:<NAME>,
  MVNM/MVIN (movement name/number, which some taggers use for series).
- Vorbis comments (flac, ogg, opus): TITLE ALBUM ARTIST ALBUMARTIST COMPOSER
  NARRATOR SERIES SERIES-PART ASIN DESCRIPTION DATE GENRE TRACKNUMBER, and the
  picture block.

The author is the album artist, else the artist; the narrator is a narrator tag,
else the composer (Audible and Libation put narrators there). Nothing here
raises: a broken file is a file without tags.
"""
from __future__ import annotations

import base64
import re
from collections import Counter
from pathlib import Path

AUDIO_EXTS = (".m4b", ".m4a", ".mp3", ".flac", ".opus", ".ogg")
ASIN_RE = re.compile(r"[0-9A-Z]{10}")
FIELDS = ("title", "album", "author", "narrator", "series", "part", "asin",
          "description", "year", "genre", "track")

_MP4 = {
    "title": ("\xa9nam",), "album": ("\xa9alb",), "artist": ("\xa9ART",),
    "album_artist": ("aART",), "composer": ("\xa9wrt",), "narrator": ("\xa9nrt",),
    "description": ("desc", "ldes", "\xa9des", "\xa9cmt"), "year": ("\xa9day",),
    "genre": ("\xa9gen",), "series": ("\xa9mvn",), "part": ("\xa9mvi",), "asin": ("CDEK",),
}
_ID3 = {
    "title": ("TIT2",), "album": ("TALB",), "artist": ("TPE1",), "album_artist": ("TPE2",),
    "composer": ("TCOM",), "year": ("TDRC", "TYER"), "genre": ("TCON",), "track": ("TRCK",),
    "series": ("MVNM",), "part": ("MVIN",),
}
_VORBIS = {
    "title": ("title",), "album": ("album",), "artist": ("artist",),
    "album_artist": ("albumartist", "album artist", "album_artist"), "composer": ("composer",),
    "narrator": ("narrator", "narratedby"), "description": ("description", "comment"),
    "year": ("date", "year"), "genre": ("genre",), "track": ("tracknumber",),
    "series": ("series", "mvnm"), "part": ("series-part", "seriespart", "mvin"),
    "asin": ("asin", "audible_asin"),
}
# Free-form names (MP4 `----:…:NAME`, ID3 `TXXX:NAME`), compared case-insensitively.
_FREE = {
    "asin": ("asin", "audible_asin"), "series": ("series",),
    "part": ("series-part", "seriespart", "series_part"), "narrator": ("narrator", "narratedby"),
    "description": ("description",),
}


def _text(value) -> str | None:
    """The first non-empty string in a tag value (a list, a frame, bytes, a tuple…)."""
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            text = _text(item)
            if text:
                return text
        return None
    if isinstance(getattr(value, "text", None), list):  # an ID3 frame
        return _text(value.text)
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).replace("\x00", " ").strip()
    return text or None


def _pick(get, names) -> str | None:
    for name in names:
        text = _text(get(name))
        if text:
            return text
    return None


def _mp4(tags) -> tuple[dict, tuple | None]:
    free = {k.rsplit(":", 1)[-1].lower(): v for k, v in tags.items() if k.startswith("----:")}
    raw = {f: _pick(tags.get, names) for f, names in _MP4.items()}
    trkn = tags.get("trkn")  # [(number, total)]
    raw["track"] = str(trkn[0][0]) if trkn and trkn[0][0] else None
    for f, names in _FREE.items():
        hit = _pick(free.get, names)
        if hit:  # a freeform atom is the more deliberate place for these
            raw[f] = hit
    cover = None
    for c in tags.get("covr") or []:
        cover = (bytes(c), "image/png" if getattr(c, "imageformat", 13) == 14 else "image/jpeg")
        break
    return raw, cover


def _id3(tags) -> tuple[dict, tuple | None]:
    def get(frame_id):
        frames = tags.getall(frame_id)
        return frames[0] if frames else None

    raw = {f: _pick(get, names) for f, names in _ID3.items()}
    free = {fr.desc.lower(): fr for fr in tags.getall("TXXX")}
    for f, names in _FREE.items():
        hit = _pick(free.get, names)
        if hit:
            raw[f] = hit
    if not raw.get("description"):
        raw["description"] = _text(next(iter(tags.getall("COMM")), None))
    cover = None
    pics = sorted(tags.getall("APIC"), key=lambda p: p.type != 3)  # the front cover first
    if pics:
        cover = (pics[0].data, pics[0].mime or "image/jpeg")
    return raw, cover


def _vorbis(f) -> tuple[dict, tuple | None]:
    tags = f.tags
    raw = {fld: _pick(lambda k: tags.get(k), names) for fld, names in _VORBIS.items()}
    cover = None
    pictures = list(getattr(f, "pictures", None) or [])
    if not pictures:
        from mutagen.flac import Picture

        for b64 in tags.get("metadata_block_picture") or []:
            try:
                pictures.append(Picture(base64.b64decode(b64)))
            except Exception:  # noqa: BLE001 — a bad picture block is no cover
                continue
    pictures.sort(key=lambda p: p.type != 3)
    if pictures:
        cover = (pictures[0].data, pictures[0].mime or "image/jpeg")
    return raw, cover


def _clean(raw: dict, cover: tuple | None, duration: float) -> dict:
    asin = (raw.get("asin") or "").strip().upper()
    year = re.match(r"\d{4}", raw.get("year") or "")
    track = re.match(r"\d+", raw.get("track") or "")
    part = raw.get("part")
    return {
        "title": raw.get("title"),
        "album": raw.get("album"),
        "author": raw.get("album_artist") or raw.get("artist"),
        "narrator": raw.get("narrator") or raw.get("composer"),
        "series": raw.get("series"),
        "part": part if part and part != "0" else None,
        "asin": asin if ASIN_RE.fullmatch(asin) else None,
        "description": raw.get("description"),
        "year": year.group(0) if year else None,
        "genre": raw.get("genre"),
        "track": int(track.group(0)) if track else None,
        "duration": round(duration, 3) if duration else None,
        "cover": cover[0] if cover else None,
        "cover_mime": cover[1] if cover else None,
    }


def read(path: Path) -> dict | None:
    """One file's tags, or None when mutagen can't read it at all."""
    try:
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4Tags

        f = mutagen.File(path)
    except Exception:  # noqa: BLE001 — unreadable is "no tags", never a failed scan
        return None
    if f is None:
        return None
    duration = getattr(f.info, "length", 0.0) or 0.0
    tags = f.tags
    try:
        if tags is None:
            raw, cover = {}, None
        elif isinstance(tags, MP4Tags):
            raw, cover = _mp4(tags)
        elif isinstance(tags, ID3):
            raw, cover = _id3(tags)
        else:
            raw, cover = _vorbis(f)
    except Exception:  # noqa: BLE001
        raw, cover = {}, None
    return _clean(raw, cover, duration)


def _most_common(values) -> str | None:
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else None


def merge_tracks(tracks: list[dict | None]) -> dict | None:
    """Per-track tags (as `read` or `summary` returns them, in play order) as one
    book: the album is its title, durations add up, everything else is the most
    common value (a stray track tagged differently doesn't win)."""
    tracks = [t for t in tracks if t]
    if not tracks:
        return None
    out = {f: _most_common(t.get(f) for t in tracks) for f in FIELDS if f not in ("track",)}
    album = out.pop("album")
    out["title"] = album or tracks[0].get("title")
    out["album"] = album
    out["track"] = None
    total = sum(t.get("duration") or 0 for t in tracks)
    out["duration"] = round(total, 3) if total else None
    with_cover = next((t for t in tracks if t.get("cover")), None)
    out["cover"] = with_cover["cover"] if with_cover else None
    out["cover_mime"] = with_cover["cover_mime"] if with_cover else None
    if with_cover or any(t.get("has_cover") for t in tracks):
        out["has_cover"] = True
    out["tracks"] = len(tracks)
    return out


def read_tracks(paths: list[Path]) -> dict | None:
    """A folder of tracks as one book (see `merge_tracks`)."""
    return merge_tracks([read(p) for p in paths])


def summary(tags: dict | None) -> dict | None:
    """`tags` without the cover bytes (JSON-safe, for the scan's cache)."""
    if not tags:
        return None
    out = {k: v for k, v in tags.items() if k not in ("cover", "cover_mime") and v is not None}
    if tags.get("cover") or tags.get("has_cover"):
        out["has_cover"] = True
    return out
