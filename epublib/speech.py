"""Checks an alignment map against the audio itself.

Coverage can't tell a good map from a bad one: SubPlz `sync` writes a cue for every line, so a map
can read 99.9% and still be minutes behind the voice. The audio can tell: where the
map has a gap of several seconds between two sentences, the narrator should be quiet, and ffmpeg's
`silencedetect` says whether they are. A gap that is mostly speech is a stretch the map lost.

`detect_silences` runs ffmpeg once per audio file (minutes for a long book, so never on a request);
the result is cached in `alignments/{book_id}.silence.json`, keyed by each file's size and mtime,
and `align._rebuild` reads it to put a `quality` block in the map. Derived data: a purge may drop it,
`cli.py align-audit` makes it again.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import Config
from .index_store import save_json

NOISE_DB = -35      # quieter than this is silence
MIN_SILENCE = 0.5   # seconds: shorter dips are inside speech
MIN_GAP_MS = 5000   # a gap between sentences worth checking
SPEECH_SHARE = 0.3  # a gap more than this much speech is narration the map lost (narration is ~0.65)

_EVENT = re.compile(r"silence_(start|end): (-?[0-9.]+)")


def parse_silences(stderr: str) -> list[list[int]]:
    """`[[startMs, endMs], …]` from ffmpeg's `silencedetect` output. A silence still open at the end of
    the file is dropped (it is the tail, after the last sentence)."""
    out, start = [], None
    for kind, value in _EVENT.findall(stderr):
        ms = max(0, round(float(value) * 1000))
        if kind == "start":
            start = ms
        elif start is not None:
            out.append([start, ms])
            start = None
    return out


def detect_silences(path: Path, ffmpeg: str = "ffmpeg") -> list[list[int]]:
    """The silences of one audio file. Raises RuntimeError, with a message to show as it is, when
    ffmpeg isn't installed or can't read it: nothing else in Hondana needs ffmpeg."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-vn",
             "-af", f"silencedetect=noise={NOISE_DB}dB:d={MIN_SILENCE}", "-f", "null", "-"],
            capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg isn't installed (looked for {ffmpeg!r}): checking a map against "
                           "its audio needs it, so install ffmpeg and run this again") from None
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg couldn't read {path.name}: {proc.stderr.strip()[-300:]}")
    return parse_silences(proc.stderr)


def silence_path(cfg: Config, book_id: str) -> Path:
    return cfg.alignments_dir / f"{book_id}.silence.json"


def load(cfg: Config, book_id: str) -> dict[str | None, list[list[int]]] | None:
    """The cached silences per part (None for an m4b), or None when the book hasn't been checked."""
    try:
        data = json.loads(silence_path(cfg, book_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return {(None if k == "" else k): v["silences"] for k, v in data.get("audio", {}).items()}


def check(cfg: Config, book_id: str, audio: list[tuple[str | None, Path]], ffmpeg: str = "ffmpeg") -> bool:
    """Finds the silences of each audio file (part name, path), reusing the cache for a file whose size
    and mtime haven't changed. Returns whether anything was computed afresh."""
    path = silence_path(cfg, book_id)
    try:
        cached = json.loads(path.read_text("utf-8")).get("audio", {})
    except (OSError, ValueError):
        cached = {}
    out, fresh = {}, False
    for part, file in audio:
        try:
            st = file.stat()
        except OSError as exc:
            raise RuntimeError(f"can't read {file}: {exc.strerror or exc}") from exc
        key = part or ""
        have = cached.get(key)
        if have and have.get("size") == st.st_size and have.get("mtime") == int(st.st_mtime):
            out[key] = have
            continue
        out[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "silences": detect_silences(file, ffmpeg)}
        fresh = True
    save_json(path, {"audio": out})
    return fresh


def speech_gaps(docs: list[dict], silences: dict[str | None, list[list[int]]]) -> list[list]:
    """The gaps of MIN_GAP_MS or more between two timed sentences of one section that are mostly
    speech: `[doc, sentence before, sentence after, startMs, endMs, speech share]`, in book order. A
    gap between sections is the book's own silence (a chapter break) and is not checked; neither is
    a part whose silences aren't known."""
    out = []
    for d in docs:
        last = None
        for i, row in enumerate(d["sents"]):
            if not row:
                continue
            if last is not None:
                prev = d["sents"][last]
                part = row[2] if len(row) > 2 else None
                if (prev[2] if len(prev) > 2 else None) == part and part in silences:
                    a, b = prev[1], row[0]
                    if b - a >= MIN_GAP_MS:
                        quiet = sum(max(0, min(b, e) - max(a, s)) for s, e in silences[part])
                        share = round((b - a - quiet) / (b - a), 2)
                        if share > SPEECH_SHARE:
                            out.append([d["index"], last, i, a, b, share])
            last = i
    return out


def quality(docs: list[dict], silences: dict[str | None, list[list[int]]] | None) -> dict:
    """The map's `quality` block: `checked` false until the audio has been looked at."""
    if silences is None:
        return {"checked": False}
    gaps = speech_gaps(docs, silences)
    return {"checked": True, "speech_gaps": len(gaps),
            "speech_gap_s": round(sum(g[4] - g[3] for g in gaps) / 1000, 1), "gaps": gaps[:200]}
