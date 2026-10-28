"""FastAPI app: browse UI + JSON API + cover/download streaming.

Every index read on a hot request path (epub_library.json, audiobook_library.json,
per-user known_stats.json, etc.) goes through index_store's mtime-keyed cache
(`cached_json`, imported here as `_cached_json`), so a pipeline stage, delete, or
recompute is picked up on the next request without a restart, but repeat page loads
don't re-parse the file. The server does no matching per request. EPUB files are
streamed straight from epub_dir with a traversal guard.
"""
from __future__ import annotations

import asyncio
import gzip
import html
import io
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from contextlib import asynccontextmanager
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as req_lib
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect
from starlette.responses import Response
from starlette.types import Scope

from . import anki_notetype as anki_notetype_mod
from . import auth as auth_mod
from . import align as align_mod
from . import annotate as annotate_mod
from . import audible as audible_mod
from . import audiobook_catalog as audiobook_catalog_mod
from . import audiobook_details as audiobook_details_mod
from . import audiobook_match as audiobook_match_mod
from . import audiobook_scanner as audiobook_scanner_mod
from . import audiobook_xref as xref_mod
from . import align_jobs as align_jobs_mod
from . import book_jobs as book_jobs_mod
from . import build as build_mod
from . import corpus as corpus_mod
from . import custom_cover as custom_cover_mod
from . import dicts as dicts_mod
from . import discovery as discovery_mod
from . import incoming as incoming_mod
from . import inbox as inbox_mod
from . import intake as intake_mod
from . import known as known_mod
from . import orphans as orphans_mod
from . import pairing as pairing_mod
from . import pipeline as pipeline_mod
from . import profiles as profiles_mod
from . import purge as purge_mod
from . import reader as reader_mod
from . import scanner as scanner_mod
from . import settings as settings_mod
from . import setup_status as setup_status_mod
from . import schemas
from . import search_index as search_index_mod
from . import study as study_mod
from . import textnorm
from . import thumbs as thumbs_mod
from . import uploads as uploads_mod
from .compress import TextGZipMiddleware
from .config import ROOT, Config
from .config import load as load_config
from .index_store import cached_json as _cached_json
from .index_store import load_json, save_json
from .scanner import book_id_for

cfg: Config = load_config()
# Create data/profiles.json on first run (migrating any legacy top-level
# known_words/read_books/known_stats into users/<default slug>/) and point cfg at the
# active profile before any request reads a per-user path.
profiles_mod.ensure_initialized(cfg)


def _prewarm_known_review() -> None:
    """Build /known's review list in the background so the first visit after a
    restart isn't the one that pays the ~5 s cold sweep. Never blocks startup."""
    def run() -> None:
        try:
            corpus_mod.frequent_unknown(cfg, 1)
        except Exception:
            traceback.print_exc()

    threading.Thread(target=run, name="known-prewarm", daemon=True).start()


def _sweep_uploads() -> None:
    """Clear audiobook uploads nobody touched for a week, so a crash or an abandoned tab
    can't fill the library's drive. In a thread: the folder may be on a slow share."""
    def run() -> None:
        try:
            uploads_mod.sweep(cfg)
        except Exception:
            traceback.print_exc()

    threading.Thread(target=run, name="uploads-sweep", daemon=True).start()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _prewarm_known_review()
    _sweep_uploads()
    _inbox_watcher.start()
    advertiser = discovery_mod.Advertiser()
    await advertiser.start(cfg)
    try:
        yield
    finally:
        await advertiser.stop()
        _inbox_watcher.stop()


def _auth_gate(request: Request) -> None:
    """Every route: the cross-site check and, once a password is set, the admin gate
    (auth.py). Reads the live cfg, so a password set from the CLI applies at once."""
    auth_mod.gate(lambda: cfg, request)


app = FastAPI(title="Hondana", lifespan=_lifespan, dependencies=[Depends(_auth_gate)])
# /api/audiobooks can be megabytes of JSON; neither uvicorn nor a proxy
# compresses. Text-only so epub/m4b downloads and range requests stay raw.
app.add_middleware(TextGZipMiddleware)


STATIC_DIR = ROOT / "static"
TEMPLATES_DIR = ROOT / "templates"

# Templates are plain HTML, not Jinja. The one bit of templating is shared
# partials: `<!-- include: sidebar active=books -->` is replaced by
# templates/partials/sidebar.html. An include takes `name=value` attributes
# (`title="Read Next"` for values with spaces); a partial reads them as `{name}`
# (HTML-escaped), and `{active:key}` becomes " active" when the include's
# `active=` equals key. `{h1id}` is ` id="<h1id>"` when `h1id=` was given, else
# nothing. Partials carry their own indentation (the include comment's is
# swallowed) and may include other partials (shell → sidebar).
#   head    <meta>s, fonts, app.css, ui.js, viewstate.js, favicon  title=
#   shell   theme init, sidebar, overlay, opens .main-col  active=
#   header  nav toggle + <h1> (inside <div class="header-row">)  title= [h1id=]
_INCLUDE_RE = re.compile(r'[ \t]*<!--\s*include:\s*(\w+)((?:\s+\w+=(?:"[^"]*"|[\w-]+))*)\s*-->')
_ATTR_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([\w-]+))')
_ACTIVE_RE = re.compile(r"\{active:(\w+)\}")
_SLOT_RE = re.compile(r"\{(\w+)\}")
# `src="/static/app.js"` / `href="/static/app.css"` in a rendered page → `…?v=<mtime>`
_STATIC_REF_RE = re.compile(r'((?:src|href)="/static/)([\w./-]+)(")')


# (templates dir, page) → (files it was built from → (mtime_ns, size), html). A page
# is re-expanded only when it or a partial it pulled in changed on disk, so repeat
# loads cost a few stat()s rather than reading and regex-expanding ~5 files.
_render_cache: dict[tuple[str, str], tuple[dict[Path, tuple[int, int]], str]] = {}


def _stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _render(name: str) -> str:
    key = (str(TEMPLATES_DIR), name)
    hit = _render_cache.get(key)
    try:
        if hit and all(_stamp(p) == st for p, st in hit[0].items()):
            return hit[1]
    except OSError:
        pass  # a file vanished mid-edit: fall through, re-read (and raise if it's really gone)

    deps: dict[Path, tuple[int, int]] = {}

    def read(path: Path) -> str:
        deps[path] = _stamp(path)
        return path.read_text(encoding="utf-8")

    def expand(m: re.Match) -> str:
        attrs = {k: (q or w) for k, q, w in _ATTR_RE.findall(m.group(2))}
        partial = read(TEMPLATES_DIR / "partials" / f"{m.group(1)}.html")
        partial = _ACTIVE_RE.sub(lambda k: " active" if attrs.get("active") == k.group(1) else "", partial)

        def slot(k: re.Match) -> str:
            if k.group(1) == "h1id":
                return f' id="{html.escape(attrs["h1id"])}"' if attrs.get("h1id") else ""
            return html.escape(attrs.get(k.group(1), ""))

        return _SLOT_RE.sub(slot, partial)

    def version(m: re.Match) -> str:
        f = STATIC_DIR / m.group(2)
        try:
            deps[f] = stamp = _stamp(f)
        except OSError:
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}?v={stamp[0]}{m.group(3)}"

    text = read(TEMPLATES_DIR / name)
    for _ in range(3):  # a partial can include another (shell → sidebar)
        text, n = _INCLUDE_RE.subn(expand, text)
        if not n:
            break
    # Versioned so /static can be cached for good (_CachedStatic); the asset is a dep
    # of the page, so editing it re-renders the page with the new version.
    text = _STATIC_REF_RE.sub(version, text)
    _render_cache[key] = (deps, text)
    return text


class _CachedStatic(StaticFiles):
    """A `?v=` request is for a URL `_render` stamped with the file's mtime, so its
    content can never change under that URL — cache it for good. Anything else
    (the font `app.css` pulls in by `url()`) revalidates against ETag as before."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            versioned = "v" in urllib.parse.parse_qs(scope["query_string"].decode("latin-1"))
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if versioned else "no-cache"
            )
        return resp


app.mount("/static", _CachedStatic(directory=STATIC_DIR), name="static")


def _load_library() -> dict:
    return _cached_json(cfg.library_index, {})


def _load_audiobooks() -> dict:
    return _cached_json(cfg.audiobook_index, {})


def _load_known_stats(req_cfg: Config) -> dict:
    return _cached_json(req_cfg.known_stats, {})


def _request_cfg(request: Request) -> Config:
    """Per-request Config whose per-user paths (known_words/known_stats/
    read_books) point at *this device's* profile, resolved from its `profile`
    cookie rather than the shared module-level `cfg` singleton — see
    profiles.resolve_slug. Safe to use anywhere the bare `cfg`
    was used: every property besides the per-user ones is unaffected."""
    slug = profiles_mod.resolve_slug(cfg, request.cookies.get(profiles_mod.COOKIE_NAME))
    return replace(cfg, active_user=slug)


def _safe_epub_path(rel: str) -> Path:
    """Resolve a library-relative path inside epub_dir, blocking traversal."""
    base = cfg.epub_dir.resolve()
    target = (base / rel).resolve()
    if base != target and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    return target


# Every page is a static shell around `_render(template)`; its JS fetches the data.
# The series pages take the series as ?name=, not a path segment: names contain
# spaces, brackets and sometimes "/", and a reverse proxy in front of the app would
# likely normalize the resulting %2F before this app ever saw it — so their JS reads
# location.search and filters /api/books or /api/audiobooks, and renders its own "no
# such series" message. /book/{id} works the same way (book.js reads the id from the
# URL; an unknown id renders the page's own 404 message).
PAGES = {
    "/": "index.html",
    "/pair": "pair.html",
    "/audiobooks": "audiobooks.html",
    "/series": "series.html",
    "/audiobook-series": "audiobook_series.html",
    "/next": "next.html",
    "/book/{book_id}": "book.html",
    "/add": "add.html",
    "/profile": "profile.html",
    "/known": "known.html",
    "/admin": "admin.html",
    "/login": "login.html",
    "/settings": "settings.html",
}


def _register_page(path: str, template: str) -> None:
    def page() -> str:
        return _render(template)

    app.add_api_route(path, page, methods=["GET"], response_class=HTMLResponse,
                      name=f"{template.removesuffix('.html')}_page")


for _path, _template in PAGES.items():
    _register_page(_path, _template)


@app.get("/healthz", response_model=schemas.StatusResponse)
def healthz() -> dict:
    return {"status": "ok"}


# ── admin password (auth.py) ──────────────────────────────────────────────────

_login_throttle = auth_mod.Throttle()


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").startswith("https")


def _with_session(resp: JSONResponse, request: Request) -> JSONResponse:
    resp.set_cookie(auth_mod.COOKIE, auth_mod.make_session(cfg), max_age=auth_mod.SESSION_DAYS * 86400,
                    httponly=True, samesite="strict", secure=_https(request), path="/")
    return resp


@app.get("/api/auth/state", response_model=schemas.AuthState)
def auth_state(request: Request) -> dict:
    """Whether a password is set, and whether this browser may use the admin pages."""
    return {"password_set": auth_mod.password_set(cfg), "admin": auth_mod.is_admin(cfg, request)}


@app.post("/api/auth/login", response_model=schemas.AuthState)
def auth_login(payload: schemas.LoginRequest, request: Request) -> JSONResponse:
    who = request.client.host if request.client else "?"
    wait = _login_throttle.wait(who)
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many tries. Wait {wait} s.",
                            headers={"Retry-After": str(wait)})
    stored = auth_mod.state(cfg).get("password")
    if not stored:
        return JSONResponse({"password_set": False, "admin": True})
    if not auth_mod.verify_password(payload.password, stored):
        _login_throttle.failed(who)
        raise HTTPException(status_code=401, detail="Wrong password")
    _login_throttle.succeeded(who)
    return _with_session(JSONResponse({"password_set": True, "admin": True}), request)


@app.post("/api/auth/logout", response_model=schemas.StatusResponse)
def auth_logout() -> JSONResponse:
    resp = JSONResponse({"status": "signed_out"})
    resp.delete_cookie(auth_mod.COOKIE, path="/")
    return resp


@app.post("/api/auth/password", response_model=schemas.AuthState)
def auth_set_password(payload: schemas.PasswordRequest, request: Request) -> JSONResponse:
    """Set or change the admin password (the gate already required the old one's
    session). Changing it signs every other browser out; this one gets a fresh session."""
    try:
        auth_mod.set_password(cfg, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _with_session(JSONResponse({"password_set": True, "admin": True}), request)


@app.delete("/api/auth/password", response_model=schemas.AuthState)
def auth_clear_password() -> JSONResponse:
    auth_mod.clear_password(cfg)
    resp = JSONResponse({"password_set": False, "admin": True})
    resp.delete_cookie(auth_mod.COOKIE, path="/")
    return resp


@app.get("/api/auth/tokens", response_model=list[schemas.TokenInfo])
def auth_tokens() -> list:
    return auth_mod.list_tokens(cfg)


@app.post("/api/auth/tokens", response_model=schemas.TokenCreated)
def auth_create_token(payload: schemas.TokenCreateRequest) -> dict:
    row, token = auth_mod.create_token(cfg, payload.name)
    return {**row, "token": token}


@app.delete("/api/auth/tokens/{token_id}", response_model=schemas.StatusResponse)
def auth_revoke_token(token_id: str) -> dict:
    if not auth_mod.revoke_token(cfg, token_id):
        raise HTTPException(status_code=404, detail="No such token")
    return {"status": "revoked"}


# ── pairing review API (LAN-only; writes the overrides file) ─────────────────

@app.get("/api/pairing/suggestions")
def pairing_suggestions() -> JSONResponse:
    return JSONResponse(pairing_mod.compute_suggestions(cfg))


def _recompute() -> None:
    """Re-run pairing + build so /api/books reflects an override change."""
    pairing_mod.pair(cfg)
    build_mod.build(cfg)


@app.post("/api/pairing/link", response_model=schemas.PairingActionResponse)
def pairing_link(payload: schemas.PairingKeysRequest) -> dict:
    en_key, jp_key = payload.en_key, payload.jp_key
    if not en_key or not jp_key:
        raise HTTPException(status_code=400, detail="en_key and jp_key are required")
    with _index_lock:
        pairing_mod.link(cfg, en_key, jp_key)
        _recompute()
    return {"status": "linked", "en_key": en_key, "jp_key": jp_key}


@app.post("/api/pairing/unlink", response_model=schemas.PairingActionResponse)
def pairing_unlink(payload: schemas.PairingUnlinkRequest) -> dict:
    en_key, jp_key = payload.en_key, payload.jp_key
    if not en_key or not jp_key:
        raise HTTPException(status_code=400, detail="en_key and jp_key are required")
    with _index_lock:
        pairing_mod.unlink(cfg, en_key, jp_key, block=payload.block)
        _recompute()
    return {"status": "unlinked", "en_key": en_key, "jp_key": jp_key}


@app.post("/api/pairing/block", response_model=schemas.PairingActionResponse)
def pairing_block(payload: schemas.PairingKeysRequest) -> dict:
    """Reject a suggestion: that pair is never offered or auto-paired again (same write as an unlink)."""
    en_key, jp_key = payload.en_key, payload.jp_key
    if not en_key or not jp_key:
        raise HTTPException(status_code=400, detail="en_key and jp_key are required")
    with _index_lock:
        pairing_mod.unlink(cfg, en_key, jp_key)
        _recompute()
    return {"status": "blocked", "en_key": en_key, "jp_key": jp_key}


@app.post("/api/pairing/unblock", response_model=schemas.PairingActionResponse)
def pairing_unblock(payload: schemas.PairingKeysRequest) -> dict:
    en_key, jp_key = payload.en_key, payload.jp_key
    if not en_key or not jp_key:
        raise HTTPException(status_code=400, detail="en_key and jp_key are required")
    with _index_lock:
        pairing_mod.unblock(cfg, en_key, jp_key)
        _recompute()
    return {"status": "unblocked", "en_key": en_key, "jp_key": jp_key}


@app.get("/api/books/{book_id}/edition")
def get_edition(book_id: str) -> JSONResponse:
    """The book page's other-edition state: `{eligible, mode, partner, candidates, others}`
    (see `pairing.edition_state`). `eligible` is false for books that aren't ja/en."""
    _require_book(book_id)
    return JSONResponse(pairing_mod.edition_state(cfg, book_id))


