"""hondana export / import (backup.py): a data folder to a zip and back, from a zip, a
folder, a checkout or a mirror clone, with the refusals that keep it safe. Synthetic data only."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import audio_fixtures as af
from conftest import make_cfg, toml_str
from epublib import audiobook_scanner as scanner
from epublib import backup, cli, data_mirror, desktop, known, pipeline
from epublib.index_store import load_json, save_json

NEVER_FILES = [
    "auth.json", "secret.key", "hondana.lock", "hondana.url", "consolidation_plan.json",
    "logs/server.log", ".hondana-staging/abc.part", ".hondana-staging/abc.json", "alignments/.work/b1/book.txt",
    "backups/old.zip", "search.sqlite3", "search.sqlite3-wal", ".DS_Store", "covers/.DS_Store",
    "wordlists/half.json.4242.tmp", "dicts/.upload-xyz.part", "users/ann/known_stats.json", "users/ben/known_stats.json",
]
STRAYS = ["users/old/known_words.json", "users/spare/known_words.json"]


def put(root: Path, rel: str, content, mtime: int | None = None) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def world(tmp_path: Path, name: str = "a", **cfg_overrides):
    """A data folder with two profiles and a bit of everything: hand-made files, derived caches, a
    zip of dictionaries, files that never travel, and two leftover user folders."""
    cfg = make_cfg(tmp_path / name, **cfg_overrides)
    d = cfg.data_dir
    put(d, "profiles.json", {"active": "ann", "profiles": [
        {"slug": "ann", "name": "Ann", "language": "ja"}, {"slug": "ben", "name": "Ben", "language": "en"}]})
    put(d, "users/ann/known_words.json", {"words": ["猫", "犬", "本"], "marked": ["本"]})
    put(d, "users/ann/read_books.json", {"books": {"b1": {"read": True, "read_at": "2026-10-01"},
                                                   "b2": {"read": False}}})
    put(d, "users/ann/reader_progress.json", {"asin:B0X": {"book_id": "b1", "fraction": 0.5}})
    put(d, "users/ann/audio_progress.json", {"B0AUDIO001": {"track": 0, "seconds": 5.0}})
    put(d, "users/ann/bookmarks.json", {"bm1": {"ab_id": "B0AUDIO001"}, "bm2": {"deleted": True}})
    put(d, "users/ann/learning.json", {"words": {"鳥": {"lemma": "鳥"}}})
    put(d, "users/ann/history/20261001-replace.json", {"words": ["猫"]})
    put(d, "users/ben/known_words.json", {"words": ["cat", "dog"]})
    put(d, "series_overrides.json", {"b1": {"series_name": "S", "series_order": "01"}})
    put(d, "audiobook_hidden.json", {"ab_ids": ["B0HIDDEN01"]})
    put(d, "audiobook_catalog_base.json", {"B0AUDIO001": {"title": "t1"}, "B0AUDIO002": {"title": "t2"}})
    put(d, "audiobook_catalog_extra.json", {"B0AUDIO003": {"title": "t3"}})
    put(d, "epub_library.json", {"b1": {"book_id": "b1"}, "b2": {"book_id": "b2"}})
    put(d, "audiobook_files.json", {"B0AUDIO001": {"ab_id": "B0AUDIO001"}})
    put(d, "settings.json", {"schema": 1, "values": {"epub_dir": "/old/machine/books", "port": 9000}})
    put(d, "covers/b1.jpg", b"\xff\xd8 extracted")
    put(d, "covers/custom/b1.png", b"\x89PNG hand-set")
    put(d, "covers/audiobooks/custom/B0AUDIO001.png", b"\x89PNG audiobook")
    put(d, "alignments/b1.srt", "1\n00:00:00,000 --> 00:00:01,000\nx\n")
    put(d, "alignments/b1.transcript.srt", "1\n00:00:00,000 --> 00:00:01,000\ny\n")
    put(d, "alignments/b1.anchors.jsonl", '{"ab_id": "B0AUDIO001", "doc": 0, "sentence": 1}\n')
    put(d, "alignments/b1.json", {"schema": 5})
    put(d, "alignments/b1.silence.json", {"audio": {}})
    put(d, "annotations/b1.json.gz", b"\x1f\x8b gz bytes")
    put(d, "dicts/jitendex.zip", b"PK\x03\x04 pretend zip")
    for n, rel in enumerate(NEVER_FILES + STRAYS):
        put(d, rel, f"never {n}")
    for n, p in enumerate(sorted(q for q in d.rglob("*") if q.is_file())):  # distinct mtimes
        os.utime(p, (1_700_000_000 + n * 1000, 1_700_000_000 + n * 1000))
    return cfg


def quiet(_line: str) -> None:
    pass


def tree(root: Path) -> dict[str, tuple[str, int]]:
    """Every file under `root` that may travel: its sha256 and mtime."""
    return {p.relative_to(root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file() and not backup.never(p.relative_to(root).as_posix())}


def read_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read("manifest.json"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hand_zip(path: Path, files: dict[str, bytes], *, manifest: dict | None = None, extra: dict[str, bytes] | None = None,
             skip: set[str] = frozenset(), actual: dict[str, bytes] | None = None, links: set[str] = frozenset(),
             scope: str = "full") -> Path:
    """A backup made by hand so a test can break it: `files` is what the manifest lists, `actual` what is
    stored instead for a path, `extra` entries the manifest doesn't know, `skip` listed files left out."""
    entries = [{"path": rel, "size": len(data), "mtime_ns": 1_700_000_000_000_000_000, "sha256": sha(data)}
               for rel, data in files.items()]
    m = {"format": backup.FORMAT, "schema": backup.SCHEMA, "app_version": "0.0", "scope": scope,
         "created_at": "2026-10-26T00:00:00+00:00", "files": entries, "counts": {}}
    m.update(manifest or {})
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        for rel, data in files.items():
            if rel in skip:
                continue
            if rel in links:
                info = zipfile.ZipInfo("data/" + rel)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, "target")
            else:
                zf.writestr("data/" + rel, (actual or {}).get(rel, data))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


