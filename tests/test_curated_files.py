"""Hand-curated files: every list that names them stays complete, and the data
mirror backs them up.

Three modules each keep their own list of the curated files: data_mirror
(backup), purge (explicit delete) and rekey (rename/move). A new override file
has to be added to all three, and a missed entry once — four
overrides went unmirrored. The guard test below fails the moment a new
`*_overrides` path on Config is missing from any of them.
"""
import shutil
import subprocess

import pytest

from conftest import make_cfg
from epublib import config, data_mirror, purge, rekey
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
    per_book = [a for a in attrs if a != "pairing_overrides"]
    for attr in per_book:
        assert attr in purge._CURATED_INDEXES, f"{attr} survives a delete (purge._CURATED_INDEXES)"
        assert attr in rekey._INDEXES, f"{attr} is lost on a rename (rekey._INDEXES)"


# ── data_mirror.push() into a throwaway remote ───────────────────────────────

def _git(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout


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
    cfg.alignments_dir.mkdir()
    (cfg.alignments_dir / "b1.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
    (cfg.alignments_dir / "b1.json").write_text("{}")  # derived map
    user = cfg.users_dir / "reader2"
    (user / "history").mkdir(parents=True)
    (user / "known_words.json").write_text("{}")
    (user / "read_books.json").write_text("{}")
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
        "alignments/b1.srt",
        "users/reader2/known_words.json",
        "users/reader2/read_books.json",
        "users/reader2/future_file.json",
    }


def test_push_with_nothing_changed_makes_no_commit(tmp_path, mirror, capsys):
    remote, clone = mirror
    cfg = make_cfg(tmp_path, data_mirror_publish=True, data_mirror_path=clone)
    _seed_curated(cfg)
    data_mirror.push(cfg)
    head = _git(remote, "rev-parse", "HEAD")

    data_mirror.push(cfg)
    assert _git(remote, "rev-parse", "HEAD") == head
    assert "no changes" in capsys.readouterr().out

    (cfg.data_dir / "genre_overrides.json").write_text('{"b1": "ホラー"}')
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
