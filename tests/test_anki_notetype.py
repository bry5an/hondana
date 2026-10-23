"""The Anki note type the iOS reader sends (PUT /api/reader/anki-note-type) and the .apkg
/profile offers for desktop Anki, since AnkiMobile can't create a note type."""
import io
import json
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from epublib import anki_notetype, profiles

SPEC = {
    "name": "Japanese",
    "fields": [
        {"name": "Expression", "sample": "猫"},
        {"name": "ExpressionReading", "sample": "ねこ"},
        {"name": "Notes", "sample": ""},
    ],
    "front": '<div class="word">{{Expression}}</div>',
    "back": "{{FrontSide}}<hr id=answer>{{ExpressionReading}}",
    "css": ".card { font-size: 20px; }",
}


@pytest.fixture
def client(server):
    return TestClient(server.app)


def _collection(apkg: bytes) -> sqlite3.Connection:
    zf = zipfile.ZipFile(io.BytesIO(apkg))
    names = zf.namelist()
    assert "collection.anki2" in names and "media" in names
    db = sqlite3.connect(":memory:")
    db.deserialize(zf.read("collection.anki2"))
    return db


def _models(db) -> dict:
    return json.loads(db.execute("select models from col").fetchone()[0])


def test_nothing_is_offered_until_the_phone_has_sent_one(client):
    assert client.get("/api/profile/anki-note-type").status_code == 404
    assert client.get("/profile/anki-note-type.apkg").status_code == 404


def test_what_was_sent_is_described_back_and_packaged(client):
    assert client.put("/api/reader/anki-note-type", json=SPEC).status_code == 200
    described = client.get("/api/profile/anki-note-type").json()
    assert described["name"] == "Japanese"
    assert described["fields"] == ["Expression", "ExpressionReading", "Notes"]

    r = client.get("/profile/anki-note-type.apkg")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and ".apkg" in r.headers["content-disposition"]
    db = _collection(r.content)
    (model,) = _models(db).values()
    assert model["name"] == "Japanese"
    assert [f["name"] for f in model["flds"]] == ["Expression", "ExpressionReading", "Notes"]
    assert model["tmpls"][0]["qfmt"] == SPEC["front"] and model["tmpls"][0]["afmt"] == SPEC["back"]
    assert model["css"] == SPEC["css"]
    # The sample card is what makes Anki import the note type at all.
    flds, tags = db.execute("select flds, tags from notes").fetchone()
    assert flds.split("\x1f") == ["猫", "ねこ", ""]
    assert anki_notetype.SAMPLE_TAG in tags


def test_the_same_name_is_the_same_note_type_to_anki(client):
    client.put("/api/reader/anki-note-type", json=SPEC)
    first = list(_models(_collection(client.get("/profile/anki-note-type.apkg").content)))
    client.put("/api/reader/anki-note-type", json={**SPEC, "css": ".card { color: red; }"})
    again = list(_models(_collection(client.get("/profile/anki-note-type.apkg").content)))
    client.put("/api/reader/anki-note-type", json={**SPEC, "name": "Other"})
    other = list(_models(_collection(client.get("/profile/anki-note-type.apkg").content)))
    assert first == again and first != other


def test_a_bad_description_is_refused(client):
    assert client.put("/api/reader/anki-note-type", json={**SPEC, "fields": []}).status_code == 422
    assert client.put("/api/reader/anki-note-type", json={**SPEC, "name": ""}).status_code == 422
    dup = {**SPEC, "fields": [{"name": "A", "sample": ""}, {"name": "A", "sample": ""}]}
    assert client.put("/api/reader/anki-note-type", json=dup).status_code == 400
    assert client.get("/api/profile/anki-note-type").status_code == 404


def test_each_profile_has_its_own(server, client):
    ben = profiles.create(server.cfg, "Ben", "ja")["slug"]
    client.put("/api/reader/anki-note-type", json=SPEC)
    ben_client = TestClient(server.app, cookies={profiles.COOKIE_NAME: ben})
    assert ben_client.get("/api/profile/anki-note-type").status_code == 404