# ── export ───────────────────────────────────────────────────────────────────

def test_a_full_export_holds_everything_but_what_never_travels(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    manifest = read_manifest(out)
    assert (manifest["format"], manifest["schema"], manifest["scope"]) == ("hondana-backup", 1, "full")
    assert manifest["app_version"] and manifest["created_at"]
    paths = {f["path"] for f in manifest["files"]}
    assert not [p for p in paths if backup.never(p)]
    assert not paths & set(NEVER_FILES)
    assert {"covers/b1.jpg", "annotations/b1.json.gz", "dicts/jitendex.zip", "users/ann/history/20261001-replace.json",
            "alignments/b1.json", "alignments/b1.silence.json", "settings.json", *STRAYS} <= paths
    with zipfile.ZipFile(out) as zf:
        assert sorted(n for n in zf.namelist() if n != "manifest.json") == sorted("data/" + p for p in paths)
        kinds = {i.filename: i.compress_type for i in zf.infolist()}
    assert kinds["data/annotations/b1.json.gz"] == zipfile.ZIP_STORED and kinds["data/covers/b1.jpg"] == zipfile.ZIP_STORED
    assert kinds["data/epub_library.json"] == zipfile.ZIP_DEFLATED
    for f in manifest["files"]:
        data = (src.data_dir / f["path"]).read_bytes()
        assert (f["size"], f["sha256"]) == (len(data), sha(data))
        assert f["mtime_ns"] == (src.data_dir / f["path"]).stat().st_mtime_ns


def test_a_curated_export_is_exactly_the_files_the_mirror_holds(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "c.zip", "curated", say=quiet)
    manifest = read_manifest(out)
    assert manifest["scope"] == "curated"
    assert {f["path"] for f in manifest["files"]} == {rel for rel, _ in data_mirror.curated_files(src.data_dir)}
    assert manifest["counts"]["books"] == 0 and manifest["counts"]["profiles"]["ann"]["known_words"] == 3


def test_the_counts_say_what_is_in_the_folder(tmp_path):
    src = world(tmp_path)
    c = backup.counts(src.data_dir)
    assert (c["books"], c["audiobook_files"], c["catalog_rows"], c["alignments"]) == (2, 1, 3, 1)
    assert set(c["profiles"]) == {"ann", "ben"}  # the registry's, not every folder under users/
    assert c["profiles"]["ann"] == {"language": "ja", "known_words": 3, "marked": 1, "read_books": 1,
                                    "reader_progress": 1, "audio_progress": 1, "bookmarks": 1, "learning": 1}
    assert c["profiles"]["ben"]["known_words"] == 2 and c["profiles"]["ben"]["bookmarks"] == 0
    lines = backup.format_counts(c)
    assert "2 books" in lines[0] and any("ann (ja): 3 known words (+1 marked)" in ln for ln in lines)


def test_counts_cope_with_files_of_the_wrong_shape(tmp_path):
    cfg = make_cfg(tmp_path)
    put(cfg.data_dir, "epub_library.json", "not json {")
    put(cfg.data_dir, "profiles.json", {"profiles": [{"slug": "ann"}]})
    put(cfg.data_dir, "users/ann/known_words.json", ["a", "b"])
    put(cfg.data_dir, "users/ann/bookmarks.json", [1, 2])
    c = backup.counts(cfg.data_dir)
    assert c["books"] == 0 and c["profiles"]["ann"]["known_words"] == 0 and c["profiles"]["ann"]["bookmarks"] == 0


def test_the_default_export_goes_to_backups_and_never_into_the_next_one(tmp_path):
    src = world(tmp_path)
    first = backup.export_data(src, None, say=quiet)
    assert first.parent == src.data_dir / "backups" and first.name.startswith("hondana-")
    second = backup.export_data(src, src.data_dir / "backups" / "again.zip", say=quiet)
    assert not [f for f in read_manifest(second)["files"] if f["path"].startswith("backups/")]


def test_an_archive_written_inside_the_data_folder_is_not_packed_into_itself(tmp_path):
    src = world(tmp_path)
    out = src.data_dir / "mine.zip"
    backup.export_data(src, out, say=quiet)
    again = backup.export_data(src, out, say=quiet)  # the old one is still there, and is being replaced
    assert "mine.zip" not in {f["path"] for f in read_manifest(again)["files"]}
    assert not list(src.data_dir.glob("*.part"))


def test_an_out_that_is_a_folder_gets_a_named_file(tmp_path):
    src = world(tmp_path)
    folder = tmp_path / "usb"
    folder.mkdir()
    out = backup.export_data(src, folder, say=quiet)
    assert out.parent == folder.resolve() and out.suffix == ".zip"


def test_a_failed_export_leaves_neither_an_archive_nor_a_part_file(tmp_path, monkeypatch):
    src = world(tmp_path)
    calls = []
    real = backup._add

    def add(zf, rel, path):
        calls.append(rel)
        if len(calls) == 3:
            raise RuntimeError("disk on fire")
        return real(zf, rel, path)

    monkeypatch.setattr(backup, "_add", add)
    out = tmp_path / "out.zip"
    with pytest.raises(RuntimeError):
        backup.export_data(src, out, say=quiet)
    assert not out.exists() and not list(tmp_path.glob("out.zip*"))


def test_a_file_that_cannot_be_read_stops_the_export_instead_of_vanishing_from_it(tmp_path, monkeypatch):
    src = world(tmp_path)

    def add(zf, rel, path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(backup, "_add", add)
    with pytest.raises(backup.BackupError, match="can't read .*Permission denied"):
        backup.export_data(src, tmp_path / "out.zip", say=quiet)
    assert not list(tmp_path.glob("out.zip*"))


def test_a_file_that_vanishes_during_the_export_is_left_out_and_said(tmp_path, monkeypatch):
    src = world(tmp_path)
    real = backup._add

    def add(zf, rel, path):
        if rel == "covers/b1.jpg":
            raise FileNotFoundError(rel)
        return real(zf, rel, path)

    monkeypatch.setattr(backup, "_add", add)
    lines: list[str] = []
    out = backup.export_data(src, tmp_path / "out.zip", say=lines.append)
    assert "covers/b1.jpg" not in {f["path"] for f in read_manifest(out)["files"]}
    assert any("vanished" in ln for ln in lines)


def test_links_are_never_followed_into_an_export(tmp_path):
    src = world(tmp_path)
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("not yours", encoding="utf-8")
    try:
        (src.data_dir / "covers" / "link.jpg").symlink_to(secret)
        (src.data_dir / "linked-dir").symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    assert not [p for p in {f["path"] for f in read_manifest(out)["files"]} if "link" in p]


# ── round trip ───────────────────────────────────────────────────────────────

def test_an_export_imports_byte_for_byte_with_its_mtimes(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    dst = make_cfg(tmp_path / "b")
    res = backup.import_data(dst, out, apply=True, rebuild="none", say=quiet)
    expected = {rel: v for rel, v in tree(src.data_dir).items() if rel not in STRAYS and rel != "settings.json"}
    assert tree(dst.data_dir) == expected
    assert res.applied and res.written == len(expected)  # settings.json and the strays were not written
    assert res.after == backup.counts(src.data_dir)
    assert not [p for p in dst.data_dir.rglob("*.tmp")]


def test_a_data_folder_checkout_and_mirror_import_the_same_way(tmp_path):
    src = world(tmp_path)
    repo = tmp_path / "repo"
    shutil.copytree(src.data_dir, repo / "data")
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("a checkout", encoding="utf-8")
    mirror = tmp_path / "mirror"
    for rel, path in data_mirror.curated_files(src.data_dir):
        (mirror / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, mirror / rel)
    (mirror / ".git").mkdir()
    (mirror / "README").write_text("mirror", encoding="utf-8")

    folder = backup.inspect(src.data_dir)
    assert (folder.kind, folder.scope, folder.root) == ("data", "full", src.data_dir)
    checkout = backup.inspect(repo)
    assert (checkout.kind, checkout.scope, checkout.root) == ("checkout", "full", repo / "data")
    clone = backup.inspect(mirror)
    assert (clone.kind, clone.scope) == ("mirror", "curated")
    assert clone.entries and all(data_mirror.is_curated(e.path) for e in clone.entries)
    assert ("README", "not hand-made data") in clone.skipped
    zipped = backup.inspect(backup.export_data(src, tmp_path / "z.zip", say=quiet))
    assert (zipped.kind, zipped.scope) == ("backup", "full") and zipped.root is None

    full = {e.path for e in folder.entries}
    assert {e.path for e in checkout.entries} == full == {e.path for e in zipped.entries}
    for n, source in enumerate((src.data_dir, repo, tmp_path / "z.zip")):
        dst = make_cfg(tmp_path / f"d{n}")
        backup.import_data(dst, source, apply=True, rebuild="none", say=quiet)
        assert tree(dst.data_dir) == {r: v for r, v in tree(src.data_dir).items()
                                      if r not in STRAYS and r != "settings.json"}
    curated = make_cfg(tmp_path / "dm")
    backup.import_data(curated, mirror, apply=True, rebuild="none", say=quiet)
    assert set(tree(curated.data_dir)) == {r for r, _ in data_mirror.curated_files(src.data_dir)} - set(STRAYS) - {"settings.json"}


@pytest.mark.parametrize("make", ["missing", "empty folder", "plain file", "folder without data"])
def test_a_source_that_is_not_a_hondana_folder_is_refused(tmp_path, make):
    if make == "missing":
        source = tmp_path / "nope"
    elif make == "empty folder":
        source = tmp_path / "empty"
        source.mkdir()
    elif make == "plain file":
        source = tmp_path / "notes.txt"
        source.write_text("hello", encoding="utf-8")
    else:
        source = tmp_path / "other"
        put(source, "src/main.py", "print(1)")
    with pytest.raises(backup.BackupError):
        backup.inspect(source)
    assert not make_cfg(tmp_path / "x").data_dir.exists()


# ── nothing unsafe lands ─────────────────────────────────────────────────────

def test_paths_that_never_travel_are_not_imported_even_from_a_hand_made_source(tmp_path):
    src = world(tmp_path)
    folder = backup.inspect(src.data_dir)
    assert not [e for e in folder.entries if backup.never(e.path)]
    assert ("auth.json", "never copied") in folder.skipped
    files = {"profiles.json": b'{"profiles": [{"slug": "ann"}]}', "auth.json": b'{"hash": "x"}', "secret.key": b"k",
             "logs/server.log": b"log", "search.sqlite3": b"db", "users/ann/known_stats.json": b"{}",
             "users/ann/known_words.json": b'{"words": ["a"]}'}
    z = hand_zip(tmp_path / "h.zip", files)
    dst = make_cfg(tmp_path / "b")
    backup.import_data(dst, z, apply=True, rebuild="none", say=quiet)
    assert set(tree(dst.data_dir)) == {"profiles.json", "users/ann/known_words.json"}
    assert not (dst.data_dir / "auth.json").exists() and not (dst.data_dir / "logs").exists()


def test_a_user_folder_the_registry_does_not_list_is_left_out_by_name(tmp_path):
    src = world(tmp_path)
    insp = backup.inspect(src.data_dir)
    assert not [e for e in insp.entries if e.path.startswith(("users/old/", "users/spare/"))]
    assert any("users/old" in w and "users/spare" in w for w in insp.warnings)
    dst = make_cfg(tmp_path / "b")
    backup.import_data(dst, src.data_dir, apply=True, rebuild="none", say=quiet)
    assert sorted(p.name for p in (dst.data_dir / "users").iterdir()) == ["ann", "ben"]


def test_a_source_with_no_registry_keeps_every_user_folder(tmp_path):
    d = tmp_path / "src"
    put(d, "epub_library.json", {})
    put(d, "users/a/known_words.json", {"words": ["x"]})
    assert [e.path for e in backup.inspect(d).entries if e.path.startswith("users/")] == ["users/a/known_words.json"]


BAD = {
    "a ..": dict(files={"../evil.json": b"{}"}),
    "an absolute path": dict(files={"/etc/evil.json": b"{}"}),
    "a drive path": dict(files={"C:/evil.json": b"{}"}),
    "a backslash": dict(files={"users\\ann\\known_words.json": b"{}"}),
    "a dot component": dict(files={"covers/./b1.jpg": b"{}"}),
    "an empty component": dict(files={"covers//b1.jpg": b"{}"}),
    "a link": dict(files={"covers/b1.jpg": b"{}"}, links={"covers/b1.jpg"}),
    "a damaged file": dict(files={"covers/b1.jpg": b"original"}, actual={"covers/b1.jpg": b"tampered"}),
    "a file the manifest does not list": dict(files={"a.json": b"{}"}, extra={"data/surprise.json": b"{}"}),
    "a file the manifest lists but the zip lacks": dict(files={"a.json": b"{}", "b.json": b"{}"}, skip={"b.json"}),
    "a size that disagrees": dict(files={"a.json": b"{}"}, manifest={"files": [
        {"path": "a.json", "size": 99, "mtime_ns": 1, "sha256": sha(b"{}")}]}),
    "a malformed entry": dict(files={"a.json": b"{}"}, manifest={"files": [{"path": "a.json", "size": "big"}]}),
    "a bad sha256": dict(files={"a.json": b"{}"}, manifest={"files": [
        {"path": "a.json", "size": 2, "mtime_ns": 1, "sha256": "nope"}]}),
    "another format": dict(files={"a.json": b"{}"}, manifest={"format": "something-else"}),
    "a newer schema": dict(files={"a.json": b"{}"}, manifest={"schema": 99, "app_version": "9.9"}),
    "an unknown scope": dict(files={"a.json": b"{}"}, manifest={"scope": "everything"}),
    "no file list": dict(files={"a.json": b"{}"}, manifest={"files": "none"}),
}


@pytest.mark.parametrize("kind", sorted(BAD))
def test_an_unsafe_or_damaged_backup_is_refused_before_anything_is_written(tmp_path, kind):
    z = hand_zip(tmp_path / "bad.zip", **BAD[kind])
    dst = make_cfg(tmp_path / "b")
    with pytest.raises(backup.BackupError):
        backup.import_data(dst, z, apply=True, rebuild="none", say=quiet)
    assert not dst.data_dir.exists()


def test_the_same_file_twice_in_a_zip_is_refused(tmp_path):
    z = tmp_path / "dup.zip"
    data = b"{}"
    with zipfile.ZipFile(z, "w") as zf, pytest.warns(UserWarning, match="Duplicate"):
        zf.writestr("manifest.json", json.dumps({"format": backup.FORMAT, "schema": 1, "scope": "full", "files": [
            {"path": "a.json", "size": 2, "mtime_ns": 1, "sha256": sha(data)}]}))
        zf.writestr("data/a.json", data)
        zf.writestr("data/a.json", data)
    with pytest.raises(backup.BackupError, match="twice"):
        backup.inspect(z)


def test_a_zip_without_a_manifest_or_with_a_broken_one_is_refused(tmp_path):
    z = tmp_path / "plain.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("data/a.json", "{}")
    with pytest.raises(backup.BackupError, match="no manifest.json"):
        backup.inspect(z)
    z2 = tmp_path / "broken.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("manifest.json", "{not json")
    with pytest.raises(backup.BackupError, match="manifest can't be read"):
        backup.inspect(z2)


def test_a_zip_that_runs_past_its_declared_size_is_refused_as_it_is_written(tmp_path, monkeypatch):
    """The manifest and the zip agree (so inspect passes), then the bytes turn out to be longer."""
    z = hand_zip(tmp_path / "ok.zip", {"a.json": b'{"a": 1}'})
    monkeypatch.setattr(backup, "_verify", lambda zf, entries: [])
    real = backup._inspect_zip

    def inflated(path, verify):
        insp = real(path, verify)
        insp.entries[0].size = 3  # what the manifest "said"
        return insp

    monkeypatch.setattr(backup, "_inspect_zip", inflated)
    dst = make_cfg(tmp_path / "b")
    with pytest.raises(backup.BackupError, match="bigger than the manifest says"):
        backup.import_data(dst, z, apply=True, rebuild="none", say=quiet)
    assert not list(dst.data_dir.rglob("a.json")) and not list(dst.data_dir.rglob("*.tmp"))


# ── what happens to the folder it lands in ───────────────────────────────────

def test_a_dry_run_changes_nothing(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    dst = make_cfg(tmp_path / "b")
    lines: list[str] = []
    res = backup.import_data(dst, out, say=lines.append)
    assert not res.applied and res.written == 0 and not dst.data_dir.exists()
    text = "\n".join(lines)
    assert "dry run" in text and "--apply" in text and "2 books" in text and "ann (ja)" in text
    assert "Left out: 2 (not in profiles.json)" in text and "users/old, users/spare" in text


def test_a_folder_that_holds_work_of_its_own_is_refused_unless_replaced(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    mine = make_cfg(tmp_path / "mine")
    put(mine.data_dir, "profiles.json", {"profiles": [{"slug": "zed", "language": "ja"}]})
    put(mine.data_dir, "users/zed/known_words.json", {"words": ["私"]})
    before = tree(mine.data_dir)
    for apply in (False, True):
        with pytest.raises(backup.BackupError, match="work of its own"):
            backup.import_data(mine, out, apply=apply, rebuild="none", say=quiet)
    assert tree(mine.data_dir) == before


def test_replace_saves_a_full_export_first_then_clears_all_but_this_machines_own(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    mine = make_cfg(tmp_path / "mine")
    put(mine.data_dir, "profiles.json", {"profiles": [{"slug": "zed", "language": "ja"}]})
    put(mine.data_dir, "users/zed/known_words.json", {"words": ["私"]})
    put(mine.data_dir, "covers/zed.jpg", b"jpg")
    put(mine.data_dir, "auth.json", '{"password": "scrypt"}')
    put(mine.data_dir, "secret.key", "key")
    put(mine.data_dir, "settings.json", {"schema": 1, "values": {"port": 1234}})
    put(mine.data_dir, "logs/server.log", "log")
    put(mine.data_dir, "backups/earlier.zip", "keep me")

    res = backup.import_data(mine, out, apply=True, replace=True, rebuild="none", say=quiet)

    assert res.backup and res.backup.parent == mine.data_dir / "backups" and res.backup.name.startswith("before-import-")
    saved = read_manifest(res.backup)
    assert saved["scope"] == "full" and {"users/zed/known_words.json", "covers/zed.jpg"} <= {f["path"] for f in saved["files"]}
    assert "auth.json" not in {f["path"] for f in saved["files"]}
    assert not (mine.data_dir / "users" / "zed").exists() and not (mine.data_dir / "covers" / "zed.jpg").exists()
    assert (mine.data_dir / "auth.json").read_text() == '{"password": "scrypt"}'
    assert (mine.data_dir / "secret.key").read_text() == "key"
    assert load_json(mine.data_dir / "settings.json")["values"] == {"port": 1234}  # this machine's own
    assert (mine.data_dir / "logs" / "server.log").exists() and (mine.data_dir / "backups" / "earlier.zip").exists()
    assert tree(mine.data_dir)["users/ann/known_words.json"] == tree(src.data_dir)["users/ann/known_words.json"]


def test_a_backup_made_by_replace_can_be_imported_back(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    mine = make_cfg(tmp_path / "mine")
    put(mine.data_dir, "profiles.json", {"profiles": [{"slug": "zed", "language": "ja"}]})
    put(mine.data_dir, "users/zed/known_words.json", {"words": ["私", "君"]})
    res = backup.import_data(mine, out, apply=True, replace=True, rebuild="none", say=quiet)
    again = backup.import_data(mine, res.backup, apply=True, replace=True, rebuild="none", say=quiet)  # undo
    assert load_json(mine.data_dir / "users" / "zed" / "known_words.json") == {"words": ["私", "君"]}
    assert again.backup and not (mine.data_dir / "users" / "ann").exists()


def test_a_backup_inside_the_data_folder_cannot_be_replaced_out_from_under_itself(tmp_path):
    src = world(tmp_path)
    inside = backup.export_data(src, src.data_dir / "mine.zip", say=quiet)
    with pytest.raises(backup.BackupError, match="inside the data folder"):
        backup.import_data(src, inside, apply=True, replace=True, rebuild="none", say=quiet)
    kept = backup.export_data(src, src.data_dir / "backups" / "mine.zip", say=quiet)
    backup.import_data(src, kept, apply=True, replace=True, rebuild="none", say=quiet)  # backups/ is kept
    assert kept.exists()


def test_a_folder_cannot_be_imported_into_itself_or_into_what_holds_it(tmp_path):
    src = world(tmp_path)
    with pytest.raises(backup.BackupError, match="overlap"):
        backup.import_data(src, src.data_dir, apply=True, rebuild="none", say=quiet)
    inner = make_cfg(src.data_dir / "nested")
    with pytest.raises(backup.BackupError, match="overlap"):
        backup.import_data(inner, src.data_dir, apply=True, replace=True, rebuild="none", say=quiet)
    assert (src.data_dir / "users" / "ann" / "known_words.json").exists()


def test_a_full_disk_is_refused_up_front(tmp_path, monkeypatch):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: SimpleNamespace(total=10, used=10, free=10))
    dst = make_cfg(tmp_path / "b")
    with pytest.raises(backup.BackupError, match="not enough disk space"):
        backup.import_data(dst, out, apply=True, rebuild="none", say=quiet)
    assert not dst.data_dir.exists()


def test_settings_json_comes_only_with_the_flag_and_is_otherwise_printed(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    plain = make_cfg(tmp_path / "plain")
    lines: list[str] = []
    res = backup.import_data(plain, out, apply=True, rebuild="none", say=lines.append)
    assert not (plain.data_dir / "settings.json").exists()
    assert res.settings == {"epub_dir": "/old/machine/books", "port": 9000}
    assert any("epub_dir = '/old/machine/books'" in ln for ln in lines)
    with_it = make_cfg(tmp_path / "with")
    backup.import_data(with_it, out, apply=True, settings=True, rebuild="none", say=quiet)
    assert load_json(with_it.data_dir / "settings.json") == load_json(src.data_dir / "settings.json")
    from_folder = make_cfg(tmp_path / "ff")
    assert backup.import_data(from_folder, src.data_dir, say=quiet).settings["port"] == 9000


def test_what_counts_as_the_folders_own_work(tmp_path):
    cfg = make_cfg(tmp_path)
    assert backup.user_data(cfg.data_dir) == []  # no folder at all
    put(cfg.data_dir, "profiles.json", {"active": "reader", "profiles": [{"slug": "reader", "language": "ja"}]})
    put(cfg.data_dir, "users/reader/known_words.json", {"words": [], "marked": []})
    put(cfg.data_dir, "users/reader/read_books.json", {"books": {}})
    put(cfg.data_dir, "audiobook_hidden.json", {"ab_ids": []})
    put(cfg.data_dir, "series_overrides.json", {})
    put(cfg.data_dir, "epub_library.json", {"b1": {}})  # a rebuildable index is not work
    assert backup.user_data(cfg.data_dir) == []  # what a first start leaves behind
    put(cfg.data_dir, "users/reader/known_words.json", {"words": ["猫"], "marked": ["猫"]})
    assert backup.user_data(cfg.data_dir) == ["reader: 1 known words", "reader: 1 words marked known"]
    put(cfg.data_dir, "users/reader/known_words.json", {"words": []})
    put(cfg.data_dir, "series_overrides.json", {"b1": {"series_name": "S"}})
    put(cfg.data_dir, "covers/custom/b1.png", b"png")
    put(cfg.data_dir, "alignments/b1.srt", "x")
    assert backup.user_data(cfg.data_dir) == ["hand-made data in series_overrides.json", "hand-set covers",
                                              "audio sync files"]


def test_a_curated_import_says_to_run_all_and_rebuilds_nothing(tmp_path, monkeypatch):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "c.zip", "curated", say=quiet)
    ran: list[str] = []
    for stage in ("audio-scan", "build"):
        monkeypatch.setitem(pipeline.STAGE_BY_ID, stage, SimpleNamespace(run=lambda cfg, s=stage: ran.append(s)))
    monkeypatch.setattr(known, "recompute_all", lambda cfg: ran.append("recompute"))
    lines: list[str] = []
    dst = make_cfg(tmp_path / "b")
    backup.import_data(dst, out, apply=True, say=lines.append)
    assert ran == [] and any("hondana all" in ln for ln in lines)
    assert (dst.data_dir / "users" / "ann" / "known_words.json").exists() and not (dst.data_dir / "epub_library.json").exists()


@pytest.mark.parametrize("mode, expected", [("index", ["audio-scan", "build", "recompute"]),
                                            ("all", ["all", "recompute"]), ("none", [])])
def test_the_rebuild_modes(tmp_path, monkeypatch, mode, expected):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    ran: list[str] = []
    for stage in ("audio-scan", "build"):
        monkeypatch.setitem(pipeline.STAGE_BY_ID, stage, SimpleNamespace(run=lambda cfg, s=stage: ran.append(s)))
    monkeypatch.setattr(pipeline, "run_all", lambda cfg, refresh=False: ran.append("all"))
    monkeypatch.setattr(known, "recompute_all", lambda cfg: ran.append("recompute"))
    dst = make_cfg(tmp_path / "b")
    backup.import_data(dst, out, apply=True, rebuild=mode, say=quiet)
    assert ran == expected


def test_a_bad_rebuild_mode_is_refused(tmp_path):
    with pytest.raises(backup.BackupError, match="--rebuild"):
        backup.import_data(make_cfg(tmp_path), tmp_path, rebuild="sometimes", say=quiet)


def test_an_old_audiobook_index_gets_its_fids_and_no_id_changes(tmp_path):
    """The import's rebuild runs audio-scan, which upgrades a pre-fid audiobook_files.json
    (audiobook_match would KeyError on it before) without moving any id."""
    audio = tmp_path / "audiobooks"
    af.m4b(audio / "Keyed [B0KEYED001].m4b", seconds=5)
    af.m4b(audio / "Loose" / "loose.m4b", seconds=5, pad=500)
    first = make_cfg(tmp_path / "a", audiobook_dirs=[audio])
    ids = set(scanner.scan(first))
    old = load_json(first.audiobook_files_index)
    assert len(ids) == 2 and all("fid" in e for e in old.values())
    for e in old.values():
        del e["fid"]  # how an older version wrote it
    save_json(first.audiobook_files_index, old)
    put(first.data_dir, "profiles.json", {"active": "ann", "profiles": [{"slug": "ann", "name": "Ann", "language": "ja"}]})
    out = backup.export_data(first, tmp_path / "out.zip", say=quiet)

    second = make_cfg(tmp_path / "b", audiobook_dirs=[audio])
    backup.import_data(second, out, apply=True, say=quiet)  # rebuild=index: audio-scan, build, known words

    new = load_json(second.audiobook_files_index)
    assert set(new) == ids and all(e.get("fid") for e in new.values())
    assert load_json(second.audiobook_index)  # the joined index was rebuilt too


# ── the server guard ─────────────────────────────────────────────────────────

def test_an_import_refuses_while_a_server_holds_the_lock(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    dst = make_cfg(tmp_path / "b")
    server = desktop.InstanceLock(dst.data_dir / backup.SERVER_LOCK)
    assert server.acquire()
    try:
        with pytest.raises(backup.BackupError, match="stop the server first"):
            backup.import_data(dst, out, apply=True, rebuild="none", say=quiet)
        assert not (dst.data_dir / "users").exists()
        backup.import_data(dst, out, say=quiet)  # looking is fine
    finally:
        server.release()
    backup.import_data(dst, out, apply=True, rebuild="none", say=quiet)
    assert (dst.data_dir / "users" / "ann").exists()
    assert desktop.InstanceLock(dst.data_dir / backup.SERVER_LOCK).acquire()  # released again


def test_an_import_refuses_while_the_desktop_app_runs(tmp_path):
    src = world(tmp_path)
    out = backup.export_data(src, tmp_path / "out.zip", say=quiet)
    dst = make_cfg(tmp_path / "b")
    app = desktop.InstanceLock(dst.data_dir / desktop.LOCK_NAME)
    assert app.acquire()
    try:
        with pytest.raises(backup.BackupError, match="app is running"):
            backup.import_data(dst, out, apply=True, rebuild="none", say=quiet)
    finally:
        app.release()


def test_two_imports_cannot_run_at_once(tmp_path):
    data = tmp_path / "data"
    with backup.hold_server_lock(data):
        with pytest.raises(backup.BackupError, match="stop the server first"):
            with backup.hold_server_lock(data):
                pass
    with backup.hold_server_lock(data):
        pass


def test_serve_holds_the_lock_for_as_long_as_it_runs_and_a_second_one_refuses(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'epub_dir = {toml_str(tmp_path / "books")}\ndata_dir = {toml_str(tmp_path / "data")}\n',
                        encoding="utf-8")
    import uvicorn
    held: list[bool] = []

    def fake_run(*args, **kw):
        held.append(not desktop.InstanceLock(tmp_path / "data" / backup.SERVER_LOCK).acquire())
        assert cli.main(["--config", str(cfg_file), "serve"]) == 1  # the same folder again
        return None

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(cli.stdio_mod, "redirect_to_log", lambda path: None)
    cli.main(["--config", str(cfg_file), "serve"])
    assert held == [True]
    assert "already running" in capsys.readouterr().err
    assert desktop.InstanceLock(tmp_path / "data" / backup.SERVER_LOCK).acquire()  # let go at exit


# ── the commands ─────────────────────────────────────────────────────────────

def _config(tmp_path: Path, name: str) -> Path:
    cfg_file = tmp_path / f"{name}.toml"
    cfg_file.write_text(f'epub_dir = {toml_str(tmp_path / name / "books")}\n'
                        f'data_dir = {toml_str(tmp_path / name / "data")}\n', encoding="utf-8")
    return cfg_file


def test_export_and_import_through_the_command_line(tmp_path, capsys):
    world(tmp_path, "src")
    out = tmp_path / "cli.zip"
    assert cli.main(["--config", str(_config(tmp_path, "src")), "export", "--out", str(out)]) in (0, None)
    assert out.exists() and read_manifest(out)["scope"] == "full"
    assert "Wrote" in capsys.readouterr().out
    curated = tmp_path / "cli-curated.zip"
    assert cli.main(["--config", str(_config(tmp_path, "src")), "export", "--curated", "--out", str(curated)]) in (0, None)
    assert read_manifest(curated)["scope"] == "curated"

    dst_cfg = _config(tmp_path, "dst")
    assert cli.main(["--config", str(dst_cfg), "import", str(out)]) in (0, None)
    assert "dry run" in capsys.readouterr().out
    assert not (tmp_path / "dst" / "data").exists()  # and no default profile was seeded either
    assert cli.main(["--config", str(dst_cfg), "import", str(out), "--apply", "--rebuild", "none"]) in (0, None)
    assert (tmp_path / "dst" / "data" / "users" / "ann" / "known_words.json").exists()
    assert not (tmp_path / "dst" / "data" / "users" / "reader").exists()


def test_the_commands_report_a_refusal_as_an_error_and_exit_one(tmp_path, capsys):
    cfg_file = _config(tmp_path, "dst")
    assert cli.main(["--config", str(cfg_file), "import", str(tmp_path / "missing.zip")]) == 1
    assert "error:" in capsys.readouterr().err
    assert cli.main(["--config", str(cfg_file), "export", "--out", str(tmp_path / "x.zip")]) == 1
    assert "no data folder" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg_file), "import", str(tmp_path), "--rebuild", "sometimes"])


def test_a_backup_named_by_the_second_never_replaces_another(tmp_path, monkeypatch):
    import datetime as dt

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 26, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", Frozen)
    src = world(tmp_path)
    names = [backup.export_data(src, None, say=quiet).name for _ in range(3)]
    assert names == ["hondana-20261026-120000.zip", "hondana-20261026-120000-2.zip", "hondana-20261026-120000-3.zip"]
    mine = make_cfg(tmp_path / "mine")
    put(mine.data_dir, "profiles.json", {"profiles": [{"slug": "zed", "name": "Z", "language": "ja"}]})
    put(mine.data_dir, "users/zed/known_words.json", {"words": ["私"]})
    one = backup.import_data(mine, src.data_dir / "backups" / names[0], apply=True, replace=True, rebuild="none", say=quiet)
    two = backup.import_data(mine, one.backup, apply=True, replace=True, rebuild="none", say=quiet)
    assert one.backup.name == "before-import-20261026-120000.zip" and two.backup.name == "before-import-20261026-120000-2.zip"
    assert one.backup.exists() and two.backup.exists()
