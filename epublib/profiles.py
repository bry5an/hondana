"""Lightweight named reader profiles.

No auth — a profile is just a slug, a display name, and the language the reader
is learning ("ja"/"en"). Each *device* (browser) tracks its own active profile
independently via the `profile` cookie (COOKIE_NAME) — see resolve_slug() and
server.py's `_request_cfg` dependency. A device with no cookie yet (or a
cookie naming a profile that no longer exists) follows data/profiles.json's
`active` slug, the household default set by whichever device last switched.
Switching repoints Config's three per-user paths (known_words / read_books /
known_stats) at data/users/<slug>/ for that request only. Everything else —
the book/audiobook collection and every global index — is shared across
profiles.

data/profiles.json shape:
    {"active": "<slug>",
     "profiles": [{"slug", "name", "language", "created_at"}, ...]}

A fresh profile's three files are seeded empty: known_words.json is {} (an empty
list, matching known.clear); read_books.json is {"books": {}, "schema": N}; and
known_stats.json is {} — which is exactly what known.recompute writes for an
empty known-words list, so no tokenizer run is needed to seed it.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from datetime import datetime, timezone

from . import known as known_mod
from .config import Config
from .index_store import cached_json, load_json, save_json

# The per-user files that live under data/users/<slug>/ instead of data/.
USER_FILES = ("known_words.json", "read_books.json", "known_stats.json")

# Cookie a device carries once it has explicitly picked a profile. Persistent
# (~5 years, set in server.py) — "stay on its own profile".
COOKIE_NAME = "profile"

SUPPORTED_LANGUAGES = ("ja", "en")

# Default first profile, created on first run from any existing top-level data.
# Generic, so a fresh install doesn't open as someone else; add your own from
# the sidebar's profile menu (＋ Add profile).
DEFAULT_SLUG = "reader"
DEFAULT_NAME = "Reader"
DEFAULT_LANGUAGE = "ja"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    """Filesystem-safe slug from a display name. Non-ASCII (e.g. a Japanese
    name) reduces to "user", which _unique_slug then numbers."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = _SLUG_RE.sub("-", ascii_.lower()).strip("-")
    return slug or "user"


def load(cfg: Config) -> dict:
    reg = load_json(cfg.profiles_index, {})
    if not isinstance(reg, dict) or not reg.get("profiles"):
        return {"active": None, "profiles": []}
    return reg


def save(cfg: Config, reg: dict) -> None:
    save_json(cfg.profiles_index, reg)


def _load_cached(cfg: Config) -> dict:
    """Read-only counterpart to load(), for resolve_slug/active_language —
    both run on nearly every request (_request_cfg + several handlers), so
    they go through the mtime-keyed cache instead of re-parsing profiles.json
    every time. Callers that mutate the registry (create/set_active/
    ensure_initialized) must keep using load(): they read-modify-save it in
    place, and mutating a cache hit before the save lands would leak the
    half-written state to any other request served from the same hit."""
    reg = cached_json(cfg.profiles_index, {})
    if not isinstance(reg, dict) or not reg.get("profiles"):
        return {"active": None, "profiles": []}
    return reg


def list_profiles(cfg: Config) -> dict:
    """Public API shape: active slug + [{slug, name, language, created_at}]. The
    known-words lists themselves never appear here."""
    reg = load(cfg)
    return {
        "active": reg.get("active"),
        "profiles": [
            {
                "slug": p["slug"],
                "name": p["name"],
                "language": p.get("language", DEFAULT_LANGUAGE),
                "created_at": p.get("created_at", ""),
            }
            for p in reg.get("profiles", [])
        ],
    }


def _seed_files(cfg: Config, slug: str) -> None:
    """Create any missing per-user data files for a profile. Never overwrites an
    existing file, so it is safe to call over just-migrated data."""
    d = cfg.users_dir / slug
    files = {
        "known_words.json": {},
        "read_books.json": {"books": {}, "schema": known_mod.SCHEMA_VERSION},
        "known_stats.json": {},
    }
    for name, empty in files.items():
        path = d / name
        if not path.exists():
            save_json(path, empty)


