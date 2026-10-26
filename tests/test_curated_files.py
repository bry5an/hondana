"""Hand-curated files: every list that names them stays complete, and the data
mirror backs them up.

Three modules each keep their own list of the curated files: data_mirror
(backup), purge (explicit delete) and rekey (rename/move). A new override file
has to be added to all three, and a missed entry once — four
overrides went unmirrored. The guard test below fails the moment a new
`*_overrides` path on Config is missing from any of them. Audiobook-keyed
stores follow an ab_id instead (audiobook_rekey), and are checked against that.
"""
import json
import shutil
import subprocess
import zipfile

import pytest

from conftest import make_cfg
from epublib import audiobook_rekey, backup, config, data_mirror, purge, rekey
from epublib.config import Config

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _override_attrs() -> list[str]:
    return sorted(n for n, v in vars(Config).items()
                  if isinstance(v, property) and n.endswith("_overrides"))


def test_every_override_file_is_in_every_curated_list(cfg):
    attrs = _override_attrs()
    assert len(attrs) >= 8  # the scan itself works

    mirrored = set(data_mirror.ROOT_FILES)
    for attr in attrs:
        assert getattr(cfg, attr).name in mirrored, f"{attr} isn't backed up (data_mirror.ROOT_FILES)"
    # Curated files that aren't overrides: the prune list and the profile registry.
    assert cfg.audiobook_hidden.name in mirrored
    assert cfg.profiles_index.name in mirrored

    # pairing_overrides is keyed by the pair's stable key, not a book_id, so a
    # delete or rename leaves it alone by design (purge.py / rekey.py docstrings).
    per_book = [a for a in attrs if a != "pairing_overrides" and a not in audiobook_rekey.AB_INDEXES]
    for attr in per_book:
        assert attr in purge._CURATED_INDEXES, f"{attr} survives a delete (purge._CURATED_INDEXES)"
        assert attr in rekey._INDEXES, f"{attr} is lost on a rename (rekey._INDEXES)"


def test_every_audiobook_store_follows_a_new_ab_id(cfg):
    """What an audiobook's id keys, by hand or by the phone, moves when the id
    changes (audiobook_rekey) and is backed up."""
    ab_keyed = ["audiobook_metadata_overrides", "audiobook_natively"]
    assert sorted(audiobook_rekey.AB_INDEXES) == sorted(ab_keyed)
    mirrored = set(data_mirror.ROOT_FILES)
    for attr in ("audiobook_metadata_overrides", "audiobook_assignments", "audiobook_aliases",
                 "audiobook_hidden", "audiobook_link_overrides"):
        assert getattr(cfg, attr).name in mirrored, f"{attr} isn't backed up"
    assert cfg.audiobook_natively.name in mirrored


# ── data_mirror.push() into a throwaway remote ───────────────────────────────

def _git(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


@pytest.fixture
def mirror(tmp_path):
    """A bare 'remote' plus a clone with one commit and an upstream, which is
    what a backup checkout looks like (docs/running-a-server.md)."""
    remote = tmp_path / "remote.git"
    clone = tmp_path / "mirror"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True,
                   capture_output=True)
    for key, value in [("user.name", "Test"), ("user.email", "test@example.com"),
                       ("commit.gpgsign", "false")]:
        _git(clone, "config", key, value)
    (clone / "README").write_text("mirror\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-q", "-m", "init")
    _git(clone, "push", "-q", "-u", "origin", "HEAD")
    return remote, clone


