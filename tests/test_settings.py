"""settings.py: which file is read, where each value comes from, and that a
fully specified config loads exactly as it did before the registry."""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from epublib import cli, settings
from epublib.config import Config


def _legacy_load(cfg_path: Path, root: Path) -> Config:
    """The loader as it was before settings.py (paths relative to `root`)."""
    def _resolve(base: Path, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    nat, pair, xref = raw.get("natively", {}), raw.get("pairing", {}), raw.get("xref", {})
    mirror = raw.get("data_mirror", {})
    return Config(
        epub_dir=_resolve(root, raw.get("epub_dir", "./books")),
        data_dir=_resolve(root, raw.get("data_dir", "./data")),
        audiobook_data=_resolve(root, raw.get("audiobook_data", "./data/audiobook_catalog_base.json")),
        audiobook_dirs=[_resolve(root, d) for d in raw.get("audiobook_dirs", [])],
        audiobook_natively=_resolve(root, raw.get("audiobook_natively", "./data/natively_levels.json")),
        host=raw.get("host", "0.0.0.0"),
        port=int(raw.get("port", 8000)),
        natively_delay=float(nat.get("delay", 0.4)),
        natively_min_score=float(nat.get("min_score", 0.55)),
        pairing_min_score=float(pair.get("min_score", 0.5)),
        xref_min_score=float(xref.get("min_score", 0.62)),
        data_mirror_publish=bool(mirror.get("publish", False)),
        data_mirror_path=_resolve(root, mirror["path"]) if mirror.get("path") else None,
        google_books_api_key=os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip(),
        dicts_dir=_resolve(root, raw["dicts_dir"]) if raw.get("dicts_dir") else None,
        subplz_cmd=str(raw.get("alignment", {}).get("subplz_cmd", "")).strip(),
        discovery_enabled=bool(raw.get("discovery", {}).get("enabled", True)),
        discovery_advertise_port=(int(raw["discovery"]["advertise_port"])
                                  if raw.get("discovery", {}).get("advertise_port") else None),
        discovery_advertise_host=str(raw.get("discovery", {}).get("advertise_host", "")).strip(),
    )


FULL = """
epub_dir = "./books"
data_dir = "./data"
audiobook_data = "./data/catalog.json"
audiobook_natively = "./data/levels.json"
audiobook_dirs = ["./audio", "/abs/audio2"]
dicts_dir = "./dicts"
host = "127.0.0.1"
port = 8123
[natively]
delay = 1.5
min_score = 0.7
[pairing]
min_score = 0.4
[xref]
min_score = 0.8
[alignment]
subplz_cmd = " subplz sync --audio {audio} "
[discovery]
enabled = false
advertise_port = 80
advertise_host = " books.local "
[data_mirror]
publish = true
path = "../backup"
"""


@pytest.mark.parametrize("body", [
    FULL,
    FULL.replace('dicts_dir = "./dicts"\n', "").replace("advertise_port = 80\n", ""),
    'epub_dir = "/srv/books"\ndata_dir = "/srv/data"\naudiobook_dirs = []\n'
    'audiobook_data = "/srv/data/audiobook_catalog_base.json"\n'
    'audiobook_natively = "/srv/data/natively_levels.json"\n',
])
def test_a_full_config_loads_as_it_did_before_the_registry(tmp_path, body):
    f = tmp_path / "config.toml"
    f.write_text(body)
    assert settings.load(f) == _legacy_load(f, tmp_path)


def test_only_the_documented_defaults_changed(tmp_path, monkeypatch):
    """audiobook_dirs now defaults to a folder under home, and the catalog files
    follow data_dir rather than staying under the checkout."""
    monkeypatch.delenv("HONDANA_HOME")  # home is the config file's folder
    f = tmp_path / "config.toml"
    f.write_text('data_dir = "./elsewhere"\n')
    cfg = settings.load(f)
    assert cfg.audiobook_dirs == [tmp_path / "audiobooks"]
    assert cfg.audiobook_data == tmp_path / "elsewhere" / "audiobook_catalog_base.json"
    assert cfg.audiobook_natively == tmp_path / "elsewhere" / "natively_levels.json"


def test_no_config_file_at_all_starts_under_the_home_folder():
    home = Path(os.environ["HONDANA_HOME"])
    cfg = settings.load()
    assert cfg.config_path is None and cfg.home == home
    assert cfg.data_dir == home / "data" and cfg.epub_dir == home / "books"
    assert cfg.audiobook_dirs == [home / "audiobooks"]
    assert all(p.is_dir() for p in (cfg.data_dir, cfg.epub_dir, cfg.audiobook_dirs[0]))
    assert set(cfg.sources.values()) == {"default"}


def test_a_config_in_the_home_folder_is_found():
    home = Path(os.environ["HONDANA_HOME"])
    home.mkdir(parents=True)
    (home / "config.toml").write_text("port = 9000\n")
    cfg = settings.load()
    assert cfg.config_path == home / "config.toml" and cfg.port == 9000
    assert cfg.sources["port"] == "file" and cfg.sources["host"] == "default"


def test_a_checkout_config_wins_over_the_home_folder(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root).mkdir()
    (root / "config.toml").write_text("port = 7000\n")
    home = Path(os.environ["HONDANA_HOME"])
    home.mkdir(parents=True)
    (home / "config.toml").write_text("port = 9000\n")
    assert settings.load().port == 7000
    (root / "config.dev.toml").write_text("port = 7001\n")
    assert settings.load().port == 7001
    monkeypatch.setenv("EPUB_LIBRARY_ENV", "prod")
    assert settings.load().port == 7000  # no config.prod.toml: config.toml
    (root / "config.toml").unlink()
    with pytest.raises(SystemExit, match="config.prod.toml"):
        settings.load()


def test_configured_paths_are_relative_to_the_file_and_never_created(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    (d / "config.toml").write_text('epub_dir = "nas/books"\naudiobook_dirs = ["nas/audio"]\n')
    cfg = settings.load(d / "config.toml")
    assert cfg.epub_dir == d / "nas" / "books" and cfg.audiobook_dirs == [d / "nas" / "audio"]
    assert not (d / "nas").exists()  # an unmounted drive must stay visibly missing


def test_an_explicit_config_that_is_missing_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="Config not found"):
        settings.load(tmp_path / "nope.toml")
    monkeypatch.setenv("HONDANA_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(SystemExit, match="Config not found"):
        settings.load()


def test_a_bad_value_names_the_key(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('port = "eighty"\n')
    with pytest.raises(SystemExit, match="port"):
        settings.load(f)
    f.write_text("port = [\n")
    with pytest.raises(SystemExit, match="isn't valid TOML"):
        settings.load(f)


def test_environment_beats_the_overlay_which_beats_the_file(monkeypatch, tmp_path):
    monkeypatch.delenv("HONDANA_HOME")  # home is the config file's folder
    f = tmp_path / "config.toml"
    f.write_text('port = 8001\n[natively]\ndelay = 1.0\n[xref]\nmin_score = 0.7\n')
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(json.dumps(
        {"schema": 1, "values": {"port": 8002, "natively.delay": 2.0}}))
    monkeypatch.setenv("HONDANA_PORT", "8003")
    cfg = settings.load(f)
    assert (cfg.port, cfg.sources["port"]) == (8003, "env")
    assert (cfg.natively_delay, cfg.sources["natively_delay"]) == (2.0, "settings")
    assert (cfg.xref_min_score, cfg.sources["xref_min_score"]) == (0.7, "file")
    assert cfg.sources["host"] == "default"


def test_environment_values_are_parsed_by_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("HONDANA_AUDIOBOOK_DIRS", os.pathsep.join(["/a", "/b"]))
    monkeypatch.setenv("HONDANA_DISCOVERY_ENABLED", "no")
    monkeypatch.setenv("HONDANA_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", " k ")
    cfg = settings.load()
    assert cfg.audiobook_dirs == [Path("/a"), Path("/b")]
    assert cfg.discovery_enabled is False
    assert cfg.data_dir == tmp_path / "d" and cfg.dicts_dir == tmp_path / "d" / "dicts"
    assert cfg.google_books_api_key == "k" and cfg.sources["google_books_api_key"] == "env"
    monkeypatch.setenv("HONDANA_AUDIOBOOK_DIRS", '["/c"]')
    assert settings.load().audiobook_dirs == [Path("/c")]
    monkeypatch.setenv("HONDANA_DISCOVERY_ENABLED", "maybe")
    with pytest.raises(SystemExit, match="discovery.enabled"):
        settings.load()


def test_the_overlay_never_sets_file_only_keys_and_skips_bad_values(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HONDANA_HOME")  # home is the config file's folder
    f = tmp_path / "config.toml"
    f.write_text("")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(json.dumps({"schema": 1, "values": {
        "alignment.subplz_cmd": "rm -rf /", "data_mirror.publish": True,
        "port": "not a port", "no.such.key": 1, "epub_dir": str(tmp_path / "gui-books")}}))
    cfg = settings.load(f)
    assert cfg.subplz_cmd == "" and cfg.data_mirror_publish is False
    assert cfg.port == 8000 and cfg.epub_dir == tmp_path / "gui-books"
    err = capsys.readouterr().err
    assert "port" in err and "no.such.key" in err


def test_an_unreadable_overlay_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("HONDANA_HOME")  # home is the config file's folder
    f = tmp_path / "config.toml"
    f.write_text("")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text("{not json")
    assert settings.load(f).port == 8000


def test_dotenv_in_the_home_folder_is_read(monkeypatch):
    home = Path(os.environ["HONDANA_HOME"])
    home.mkdir(parents=True)
    (home / ".env").write_text('GOOGLE_BOOKS_API_KEY="from-dotenv"\n')
    assert settings.load().google_books_api_key == "from-dotenv"


def test_every_key_maps_to_a_config_field():
    fields = set(Config.__dataclass_fields__)
    assert {k.field for k in settings.KEYS} <= fields
    assert len({k.name for k in settings.KEYS}) == len(settings.KEYS)


def test_the_config_command_reports_sources(capsys, monkeypatch):
    monkeypatch.setenv("HONDANA_PORT", "8124")
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "secret-value")
    assert cli.main(["config", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    rows = {r["key"]: r for r in out["settings"]}
    assert rows["port"]["value"] == 8124 and rows["port"]["source"] == "env"
    assert rows["google_books_api_key"]["value"] == "(set)"  # never printed
    assert "secret-value" not in json.dumps(out)
    assert cli.main(["config"]) == 0
    assert "[env]" in capsys.readouterr().out