def _unique_slug(base: str, taken: set[str]) -> str:
    slug = base
    i = 2
    while slug in taken:
        slug = f"{base}-{i}"
        i += 1
    return slug


def create(cfg: Config, name: str, language: str = DEFAULT_LANGUAGE) -> dict:
    """Create a profile (seeded empty). Does not change the active profile."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Profile name is required")
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Unsupported language: {language!r}")

    reg = load(cfg)
    profiles = reg.get("profiles", [])
    slug = _unique_slug(slugify(name), {p["slug"] for p in profiles})
    profile = {
        "slug": slug,
        "name": name,
        "language": language,
        "created_at": _now(),
    }
    _seed_files(cfg, slug)
    profiles.append(profile)
    reg["profiles"] = profiles
    if not reg.get("active"):
        reg["active"] = slug
    save(cfg, reg)
    return profile


def active_language(cfg: Config) -> str:
    """The language the active profile is learning ("ja"/"en").

    Resolved from the live cfg.active_user (the slug whose per-user paths every
    request is reading), so it always agrees with which known-words list is in
    effect. Falls back to the JP default when the profile can't be found."""
    reg = _load_cached(cfg)
    for p in reg.get("profiles", []):
        if p["slug"] == cfg.active_user:
            return p.get("language", DEFAULT_LANGUAGE)
    return DEFAULT_LANGUAGE


def resolve_slug(cfg: Config, cookie_value: str | None) -> str:
    """Which profile a single request should use.

    `cookie_value` if it names a real profile (a device that has explicitly
    switched before); otherwise the registry's `active` slug — the household
    default that follows whichever device last switched, for any device that
    hasn't picked one of its own yet; otherwise the first registered profile.
    Self-healing against a cookie naming a since-deleted profile.
    """
    reg = _load_cached(cfg)
    profiles = reg.get("profiles") or []
    slugs = {p["slug"] for p in profiles}
    if cookie_value in slugs:
        return cookie_value
    if reg.get("active") in slugs:
        return reg["active"]
    return profiles[0]["slug"] if profiles else DEFAULT_SLUG


def set_active(cfg: Config, slug: str) -> str:
    """Persist `slug` as the registry's default (the household fallback used
    by resolve_slug for any device without its own cookie yet), and stamp it
    onto the given Config's active_user. Callers on the server pass a
    request-scoped Config (see server.py's `_request_cfg`), never the shared
    singleton, so this mutation is local to that one call."""
    reg = load(cfg)
    if slug not in {p["slug"] for p in reg.get("profiles", [])}:
        raise KeyError(slug)
    reg["active"] = slug
    save(cfg, reg)
    cfg.active_user = slug
    return slug


def ensure_initialized(cfg: Config) -> str:
    """Create the registry on first run and set cfg.active_user. Idempotent.

    First run migrates any existing top-level data/{known_words,read_books,
    known_stats}.json into data/users/<DEFAULT_SLUG>/ so the current reader's
    data becomes the first profile without loss.
    """
    reg = load(cfg)
    if reg.get("profiles"):
        slugs = {p["slug"] for p in reg["profiles"]}
        active = reg.get("active")
        if active not in slugs:  # registry hand-edited into an odd state
            active = reg["profiles"][0]["slug"]
            reg["active"] = active
            save(cfg, reg)
        cfg.active_user = active
        return active

    # First run: seed the default profile, migrating any legacy top-level files.
    dest = cfg.users_dir / DEFAULT_SLUG
    dest.mkdir(parents=True, exist_ok=True)
    for name in USER_FILES:
        src = cfg.data_dir / name
        tgt = dest / name
        if src.exists() and not tgt.exists():
            shutil.move(str(src), str(tgt))
    _seed_files(cfg, DEFAULT_SLUG)  # fill any of the three that didn't exist
    reg = {
        "active": DEFAULT_SLUG,
        "profiles": [
            {
                "slug": DEFAULT_SLUG,
                "name": DEFAULT_NAME,
                "language": DEFAULT_LANGUAGE,
                "created_at": _now(),
            }
        ],
    }
    save(cfg, reg)
    cfg.active_user = DEFAULT_SLUG
    return DEFAULT_SLUG