def _edition_none(book_id: str, on: bool) -> JSONResponse:
    book = _require_book(book_id)
    if book.get("language") not in ("en", "ja"):
        raise HTTPException(status_code=400, detail="Only Japanese and English books have another edition")
    with _index_lock:
        if on and pairing_mod.edition_state(cfg, book_id)["partner"]:
            raise HTTPException(status_code=409, detail="This book is paired. Unlink it first.")
        (pairing_mod.set_none if on else pairing_mod.clear_none)(cfg, pairing_mod.stable_key(book))
        _recompute()
        return JSONResponse(pairing_mod.edition_state(cfg, book_id))


@app.post("/api/books/{book_id}/edition/none")
def set_edition_none(book_id: str) -> JSONResponse:
    """Say this book has no other edition: it is never auto-paired or suggested until cleared."""
    return _edition_none(book_id, True)


@app.delete("/api/books/{book_id}/edition/none")
def clear_edition_none(book_id: str) -> JSONResponse:
    """Back to automatic: paired and suggested as usual again."""
    return _edition_none(book_id, False)


# What the list endpoint leaves out of each book. Synopsis text: only the book page
# wants it (GET /api/books/{id}). The rest is scan/provenance bookkeeping no page's JS
# reads — a denylist, so a field added later reaches the UI by default.
# `stable_key` is derived from `identifiers`/`path` before they are dropped, so
# the iOS reader can file a position for a book that has no pack. `scanned_at` stays in the
# list: it is when the scanner last read the file, the nearest thing to "added", and the iOS
# shelf's Recent sort puts a book you haven't opened by it.
_BULKY_BOOK_FIELDS = frozenset((
    "description", "raw_description",
    "identifiers", "opf_identifier", "raw_title", "raw_author", "raw_language",
    "raw_lang_source", "raw_cover", "schema", "lang_source", "mtime", "filesize", "path",
))


