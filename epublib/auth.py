"""The optional admin password, and the gate in front of every route.

Off until someone sets a password (on the Settings page, or `hondana
admin-password set`), and then only the admin surface needs it: /admin,
/settings, and the writes that change the library or the server. Browsing,
downloads, each reader's own state (read marks, known words) and the iPhone
reader API stay open, so a household and their phones work as before.

- **Password**: scrypt, in `<data_dir>/auth.json` with an `epoch` that every
  change bumps (which signs everyone out) and hashed script tokens.
- **Session**: the `hondana_admin` cookie, `v1.<issued>.<epoch>.<hmac>`, signed
  with `<data_dir>/secret.key`; HttpOnly, SameSite=Strict, 30 days.
- **Script tokens**: `Authorization: Bearer hnd_…` for `align-remote` and the like.
- **Cross-site writes**: an unsafe request whose `Origin` isn't this server is
  refused whether or not a password is set, so a web page elsewhere can't drive
  the open writes either. No `Origin` (the phone, curl) passes.
- **One open write is shared by everyone**: a `sync_anchor` event in
  `POST /api/reader/events` is a listener's fix to a book's audio sync map, kept per
  book and not per profile, so any device that can reach the server can move the sync
  points every profile gets, even with a password set. It stays open because the phone
  carries no token; a wrong fix is withdrawn with another event (`op: clear`) or by
  removing the alignment on the book's page (admin).

Shell access is the recovery path: `hondana admin-password clear`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import HTTPException, Request

from .config import Config
from .index_store import load_json, save_json

COOKIE = "hondana_admin"
SESSION_DAYS = 30
TOKEN_PREFIX = "hnd_"
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
MIN_PASSWORD = 8

# Pages and reads that are admin-only once a password is set (a prefix of the
# route's path template).
ADMIN_READ_PREFIXES = ("/admin", "/settings", "/api/admin/", "/api/settings", "/api/auth/tokens",
                       "/api/audible/", "/api/audiobooks/uploads", "/api/inbox")
# Writes anyone may make even with a password set: the reader's own state, the
# phone's sync, signing in and out, and choosing a profile.
OPEN_WRITES = {
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
    ("POST", "/api/profiles"), ("POST", "/api/profiles/active"),
    ("POST", "/api/books/{book_id}/read"), ("PATCH", "/api/books/{book_id}/read-date"),
    ("POST", "/api/books/{book_id}/analyze"),
    ("DELETE", "/api/profile/orphan-reads"),
}
OPEN_WRITE_PREFIXES = ("/api/reader/", "/api/known-words")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def auth_file(cfg: Config) -> Path:
    return cfg.data_dir / "auth.json"


def _secret_file(cfg: Config) -> Path:
    return cfg.data_dir / "secret.key"


def _private_write(path: Path, doc) -> None:
    save_json(path, doc)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def state(cfg: Config) -> dict:
    """auth.json, read fresh: the CLI may have changed it under a running server."""
    doc = load_json(auth_file(cfg), {})
    return doc if isinstance(doc, dict) else {}


def password_set(cfg: Config) -> bool:
    return bool(state(cfg).get("password"))


def _secret(cfg: Config) -> bytes:
    path = _secret_file(cfg)
    try:
        raw = path.read_text().strip()
        if len(raw) >= 64:
            return bytes.fromhex(raw)
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.hex())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


# ── password ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r),
                             p=int(p), dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def set_password(cfg: Config, password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters")
    if password != password.strip():
        raise ValueError("Don't start or end it with a space")
    doc = state(cfg)
    doc.update(schema=1, password=hash_password(password), epoch=int(doc.get("epoch", 0)) + 1)
    _private_write(auth_file(cfg), doc)


def clear_password(cfg: Config) -> None:
    doc = state(cfg)
    doc.update(schema=1, password=None, epoch=int(doc.get("epoch", 0)) + 1)
    _private_write(auth_file(cfg), doc)


# ── sessions ──────────────────────────────────────────────────────────────────

def _sign(cfg: Config, payload: str) -> str:
    return hmac.new(_secret(cfg), payload.encode(), hashlib.sha256).hexdigest()


def make_session(cfg: Config, now: float | None = None) -> str:
    payload = f"v1.{int(now if now is not None else time.time())}.{int(state(cfg).get('epoch', 0))}"
    return f"{payload}.{_sign(cfg, payload)}"


def session_valid(cfg: Config, value: str | None, now: float | None = None) -> bool:
    if not value:
        return False
    try:
        version, issued, epoch, sig = value.split(".")
    except ValueError:
        return False
    payload = f"{version}.{issued}.{epoch}"
    if version != "v1" or not hmac.compare_digest(sig, _sign(cfg, payload)):
        return False
    try:
        fresh = (now if now is not None else time.time()) - int(issued) < SESSION_DAYS * 86400
        return fresh and int(epoch) == int(state(cfg).get("epoch", 0))
    except ValueError:
        return False


# ── script tokens ─────────────────────────────────────────────────────────────

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(cfg: Config, name: str) -> tuple[dict, str]:
    """A new token: its listing row and the secret, which is shown once."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = {"id": secrets.token_hex(4), "name": (name or "").strip()[:60] or "token",
           "created_at": int(time.time()), "last_used": None}
    doc = state(cfg)
    doc.setdefault("schema", 1)
    doc.setdefault("tokens", []).append({**row, "sha256": _token_hash(token)})
    _private_write(auth_file(cfg), doc)
    return row, token


