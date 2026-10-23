"""Tiny audio files, built byte by byte and tagged with mutagen, for the
scanner and tag tests. No encoder needed: each is a valid container whose
duration the header states (silence, or no audio data at all), so mutagen
reads the length and tags exactly as it would from a real audiobook.

    m4b(path, seconds=60, title="…", asin="B0…", cover=PNG, pad=bytes of "audio")
    mp3(path, seconds=…, album="…", track=1)
    flac(path, …) / opus(path, …)

Tag keywords: title, album, artist, album_artist, composer, narrator, series,
part, asin, description, year, genre, track, cover (image bytes).
"""
from __future__ import annotations

import base64
import struct
from pathlib import Path

# A 1×1 PNG, enough for Pillow and for any "has a cover" check.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), name) + payload


def _full(name: bytes, payload: bytes) -> bytes:
    return _atom(name, b"\0\0\0\0" + payload)


def _mp4_bytes(seconds: float, brand: bytes, pad: int = 0) -> bytes:
    scale = 1000
    dur = int(seconds * scale)
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = _full(b"mvhd", struct.pack(">4I", 0, 0, scale, dur) + struct.pack(">IH", 0x10000, 0x100)
                 + b"\0" * 10 + matrix + b"\0" * 24 + struct.pack(">I", 2))
    mdhd = _full(b"mdhd", struct.pack(">4I", 0, 0, scale, dur) + b"\0\0\0\0")
    hdlr = _full(b"hdlr", b"\0\0\0\0soun" + b"\0" * 12 + b"Sound\0")
    trak = _atom(b"trak", _atom(b"mdia", mdhd + hdlr))
    ftyp = _atom(b"ftyp", brand + b"\0\0\0\0" + brand + b"mp42isom")
    # `pad` stands in for the audio: bytes that differ along the file, like real sound.
    audio = bytes(i * 7 % 251 for i in range(pad))
    return ftyp + _atom(b"moov", mvhd + trak) + _atom(b"mdat", audio)


def _tag_mp4(path: Path, t: dict) -> None:
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    f = MP4(path)
    simple = {"title": "\xa9nam", "album": "\xa9alb", "artist": "\xa9ART",
              "album_artist": "aART", "composer": "\xa9wrt", "narrator": "\xa9nrt",
              "description": "desc", "year": "\xa9day", "genre": "\xa9gen"}
    for k, atom in simple.items():
        if t.get(k):
            f[atom] = [t[k]]
    free = {"asin": "ASIN", "series": "SERIES", "part": "SERIES-PART"}
    for k, name in free.items():
        if t.get(k):
            f[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(t[k].encode())]
    if t.get("cdek"):
        f["CDEK"] = [t["cdek"]]
    if t.get("track"):
        f["trkn"] = [(int(t["track"]), 0)]
    if t.get("cover"):
        f["covr"] = [MP4Cover(t["cover"], imageformat=MP4Cover.FORMAT_PNG)]
    f.save()


def m4b(path: Path, seconds: float = 60.0, pad: int = 0, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_mp4_bytes(seconds, b"M4B ", pad))
    if tags:
        _tag_mp4(path, tags)
    return path


def m4a(path: Path, seconds: float = 60.0, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_mp4_bytes(seconds, b"M4A "))
    if tags:
        _tag_mp4(path, tags)
    return path


def mp3(path: Path, seconds: float = 2.0, **tags) -> Path:
    """MPEG-1 Layer III, 128 kb/s, 44.1 kHz: 417-byte frames of 1152 samples."""
    from mutagen.id3 import APIC, COMM, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX

    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(seconds * 44100 / 1152))
    path.write_bytes((b"\xff\xfb\x90\x64" + b"\0" * 413) * frames)
    if not tags:
        return path
    id3 = ID3()
    for k, frame in {"title": TIT2, "album": TALB, "artist": TPE1, "album_artist": TPE2,
                     "composer": TCOM, "year": TDRC, "genre": TCON, "track": TRCK}.items():
        if tags.get(k):
            id3.add(frame(encoding=3, text=[str(tags[k])]))
    for k, desc in {"asin": "ASIN", "series": "SERIES", "part": "SERIES-PART",
                    "narrator": "NARRATOR"}.items():
        if tags.get(k):
            id3.add(TXXX(encoding=3, desc=desc, text=[tags[k]]))
    if tags.get("description"):
        id3.add(COMM(encoding=3, lang="eng", desc="", text=[tags["description"]]))
    if tags.get("cover"):
        id3.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=tags["cover"]))
    id3.save(path)
    return path


_VORBIS = {"title": "TITLE", "album": "ALBUM", "artist": "ARTIST", "album_artist": "ALBUMARTIST",
           "composer": "COMPOSER", "narrator": "NARRATOR", "series": "SERIES",
           "part": "SERIES-PART", "asin": "ASIN", "description": "DESCRIPTION",
           "year": "DATE", "genre": "GENRE", "track": "TRACKNUMBER"}


def _picture(data: bytes):
    from mutagen.flac import Picture

    pic = Picture()
    pic.type, pic.mime, pic.data, pic.width, pic.height, pic.depth = 3, "image/png", data, 1, 1, 24
    return pic


def flac(path: Path, seconds: float = 60.0, **tags) -> Path:
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    rate, samples = 44100, int(seconds * 44100)
    # STREAMINFO: block sizes, frame sizes, then rate(20) channels-1(3) bps-1(5) samples(36).
    packed = (rate << 44) | (1 << 41) | (15 << 36) | samples
    info = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + packed.to_bytes(8, "big") + b"\0" * 16
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    f = FLAC(path)
    for k, name in _VORBIS.items():
        if tags.get(k):
            f[name] = str(tags[k])
    if tags.get("cover"):
        f.add_picture(_picture(tags["cover"]))
    f.save()
    return path


def opus(path: Path, seconds: float = 60.0, **tags) -> Path:
    from mutagen.ogg import OggPage
    from mutagen.oggopus import OggOpus

    path.parent.mkdir(parents=True, exist_ok=True)
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 312, 48000, 0, 0)
    comments = b"OpusTags" + struct.pack("<I", 7) + b"fixture" + struct.pack("<I", 0)
    pages = []
    for seq, (packet, granule) in enumerate(((head, 0), (comments, 0),
                                             (b"\xf8", int(seconds * 48000) + 312))):
        page = OggPage()
        page.serial, page.sequence, page.position, page.packets = 1, seq, granule, [packet]
        page.first = seq == 0
        page.last = seq == 2
        pages.append(page.write())
    path.write_bytes(b"".join(pages))
    f = OggOpus(path)
    for k, name in _VORBIS.items():
        if tags.get(k):
            f[name] = str(tags[k])
    if tags.get("cover"):
        f["METADATA_BLOCK_PICTURE"] = base64.b64encode(_picture(tags["cover"]).write()).decode()
    f.save()
    return path