def _seed_curated(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    for name in data_mirror.ROOT_FILES:
        (cfg.data_dir / name).write_text(f'{{"file": "{name}"}}')
    cfg.custom_covers_dir.mkdir(parents=True)
    (cfg.custom_covers_dir / "b1.png").write_bytes(b"\x89PNG fake")
    (cfg.covers_dir / "b1.jpg").write_bytes(b"extracted, rebuildable")
    (cfg.audiobook_custom_covers_dir / "thumbs").mkdir(parents=True)
    (cfg.audiobook_custom_covers_dir / "B0AAAAAAAA.png").write_bytes(b"\x89PNG hand-set")
    (cfg.audiobook_custom_covers_dir / "thumbs" / "B0AAAAAAAA.webp").write_bytes(b"derived")
    (cfg.audiobook_covers_dir / "f0123456789abcd.jpg").write_bytes(b"from tags, rebuildable")
    cfg.alignments_dir.mkdir()
    (cfg.alignments_dir / "b1.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
    (cfg.alignments_dir / "b1.json").write_text("{}")  # derived map
    (cfg.alignments_dir / "b1.transcript.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\ny\n")
    (cfg.alignments_dir / "b1.anchors.jsonl").write_text('{"ab_id": "B0AAAAAAAA", "doc": 0, "sentence": 1}\n')
    (cfg.alignments_dir / "b1.silence.json").write_text("{}")  # ffmpeg's cache, rebuildable
    user = cfg.users_dir / "reader2"
    (user / "history").mkdir(parents=True)
    (user / "known_words.json").write_text("{}")
    (user / "read_books.json").write_text("{}")
    (user / "bookmarks.json").write_text("{}")
    (user / "future_file.json").write_text("{}")  # a per-user file nobody listed
    (user / "known_stats.json").write_text("{}")  # derived
    (user / ".DS_Store").write_text("")
    (user / "history" / "20261001-replace.json").write_text("{}")  # snapshots
    (cfg.data_dir / "epub_library.json").write_text("{}")  # rebuildable index


def test_push_mirrors_the_curated_files_and_nothing_derived(tmp_path, mirror):
    remote, clone = mirror
    cfg = make_cfg(tmp_path, data_mirror_publish=True, data_mirror_path=clone)
    _seed_curated(cfg)

    data_mirror.push(cfg)

    files = set(_git(remote, "ls-tree", "-r", "--name-only", "HEAD").split())
    assert files == {
        "README",
        *data_mirror.ROOT_FILES,
        "covers/custom/b1.png",
        "covers/audiobooks/custom/B0AAAAAAAA.png",
        "alignments/b1.srt",
        "alignments/b1.transcript.srt",
        "alignments/b1.anchors.jsonl",
        "users/reader2/known_words.json",
        "users/reader2/read_books.json",
        "users/reader2/bookmarks.json",
        "users/reader2/future_file.json",
    }


def test_a_curated_export_holds_exactly_what_push_mirrors(tmp_path, mirror):
    """`hondana export --curated` and the git mirror are one list (data_mirror.curated_files)."""
    remote, clone = mirror
    cfg = make_cfg(tmp_path, data_mirror_publish=True, data_mirror_path=clone)
    _seed_curated(cfg)
    data_mirror.push(cfg)
    mirrored = set(_git(remote, "ls-tree", "-r", "--name-only", "HEAD").split()) - {"README"}

    out = backup.export_data(cfg, tmp_path / "curated.zip", "curated", say=lambda line: None)

    with zipfile.ZipFile(out) as zf:
        assert json.loads(zf.read("manifest.json"))["scope"] == "curated"
        exported = {n[len("data/"):] for n in zf.namelist() if n != "manifest.json"}
    assert exported == mirrored and exported


def test_push_with_nothing_changed_makes_no_commit(tmp_path, mirror, capsys):
    remote, clone = mirror
    cfg = make_cfg(tmp_path, data_mirror_publish=True, data_mirror_path=clone)
    _seed_curated(cfg)
    data_mirror.push(cfg)
    head = _git(remote, "rev-parse", "HEAD")

    data_mirror.push(cfg)
    assert _git(remote, "rev-parse", "HEAD") == head
    assert "no changes" in capsys.readouterr().out

    (cfg.data_dir / "genre_overrides.json").write_text('{"b1": "ホラー"}', encoding="utf-8")
    data_mirror.push(cfg)
    assert _git(remote, "rev-parse", "HEAD") != head
    assert _git(remote, "show", "HEAD:genre_overrides.json") == '{"b1": "ホラー"}'


def test_push_is_a_no_op_on_dev_and_refuses_a_path_that_isnt_a_clone(tmp_path):
    cfg = make_cfg(tmp_path)  # data_mirror_publish=False, the default
    data_mirror.push(cfg)
    assert not cfg.data_mirror_path.exists()

    cfg = make_cfg(tmp_path, data_mirror_publish=True)
    with pytest.raises(SystemExit, match="not a git clone"):
        data_mirror.push(cfg)


def test_the_untracked_audiobook_data_files_are_mirrored_from_data_dir(tmp_path):
    """data.json/natively_levels.json left git, so the mirror is their only backup —
    which only works while the configured paths stay inside data_dir."""
    cfg = config.load(_write_config(tmp_path))
    for path in (cfg.audiobook_data, cfg.audiobook_natively):
        assert path.parent == cfg.data_dir
        assert path.name in data_mirror.ROOT_FILES


def _write_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('epub_dir = "./books"\ndata_dir = "./data"\n')
    return path