@app.get("/api/books")
def api_books(
    series: str | None = None, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    """All books, or with `?series=NAME` only those whose series_name matches
    exactly (the book and series pages need one series, not the whole library)."""
    library = _load_library()
    stats = _load_known_stats(req_cfg)
    # Return as a list sorted by title for stable client rendering. New dicts,
    # not mutations of the cached library — that cache is shared across every
    # device/profile.
    # The synopsis is only wanted on the book page (GET /api/books/{id}); leaving
    # it out keeps every page's whole-library fetch small.
    books = sorted(
        (
            {**{k: v for k, v in b.items() if k not in _BULKY_BOOK_FIELDS},
             "stable_key": pairing_mod.stable_key(b),
             "known": stats.get(bid),
             "audio_synced": align_mod.is_synced(cfg, b, _book_audiobook(b)[0])}
            for bid, b in library.items()
            if series is None or b.get("series_name") == series
        ),
        key=lambda b: (b.get("title") or "").lower(),
    )
    return JSONResponse(books)


@app.get("/api/books/{book_id}")
def api_book(book_id: str, req_cfg: Config = Depends(_request_cfg)) -> dict:
    library = _load_library()
    book = library.get(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    stats = _load_known_stats(req_cfg)
    return {**book, "stable_key": pairing_mod.stable_key(book), "known": stats.get(book_id),
            "audio_synced": align_mod.is_synced(cfg, book, _book_audiobook(book)[0])}


@app.get("/api/books/{book_id}/study")
async def api_book_study(
    book_id: str, n: int = 100, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    library = _load_library()
    if book_id not in library:
        raise HTTPException(status_code=404, detail="Book not found")
    n = max(1, min(n, 1000))
    # Reads the EPUB to resolve example sentences — off the event loop.
    return JSONResponse(
        await asyncio.to_thread(study_mod.study_path, req_cfg, book_id, n)
    )


@app.post("/api/books/{book_id}/read")
async def api_book_read(
    book_id: str,
    payload: schemas.ReadStatusRequest = schemas.ReadStatusRequest(),
    req_cfg: Config = Depends(_request_cfg),
) -> JSONResponse:
    """Mark a book read (or unread). Words met often enough in a read book
    become part of the reader's exposed vocabulary, so every other book's
    reading load is recomputed. Scoped to the requesting device's own profile
    — `known` is no longer baked into the shared index, so no rebuild needed."""
    library = _load_library()
    if book_id not in library:
        raise HTTPException(status_code=404, detail="Book not found")
    read = payload.read

    def _run() -> dict:
        with _index_lock:
            known_mod.mark_read(req_cfg, book_id, read)
            known_mod.recompute(req_cfg)
        return {"status": "ok", "book_id": book_id, "read": read,
                **known_mod.summary(req_cfg, library)}

    return JSONResponse(await asyncio.to_thread(_run))


@app.patch("/api/books/{book_id}/read-date")
async def api_book_read_date(
    book_id: str,
    payload: schemas.ReadDateRequest,
    req_cfg: Config = Depends(_request_cfg),
) -> JSONResponse:
    """Change the day this profile finished a book. 404 for a book not in
    the library, 409 when it isn't marked read, 400 for a day in the future (a
    day of slack for timezones ahead of UTC). Exposure depends on which books
    are read, not when, so nothing is recomputed."""
    if book_id not in _load_library():
        raise HTTPException(status_code=404, detail="Book not found")
    if payload.read_at > datetime.now(timezone.utc).date() + timedelta(days=1):
        raise HTTPException(status_code=400, detail="That date is in the future")

    def _run() -> str:
        with _index_lock:
            return known_mod.set_read_date(req_cfg, book_id, payload.read_at)

    try:
        read_at = await asyncio.to_thread(_run)
    except KeyError:
        raise HTTPException(status_code=409, detail="Not marked read") from None
    return JSONResponse({"status": "ok", "book_id": book_id, "read_at": read_at})


@app.delete("/api/books/{book_id}", response_model=schemas.DeleteBookResponse)
def delete_book(book_id: str) -> JSONResponse:
    # Lock first, then work on a private copy of the index: _load_library()
    # returns the shared mtime-cached dict, which other requests iterate, and
    # a build() landing between the load and our save would be overwritten by
    # a stale copy.
    with _index_lock:
        return _delete_book_locked(book_id)


def _delete_book_locked(book_id: str) -> JSONResponse:
    library = load_json(cfg.library_index, {})
    book = library.get(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    deleted_files: list[str] = []
    warnings: list[str] = []

    epub_path = _safe_epub_path(book["path"])
    try:
        epub_path.unlink()
        deleted_files.append(epub_path.name)
    except FileNotFoundError:
        warnings.append(f"EPUB file not found on disk: {book['path']}")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete EPUB: {exc}")

    del library[book_id]
    save_json(cfg.library_index, library)
    # An explicit delete drops the book's curated state too (read status,
    # overrides), not just its derived data — see purge.py.
    purge_mod.purge_book(cfg, book_id, cover=book.get("raw_cover", book.get("cover")), curated=True)
    build_mod.build(cfg)
    # The wordlist feeds exposure_counts() for read books, so every profile
    # learning this book's language has shifted numbers now.
    known_mod.recompute_all(
        cfg, {book["language"]} if book.get("language") else None
    )

    resp: dict = {"status": "deleted", "book_id": book_id, "deleted_files": deleted_files}
    if warnings:
        resp["warnings"] = warnings
    return JSONResponse(resp)


@app.get("/covers/{book_id}")
def cover(book_id: str, full: int = 0) -> FileResponse:
    library = _load_library()
    book = library.get(book_id)
    if not book or not book.get("cover"):
        raise HTTPException(status_code=404, detail="No cover")
    cover_path = (cfg.data_dir / book["cover"]).resolve()
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="No cover")

    path, media_type = cover_path, None
    if not full:
        thumb = thumbs_mod.ensure_thumb(cfg.data_dir, book_id, cover_path)
        if thumb is not None:
            path, media_type = thumb, "image/webp"

    # Immutable is safe because pages request covers through coverUrl() (ui.js):
    # a hand-set cover is served as ?v=<cover_v, its mtime>, so each
    # replacement is a new URL, and a reset drops cover_v, going back to the
    # plain URL that has only ever meant the EPUB's own cover. custom_cover
    # drops the cached thumbnail on replace and reset.
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/covers/audiobook/{ab_id}")
def audiobook_cover(ab_id: str, request: Request, full: int = 0):
    """An audiobook's hand-set cover, else a local file's own (from its tags, or
    its folder's cover.jpg), as a thumbnail unless `full`. Versioned URLs
    (audiobook_scanner.cover_url, audiobook_catalog) make it safe to cache for good."""
    entry = _cached_json(cfg.audiobook_files_index, {}).get(ab_id)
    custom = audiobook_details_mod.custom_cover_path(cfg, ab_id)
    if not entry and not custom:
        moved = _moved_audiobook(ab_id, request, "/covers/audiobook/")
        if moved:
            return moved
    src = custom or (cfg.data_dir / entry["cover"] if entry and entry.get("cover") else None)
    if src is None or not src.exists():
        raise HTTPException(status_code=404, detail="No cover")
    path, media_type = src, None
    if not full:
        thumb = thumbs_mod.ensure_thumb(cfg.data_dir, ab_id, src, src.parent / "thumbs" / f"{src.stem}.webp")
        if thumb is not None:
            path, media_type = thumb, "image/webp"
    return FileResponse(path, media_type=media_type,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/audiobooks")
def api_audiobooks() -> JSONResponse:
    index = _load_audiobooks()
    entries = sorted(
        index.values(),
        key=lambda e: ((e.get("series") or e.get("title") or "").lower(),
                       e.get("order") or "", (e.get("title") or "").lower()),
    )
    return JSONResponse(entries)


# Fields scanned by the local substring search, per kind.
_SEARCH_FIELDS = {
    "catalog": ("title", "series", "author", "narrators", "description_full"),
    "audiobooks": ("title", "author", "series"),
    "epub": ("title", "author", "series_name"),
}
_SEARCH_SOURCE = {
    "catalog": lambda: audiobook_catalog_mod.cached(cfg),
    "audiobooks": lambda: _cached_json(cfg.audiobook_index, {}),
    "epub": lambda: _cached_json(cfg.library_index, {}),
}


def _search_local(q: str, kind: str, limit: int) -> list[str]:
    """Local substring search over the already-loaded JSON indices.

    Substring only (no word-boundary awareness) — used for queries too short
    for the FTS trigram index (see search_index.py) and as a fallback if that
    index has a problem. Returns matching ids only.
    """
    fields = _SEARCH_FIELDS.get(kind, _SEARCH_FIELDS["catalog"])
    source = _SEARCH_SOURCE.get(kind, _SEARCH_SOURCE["catalog"])()
    needle = q.lower()
    out: list[str] = []
    for key, entry in source.items():
        if any(needle in str(entry.get(f) or "").lower() for f in fields):
            out.append(key)
            if len(out) >= limit:
                break
    # Readings (romaji / kana) live only in the search index; a short query like
    # "かぜ" is still a plain scan of that column.
    out += [i for i in search_index_mod.readings_like(cfg, q, kind, limit) if i not in set(out)]
    return out[:limit]


@app.get("/api/search", response_model=schemas.SearchResponse)
async def api_search(q: str = "", kind: str = "catalog", limit: int = 50) -> JSONResponse:
    """Search over the JSON indices: SQLite FTS5 (trigram tokenizer, so it
    works without Japanese word boundaries — see search_index.py) with a local
    substring fallback for queries under 3 characters (too short to tokenize)
    or if the FTS index has a problem.

    kind: catalog (Audible metadata incl. descriptions) | audiobooks | epub.
    Returns ids only, not full records — every caller (static/search.js) just
    filters its already-loaded data by id, and the full catalog shape (esp.
    description_full) made this the largest response on the site for no reason.
    """
    q = q.strip()
    # High cap so the UI can pull the full match set to filter its loaded data;
    # epub/audiobook rows are small (no descriptions).
    limit = max(1, min(limit, 5000))
    if not q:
        return JSONResponse({"source": "empty", "ids": []})
    try:
        ids = await asyncio.to_thread(search_index_mod.search, cfg, q, kind, limit)
        return JSONResponse({"source": "fts", "ids": ids})
    except Exception:
        ids = await asyncio.to_thread(_search_local, q, kind, limit)
        return JSONResponse({"source": "substring", "ids": ids})


def _safe_audiobook_path(entry: dict) -> Path:
    """Resolve an entry's file under its configured root, blocking traversal."""
    root = entry.get("root")
    base = next((d.resolve() for d in cfg.audiobook_dirs
                 if str(d) == root), None)
    if base is None:
        raise HTTPException(status_code=400, detail="Unknown audiobook root")
    target = (base / entry["path"]).resolve()
    if base != target and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    return target


@app.delete("/api/audiobooks/{ab_id}", response_model=schemas.DeleteAudiobookResponse)
def delete_audiobook(ab_id: str) -> JSONResponse:
    # Lock first and mutate a private copy — see delete_book.
    with _index_lock:
        return _delete_audiobook_locked(ab_id)


def _delete_audiobook_locked(ab_id: str) -> JSONResponse:
    files_index = load_json(cfg.audiobook_files_index, {})
    entry = files_index.get(ab_id)
    # `local` is a field of the *joined* index (audiobook_library.json), where
    # build_index() sets it per catalog ISBN. audiobook_files.json is the raw
    # disk scan, so it has no such key and never did — every entry in it is a
    # file that was walked off disk. Guarding on entry["local"] here therefore
    # rejected every audiobook, making deletion impossible. Absence from the
    # files index is the real "Audible-only" test.
    if not entry:
        if ab_id in _load_audiobooks():
            raise HTTPException(
                status_code=400,
                detail="Not a local audiobook — nothing to delete",
            )
        raise HTTPException(status_code=404, detail="Audiobook not found")
    if entry.get("path") in (".", ""):
        raise HTTPException(status_code=400, detail="Refusing to delete audiobook root directory")

    path = _safe_audiobook_path(entry)
    deleted_files: list[str] = []
    warnings: list[str] = []

    if entry.get("format") == "mp3-folder":
        try:
            shutil.rmtree(path)
            deleted_files.append(f"directory: {entry['path']}")
        except FileNotFoundError:
            warnings.append(f"Directory not found: {entry['path']}")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete directory: {exc}")
    elif entry.get("files"):
        # A `tracks` book is one album of a folder that may hold others: only
        # its own files go, then the folder if nothing else is left in it.
        for name in entry["files"]:
            try:
                (path / name).unlink()
                deleted_files.append(name)
            except FileNotFoundError:
                warnings.append(f"File not found: {entry['path']}/{name}")
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Failed to delete file: {exc}")
        try:
            path.rmdir()
        except OSError:
            pass
    else:
        try:
            path.unlink()
            deleted_files.append(path.name)
        except FileNotFoundError:
            warnings.append(f"File not found: {entry['path']}")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {exc}")

    del files_index[ab_id]
    save_json(cfg.audiobook_files_index, files_index)
    _rebuild_audiobooks()

    resp: dict = {"status": "deleted", "ab_id": ab_id, "deleted_files": deleted_files}
    if warnings:
        resp["warnings"] = warnings
    return JSONResponse(resp)


# ── Reader API (the Hondana iOS app; LAN-only like the rest) ─────────────────
#
# The server does the language work and the phone only places it on the page:
# GET /api/reader/books/{id}/pack is the book's annotation pack (annotate.py).
# It is profile-independent, so it needs no request cfg, and it never takes
# _index_lock: a stage run holds that for minutes, and the pack is built from
# the EPUB alone. The vocab and progress routes are the requesting profile's
# (reader.py) and read-only here; they skip _index_lock for the same reason,
# which is safe because every writer replaces its file atomically.

def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    tags = [t.strip().removeprefix("W/") for t in header.split(",")]
    return "*" in tags or etag in tags


def _serve_pack(book: dict, if_none_match: str | None, accepts_gzip: bool) -> Response:
    sha = annotate_mod.book_sha(cfg, book)
    tag = annotate_mod.etag(sha)
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if _etag_matches(if_none_match, tag):
        return Response(status_code=304, headers=headers)
    try:
        body = annotate_mod.ensure_pack(cfg, book).read_bytes()
    except annotate_mod.NoPack as e:
        return JSONResponse({"reason": e.reason}, status_code=409)
    # Stored gzipped, sent as is. TextGZipMiddleware leaves an encoded response alone.
    if accepts_gzip:
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)
    return Response(body, media_type="application/json", headers=headers)


@app.api_route("/api/reader/books/{book_id}/pack", methods=["GET", "HEAD"])
async def api_reader_pack(book_id: str, request: Request) -> Response:
    # The full record, not /api/books' view: the stable key needs `identifiers`.
    book = _load_library().get(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return await asyncio.to_thread(
        _serve_pack, book, request.headers.get("if-none-match"),
        "gzip" in request.headers.get("accept-encoding", ""),
    )


@app.get("/api/reader/vocab")
def api_reader_vocab(request: Request, req_cfg: Config = Depends(_request_cfg)) -> Response:
    """The profile's effective known set for the phone to resolve word status
    from (reader.snapshot). The tag is checked before anything is built, so a
    304 costs three stat calls."""
    tag = reader_mod.etag(req_cfg)
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), tag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(reader_mod.snapshot(req_cfg), headers=headers)


@app.get("/api/reader/progress")
def api_reader_progress(req_cfg: Config = Depends(_request_cfg)) -> JSONResponse:
    """Every reading position this profile has synced, by stable key, every
    audiobook position, by ab_id, and its bookmarks, by id, deletions included."""
    return JSONResponse({"progress": reader_mod.load_progress(req_cfg),
                         "audio": reader_mod.load_audio_progress(req_cfg),
                         "bookmarks": reader_mod.load_bookmarks(req_cfg)},
                        headers={"Cache-Control": "no-cache"})


# Audio↔text sync: an SRT made elsewhere is ingested and matched to the
# pack's sentences (align.py); the phone reads the map. Like the pack, none of
# it takes _index_lock — it reads the EPUB and the SRT and writes only
# data/alignments/, one book at a time.

ALIGN_MAX_BYTES = 5 * 1024 * 1024


def _book_audiobook(book: dict) -> tuple[str | None, list[str] | None]:
    """`(ab_id, track files)` of the book's linked audiobook when it is on this
    machine, else `(None, None)`; `files` is None for a single-file book."""
    link = book.get("audiobook") or {}
    ab_id = link.get("audible_isbn")
    entry = _load_audiobooks().get(ab_id) if ab_id and link.get("audiobook_local") else None
    if not entry or not entry.get("local"):
        return None, None
    return ab_id, align_mod.track_files(entry)


def _book_alignment_state(book: dict) -> dict:
    """`align.state` plus what the book page needs to decide what to offer:
    `eligible` (a Japanese book whose linked audiobook is on this machine, the only
    kind that can be synced) and `can_generate` (`subplz_cmd` can run here, so the
    page offers Generate; otherwise it shows the command for a machine with a GPU).
    `accepts` lists the SRT kinds an upload may be, so `align-remote` can tell
    a server that takes transcripts from one that would store them as synced."""
    ab_id = _book_audiobook(book)[0]
    out = align_mod.state(cfg, book, ab_id, _align_jobs.status(book["book_id"]))
    out["eligible"] = book.get("language") == "ja" and ab_id is not None
    out["can_generate"] = align_jobs_mod.command_problem(cfg) is None
    out["accepts"] = list(align_mod.KINDS)
    return out


def _align_error(e: Exception) -> JSONResponse:
    if isinstance(e, annotate_mod.NoPack):
        return JSONResponse({"reason": e.reason}, status_code=409)
    status = 409 if e.reason in ("no_audiobook", "audiobook_changed", "book_changed") else 400  # bad_srt, bad_part, bad_kind
    return JSONResponse({"reason": e.reason, "detail": str(e)}, status_code=status)


def _serve_alignment(book: dict, if_none_match: str | None) -> Response:
    ab_id, files = _book_audiobook(book)
    try:
        m = align_mod.ensure(cfg, book, ab_id, files)
    except (align_mod.AlignError, annotate_mod.NoPack) as e:
        return _align_error(e)
    if m is None:
        raise HTTPException(status_code=404, detail="No alignment for this book")
    headers = {"ETag": align_mod.etag(m), "Cache-Control": "no-cache"}
    if _etag_matches(if_none_match, headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return JSONResponse(m, headers=headers)


@app.get("/api/reader/books/{book_id}/alignment")
async def api_reader_alignment(book_id: str, request: Request) -> Response:
    """The sentence → time map of the book's linked audiobook. 404 when none was
    ingested; 409 `{reason}` when it can't be served (`audiobook_changed`,
    `no_audiobook`, or the pack's `drm`/`not_ja`/`no_text`)."""
    book = _require_book(book_id)
    return await asyncio.to_thread(_serve_alignment, book, request.headers.get("if-none-match"))


@app.get("/api/reader/books/{book_id}/alignment.srt")
async def api_reader_alignment_srt(book_id: str, part: str | None = None, kind: str | None = None) -> Response:
    """The raw SRT as ingested (`?part=` names the mp3 of a multi-file audiobook):
    the one the map is made from, or `?kind=synced|transcript`."""
    book = _require_book(book_id)
    _, files = _book_audiobook(book)
    if kind is not None and kind not in align_mod.KINDS:
        return _align_error(align_mod.AlignError("bad_kind", f"kind is one of {', '.join(align_mod.KINDS)}"))
    try:
        raw = await asyncio.to_thread(align_mod.srt_bytes, cfg, book_id, files, part, kind)
    except align_mod.AlignError as e:
        return _align_error(e)
    if raw is None:
        raise HTTPException(status_code=404, detail="No alignment for this book")
    return Response(raw, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.get("/api/books/{book_id}/alignment/text")
async def alignment_text(book_id: str, numbered: bool = False) -> Response:
    """The book's sentences, one per line (`text/plain`), exactly what a SubPlz run
    on this machine is given as `{text}`: a machine with the GPU but not the
    library aligns against it, so the cues it returns are the sentences this
    server matches. `X-Book-Sha256` is the EPUB they came from; send it back with
    the upload. `?numbered=1` gives every pack sentence as `doc:sentence<TAB>text`,
    the empty ones too, so a line can be read against a map. 409 `{reason}`:
    `no_audiobook`, or the pack's `drm`/`not_ja`/`no_text`."""
    book = _require_book(book_id)
    if not _book_audiobook(book)[0]:
        return _align_error(align_mod.AlignError("no_audiobook", "link a local audiobook first"))

    def _run() -> tuple[str, str]:
        text = align_mod.numbered_text(cfg, book) if numbered else align_mod.book_text(cfg, book)
        return text, annotate_mod.book_sha(cfg, book)

    try:
        text, sha = await asyncio.to_thread(_run)
    except annotate_mod.NoPack as e:
        return _align_error(e)
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"X-Book-Sha256": sha, "Cache-Control": "no-cache"})


@app.post("/api/books/{book_id}/alignment", response_model=schemas.AlignmentIngestResponse)
async def ingest_alignment(book_id: str, file: list[UploadFile] = File(...),
                           part: list[str] = Form(default=[]), kind: str = Form(default="synced"),
                           x_book_sha256: str | None = Header(default=None)) -> JSONResponse:
    """Ingest an SRT of the book's linked audiobook (made by SubPlz or similar) and
    match it to the book's sentences. Needs a local audiobook link. For a
    multi-file (mp3) audiobook send one `file` per mp3, each with a `part` (its
    file name) in the same order; they are stored and matched together or not at
    all. `X-Book-Sha256` (from `…/alignment/text`) makes the upload 409
    `book_changed` when the EPUB is no longer the one the SRTs were made from.
    `kind`: `synced` (default; SubPlz `sync`, the book's own lines) or
    `transcript` (SubPlz `gen`, what Whisper heard), which the map is then made
    from; a `transcript` that is really `sync` output is 400 `bad_srt`."""
    book = _require_book(book_id)
    if part and len(part) != len(file):
        raise HTTPException(status_code=400, detail="Send one part per file")
    if len(set(part)) != len(part):
        raise HTTPException(status_code=400, detail="Each part may be sent once")
    srts: dict[str | None, str] = {}
    for i, f in enumerate(file):
        raw = await f.read(ALIGN_MAX_BYTES + 1)
        if len(raw) > ALIGN_MAX_BYTES:
            raise HTTPException(status_code=413, detail="SRT is over 5 MB")
        srts[part[i] if part else None] = raw.decode("utf-8-sig", errors="replace")
    if len(file) > 1 and not part:
        raise HTTPException(status_code=400, detail="Name each file's part")
    ab_id, files = _book_audiobook(book)

    def _run() -> dict:
        if x_book_sha256 and x_book_sha256 != annotate_mod.book_sha(cfg, book):
            raise align_mod.AlignError("book_changed", "the EPUB changed since its text was fetched")
        m = align_mod.ingest_parts(cfg, book, ab_id, files, srts, kind)
        return {"status": "ok", "audiobook": m["audiobook"], "source": m["source"], "coverage": m["coverage"],
                "ok": m["ok"], "matched": m["matched"], "estimated": m["estimated"], "total": m["total"]}

    try:
        return JSONResponse(await asyncio.to_thread(_run))
    except (align_mod.AlignError, annotate_mod.NoPack) as e:
        return _align_error(e)


@app.post("/api/books/{book_id}/alignment/generate", response_model=schemas.AlignmentGenerateResponse)
async def generate_alignment(book_id: str) -> JSONResponse:
    """Queue a SubPlz run (`subplz_cmd`) on the book's linked local audiobook.
    Not part of the book's chain: hours of Whisper, so only on request. Idempotent
    while one is queued or running; poll `GET /api/books/{id}/status` (`alignment`).
    409 `{reason}`: `subplz_not_configured`, `no_audiobook`, or the pack's
    `drm`/`not_ja`/`no_text`."""
    book = _require_book(book_id)
    problem = align_jobs_mod.command_problem(cfg)
    if problem:
        return JSONResponse({"reason": "subplz_not_configured", "detail": problem}, status_code=409)
    ab_id, files = _book_audiobook(book)
    if not ab_id:
        return _align_error(align_mod.AlignError("no_audiobook", "link a local audiobook first"))
    try:  # refuse a book with no sentences now, not after the first hour of audio
        await asyncio.to_thread(annotate_mod.sentence_texts, cfg, book)
    except annotate_mod.NoPack as e:
        return _align_error(e)
    root = _safe_audiobook_path(_load_audiobooks()[ab_id])
    audio = [(f, root / f) for f in files] if files else [(None, root)]
    _align_jobs.enqueue(book, ab_id, files, audio)
    return JSONResponse({"status": "queued", "alignment": _book_alignment_state(book)})


@app.delete("/api/books/{book_id}/alignment/generate", response_model=schemas.AlignmentGenerateResponse)
async def cancel_alignment(book_id: str) -> JSONResponse:
    """Stop a queued or running SubPlz run. Files it had finished are kept for a retry."""
    book = _require_book(book_id)
    if not _align_jobs.cancel(book_id):
        raise HTTPException(status_code=404, detail="No alignment run to cancel")
    return JSONResponse({"status": "cancelled", "alignment": _book_alignment_state(book)})


@app.delete("/api/books/{book_id}/alignment")
async def delete_alignment(book_id: str, kind: str | None = None) -> JSONResponse:
    """Remove the book's SRTs and map, or with `?kind=` only that kind's SRTs (the map
    is then rebuilt from what is left: removing a transcript goes back to the synced SRT)."""
    _require_book(book_id)
    if kind is not None and kind not in align_mod.KINDS:
        return _align_error(align_mod.AlignError("bad_kind", f"kind is one of {', '.join(align_mod.KINDS)}"))
    if kind is None:
        _align_jobs.cancel(book_id)  # a run in flight would write the alignment straight back
    if not await asyncio.to_thread(align_mod.delete, cfg, book_id, kind):
        raise HTTPException(status_code=404, detail="No alignment for this book")
    return JSONResponse({"status": "deleted", "book_id": book_id})


def _record_anchors(events: list[dict], who: str | None) -> list[dict]:
    """Stores listeners' sync fixes against each book's linked audiobook; the map picks them
    up on its next request. A fix made against an id the audiobook has since left (it gained a
    key, audiobook_rekey.py) is read as the one it went to. Refused: an unknown book (`no_book`),
    a fix made against another audiobook (`audiobook_changed`) or another copy of the book
    (`book_changed`)."""
    rejected: list[dict] = []
    library = _load_library()
    aliases = load_json(cfg.audiobook_aliases, {})
    by_book: dict[str, list[dict]] = {}
    for e in events:
        book = library.get(e["book_id"])
        if not book:
            rejected.append({"id": e["id"], "reason": "no_book"})
            continue
        ab_id = _book_audiobook(book)[0]
        e = {**e, "ab_id": aliases.get(e["ab_id"], e["ab_id"])}
        if ab_id != e["ab_id"]:
            rejected.append({"id": e["id"], "reason": "audiobook_changed"})
            continue
        if e.get("sha256") and e["sha256"] != annotate_mod.book_sha(cfg, book):
            rejected.append({"id": e["id"], "reason": "book_changed"})
            continue
        by_book.setdefault(e["book_id"], []).append(e)
    for book_id, fixes in by_book.items():
        align_mod.add_anchors(cfg, book_id, fixes[0]["ab_id"], fixes, who)
    return rejected


@app.post("/api/reader/events", response_model=schemas.ReaderEventsResponse)
async def api_reader_events(
    payload: schemas.ReaderEventsRequest, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    """Replay the phone's outbox. Safe to send twice: statuses collapse to the
    newest per orth, lookups de-duplicate by id and progress (reading and
    audiobook) is last-write-wins by ts. Any number of status events costs one
    known-list write and one recompute (known.apply), under the lock the mark
    routes use; learning, lookups and progress take _reader_lock instead."""
    if len(payload.events) > 1000:
        raise HTTPException(status_code=400, detail="At most 1000 events per request")

    def _run() -> dict:
        events, rejected = reader_mod.parse_events(payload.events)
        moved, skipped, final = 0, [], {}
        if events["status"]:
            with _index_lock:
                final, res = reader_mod.apply_status(req_cfg, events["status"])
                if res["written"]:
                    _, moved = known_mod.recompute_moved(req_cfg)
                skipped = reader_mod.unsettable(req_cfg, final)
        with _reader_lock:
            if final:
                reader_mod.record_status(req_cfg, final, skipped)
            reader_mod.append_lookups(req_cfg, events["lookup"])
            reader_mod.merge_progress(req_cfg, events["progress"])
            reader_mod.merge_audio_progress(req_cfg, events["audio_progress"])
            reader_mod.merge_bookmarks(req_cfg, events["bookmark"])
            tag = reader_mod.etag(req_cfg)
        rejected += _record_anchors(events["sync_anchor"], req_cfg.active_user)
        return {"status": "ok", "skipped": skipped, "rejected": rejected,
                "etag": tag, "books_moved": moved}

    return JSONResponse(await asyncio.to_thread(_run))


@app.put("/api/reader/anki-note-type")
async def api_reader_anki_note_type_put(
    payload: schemas.AnkiNoteTypeRequest, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    """Keep the note type the phone's Settings › Anki describes, to download from /profile as
    a .apkg for desktop Anki (AnkiMobile can't create a note type)."""
    def _run() -> dict:
        with _reader_lock:
            return anki_notetype_mod.save(req_cfg, payload)

    try:
        data = await asyncio.to_thread(_run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"status": "ok", "name": data["name"], "updated_at": data["updated_at"]})


@app.get("/api/profile/anki-note-type")
def api_profile_anki_note_type(req_cfg: Config = Depends(_request_cfg)) -> JSONResponse:
    """What the phone sent: name, field names and when, or 404 when it never has."""
    data = anki_notetype_mod.load(req_cfg)
    if not data:
        raise HTTPException(status_code=404, detail="No note type has been sent from the app yet")
    return JSONResponse({"name": data["name"], "fields": [f["name"] for f in data["fields"]],
                         "updated_at": data["updated_at"]}, headers={"Cache-Control": "no-cache"})


@app.get("/profile/anki-note-type.apkg")
def profile_anki_note_type_apkg(req_cfg: Config = Depends(_request_cfg)) -> Response:
    data = anki_notetype_mod.load(req_cfg)
    if not data:
        raise HTTPException(status_code=404, detail="No note type has been sent from the app yet")
    return Response(
        anki_notetype_mod.build_apkg(data), media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="hondana-note-type.apkg"',
                 "Cache-Control": "no-cache"})


@app.get("/api/setup")
async def api_setup(req_cfg: Config = Depends(_request_cfg)) -> JSONResponse:
    """The first-run checklist behind `/`'s "Getting started" card: each step's state
    for the requesting profile, and `complete` once every required one is done. Reads existing
    indices only; takes no lock."""
    out = await asyncio.to_thread(setup_status_mod.summary, req_cfg, _load_library())
    return JSONResponse(out, headers={"Cache-Control": "no-cache"})


@app.get("/api/connect")
def api_connect(request: Request, req_cfg: Config = Depends(_request_cfg)) -> JSONResponse:
    """What Hondana for iPhone needs to connect to this server: the address to
    type (this request's own Host, so it is the one that works on this network), the
    `hondana://connect` link the QR code encodes, and the profile the link selects.
    `lan_substituted` is true when the page was opened as localhost and the address was
    swapped for the machine's LAN one; `hint` says why the address won't work from a
    phone, when that is knowable (a container's own address). `public_url` overrides."""
    url, substituted = discovery_mod.connect_url(request.url.scheme, request.headers.get("host") or "", cfg)
    return JSONResponse({
        "url": url,
        "profile": req_cfg.active_user,
        "link": discovery_mod.connect_link(url, req_cfg.active_user),
        "lan_substituted": substituted,
        "hint": discovery_mod.connect_hint(url, cfg),
    }, headers={"Cache-Control": "no-cache"})


@app.get("/api/connect/qr.svg")
def api_connect_qr(request: Request, req_cfg: Config = Depends(_request_cfg)) -> Response:
    """The connect link as a QR code, for the phone's camera."""
    url, _ = discovery_mod.connect_url(request.url.scheme, request.headers.get("host") or "", cfg)
    svg = discovery_mod.qr_svg(discovery_mod.connect_link(url, req_cfg.active_user))
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/api/reader/dicts")
async def api_reader_dicts() -> JSONResponse:
    """The Yomitan zips in dicts_dir for the phone to import (dicts.py). Reads
    each zip once, then answers from a (mtime_ns, size) cache, so the first call
    after a new zip can take a moment."""
    return JSONResponse({"dicts": await asyncio.to_thread(dicts_mod.manifest, cfg)},
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/reader/dicts/{file}")
async def api_reader_dict_file(file: str) -> FileResponse:
    # Whitelist, like the mp3 `part` check: only a name in the manifest is served.
    path = await asyncio.to_thread(dicts_mod.find, cfg, file)
    if path is None:
        raise HTTPException(status_code=404, detail="No such dictionary")
    # FileResponse answers Range requests (the phone resumes a big zip) and
    # sets an ETag and Last-Modified.
    return FileResponse(path, media_type="application/zip")


@app.post("/api/admin/dicts")
async def api_admin_dict_upload(file: UploadFile = File(...)) -> JSONResponse:
    """Add (or replace, by file name) a Yomitan zip in dicts_dir from /admin. Takes no
    `_index_lock`: it writes only dicts_dir, atomically, and the phone's next pull sees the
    changed `(mtime_ns, size)`."""
    try:
        row = await asyncio.to_thread(dicts_mod.store, cfg, file.filename, file.file)
    except dicts_mod.BadDict as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(row, status_code=201)


@app.delete("/api/admin/dicts/{file}")
async def api_admin_dict_delete(file: str) -> JSONResponse:
    # Same whitelist as the download: only a name in the manifest can be removed.
    if not await asyncio.to_thread(dicts_mod.remove, cfg, file):
        raise HTTPException(status_code=404, detail="No such dictionary")
    return JSONResponse({"status": "deleted", "file": file})


# ── pruning the catalog ───────────────────────────────────────────────────────
#
# The catalog is a scrape of everything the account ever owned, so a few hundred
# of its rows are books the reader will never read, and deleting a downloaded
# file only demotes a row to "Audible-only" rather than removing it. These
# endpoints maintain a suppression list that build_index() applies at join time.
#
# Hiding is deliberately NOT deletion: data.json is never touched (it is the
# immutable scrape output, and a re-scrape would undo an edit anyway), and the
# local file, if any, is left alone — that is what DELETE /api/audiobooks/{id}
# is for. So a prune is always reversible, and the two actions compose:
# delete the file to reclaim disk, hide the row to stop seeing it.

def _hidden_ids() -> list[str]:
    return list(load_json(cfg.audiobook_hidden, {}).get("ab_ids") or [])


def _ab_ids_arg(payload: schemas.AbIdsRequest) -> list[str]:
    ids = payload.ab_ids
    if not ids:
        raise HTTPException(status_code=400, detail="Provide a non-empty 'ab_ids' list")
    if len(ids) > 2000:
        raise HTTPException(status_code=400, detail="At most 2000 ids per request")
    return ids


def _rebuild_audiobooks() -> None:
    """Full chain: only DELETE needs this. `audiobook_files.json` actually
    changed, so `audiobook_local`/`audiobook_path` need recomputing — that
    means re-running xref, which is O(JP EPUBs x catalog rows) of
    difflib.SequenceMatcher and by far the most expensive stage (~80% of the
    chain). Hide/unhide use `_rebuild_audiobooks_light()` instead: they touch
    only `audiobook_hidden.json`, which xref never reads, so re-running it
    would recompute the exact same `audiobook_links.json` every time."""
    audiobook_scanner_mod.build_index(cfg)
    xref_mod.xref(cfg)
    build_mod.build(cfg)


def _rebuild_audiobooks_light() -> None:
    """The chain hide/unhide need: build_index() re-applies the suppression
    list to drop/restore rows in audiobook_library.json, build() re-joins it
    into the combined index. No xref — see `_rebuild_audiobooks()`."""
    audiobook_scanner_mod.build_index(cfg)
    build_mod.build(cfg)


@app.get("/api/audiobooks/hidden", response_model=schemas.HiddenAudiobooksResponse)
def audiobooks_hidden() -> dict:
    # Hidden entries are gone from the joined index by definition, so their
    # titles come back from the catalog itself.
    catalog = audiobook_catalog_mod.cached(cfg)
    out = []
    for ab_id in _hidden_ids():
        ab = catalog.get(ab_id) or {}
        out.append({
            "ab_id": ab_id,
            "title": ab.get("title"),
            "author": ab.get("author"),
            "series": ab.get("series"),
            "in_catalog": ab_id in catalog,
        })
    out.sort(key=lambda e: (e["author"] or "", e["series"] or "", e["title"] or ""))
    return {"count": len(out), "hidden": out}


@app.post("/api/audiobooks/hide", response_model=schemas.HideResponse)
async def audiobooks_hide(payload: schemas.AbIdsRequest) -> JSONResponse:
    ids = _ab_ids_arg(payload)

    def _run() -> dict:
        with _index_lock:
            current = _hidden_ids()
            added = [i for i in ids if i not in set(current)]
            if added:
                save_json(cfg.audiobook_hidden, {"ab_ids": current + added})
                _rebuild_audiobooks_light()
        return {"status": "ok", "hidden": len(added), "total": len(current) + len(added)}

    return JSONResponse(await asyncio.to_thread(_run))


@app.post("/api/audiobooks/unhide", response_model=schemas.UnhideResponse)
async def audiobooks_unhide(payload: schemas.AbIdsRequest) -> JSONResponse:
    ids = _ab_ids_arg(payload)

    def _run() -> dict:
        with _index_lock:
            current = _hidden_ids()
            drop = set(ids)
            kept = [i for i in current if i not in drop]
            removed = len(current) - len(kept)
            if removed:
                save_json(cfg.audiobook_hidden, {"ab_ids": kept})
                _rebuild_audiobooks_light()
        return {"status": "ok", "restored": removed, "total": len(kept)}

    return JSONResponse(await asyncio.to_thread(_run))


# NOTE: declared before /download/{book_id} so the literal segment wins.
# A track's type, for players that go by it (a whole file is octet-stream, below).
_AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
                ".flac": "audio/flac", ".opus": "audio/ogg", ".ogg": "audio/ogg"}


def _moved_audiobook(ab_id: str, request: Request, prefix: str) -> RedirectResponse | None:
    """A redirect to where an old ab_id went (audiobook_rekey), for links and
    phone downloads made before it gained a key."""
    new = load_json(cfg.audiobook_aliases, {}).get(ab_id)
    if not new or new not in _load_audiobooks():
        return None
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"{prefix}{urllib.parse.quote(new)}{query}", status_code=307)


@app.get("/download/audiobook/{ab_id}")
def download_audiobook(ab_id: str, request: Request, part: str | None = None):
    index = _load_audiobooks()
    entry = index.get(ab_id)
    if not entry:
        moved = _moved_audiobook(ab_id, request, "/download/audiobook/")
        if moved:
            return moved
    if not entry or not entry.get("local"):
        raise HTTPException(status_code=404, detail="Audiobook not found")
    path = _safe_audiobook_path(entry)
    if entry.get("files"):
        files = entry["files"]
        if part is None:
            return JSONResponse({"ab_id": ab_id, "files": files})
        if part not in files:  # whitelist: no traversal possible
            raise HTTPException(status_code=404, detail="No such track")
        path = path / part
        media_type = _AUDIO_TYPES.get(Path(part).suffix.lower(), "application/octet-stream")
        filename = part
    else:
        # Use octet-stream, not audio/mp4: Safari/iOS derives the saved
        # extension from the MIME type (audio/mp4 -> .mp4) and appends it,
        # producing "Title.m4b.mp4". octet-stream has no preferred extension,
        # so Safari honors the Content-Disposition filename (.m4b, .flac…) verbatim.
        media_type, filename = "application/octet-stream", path.name
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing (volume unplugged?)")
    return FileResponse(path, media_type=media_type, filename=filename)


@app.get("/download/{book_id}")
def download(book_id: str) -> FileResponse:
    library = _load_library()
    book = library.get(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    path = _safe_epub_path(book["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")
    filename = Path(book["path"]).name
    return FileResponse(
        path,
        media_type="application/epub+zip",
        filename=filename,
    )


def _duplicate_response(dup: incoming_mod.DuplicateEpub) -> JSONResponse:
    # 409 so /add can offer "Open it" / "Add anyway" instead of a bare error.
    return JSONResponse(status_code=409, content={
        "status": "duplicate",
        "detail": f"Already in your library ({dup.reason}): {dup.title}",
        "existing": {"book_id": dup.book_id, "title": dup.title, "reason": dup.reason},
    })


class _SavedNotIndexed(Exception):
    """An EPUB is in the library folder but the scan or build after it failed."""


def _store_epub(content: bytes, filename: str | None, language: str, force: bool) -> Path:
    """File one incoming EPUB and index it, under the index lock. Raises what
    `incoming.store_incoming_epub` raises, or `_SavedNotIndexed` once the file is on disk
    but the index doesn't know about it yet."""
    with _index_lock:
        save_path = incoming_mod.store_incoming_epub(
            cfg, load_json(cfg.library_index, {}), content, filename, language, force)
        try:
            scanner_mod.scan(cfg)
            build_mod.build(cfg)
        except Exception as exc:
            traceback.print_exc()
            raise _SavedNotIndexed(str(exc)) from exc
    return save_path


def _add_incoming(content: bytes, filename: str | None, language: str, force: bool,
                  defer: bool = False) -> JSONResponse:
    """File one incoming EPUB (upload or acquire), index it, queue its pipeline —
    or, for `defer` (a file in the middle of a multi-file batch), hold it until
    POST /api/books/analyze-batch so the whole batch runs as one pass."""
    try:
        save_path = _store_epub(content, filename, language, force)
    except incoming_mod.DuplicateEpub as dup:
        return _duplicate_response(dup)
    except _SavedNotIndexed as exc:
        # Report that as a failure the user can act on, rather than letting the exception
        # escape as a bare 500 the /add UI can only guess at.
        raise HTTPException(status_code=500, detail=f"Saved, but indexing failed: {exc}")
    except (incoming_mod.UnsafeFilename, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {exc}")

    rel = save_path.relative_to(cfg.epub_dir.resolve()).as_posix()
    bid = book_id_for(rel, save_path.stat().st_size)
    book = _load_library().get(bid) or {}
    # analyze, pair, link, build — without the tab (a held batch is released by
    # the client's analyze-batch call, or by BookJobs.DEFER_TTL if it never comes)
    (_book_jobs.defer if defer else _book_jobs.enqueue)(bid)
    return JSONResponse({
        "status": "ok",
        "book_id": bid,
        "title": book.get("title") or save_path.name,
        "language": book.get("language") or language,
    })


def _fetch_epub(url: str) -> tuple[bytes, str | None]:
    """Download `url`, enforcing the size cap while streaming. Raises HTTPException."""
    parsed = urllib.parse.urlparse(url)
    try:
        with req_lib.get(url, timeout=30, allow_redirects=True, stream=True) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()
            if not (parsed.path.lower().endswith(".epub") or "epub" in ct
                    or "octet-stream" in ct or "zip" in ct):
                raise HTTPException(
                    status_code=400,
                    detail=f"URL does not appear to be an EPUB file (content-type: {ct or 'unknown'})")

            limit_msg = "File exceeds the 100 MB limit"
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > incoming_mod.MAX_BYTES:
                raise HTTPException(status_code=400, detail=limit_msg)
            chunks, size = [], 0
            for chunk in resp.iter_content(1 << 20):
                size += len(chunk)
                if size > incoming_mod.MAX_BYTES:
                    raise HTTPException(status_code=400, detail=limit_msg)
                chunks.append(chunk)
            content = b"".join(chunks)
            cd = resp.headers.get("content-disposition", "")
    except req_lib.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Download failed: {exc}")

    # Filename from Content-Disposition, else the URL path.
    filename: str | None = None
    m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';\r\n]+)", cd, re.IGNORECASE) if cd else None
    if m:
        filename = urllib.parse.unquote(m.group(1).strip().strip("\"'"))
    return content, filename or urllib.parse.unquote(Path(parsed.path).name)


@app.post("/api/acquire", response_model=schemas.AddedBookResponse,
          responses={409: {"model": schemas.DuplicateBookResponse}})
async def acquire_book(payload: schemas.AcquireRequest) -> JSONResponse:
    url = payload.url.strip()
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must use http or https")

    def _run() -> JSONResponse:
        content, filename = _fetch_epub(url)
        return _add_incoming(content, filename, payload.language, payload.force)

    return await asyncio.to_thread(_run)


@app.post("/api/upload", response_model=schemas.AddedBookResponse,
          responses={409: {"model": schemas.DuplicateBookResponse}})
async def upload_book(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    force: bool = Form(False),
    defer: bool = Form(False),
) -> JSONResponse:
    if not (file.filename or "").lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="Only .epub files are accepted")

    content = await file.read()
    return await asyncio.to_thread(_add_incoming, content, file.filename, language, force, defer)


# The per-book pipeline (analyze → enrich → pair → link → build; see
# book_jobs.py). Upload and acquire enqueue it themselves, so a book gets its
# difficulty numbers, Natively level, pairing and audiobook link even if /add is
# closed mid-way. This endpoint is the manual trigger — the book page's "Analyze
# now" — and is idempotent while a job for the book is queued or running. State
# is polled from GET /api/books/{id}/status.

@app.post("/api/books/{book_id}/analyze", response_model=schemas.AnalyzeQueuedResponse)
async def analyze_book(book_id: str) -> JSONResponse:
    book = _require_book(book_id)

    # Japanese (fugashi) and English (spaCy) both have a vocabulary pipeline;
    # any other language is a success with nothing to report, not a failure.
    if book.get("language") not in pipeline_mod.ANALYZABLE:
        return JSONResponse({"status": "skipped", "reason": "unsupported_language", "job": None})
    return JSONResponse({"status": "queued", "reason": None, "job": _book_jobs.enqueue(book_id)})


@app.post("/api/books/analyze-batch", response_model=schemas.AnalyzeBatchResponse)
async def analyze_batch(payload: schemas.AnalyzeBatchRequest) -> JSONResponse:
    """End of a multi-file upload: queue every deferred book (plus any ids named)
    as ONE pipeline pass. Unknown ids are skipped."""
    library = _load_library()
    ids = [b for b in dict.fromkeys(payload.book_ids) if b in library]
    jobs = await asyncio.to_thread(_book_jobs.release, ids)
    return JSONResponse({"status": "queued", "jobs": jobs})


@app.get("/api/books/{book_id}/status", response_model=schemas.BookStatusResponse)
def book_status(book_id: str, req_cfg: Config = Depends(_request_cfg)) -> dict:
    """Where the book's chain is, plus the analysis result as soon as `analyze` has written it:
    `vocab_status` reads the vocab index (the served `vocab` only arrives with `build`, the
    chain's fifth step) and `known` is this profile's reading-load entry, so `/add` can show
    the verdict while the Natively lookup and the rest are still running."""
    book = _require_book(book_id)
    entry = _cached_json(cfg.vocab_index, {}).get(book_id) or book.get("vocab") or {}
    return {
        "job": _book_jobs.status(book_id),
        "analyzable": book.get("language") in pipeline_mod.ANALYZABLE,
        "vocab_status": entry.get("status"),
        "known": _load_known_stats(req_cfg).get(book_id),
        "alignment": _book_alignment_state(book),
    }


# Genre for EPUBs. The vocabulary is the audiobook catalog's own (data.json plus
# the rows pulled by ASIN, and EPUB genres looked up by ISBN), so a book and its audiobook are always described
# with the same word and one genre filter could cover both — it is derived, most
# common first, so a genre Audible introduces is selectable without a code change.
# エロ is deliberately absent: those titles are on their way out of the collection,
# so offering the label would only create work.
EXCLUDED_GENRES = {"エロ"}


def _genres() -> list[str]:
    counts = Counter(ab.get("genre") for ab in audiobook_catalog_mod.cached(cfg).values())
    counts.update(e.get("genre") for e in _cached_json(cfg.epub_catalog_extra, {}).values())
    return [g for g, _ in counts.most_common() if g and g not in EXCLUDED_GENRES]


@app.get("/api/genres", response_model=schemas.GenreListResponse)
def genre_list() -> dict:
    return {"genres": _genres()}


def _merge_overrides(path: Path, edits: dict) -> int:
    """Apply {book_id: value} to one overrides file in a single write (a falsy
    value drops the entry); returns how many entries actually changed."""
    data = load_json(path, {})
    changed = 0
    for book_id, value in edits.items():
        if value:
            if data.get(book_id) != value:
                data[book_id] = value
                changed += 1
        elif book_id in data:
            del data[book_id]
            changed += 1
    if changed:
        save_json(path, data)
    return changed


def _set_override(path: Path, book_id: str, value) -> bool:
    """Set (or, for a falsy `value`, drop) one book's entry; True if it changed."""
    return bool(_merge_overrides(path, {book_id: value}))


def _series_value(name: str, order: str) -> dict:
    """A series override. An empty name is an explicit "standalone" pin, not a
    removal: dropping it would let build() re-derive the same series from
    calibre/audiobook/title/pair heuristics and the edit would appear to do
    nothing."""
    return {"series_name": name or None, "series_order": (order or None) if name else None}


async def _save_override(book_id: str, path: Path, value) -> None:
    """One override change for one book: under the index lock, write it and
    rebuild (skipped when nothing changed). The Manage-mode endpoints below are
    this and a validation; `set_book_details` batches several fields itself."""
    def _run() -> None:
        with _index_lock:
            if _set_override(path, book_id, value):
                build_mod.build(cfg)

    await asyncio.to_thread(_run)


def _require_book(book_id: str) -> dict:
    """404 unless `book_id` is in the library; returns its library entry."""
    book = _load_library().get(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Unknown book")
    return book


@app.post("/api/books/{book_id}/genre", response_model=schemas.GenreSetResponse)
async def set_book_genre(book_id: str, payload: schemas.GenreRequest) -> JSONResponse:
    genre = payload.genre.strip()
    if genre and genre not in _genres():
        raise HTTPException(status_code=400, detail=f"Unknown genre: {genre}")
    _require_book(book_id)
    # "" clears back to the derived value
    await _save_override(book_id, cfg.genre_overrides, genre)
    return JSONResponse({"status": "ok", "genre": genre or None})


@app.post("/api/books/{book_id}/title", response_model=schemas.TitleSetResponse)
async def set_book_title(book_id: str, payload: schemas.TitleRequest) -> JSONResponse:
    title = payload.title.strip()
    _require_book(book_id)
    # "" clears back to the derived value
    await _save_override(book_id, cfg.title_overrides, title)
    return JSONResponse({"status": "ok", "title": title or None})


@app.post("/api/books/{book_id}/series", response_model=schemas.SeriesSetResponse)
async def set_book_series(book_id: str, payload: schemas.SeriesRequest) -> JSONResponse:
    series_name = payload.series_name.strip()
    series_order = payload.series_order.strip()
    _require_book(book_id)
    await _save_override(book_id, cfg.series_overrides, _series_value(series_name, series_order))
    return JSONResponse({"status": "ok", "series_name": series_name or None, "series_order": series_order or None})


def _require_books(book_ids) -> None:
    missing = [b for b in book_ids if b not in _load_library()]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown book: {missing[0]}"
                            + (f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""))


# Series-level edits: each writes the overrides file once and builds once,
# however many volumes it touches — the per-book endpoints above would run a full
# build() (plus the search-index refresh) per volume.

@app.post("/api/books/genre", response_model=schemas.BulkGenreResponse)
async def set_books_genre(payload: schemas.BulkGenreRequest) -> JSONResponse:
    genre = payload.genre.strip()
    if genre and genre not in _genres():
        raise HTTPException(status_code=400, detail=f"Unknown genre: {genre}")
    ids = list(dict.fromkeys(payload.book_ids))
    _require_books(ids)

    def _run() -> int:
        with _index_lock:
            n = _merge_overrides(cfg.genre_overrides, {b: genre for b in ids})
            if n:
                build_mod.build(cfg)
            return n

    n = await asyncio.to_thread(_run)
    return JSONResponse({"status": "ok", "genre": genre or None, "changed": n})


@app.post("/api/books/series", response_model=schemas.BulkSeriesResponse)
async def set_books_series(payload: schemas.BulkSeriesRequest) -> JSONResponse:
    """Assign series name/order to several books at once: add a book to a series,
    renumber a series, or (`reset`) hand a book back to the automatic series."""
    edits = {}
    for a in payload.assignments:
        edits[a.book_id] = None if a.reset else _series_value(a.series_name.strip(), a.series_order.strip())
    _require_books(edits)

    def _run() -> int:
        with _index_lock:
            n = _merge_overrides(cfg.series_overrides, edits)
            if n:
                build_mod.build(cfg)
            return n

    return JSONResponse({"status": "ok", "changed": await asyncio.to_thread(_run)})


@app.post("/api/series/rename", response_model=schemas.SeriesRenameResponse)
async def rename_series(payload: schemas.SeriesRenameRequest) -> JSONResponse:
    """Rename a series (or merge it into another by giving that one's name): every
    volume gets an override carrying its current order, so the result doesn't
    depend on which heuristic put the volumes together. Pair-inherited
    translations are skipped — they follow their partner to the new name."""
    old, new = payload.from_name.strip(), payload.to_name.strip()
    if not old or not new:
        raise HTTPException(status_code=400, detail="Both series names are required")

    def _run() -> int:
        with _index_lock:
            members = {bid: b for bid, b in load_json(cfg.library_index, {}).items()
                       if b.get("series_name") == old and b.get("series_source") != "pair"}
            if not members:
                raise HTTPException(status_code=404, detail="Unknown series")
            if old != new:
                _merge_overrides(cfg.series_overrides, {
                    bid: _series_value(new, b.get("series_order") or "") for bid, b in members.items()})
                build_mod.build(cfg)
            return len(members)

    return JSONResponse({"status": "ok", "series_name": new, "renamed": await asyncio.to_thread(_run)})


# ── Edit details: one request corrects any combination of fields ─────────────
# The older /genre, /title and /series endpoints (Manage mode on /) each save
# and rebuild on their own; the book page and /add review card edit several
# fields at once, so they share this one — a single save, a single build.

LANGUAGES = ("ja", "en", "other")


def _clean(v: str | None) -> str:
    return (v or "").strip()


def _book_details_state(book_id: str) -> dict:
    book = _load_library().get(book_id) or {}
    catalog = audiobook_catalog_mod.cached(cfg)
    link = book.get("audiobook") or {}
    auto_genre = (catalog.get(link.get("audible_isbn")) or {}).get("genre") or None
    raw_title = book.get("raw_title") or book.get("title")
    series_ov = load_json(cfg.series_overrides, {})
    overridden = [
        field for field, path in (
            ("title", cfg.title_overrides), ("author", cfg.author_overrides),
            ("language", cfg.language_overrides), ("genre", cfg.genre_overrides),
            ("series", cfg.series_overrides),
        ) if book_id in load_json(path, {})
    ]
    names = sorted({b["series_name"] for b in _load_library().values() if b.get("series_name")})
    sug = None
    if not book.get("series_name") and book_id not in series_ov:
        hit = build_mod.suggest_series(book.get("title") or "", names)
        if hit:
            sug = {"series_name": hit[0], "series_order": hit[1]}
    return {
        "values": {
            "title": book.get("title"), "author": book.get("author"),
            "language": book.get("language"), "genre": book.get("genre"),
            "series_name": book.get("series_name"), "series_order": book.get("series_order"),
        },
        "auto": {
            "title": textnorm.display_title(raw_title) if raw_title else raw_title,
            "author": textnorm.clean_author(book["raw_author"]) if "raw_author" in book else book.get("author"),
            "language": book["raw_language"] if "raw_language" in book else book.get("language"),
            "genre": auto_genre,
        },
        "overridden": overridden,
        "series_names": names,
        "suggestion": sug,
        "cover": {"has": bool(book.get("cover")), "overridden": book_id in load_json(cfg.cover_overrides, {}),
                  "v": book.get("cover_v")},
    }


@app.get("/api/books/{book_id}/details", response_model=schemas.BookDetailsState)
def get_book_details(book_id: str) -> dict:
    _require_book(book_id)
    return _book_details_state(book_id)


@app.post("/api/books/{book_id}/details", response_model=schemas.BookDetailsResponse)
async def set_book_details(book_id: str, payload: schemas.BookDetailsRequest) -> JSONResponse:
    sent = payload.model_fields_set
    genre, language = _clean(payload.genre), _clean(payload.language)
    if genre and genre not in _genres():
        raise HTTPException(status_code=400, detail=f"Unknown genre: {genre}")
    if language and language not in LANGUAGES:
        raise HTTPException(status_code=400, detail=f"Unknown language: {language}")
    _require_book(book_id)

    def _run() -> dict:
        job = None
        with _index_lock:
            before = (load_json(cfg.library_index, {}).get(book_id) or {}).get("language")
            changed = False
            for field, path, value in (
                ("title", cfg.title_overrides, _clean(payload.title)),
                ("author", cfg.author_overrides, _clean(payload.author)),
                ("language", cfg.language_overrides, language),
                ("genre", cfg.genre_overrides, genre),
            ):
                if field in sent:
                    changed |= _set_override(path, book_id, value)
            if "series" in sent:
                if payload.series is None:
                    changed |= _set_override(cfg.series_overrides, book_id, None)
                else:
                    changed |= _set_override(cfg.series_overrides, book_id, _series_value(
                        _clean(payload.series.name), _clean(payload.series.order)))
            if changed:
                build_mod.build(cfg)
                after = (load_json(cfg.library_index, {}).get(book_id) or {}).get("language")
                if after != before:
                    # Analysis, Natively level, pairing and audiobook link are all
                    # language-specific: drop them (curated state stays), refresh
                    # everyone's stats since a read book's exposure came from this
                    # book's wordlist, and let the per-book chain redo the rest.
                    purge_mod.purge_book(cfg, book_id)
                    build_mod.build(cfg)
                    known_mod.recompute_all(cfg)
                    job = _book_jobs.enqueue(book_id)
        return {"status": "ok", "job": job, **_book_details_state(book_id)}

    return JSONResponse(await asyncio.to_thread(_run))


@app.post("/api/books/{book_id}/cover", response_model=schemas.BookDetailsResponse)
async def set_book_cover(book_id: str, file: UploadFile = File(...)) -> JSONResponse:
    """Replace the book's cover with an uploaded image. Survives rescans."""
    _require_book(book_id)
    raw = await file.read(custom_cover_mod.MAX_BYTES + 1)

    def _run() -> dict:
        with _index_lock:
            custom_cover_mod.store(cfg, book_id, raw)
            build_mod.build(cfg)
        return {"status": "ok", "job": None, **_book_details_state(book_id)}

    try:
        return JSONResponse(await asyncio.to_thread(_run))
    except custom_cover_mod.InvalidCover as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/books/{book_id}/cover", response_model=schemas.BookDetailsResponse)
async def reset_book_cover(book_id: str) -> JSONResponse:
    """Back to the EPUB's own cover."""
    _require_book(book_id)

    def _run() -> dict:
        with _index_lock:
            if custom_cover_mod.clear(cfg, book_id):
                build_mod.build(cfg)
        return {"status": "ok", "job": None, **_book_details_state(book_id)}

    return JSONResponse(await asyncio.to_thread(_run))


# ── Audiobook details ────────────────────────────────────────────────────────
# The same idea as /api/books/{id}/details for an audiobook's catalog row: one
# Save sends every changed field, kept in audiobook_metadata_overrides.json over
# whatever the catalog, the tags or the file name said (audiobook_details.py).
# One request = one rebuild of the audiobook index, the links and the library.

def _require_audiobook(ab_id: str) -> dict:
    st = audiobook_details_mod.state(cfg, ab_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Unknown audiobook")
    return st


@app.get("/api/audiobooks/{ab_id}/details", response_model=schemas.AudiobookDetailsState)
def get_audiobook_details(ab_id: str) -> dict:
    return _require_audiobook(ab_id)


@app.post("/api/audiobooks/{ab_id}/details", response_model=schemas.AudiobookDetailsResponse)
async def set_audiobook_details(ab_id: str, payload: schemas.AudiobookDetailsRequest) -> JSONResponse:
    _require_audiobook(ab_id)
    sent = {f: getattr(payload, f) for f in payload.model_fields_set}

    def _run() -> dict:
        with _index_lock:
            if audiobook_details_mod.apply(cfg, ab_id, sent, _genres()):
                audiobook_details_mod.rebuild(cfg)
            return {"status": "ok", **_require_audiobook(ab_id)}

    try:
        return JSONResponse(await asyncio.to_thread(_run))
    except audiobook_details_mod.InvalidDetails as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/audiobooks/{ab_id}/cover", response_model=schemas.AudiobookDetailsResponse)
async def set_audiobook_cover(ab_id: str, file: UploadFile = File(...)) -> JSONResponse:
    """Replace the audiobook's cover with an uploaded image (any row, local or not)."""
    _require_audiobook(ab_id)
    raw = await file.read(custom_cover_mod.MAX_BYTES + 1)

    def _run() -> dict:
        with _index_lock:
            audiobook_details_mod.store_cover(cfg, ab_id, raw)
            audiobook_details_mod.rebuild(cfg)
            return {"status": "ok", **_require_audiobook(ab_id)}

    try:
        return JSONResponse(await asyncio.to_thread(_run))
    except custom_cover_mod.InvalidCover as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/audiobooks/{ab_id}/cover", response_model=schemas.AudiobookDetailsResponse)
async def reset_audiobook_cover(ab_id: str) -> JSONResponse:
    """Back to the catalog's cover, or the file's own."""
    _require_audiobook(ab_id)

    def _run() -> dict:
        with _index_lock:
            if audiobook_details_mod.clear_cover(cfg, ab_id):
                audiobook_details_mod.rebuild(cfg)
            return {"status": "ok", **_require_audiobook(ab_id)}

    return JSONResponse(await asyncio.to_thread(_run))


# ── Audiobook uploads ────────────────────────────────────────────────────────
# A file is sent as sequential PUT chunks, so a dropped connection resumes where it stopped
# (uploads.py); once every byte is in, POST …/complete hands it to the intake queue, which
# files it and indexes it in the background (intake.py). The bytes go to staging inside the
# audiobook folder, so receiving takes no lock; only the indexing takes _index_lock.

def _upload_error(exc: uploads_mod.UploadError) -> JSONResponse:
    return JSONResponse(status_code=exc.status,
                        content={"detail": str(exc), "reason": exc.code, **exc.extra})


def _upload_state(upload_id: str) -> dict:
    """The upload as a client sees it: what was announced, where it stands and, once it is
    queued, how the filing is going. A finished or failed upload has no files left (staging is
    cleared), so its state is the filing job's, held in memory."""
    job = _intake_jobs.status(upload_id)
    try:
        info = uploads_mod.get(cfg, upload_id)
    except uploads_mod.UploadError:
        if job is None:
            raise
        info = {"id": upload_id, "offset": 0}
    if job:
        # The part file is renamed away while it is filed; every byte had arrived.
        return {**info, "offset": info.get("size", 0), "state": job["state"], "step": job["step"],
                "error": job["error"], "reason": job["reason"], "result": job["result"]}
    state = "received" if info["offset"] >= info["size"] else "receiving"
    return {**info, "state": state}


async def _drain(stream) -> None:
    """Read and drop what is left of a refused chunk's body, so the connection answers cleanly
    instead of being reset under a client that is still sending. Takes the very iterator the
    route was reading: Starlette lets a request's body be streamed only once."""
    seen = 0
    try:
        async for data in stream:
            seen += len(data)
            if seen > uploads_mod.MAX_CHUNK:
                break
    except ClientDisconnect:
        pass


@app.post("/api/audiobooks/uploads", response_model=schemas.AudioUploadState, status_code=201)
async def create_audiobook_upload(payload: schemas.AudioUploadRequest) -> JSONResponse:
    """Announce a file; the answer says where to start (`offset` 0) and how big a chunk to send."""
    try:
        info = await asyncio.to_thread(uploads_mod.create, cfg, payload.filename, payload.size)
        return JSONResponse(status_code=201, content=_upload_state(info["id"]))
    except uploads_mod.UploadError as exc:
        return _upload_error(exc)


@app.get("/api/audiobooks/uploads/{upload_id}", response_model=schemas.AudioUploadState)
async def get_audiobook_upload(upload_id: str) -> JSONResponse:
    """Where the upload stands: `offset` is where a client that lost its place resumes."""
    try:
        return JSONResponse(await asyncio.to_thread(_upload_state, upload_id))
    except uploads_mod.UploadError as exc:
        return _upload_error(exc)


@app.put("/api/audiobooks/uploads/{upload_id}", response_model=schemas.AudioUploadState)
async def put_audiobook_chunk(upload_id: str, request: Request) -> JSONResponse:
    """One chunk, `Content-Range: bytes START-END/TOTAL`, raw body. It has to start at the
    upload's `offset` (409 with the real one otherwise). Bytes are kept as they arrive, so a
    dropped connection costs only what hadn't arrived, and the newest request for an upload
    takes over from an older one that stalled."""
    body = request.stream()
    try:
        if _intake_jobs.status(upload_id):
            raise uploads_mod.UploadError(409, "already_complete", "That upload is already complete")
        start, end, total = uploads_mod.parse_content_range(request.headers.get("content-range"))
        with uploads_mod.chunk(cfg, upload_id, start, end, total) as writer:
            pending = bytearray()
            async for data in body:
                pending += data
                if len(pending) >= 1 << 20:  # write in megabytes, off the event loop
                    await asyncio.to_thread(writer.write, bytes(pending))
                    pending.clear()
            if pending:
                await asyncio.to_thread(writer.write, bytes(pending))
        return JSONResponse(await asyncio.to_thread(_upload_state, upload_id))
    except uploads_mod.UploadError as exc:
        await _drain(body)
        return _upload_error(exc)
    except ClientDisconnect:  # the chunk was undone; nobody is listening for an answer
        return JSONResponse(status_code=499, content={"detail": "Client closed the request"})


@app.post("/api/audiobooks/uploads/{upload_id}/complete", response_model=schemas.AudioUploadState,
          status_code=202)
async def complete_audiobook_upload(upload_id: str) -> JSONResponse:
    """Every byte is in: check it is what its name says and queue it to be filed. Poll
    `GET …/{id}` for the outcome. Idempotent: asking again returns the job's state."""
    try:
        if _intake_jobs.status(upload_id) is None:
            await asyncio.to_thread(uploads_mod.check_complete, cfg, upload_id)
        _intake_jobs.enqueue(upload_id)
        return JSONResponse(status_code=202, content=_upload_state(upload_id))
    except uploads_mod.UploadError as exc:
        return _upload_error(exc)


@app.delete("/api/audiobooks/uploads/{upload_id}")
async def abort_audiobook_upload(upload_id: str) -> JSONResponse:
    """Throw away what has arrived. Refused while the file is being filed."""
    try:
        if _intake_jobs.active(upload_id):
            raise uploads_mod.UploadError(409, "busy", "It is being added to the library now")
        if not await asyncio.to_thread(uploads_mod.discard, cfg, upload_id):
            raise uploads_mod.UploadError(404, "not_found", "No such upload")
    except uploads_mod.UploadError as exc:
        return _upload_error(exc)
    return JSONResponse({"status": "ok"})


# ── Audible search and Match ─────────────────────────────────────────────────
# A local file the scanner can't identify is matched to an Audible book by hand
# (audiobook_match.py): search Audible, pick the book, and the file becomes it —
# progress, alignments and edits follow. Audible is asked outside _index_lock.

def _audible_failure(exc: Exception) -> HTTPException:
    """ValueError is a bad request; an AudibleError is Audible's (or the network's)."""
    return HTTPException(status_code=400 if isinstance(exc, ValueError) else 502, detail=str(exc))


def _match_failure(exc: audiobook_match_mod.MatchError) -> JSONResponse:
    return JSONResponse({"reason": exc.reason, "detail": str(exc)}, status_code=exc.status)


@app.get("/api/audible/search", response_model=schemas.AudibleSearchResponse)
async def audible_search(title: str = "", author: str = "", keywords: str = "") -> dict:
    """Audible books matching a title, author and/or keywords, in the store set by
    `audible.marketplace`. 400 for an empty search, 502 when Audible can't answer."""
    try:
        results = await asyncio.to_thread(audiobook_match_mod.candidates, cfg, title, author, keywords)
    except (ValueError, audible_mod.AudibleError) as exc:
        raise _audible_failure(exc)
    mp = audible_mod.market(cfg.audible_marketplace)
    return {"marketplace": {"code": cfg.audible_marketplace, "label": mp.label}, "results": results}


@app.get("/api/audiobooks/{ab_id}/match", response_model=schemas.AudiobookMatchState)
def get_audiobook_match(ab_id: str) -> dict:
    st = audiobook_match_mod.state(cfg, ab_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Unknown audiobook")
    return st


@app.post("/api/audiobooks/{ab_id}/match", response_model=schemas.AudiobookMatchResponse)
async def set_audiobook_match(ab_id: str, payload: schemas.AudiobookMatchRequest):
    """Make this local file the Audible book `asin` (a pasted link works too). 409
    `{reason}`: `key_in_name`, `same`, `taken`, `not_a_book`, `not_local`; 404
    `not_found` when Audible has no such ASIN; 502 when Audible can't answer."""
    asin = audible_mod.parse_asin(payload.asin)
    if not asin:
        raise HTTPException(status_code=400, detail="That isn't an Audible ASIN or link.")

    def _run() -> dict:
        row = audiobook_match_mod.prepare(cfg, ab_id, asin)  # the network call, no lock
        with _index_lock:
            result = audiobook_match_mod.match(cfg, ab_id, asin, row, rename=payload.rename)
            return {"status": "ok", **result, **_require_audiobook(result["ab_id"])}

    try:
        return JSONResponse(await asyncio.to_thread(_run))
    except audiobook_match_mod.MatchError as exc:
        return _match_failure(exc)
    except (ValueError, audible_mod.AudibleError) as exc:
        raise _audible_failure(exc)


@app.delete("/api/audiobooks/{ab_id}/match", response_model=schemas.AudiobookMatchResponse)
async def reset_audiobook_match(ab_id: str):
    """Take back a hand match. 409 `not_matched` when the file wasn't matched by hand."""
    def _run() -> dict:
        with _index_lock:
            result = audiobook_match_mod.unmatch(cfg, ab_id)
            return {"status": "ok", **result, **_require_audiobook(result["ab_id"])}

    try:
        return JSONResponse(await asyncio.to_thread(_run))
    except audiobook_match_mod.MatchError as exc:
        return _match_failure(exc)


# ── EPUB ↔ audiobook link correction ─────────────────────────────────────────
# xref picks the audiobook for each EPUB by fuzzy title match; this is the way
# to fix a wrong one (or pin / remove it) without renaming files. Stored in
# audiobook_link_overrides.json, which xref applies last — see audiobook_xref.

def _catalog_entry(isbn: str | None) -> dict | None:
    ab = audiobook_catalog_mod.cached(cfg).get(isbn) if isbn else None
    if not ab:
        return None
    return {"isbn": isbn, "title": ab.get("title"), "author": ab.get("author"),
            "series": ab.get("series"), "order": ab.get("order"),
            "local": bool((_load_audiobooks().get(isbn) or {}).get("local"))}


def _audiobook_link_state(book_id: str) -> dict:
    overrides = load_json(cfg.audiobook_link_overrides, {})
    link = (load_json(cfg.audiobook_links, {})).get(book_id)
    if book_id in overrides and overrides[book_id] is None:
        mode = "none"
    elif link and link.get("override"):
        mode = "manual"
    else:
        mode = "auto"
    return {"mode": mode, "link": link,
            "catalog": _catalog_entry(link["audible_isbn"]) if link else None}


@app.get("/api/books/{book_id}/audiobook-link", response_model=schemas.AudiobookLinkState)
def get_audiobook_link(book_id: str) -> dict:
    _require_book(book_id)
    return _audiobook_link_state(book_id)


@app.get("/api/audiobook-catalog/search", response_model=schemas.AudiobookCandidatesResponse)
async def audiobook_catalog_search(q: str = "", limit: int = 20) -> dict:
    """Catalog entries matching `q`, with enough fields to pick one by eye."""
    q = q.strip()
    limit = max(1, min(limit, 50))
    if not q:
        return {"results": []}
    try:
        ids = await asyncio.to_thread(search_index_mod.search, cfg, q, "catalog", limit)
    except Exception:
        ids = await asyncio.to_thread(_search_local, q, "catalog", limit)
    return {"results": [e for e in map(_catalog_entry, ids) if e]}


@app.post("/api/books/{book_id}/audiobook-link", response_model=schemas.AudiobookLinkState)
async def set_audiobook_link(book_id: str, payload: schemas.AudiobookLinkRequest) -> dict:
    isbn = (payload.isbn or "").strip() or None
    _require_book(book_id)
    if isbn and isbn not in audiobook_catalog_mod.cached(cfg):
        raise HTTPException(status_code=400, detail="Not an audiobook in the catalog")
    return await asyncio.to_thread(_apply_link_override, book_id, isbn, True)


@app.delete("/api/books/{book_id}/audiobook-link", response_model=schemas.AudiobookLinkState)
async def reset_audiobook_link(book_id: str) -> dict:
    """Drop the override and go back to the automatic match."""
    _require_book(book_id)
    return await asyncio.to_thread(_apply_link_override, book_id, None, False)


def _apply_link_override(book_id: str, isbn: str | None, keep: bool) -> dict:
    with _index_lock:
        overrides = load_json(cfg.audiobook_link_overrides, {})
        if keep:
            overrides[book_id] = isbn  # null = explicitly no audiobook
        else:
            overrides.pop(book_id, None)
        save_json(cfg.audiobook_link_overrides, overrides)
        xref_mod.xref_one(cfg, book_id)
        build_mod.build(cfg)
    return _audiobook_link_state(book_id)


# ── reader profiles: per-device active user via a cookie, no auth ────────────
# A profile is just a name + the language being learned. Each device tracks
# its own active profile independently (profiles_mod.COOKIE_NAME, resolved by
# _request_cfg) — the collection and every global index are shared across
# profiles, but known-% is computed per-request from the resolved profile's
# known_stats.json rather than baked into the served index.

@app.get("/api/profiles", response_model=schemas.ProfilesListResponse)
def profiles_list(req_cfg: Config = Depends(_request_cfg)) -> dict:
    data = profiles_mod.list_profiles(cfg)
    data["active"] = req_cfg.active_user  # this device's own choice, not the household default
    return data


@app.post("/api/profiles", response_model=schemas.ProfileCreateResponse)
async def profiles_create(payload: schemas.ProfileCreateRequest) -> JSONResponse:
    name = payload.name.strip()
    language = payload.language or "ja"
    try:
        def _create() -> dict:
            with _profiles_lock:
                return profiles_mod.create(cfg, name, language)

        profile = await asyncio.to_thread(_create)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"status": "ok", "profile": profile})


# ~5 years — "stay on its own profile". Plain HTTP on the LAN, so
# no `secure` flag; nothing reads it client-side, so httponly is safe.
_PROFILE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5


@app.post("/api/profiles/active", response_model=schemas.ProfileActiveResponse)
async def profiles_set_active(payload: schemas.ProfileActiveRequest) -> JSONResponse:
    """Switch *this device's* active profile: validate, persist it as the
    household default for devices with no cookie yet, and cookie this device
    to it directly so it no longer follows that default. known-% is resolved
    per-request (see api_books/api_book), so no index rebuild is needed here."""
    slug = payload.slug.strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    if slug not in {p["slug"] for p in profiles_mod.list_profiles(cfg)["profiles"]}:
        raise HTTPException(status_code=404, detail=f"No such profile: {slug}")

    def _run() -> None:
        with _profiles_lock:
            # A private, request-scoped Config — not the shared singleton —
            # so set_active's mutation of it is harmless.
            profiles_mod.set_active(replace(cfg, active_user=slug), slug)

    await asyncio.to_thread(_run)
    resp = JSONResponse({"status": "ok", "active": slug})
    resp.set_cookie(
        profiles_mod.COOKIE_NAME, slug,
        max_age=_PROFILE_COOKIE_MAX_AGE, httponly=True, samesite="lax",
    )
    return resp


@app.get("/api/profile/read-books")
def api_profile_read_books(req_cfg: Config = Depends(_request_cfg)) -> JSONResponse:
    """The requesting device's profile's read books: `books` is the ones still in
    the shared library (joined for title/author/cover, most-recently-read
    first); `orphans` is the marks for books that are gone (deleted after being
    marked read), each with a `title` when one is on record, else "". The two
    are `known.read_split()`'s halves, so they agree with `books_read`."""
    library = _load_library()
    live, gone = known_mod.read_split(req_cfg, library)
    labels = orphans_mod.titles(cfg, {e["book_id"] for e in gone})
    return JSONResponse({
        "books": [{**library[e["book_id"]], "read_at": e["read_at"]} for e in live],
        "orphans": [{**e, "title": labels.get(e["book_id"], "")} for e in gone],
    })


@app.delete("/api/profile/orphan-reads")
async def api_profile_clear_orphan_reads(req_cfg: Config = Depends(_request_cfg)) -> JSONResponse:
    """Clear this profile's read marks for books no longer in the library."""
    library = _load_library()

    def _run() -> dict:
        with _index_lock:
            ids = known_mod.clear_orphan_reads(req_cfg, library)
            if ids:
                known_mod.recompute(req_cfg)
        return {"status": "ok", "cleared": len(ids), **known_mod.summary(req_cfg, library)}

    return JSONResponse(await asyncio.to_thread(_run))


# ── known words: the user's vocabulary list + per-book known-% ────────────────

@app.get("/api/known-words")
def known_words_status(req_cfg: Config = Depends(_request_cfg)) -> dict:
    # Only the summary. The full list leaves the server only for the profile's own
    # reader, as GET /api/reader/vocab.
    return known_mod.status(req_cfg, _load_library())


@app.post("/api/known-words", response_model=schemas.ImportWordsResponse)
async def known_words_upload(
    file: UploadFile | None = File(None),
    text: str = Form(""),
    fmt: str = Form("auto", alias="format"),
    mode: str = Form("replace"),
    req_cfg: Config = Depends(_request_cfg),
) -> JSONResponse:
    if file is not None:
        raw = await file.read()
        if len(raw) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File exceeds the 5 MB limit")
        content = raw.decode("utf-8", errors="replace")
        source = file.filename or "upload"
    elif text.strip():
        content = text
        source = "paste"
    else:
        raise HTTPException(status_code=400, detail="Provide a file or pasted text")

    lang = profiles_mod.active_language(req_cfg)
    words = known_mod.parse_words(content, fmt=fmt, lang=lang)
    if not words:
        raise HTTPException(status_code=400, detail="No words found in the input")

    def _run() -> dict:
        with _index_lock:
            saved = known_mod.save(req_cfg, words, source=source, mode=mode)
            stats = known_mod.recompute(req_cfg)
        return {
            "status": "ok",
            "imported": len(words),
            "total": saved["count"],
            "books_updated": len(stats),
            "snapshot": saved["snapshot"],
            "marked": len(saved["marked"]),
        }

    return JSONResponse(await asyncio.to_thread(_run))


@app.post("/api/known-words/seed", response_model=schemas.SeedResponse)
async def known_words_seed(
    payload: schemas.SeedRequest = schemas.SeedRequest(),
    req_cfg: Config = Depends(_request_cfg),
) -> JSONResponse:
    """Fast-start baseline for a language with no curated list to lean on yet
    (see known.SEEDABLE_LANGUAGES): replace the known-words list with
    wordfreq's top-N most frequent words, then recompute same as any other
    import.

    Optional body `{"top_n": N}` is what /known's "add the next N words" step
    calls with; omitted (the initial "Seed common words" button) defaults to
    `known.SEED_TOP_N`. `seed_common` clamps against shrinking or exceeding
    `known.SEED_MAX`, so a bad value here can't drop already-seeded words, and
    refuses to run at all against a profile whose list came from a real import
    rather than a previous seed step (400, see known.seed_common).
    """
    lang = profiles_mod.active_language(req_cfg)
    if lang not in known_mod.SEEDABLE_LANGUAGES:
        raise HTTPException(
            status_code=400, detail=f"Seeding is not supported for language: {lang!r}"
        )
    top_n = payload.top_n or known_mod.SEED_TOP_N

    def _run() -> dict:
        with _index_lock:
            saved = known_mod.seed_common(req_cfg, lang, top_n)
            stats = known_mod.recompute(req_cfg)
        return {
            "status": "ok",
            "total": saved["count"],
            "books_updated": len(stats),
            "seeded_top_n": saved["seeded_top_n"],
        }

    try:
        result = await asyncio.to_thread(_run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(result)


@app.get("/api/known-words/seed/preview")
async def known_words_seed_preview(
    top_n: int = 0, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    """Read-only: the words the next seed step up to `top_n` would add, for
    /known's skim-before-apply flow. Cheap (a frequency-list slice, no
    tokenization), so unlike the other known-words endpoints this runs
    directly on the event loop rather than a thread, and takes no admin lock —
    it never writes."""
    lang = profiles_mod.active_language(req_cfg)
    if lang not in known_mod.SEEDABLE_LANGUAGES:
        raise HTTPException(
            status_code=400, detail=f"Seeding is not supported for language: {lang!r}"
        )
    return JSONResponse(known_mod.seed_preview(req_cfg, lang, top_n))


# Marking words known one at a time — off a book's study list, or off the
# library-wide frequency list on /known. A deck export only records what the
# reader chose to drill, so the everyday words it omits (先輩, 弁当, 持つ) are
# counted as unknown everywhere until they are clicked off here.
#
# Each call takes the same full save → recompute path as a bulk import
# (~1 s), cheap enough not to warrant a staging area, and it means the
# numbers on screen are never a step behind the list. The browser batches
# clicks so a run of them costs one pass, not one per word.

def _mark_words(payload: schemas.WordsRequest) -> list[str]:
    words = payload.words
    if not words:
        raise HTTPException(status_code=400, detail="Provide a non-empty 'words' list")
    if len(words) > 1000:
        raise HTTPException(status_code=400, detail="At most 1000 words per request")
    return words


@app.post("/api/known-words/mark", response_model=schemas.MarkWordsResponse)
async def known_words_mark(
    payload: schemas.WordsRequest, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    words = _mark_words(payload)

    def _run() -> dict:
        with _index_lock:
            res = known_mod.mark(req_cfg, words)
            # Every book's numbers are measured against this list, so skip the
            # recompute only when nothing actually changed.
            stats, moved = known_mod.recompute_moved(req_cfg) if res["added"] else ({}, 0)
        return {"status": "ok", "added": len(res["added"]), "total": res["count"],
                "marked": res["marked"], "books_updated": len(stats), "books_moved": moved}

    return JSONResponse(await asyncio.to_thread(_run))


@app.post("/api/known-words/unmark", response_model=schemas.UnmarkWordsResponse)
async def known_words_unmark(
    payload: schemas.WordsRequest, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    """Undo a mark. Only hand-marked words can be removed — anything that came
    from an import is reported back untouched."""
    words = _mark_words(payload)

    def _run() -> dict:
        with _index_lock:
            res = known_mod.unmark(req_cfg, words)
            stats, moved = known_mod.recompute_moved(req_cfg) if res["removed"] else ({}, 0)
        return {"status": "ok", "unmarked": len(res["removed"]),
                "skipped": res["skipped"], "total": res["count"],
                "books_updated": len(stats), "books_moved": moved}

    return JSONResponse(await asyncio.to_thread(_run))


@app.post("/api/known-words/skip", response_model=schemas.SkipWordsResponse)
async def known_words_skip(
    payload: schemas.WordsRequest, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    """Set names and fragments aside as "not a word": free in every book's
    reading load, off the study and review lists, undone by /unskip."""
    words = _mark_words(payload)

    def _run() -> dict:
        with _index_lock:
            res = known_mod.skip(req_cfg, words)
            stats, moved = known_mod.recompute_moved(req_cfg) if res["added"] else ({}, 0)
        return {"status": "ok", "added": len(res["added"]),
                "not_words": res["count"], "books_updated": len(stats), "books_moved": moved}

    return JSONResponse(await asyncio.to_thread(_run))


@app.post("/api/known-words/unskip", response_model=schemas.UnskipWordsResponse)
async def known_words_unskip(
    payload: schemas.WordsRequest, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    words = _mark_words(payload)

    def _run() -> dict:
        with _index_lock:
            res = known_mod.unskip(req_cfg, words)
            stats, moved = known_mod.recompute_moved(req_cfg) if res["removed"] else ({}, 0)
        return {"status": "ok", "removed": len(res["removed"]),
                "not_words": res["count"], "books_updated": len(stats), "books_moved": moved}

    return JSONResponse(await asyncio.to_thread(_run))


@app.get("/api/known-words/frequent")
async def known_words_frequent(
    n: int = 500,
    loan: bool = False,
    after_books: int | None = None,
    after_total: int | None = None,
    after_word: str | None = None,
    req_cfg: Config = Depends(_request_cfg),
) -> JSONResponse:
    """Unknown words ranked by how many books use them. Sweeps every sentence
    index (~0.5 s), so it runs off the event loop. `loan=1` restricts to
    katakana loanwords, applied before the `n` cut. `after_*` is the
    last row's (books, total, word) — returns the next page below it."""
    n = max(1, min(n, 5000))
    after = (
        (after_books, after_total, after_word)
        if after_books is not None and after_total is not None and after_word is not None
        else None
    )
    return JSONResponse(
        await asyncio.to_thread(corpus_mod.frequent_unknown, req_cfg, n, loan, after)
    )


@app.get("/api/known-words/snapshots", response_model=list[schemas.KnownSnapshot])
def known_words_snapshots(req_cfg: Config = Depends(_request_cfg)) -> list[dict]:
    """Lists saved before each replace-import / Clear / restore, newest first."""
    return known_mod.list_snapshots(req_cfg)


@app.post("/api/known-words/restore", response_model=schemas.RestoreWordsResponse)
async def known_words_restore(
    payload: schemas.RestoreWordsRequest, req_cfg: Config = Depends(_request_cfg)
) -> JSONResponse:
    def _run() -> dict:
        with _index_lock:
            try:
                saved = known_mod.restore(req_cfg, payload.id)
            except KeyError:
                raise HTTPException(status_code=404, detail="Unknown snapshot") from None
            stats = known_mod.recompute(req_cfg)
        return {"status": "ok", "total": saved["count"], "books_updated": len(stats)}

    return JSONResponse(await asyncio.to_thread(_run))


@app.delete("/api/known-words", response_model=schemas.StatusResponse)
async def known_words_clear(req_cfg: Config = Depends(_request_cfg)) -> dict:
    def _run() -> None:
        with _index_lock:
            known_mod.clear(req_cfg)
            known_mod.recompute(req_cfg)

    await asyncio.to_thread(_run)
    return {"status": "cleared"}


# ── admin panel: run pipeline stages from the browser (LAN-only) ─────────────
# Same stages as cli.py (pipeline.STAGES). Each stage runs in a daemon thread
# behind a single lock so two runs can't clobber the JSON caches at once; the
# browser fire-and-polls /api/admin/status. The served index is read fresh per request (_load_library),
# so a finished stage is visible on the next /api/books call — no restart.

# id -> (label, description, callable(cfg)), generated from pipeline.STAGES (plus
# "all"). The description is shown as a tooltip on the panel button.
ADMIN_STAGES = {
    s.id: (s.label, s.description, s.run)
    for s in (*pipeline_mod.STAGES, pipeline_mod.ALL)
}
# id -> what /admin groups and flags a stage by (kept apart from the tuple above,
# whose shape the worker unpacks). "all" is its own group.
ADMIN_META = {
    s.id: {"group": s.group, "chips": list(s.chips), "in_chain": s.in_chain}
    for s in pipeline_mod.STAGES
}
ADMIN_META["all"] = {"group": "all", "chips": [], "in_chain": True}

LOG_DIR = cfg.data_dir / "logs"

# _index_lock serializes every writer of data/ — pipeline stage runs (held for
# the whole stage by _admin_worker) and every API endpoint that rebuilds an
# index or writes an override/profile file. _status_lock only guards the small
# _admin_run dict, so /api/admin/status polling never blocks behind a stage.
_index_lock = threading.Lock()
_status_lock = threading.Lock()
# profiles.json / users/<slug>/ writes (create, switch active). Separate from
# _index_lock so switching profile never queues behind a minutes-long stage —
# no pipeline stage writes these files.
_profiles_lock = threading.Lock()
# users/<slug>/ learning.json, lookups.jsonl and reader_progress.json, written by
# POST /api/reader/events. Never held together with _index_lock (the known list
# is written under that one), so the two cannot deadlock.
_reader_lock = threading.Lock()
# Per-book pipeline queue (upload/acquire/"Analyze now"); its worker takes
# _index_lock around each job.
_book_jobs = book_jobs_mod.BookJobs(cfg, lambda: _index_lock)
# Filing finished audiobook uploads: the moves take no lock, the indexing takes _index_lock.
_intake_jobs = intake_mod.IntakeJobs(cfg, lambda: _index_lock)
# SubPlz runs: hours long, so a lane of their own that never takes _index_lock.
_align_jobs = align_jobs_mod.AlignJobs(cfg)


def _inbox_add_epub(content: bytes, filename: str) -> str | None:
    """File an EPUB the inbox found, as Add would: stored and indexed under the index lock,
    then its own pipeline queued. The note, if any, goes in the inbox's recent list."""
    try:
        save_path = _store_epub(content, filename, "auto", False)
    except _SavedNotIndexed as exc:
        return f"saved, but indexing failed ({exc}); scan from Admin to pick it up"
    rel = save_path.relative_to(cfg.epub_dir.resolve()).as_posix()
    _book_jobs.enqueue(book_id_for(rel, save_path.stat().st_size))
    return None


# The folder new books are dropped into (inbox.py): polled while the server runs. EPUBs are
# filed by the watcher itself; audiobooks go through _intake_jobs, behind uploads.
_inbox_watcher = inbox_mod.Watcher(cfg, _intake_jobs, _inbox_add_epub)
_admin_run: dict = {
    "running": False, "stage": None, "label": None,
    "started_at": None, "finished_at": None, "ok": None, "error": None,
    "output": None, "log_file": None, "tail": None,
}


class _Tee(io.TextIOBase):
    """Write to several text streams at once (real stdout + log file + buffer)."""

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, s: str) -> int:
        for st in self._streams:
            try:
                st.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


class _LiveTail(io.TextIOBase):
    """Keep the last few output lines in `_admin_run["tail"]` while a stage runs, so
    /api/admin/status can show progress. Takes only `_status_lock`, never `_index_lock`."""

    LINES = 8

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=self.LINES)
        self._partial = ""

    def write(self, s: str) -> int:
        *done, self._partial = (self._partial + s).split("\n")
        self._lines.extend(line.rstrip() for line in done if line.strip())
        shown = [*self._lines, self._partial.rstrip()] if self._partial.strip() else self._lines
        with _status_lock:
            _admin_run["tail"] = "\n".join(shown)
        return len(s)


def _queue_unanalyzed() -> None:
    """After a scan, run the per-book chain for books that arrived outside /add
    (copied in by hand, synced, moved) and have no analysis yet. Queued as
    one batch; the worker takes `_index_lock` after the scan stage releases it."""
    ids = pipeline_mod.unanalyzed_ids(cfg)
    if ids:
        print(f"Queued the per-book pipeline for {len(ids)} unanalyzed book(s).")
        _book_jobs.enqueue_many(ids)


def _admin_worker(stage: str) -> None:
    label, _, func = ADMIN_STAGES[stage]
    error = None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"{ts}-{stage}.log"
    buf = io.StringIO()
    orig_out, orig_err = sys.stdout, sys.stderr

    # Tee each stage's print() output to the terminal, a per-run log file, and an
    # in-memory buffer we surface in the status report. Only one stage runs at a
    # time (lock), and uvicorn's loggers hold their own stream refs, so this
    # capture sees the stage's output and not unrelated server logs.
    with open(log_path, "w", encoding="utf-8") as logf:
        live = _LiveTail()
        sys.stdout = _Tee(orig_out, logf, buf, live)
        sys.stderr = _Tee(orig_err, logf, buf, live)
        try:
            print(f"=== {label} — started {ts} ===")
            with _index_lock:
                func(cfg)
            if stage == "scan":
                _queue_unanalyzed()
        # Catch BaseException, not just Exception: stages raise SystemExit on bad
        # config (e.g. a missing epub_dir). If that escaped, the thread would die
        # without clearing `running`, wedging the panel on "Running …" forever.
        except BaseException as exc:  # report any failure back to the panel
            error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err

    # Keep the tail of the output for the report (full text is in the log file).
    output = buf.getvalue()
    tail = output[-6000:]
    if len(output) > len(tail):
        tail = "…(truncated; see log file)…\n" + tail

    with _status_lock:
        _admin_run.update(
            running=False, finished_at=time.time(),
            ok=error is None, error=error,
            output=tail, log_file=log_path.name, tail=None,
        )


def _last_runs() -> dict[str, float]:
    """Stage id -> when its newest run log was last written (≈ when it finished).
    The logs outlive a restart, unlike `_admin_run`, so /admin can say when each stage
    last ran. A log is named `<YYYYmmdd-HHMMSS>-<stage>.log`."""
    last: dict[str, float] = {}
    for path in LOG_DIR.glob("*.log"):
        parts = path.stem.split("-", 2)
        if len(parts) != 3:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        last[parts[2]] = max(last.get(parts[2], 0.0), mtime)
    return last


@app.get("/api/admin/stages", response_model=list[schemas.AdminStageInfo])
def admin_stages() -> JSONResponse:
    last = _last_runs()
    return JSONResponse(
        [
            {
                "id": sid, "label": label, "description": desc,
                **ADMIN_META.get(sid, {"group": "index", "chips": [], "in_chain": True}),
                "last_run": last.get(sid),
            }
            for sid, (label, desc, _) in ADMIN_STAGES.items()
        ]
    )


@app.get("/api/admin/alignment-queue", response_model=schemas.AlignmentQueueResponse)
def admin_alignment_queue() -> dict:
    """Japanese books with a local audiobook and no audio sync yet, in title order, with
    the audio's length: the set `cli.py align-remote --missing` works through (a run that is
    queued or in progress here counts as not waiting). `can_generate` says whether this server
    can run SubPlz itself."""
    audiobooks = _load_audiobooks()
    books = []
    for book in sorted(_load_library().values(), key=lambda b: b.get("title_sort") or b.get("title") or ""):
        if book.get("language") != "ja":
            continue
        ab_id = _book_audiobook(book)[0]
        if not ab_id or _book_alignment_state(book)["state"] != "none":
            continue
        entry = audiobooks.get(ab_id) or {}
        books.append({"book_id": book["book_id"], "title": book.get("title"), "author": book.get("author"),
                      "runtime_min": entry.get("runtime_min"), "files": len(entry.get("files") or []) or 1})
    return {"books": books, "total_min": sum(b["runtime_min"] or 0 for b in books),
            "can_generate": align_jobs_mod.command_problem(cfg) is None}


@app.get("/api/admin/status", response_model=schemas.AdminStatusResponse)
def admin_status() -> JSONResponse:
    with _status_lock:
        # `now` lets the page measure elapsed time without trusting its own clock.
        return JSONResponse({**_admin_run, "now": time.time()})


def _start_stage(stage: str) -> bool:
    """Start a stage in the admin worker; False when one is already running."""
    with _status_lock:
        if _admin_run["running"]:
            return False
        _admin_run.update(
            running=True, stage=stage, label=ADMIN_STAGES[stage][0],
            started_at=time.time(), finished_at=None, ok=None, error=None,
            output=None, log_file=None, tail=None,
        )
    threading.Thread(target=_admin_worker, args=(stage,), daemon=True).start()
    return True


@app.post("/api/admin/run/{stage}", response_model=schemas.AdminRunResponse)
def admin_run(stage: str) -> JSONResponse:
    if stage not in ADMIN_STAGES:
        raise HTTPException(status_code=404, detail=f"Unknown stage: {stage}")
    if not _start_stage(stage):
        raise HTTPException(status_code=409, detail=f"'{_admin_run['stage']}' is already running")
    return JSONResponse({"status": "started", "stage": stage})


# ── settings (settings.py; the /settings page) ────────────────────────────────

# How long a settings save waits for a running stage before answering 409.
SETTINGS_LOCK_WAIT = 2.0


def _supervised() -> bool:
    """Something restarts the server when it exits (Docker, systemd, the desktop app)."""
    flag = os.environ.get("HONDANA_SUPERVISED", "").strip().lower()
    return flag in ("1", "true", "yes") or bool(os.environ.get("INVOCATION_ID"))


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("hondana")
    except Exception:
        return "unknown"


def _settings_doc() -> dict:
    """Every GUI setting: its saved value (what a restart would run with), where it
    came from, whether the GUI may change it, and whether it waits for a restart."""
    fresh = settings_mod.load(cfg.config_path)
    fields, pending = [], []
    for key in settings_mod.KEYS:
        if not key.gui:
            continue
        value = getattr(fresh, key.field)
        locked = settings_mod.env_locked(key)
        waiting = key.restart and value != getattr(cfg, key.field)
        if waiting:
            pending.append(key.label)
        secret = key.kind == "secret"
        fields.append({
            "key": key.name, "label": key.label, "group": key.group, "kind": key.kind,
            "help": key.help, "value": "" if secret else settings_mod.to_json(key, value),
            "choices": [list(c) for c in key.choices] if key.choices else None,
            "is_set": bool(value) if secret else None,
            "source": fresh.sources.get(key.field, "default"), "env": locked or key.env,
            "env_locked": bool(locked), "file_only": key.file_only,
            "editable": not key.file_only and not locked,
            "restart": key.restart, "restart_pending": waiting,
        })
    return {
        "fields": fields, "restart_pending": pending, "supervised": _supervised(),
        "config_path": str(cfg.config_path) if cfg.config_path else None,
        "home": str(cfg.home) if cfg.home else None, "data_dir": str(cfg.data_dir),
        "version": _version(),
    }


@app.get("/api/settings")
def settings_get() -> JSONResponse:
    return JSONResponse(_settings_doc(), headers={"Cache-Control": "no-cache"})


@app.put("/api/settings")
def settings_put(payload: schemas.SettingsUpdate) -> JSONResponse:
    """Save settings from the GUI (null = back to the file's value or the default).
    What can apply at once is applied to the running server; a changed library
    folder starts a scan; the rest says it waits for a restart."""
    overlay = dict(settings_mod.read_overlay(cfg.data_dir))
    errors: dict[str, str] = {}
    for name, value in payload.values.items():
        key = settings_mod.BY_NAME.get(name)
        if key is None or not key.gui:
            errors[name] = "Unknown setting"
        elif key.file_only:
            errors[name] = "Can only be set in the config file"
        elif settings_mod.env_locked(key):
            errors[name] = f"Set by the {settings_mod.env_locked(key)} environment variable"
        elif value is None:
            overlay.pop(name, None)
        else:
            try:
                typed = settings_mod.coerce(key, value, cfg.home or cfg.data_dir)
            except settings_mod.SettingError as exc:
                errors[name] = str(exc)
                continue
            problem = settings_mod.validate(key, typed)
            if problem:
                errors[name] = problem
            else:
                overlay[name] = settings_mod.to_json(key, typed)
    if not errors and {"inbox.dir", "epub_dir", "audiobook_dirs"} & payload.values.keys():
        # The inbox must stay apart from the library, whichever of them is changing.
        tried = settings_mod.load(cfg.config_path, overlay=overlay)
        if tried.inbox_dir is not None:
            clash = inbox_mod.overlap(tried.inbox_dir, [tried.epub_dir, *tried.audiobook_dirs])
            if clash:
                errors["inbox.dir"] = clash
    if errors:
        first = next(iter(errors.items()))
        return JSONResponse({"detail": f"{first[0]}: {first[1]}", "errors": errors}, status_code=400)

    if not _index_lock.acquire(timeout=SETTINGS_LOCK_WAIT):
        raise HTTPException(status_code=409, detail="A pipeline stage is running; save again when it finishes.")
    try:
        settings_mod.save_overlay(cfg.data_dir, overlay)
        fresh = settings_mod.load(cfg.config_path)
        applied = []
        for key in settings_mod.KEYS:
            if key.restart or key.name == "data_dir":
                continue
            new = getattr(fresh, key.field)
            if new != getattr(cfg, key.field):
                setattr(cfg, key.field, new)
                applied.append(key.name)
        cfg.sources = fresh.sources
    finally:
        _index_lock.release()

    if "audible.marketplace" in applied:
        threading.Thread(target=_refresh_audible_links, daemon=True).start()
    if "inbox.dir" in applied:
        _inbox_watcher.wake()
    started = None
    if "epub_dir" in applied:
        started = "scan" if _start_stage("scan") else None
    elif "audiobook_dirs" in applied:
        started = "audio-scan" if _start_stage("audio-scan") else None
    doc = _settings_doc()
    return JSONResponse({**doc, "applied": applied, "started": started})


def _refresh_audible_links() -> None:
    """The Audible store changed: the audiobook index and the links carry the
    address of each book's page on its site."""
    with _index_lock:
        audiobook_details_mod.rebuild(cfg)


@app.get("/api/inbox")
def inbox_status() -> JSONResponse:
    """What the inbox is doing, for Settings: whether it is on, what is waiting, what was
    filed or refused lately, and how many items sit in its _failed folder."""
    return JSONResponse(_inbox_watcher.status(), headers={"Cache-Control": "no-cache"})


@app.post("/api/settings/check-path")
def settings_check_path(payload: schemas.CheckPathRequest) -> dict:
    """What is at a folder someone is about to save: there, writable, and how many
    books and audio files it holds (counting stops at 10,000 entries)."""
    p = Path(payload.path).expanduser()
    if not p.is_absolute():
        return {"path": str(p), "exists": False, "is_dir": False, "writable": False,
                "epubs": 0, "audio": 0, "problem": "Use a full path, starting with /"
                if os.name != "nt" else "Use a full path, such as D:\\Books"}
    exists, is_dir = p.exists(), p.is_dir()
    epubs = audio = 0
    if is_dir:
        for i, f in enumerate(p.rglob("*")):
            if i >= 10_000:
                break
            suffix = f.suffix.lower()
            epubs += suffix == ".epub"
            audio += suffix in (".m4b", ".mp3", ".m4a")
    return {"path": str(p), "exists": exists, "is_dir": is_dir,
            "writable": is_dir and os.access(p, os.W_OK), "epubs": epubs, "audio": audio,
            "problem": None if is_dir else ("Not a folder" if exists else "No folder there")}


@app.post("/api/admin/restart", response_model=schemas.StatusResponse)
def admin_restart() -> dict:
    """Exit so the supervisor (Docker, systemd, the desktop app) starts the server again
    with the saved settings. Refused when nothing would restart it."""
    if not _supervised():
        raise HTTPException(status_code=409, detail="Nothing would start Hondana again: restart it yourself.")
    threading.Timer(0.5, os._exit, args=(75,)).start()
    return {"status": "restarting"}