def list_tokens(cfg: Config) -> list[dict]:
    return [{k: t.get(k) for k in ("id", "name", "created_at", "last_used")}
            for t in state(cfg).get("tokens", [])]


def revoke_token(cfg: Config, token_id: str) -> bool:
    doc = state(cfg)
    kept = [t for t in doc.get("tokens", []) if t.get("id") != token_id]
    if len(kept) == len(doc.get("tokens", [])):
        return False
    doc["tokens"] = kept
    _private_write(auth_file(cfg), doc)
    return True


def token_valid(cfg: Config, header: str | None) -> bool:
    if not header or not header.lower().startswith("bearer "):
        return False
    digest = _token_hash(header[7:].strip())
    doc = state(cfg)
    for t in doc.get("tokens", []):
        if hmac.compare_digest(t.get("sha256", ""), digest):
            if not t.get("last_used") or time.time() - t["last_used"] > 3600:
                t["last_used"] = int(time.time())  # roughly; not worth a write per request
                _private_write(auth_file(cfg), doc)
            return True
    return False


# ── the gate ──────────────────────────────────────────────────────────────────

def needs_admin(method: str, path: str) -> bool:
    """Whether a route needs the admin password once one is set."""
    if method in SAFE_METHODS:
        return path.startswith(ADMIN_READ_PREFIXES)
    if (method, path) in OPEN_WRITES or path.startswith(OPEN_WRITE_PREFIXES):
        return False
    return True


def is_admin(cfg: Config, request: Request) -> bool:
    return (not password_set(cfg) or session_valid(cfg, request.cookies.get(COOKIE))
            or token_valid(cfg, request.headers.get("authorization")))


def _netloc(scheme: str, host: str) -> str:
    host = (host or "").lower()
    default = {"http": ":80", "https": ":443"}.get(scheme, "")
    return host[: -len(default)] if default and host.endswith(default) else host


def origin_ok(cfg: Config, request: Request) -> bool:
    """An unsafe request from a browser must come from a page on this server (or a
    configured trusted origin). Requests with no Origin (apps, curl) pass."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin.rstrip("/") in [o.rstrip("/") for o in cfg.trusted_origins]:
        return True
    parts = urllib.parse.urlsplit(origin)
    if not parts.scheme or not parts.netloc:
        return False
    hosts = {request.headers.get("host", "")}
    if request.headers.get("x-forwarded-host"):
        hosts.add(request.headers["x-forwarded-host"].split(",")[0].strip())
    return any(_netloc(parts.scheme, parts.netloc) == _netloc(parts.scheme, h) for h in hosts if h)


def gate(cfg_of, request: Request) -> None:
    """The FastAPI dependency every route runs (see server.py). Raises 403 for a
    cross-site write, 401/303 when the admin password is needed."""
    cfg = cfg_of()
    method = request.method.upper()
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    if method not in SAFE_METHODS and not origin_ok(cfg, request):
        raise HTTPException(status_code=403, detail="cross_origin")
    if not needs_admin(method, path) or is_admin(cfg, request):
        return
    if method in SAFE_METHODS and not path.startswith("/api/"):
        target = urllib.parse.quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={target}"})
    raise HTTPException(status_code=401, detail="admin_required")


# ── login throttle ────────────────────────────────────────────────────────────

class Throttle:
    """After 5 failures from one address, each try waits twice as long (to 5 min)."""

    FREE = 5

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, tuple[int, float]] = {}

    def wait(self, who: str, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            count, last = self._fails.get(who, (0, 0.0))
        if count < self.FREE:
            return 0
        delay = min(300, 2 ** (count - self.FREE))
        return max(0, int(last + delay - now + 0.999))

    def failed(self, who: str, now: float | None = None) -> None:
        with self._lock:
            count, _ = self._fails.get(who, (0, 0.0))
            self._fails[who] = (count + 1, now if now is not None else time.time())

    def succeeded(self, who: str) -> None:
        with self._lock:
            self._fails.pop(who, None)
