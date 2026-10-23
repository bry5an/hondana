"""The Anki note type the Hondana iOS reader sends cards to, packaged for desktop Anki.

AnkiMobile can't create a note type, so the phone sends its description here
(`PUT /api/reader/anki-note-type`) and /profile offers it as a `.apkg` to
download. Anki desktop adds a note type by importing a package that has a note
of that type, so the package carries the phone's sample card (tagged
`hondana-sample`, in a deck called "Hondana") for the person to delete after.

Stored per profile in `users/<slug>/anki_note_type.json`:
`{schema, name, fields: [{name, sample}], front, back, css, updated_at}`.
"""
from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import genanki

from . import schemas
from .config import Config
from .index_store import load_json, save_json

SCHEMA_VERSION = 1
DECK_NAME = "Hondana"
SAMPLE_TAG = "hondana-sample"


def load(cfg: Config) -> dict | None:
    """What the phone last sent, or None if it never has."""
    data = load_json(cfg.anki_note_type, {})
    return data if data.get("name") and data.get("fields") else None


def save(cfg: Config, spec: schemas.AnkiNoteTypeRequest) -> dict:
    names = [f.name for f in spec.fields]
    if len(set(names)) != len(names):
        raise ValueError("Two fields have the same name")
    data = {
        "schema": SCHEMA_VERSION,
        "name": spec.name,
        "fields": [f.model_dump() for f in spec.fields],
        "front": spec.front,
        "back": spec.back,
        "css": spec.css,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_json(cfg.anki_note_type, data)
    return data


def _stable_id(text: str) -> int:
    """Anki identifies a note type and a deck by id: the same name must give the same id, so
    importing the file again updates the note type rather than adding a second one."""
    return (1 << 30) + int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:7], 16)


def build_apkg(spec: dict) -> bytes:
    model = genanki.Model(
        _stable_id("model:" + spec["name"]),
        spec["name"],
        fields=[{"name": f["name"]} for f in spec["fields"]],
        templates=[{"name": "Card 1", "qfmt": spec["front"], "afmt": spec["back"]}],
        css=spec.get("css") or "",
    )
    deck = genanki.Deck(_stable_id("deck:" + DECK_NAME), DECK_NAME)
    note = genanki.Note(
        model=model,
        fields=[f.get("sample", "") for f in spec["fields"]],
        tags=[SAMPLE_TAG],
        guid=genanki.guid_for("hondana-sample", spec["name"]),
    )
    deck.add_note(note)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "note-type.apkg"
        genanki.Package(deck).write_to_file(str(path))
        return path.read_bytes()
