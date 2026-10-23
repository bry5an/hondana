"""Every setting in one registry, and how its value is chosen.

A value comes from, lowest to highest:

1. the built-in default (most paths sit under the app's home folder),
2. the TOML config file, if there is one,
3. the GUI overlay, `<data_dir>/settings.json`, written by the Settings page,
4. a `HONDANA_*` environment variable (Docker, systemd).

`load()` returns the `Config` with `sources` saying where each value came from,
which `hondana config` and the Settings page show.

**Which config file.** The first that applies: an explicit path (`--config`,
`HONDANA_CONFIG` or `EPUB_LIBRARY_CONFIG`); `config.<EPUB_LIBRARY_ENV>.toml` or
`config.toml` in the checkout (ROOT), for checkouts set up that way; then
`<home>/config.toml`. None at all is fine: everything has a default.

**The home folder** holds the defaults: `HONDANA_HOME`, else the config file's own
folder, else the platform's per-user app-data folder. Relative paths in the file
resolve against the file's folder.
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Config

SCHEMA = 1
OVERLAY_NAME = "settings.json"
EXPLICIT_ENVS = ("HONDANA_CONFIG", "EPUB_LIBRARY_CONFIG")
ENV_ENV = "EPUB_LIBRARY_ENV"
DEFAULT_ENV = "dev"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class Key:
    """One setting. `name` is its TOML key (dotted for a table: `natively.delay`);
    `default` is a value or a function of the load context."""
    name: str
    field: str
    kind: str  # path | paths | optpath | str | secret | int | optint | float | bool
    default: object
    label: str
    group: str  # library | audiobooks | network | metadata | advanced | system
    help: str = ""
    restart: bool = False    # takes effect only after a restart
    file_only: bool = False  # never set from the GUI: it runs a command, or moves where data lives
    gui: bool = True         # listed on the Settings page at all
    env_aliases: tuple[str, ...] = ()

    @property
    def env(self) -> str:
        return "HONDANA_" + self.name.upper().replace(".", "_")


@dataclass
class _Ctx:
    home: Path
    data_dir: Path | None = None


KEYS: tuple[Key, ...] = (
    Key("epub_dir", "epub_dir", "path", lambda c: c.home / "books", "EPUB folder", "library",
        "The folder of .epub files, scanned recursively. Books added on Add Book are filed "
        "into its Japanese/ or English/ subfolder."),
    Key("data_dir", "data_dir", "path", lambda c: c.home / "data", "Data folder", "system",
        "Where every index, cover, profile and setting is written. Back it up.",
        restart=True, file_only=True),
    Key("audiobook_dirs", "audiobook_dirs", "paths", lambda c: [c.home / "audiobooks"],
        "Audiobook folders", "audiobooks",
        "Folders scanned for audiobooks: .m4b files, or a folder of .mp3s as one book. "
        "The first is where new audiobooks are filed."),
    Key("audiobook_data", "audiobook_data", "path",
        lambda c: c.data_dir / "audiobook_catalog_base.json", "Base audiobook catalog", "advanced",
        "Optional hand-curated catalog keyed by Audible ASIN.", file_only=True),
    Key("audiobook_natively", "audiobook_natively", "path",
        lambda c: c.data_dir / "natively_levels.json", "Audiobook Natively levels", "advanced",
        "Written by the enrich-audiobooks stage.", file_only=True, gui=False),
    Key("dicts_dir", "dicts_dir", "path", lambda c: c.data_dir / "dicts", "Dictionary folder",
        "system", "Your Yomitan dictionary zips, served to Hondana for iPhone.", file_only=True),
    Key("host", "host", "str", "0.0.0.0", "Listen address", "network",
        "0.0.0.0 lets other devices (your phone) connect; 127.0.0.1 only this computer.",
        restart=True),
    Key("port", "port", "int", 8000, "Port", "network", restart=True),
    Key("google_books_api_key", "google_books_api_key", "secret", "", "Google Books API key",
        "metadata", "Fills in genres and descriptions by ISBN. Free from a Google Cloud project "
        "with the Books API enabled.", env_aliases=("GOOGLE_BOOKS_API_KEY",)),
    Key("natively.delay", "natively_delay", "float", 0.4, "Natively request delay (s)", "advanced",
        "Seconds between learnnatively.com requests. Please keep it polite."),
    Key("natively.min_score", "natively_min_score", "float", 0.55, "Natively match threshold",
        "advanced", "Minimum title similarity (0-1) to accept a Natively match."),
    Key("pairing.min_score", "pairing_min_score", "float", 0.5, "Pairing threshold (unused)",
        "advanced", "Not used: pairing's thresholds are fixed. Kept so old configs load.",
        gui=False),
    Key("xref.min_score", "xref_min_score", "float", 0.62, "Audiobook link threshold", "advanced",
        "Minimum title/series similarity to link an audiobook to an EPUB."),
    Key("alignment.subplz_cmd", "subplz_cmd", "str", "", "SubPlz command", "advanced",
        "The command that aligns one audio file with a book's text. The server runs it, so it "
        "can only be set in the config file.", file_only=True),
    Key("discovery.enabled", "discovery_enabled", "bool", True, "Announce on the network",
        "network", "Advertise over Bonjour so Hondana for iPhone finds this server.", restart=True),
    Key("discovery.advertise_port", "discovery_advertise_port", "optint", None,
        "Advertised port", "network", "Behind a reverse proxy: the proxy's port (e.g. 80).",
        restart=True),
    Key("discovery.advertise_host", "discovery_advertise_host", "str", "", "Advertised host name",
        "network", "Behind a reverse proxy that answers to one name: that name.", restart=True),
    Key("data_mirror.publish", "data_mirror_publish", "bool", False, "Back up with cli.py push",
        "system", file_only=True, gui=False),
    Key("data_mirror.path", "data_mirror_path", "optpath", None, "Backup repo clone", "system",
        file_only=True, gui=False),
)
BY_NAME = {k.name: k for k in KEYS}


class SettingError(ValueError):
    pass


def user_home() -> Path:
    """The per-user app-data folder when there is no config file to anchor on."""
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Hondana").expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path("~/AppData/Local").expanduser())
        return Path(base) / "Hondana"
    base = os.environ.get("XDG_DATA_HOME") or str(Path("~/.local/share").expanduser())
    return Path(base) / "hondana"


def find_config(path: Path | str | None = None) -> Path | None:
    """The config file to read, or None. An explicitly named file that is missing
    is an error (a typo shouldn't silently start an empty library)."""
    explicit = path or next((os.environ[e] for e in EXPLICIT_ENVS if os.environ.get(e)), None)
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise SystemExit(f"Config not found: {p}")
        return p.resolve()
    env = os.environ.get(ENV_ENV, "").strip().lower()
    named = ROOT / f"config.{env or DEFAULT_ENV}.toml"
    if named.is_file():
        return named
    if (ROOT / "config.toml").is_file():
        return ROOT / "config.toml"
    if env:
        raise SystemExit(f"Config not found: {named} (EPUB_LIBRARY_ENV={env})")
    home = os.environ.get("HONDANA_HOME")
    candidate = (Path(home).expanduser() if home else user_home()) / "config.toml"
    return candidate.resolve() if candidate.is_file() else None


def load_dotenv(*files: Path) -> None:
    """KEY=VALUE lines into os.environ; variables already set win."""
    for env_file in files:
        if not env_file.is_file():
            continue
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _lookup(raw: dict, name: str):
    node = raw
    for part in name.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


_MISSING = object()


def _path(value, base: Path) -> Path:
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def coerce(key: Key, value, base: Path):
    """A raw value (TOML, JSON or an environment string) as the setting's type.
    Raises SettingError naming the key."""
    try:
        if key.kind == "path":
            if not str(value).strip():
                raise SettingError(f"{key.name} can't be empty")
            return _path(value, base)
        if key.kind == "optpath":
            return _path(value, base) if str(value or "").strip() else None
        if key.kind == "paths":
            if isinstance(value, str):
                text = value.strip()
                items = json.loads(text) if text.startswith("[") else [
                    p for p in text.split(os.pathsep) if p.strip()]
            else:
                items = list(value)
            return [_path(v, base) for v in items]
        if key.kind in ("str", "secret"):
            return str(value).strip()
        if key.kind == "int":
            return int(value)
        if key.kind == "optint":
            return int(value) if value not in (None, "", 0, "0") else None
        if key.kind == "float":
            return float(value)
        if key.kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
            raise SettingError(f"{key.name} must be true or false, not {value!r}")
    except SettingError:
        raise
    except (TypeError, ValueError) as exc:
        raise SettingError(f"{key.name}: {value!r} isn't a valid {key.kind}") from exc
    raise SettingError(f"{key.name}: unknown kind {key.kind}")


def _env_value(key: Key):
    for name in (key.env, *key.env_aliases):
        if name in os.environ and (os.environ[name] != "" or key.kind in ("str", "secret")):
            return os.environ[name]
    return _MISSING


def overlay_path(data_dir: Path) -> Path:
    return data_dir / OVERLAY_NAME


def read_overlay(data_dir: Path) -> dict:
    """The GUI's saved values, `{name: value}`. A missing or unreadable file is {}."""
    path = overlay_path(data_dir)
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        values = doc.get("values", {}) if isinstance(doc, dict) else {}
        return values if isinstance(values, dict) else {}
    except (OSError, ValueError) as exc:
        print(f"settings: ignoring {path} ({exc})", file=sys.stderr)
        return {}


def load(path: Path | str | None = None) -> Config:
    cfg_path = find_config(path)
    home_env = os.environ.get("HONDANA_HOME")
    home = (Path(home_env).expanduser().resolve() if home_env
            else cfg_path.parent if cfg_path else user_home())
    load_dotenv(home / ".env", ROOT / ".env")

    raw: dict = {}
    if cfg_path:
        try:
            with open(cfg_path, "rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"{cfg_path} isn't valid TOML: {exc}") from exc
    file_base = cfg_path.parent if cfg_path else home
    ctx = _Ctx(home=home)
    values: dict[str, object] = {}
    sources: dict[str, str] = {}

    def resolve(key: Key, overlay: dict) -> None:
        default = key.default(ctx) if callable(key.default) else key.default
        value, source = default, "default"
        try:
            found = _lookup(raw, key.name)
            if found is not _MISSING:
                value, source = coerce(key, found, file_base), "file"
            if key.name in overlay and not key.file_only:
                try:
                    value, source = coerce(key, overlay[key.name], home), "settings"
                except SettingError as exc:
                    print(f"settings: ignoring saved {exc}", file=sys.stderr)
            env = _env_value(key)
            if env is not _MISSING:
                value, source = coerce(key, env, Path.cwd()), "env"
        except SettingError as exc:
            raise SystemExit(f"Invalid setting: {exc}") from exc
        values[key.field], sources[key.field] = value, source

    # data_dir first: the overlay and several defaults live under it.
    resolve(BY_NAME["data_dir"], {})
    ctx.data_dir = values["data_dir"]
    overlay = read_overlay(ctx.data_dir)
    for unknown in sorted(set(overlay) - set(BY_NAME)):
        print(f"settings: ignoring unknown saved setting {unknown!r}", file=sys.stderr)
    for key in KEYS:
        if key.name != "data_dir":
            resolve(key, overlay)

    for key in (BY_NAME["data_dir"], BY_NAME["epub_dir"], BY_NAME["audiobook_dirs"]):
        if sources[key.field] == "default":  # never create a configured path (an unmounted drive)
            for p in values[key.field] if key.kind == "paths" else [values[key.field]]:
                p.mkdir(parents=True, exist_ok=True)

    return Config(**values, sources=sources, config_path=cfg_path, home=home)


def describe(cfg: Config) -> list[dict]:
    """Each setting's effective value and where it came from (secrets masked)."""
    rows = []
    for key in KEYS:
        value = getattr(cfg, key.field)
        if key.kind == "secret":
            shown = "(set)" if value else ""
        elif key.kind == "paths":
            shown = [str(p) for p in value]
        elif isinstance(value, Path):
            shown = str(value)
        else:
            shown = value
        rows.append({"key": key.name, "value": shown, "source": cfg.sources.get(key.field, "default"),
                     "env": key.env, "restart": key.restart, "file_only": key.file_only})
    return rows

