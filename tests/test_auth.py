"""The optional admin password (auth.py) and the Settings API behind /settings.

With no password nothing changes; once one is set, the admin surface needs a session
or a script token while browsing, the reader's own state and the phone stay open.
Every route's policy is pinned in tests/fixtures/route_policy.json: a new route fails
here until someone decides which side it is on."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from epublib import auth
from tests.conftest import make_cfg

POLICY = Path(__file__).parent / "fixtures" / "route_policy.json"


@pytest.fixture
def client(server):
    return TestClient(server.app)


def _set_password(client, pw="correct horse"):
    r = client.post("/api/auth/password", json={"password": pw})
    assert r.status_code == 200, r.text
    return r


# ── units ─────────────────────────────────────────────────────────────────────

def test_passwords_hash_and_verify(tmp_path):
    stored = auth.hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct" not in stored
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong horse", stored)
    assert not auth.verify_password("x", "garbage")
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError, match="at least"):
        auth.set_password(cfg, "short")
    with pytest.raises(ValueError, match="space"):
        auth.set_password(cfg, " padded password ")


def test_sessions_are_signed_expire_and_end_with_a_password_change(tmp_path):
    cfg = make_cfg(tmp_path)
    auth.set_password(cfg, "correct horse")
    s = auth.make_session(cfg, now=1000)
    assert auth.session_valid(cfg, s, now=1000 + 3600)
    assert not auth.session_valid(cfg, s, now=1000 + auth.SESSION_DAYS * 86400 + 1)
    assert not auth.session_valid(cfg, s[:-1] + ("0" if s[-1] != "0" else "1"), now=1001)
    forged = s.replace("v1.1000.", "v1.9999999999.")
    assert not auth.session_valid(cfg, forged, now=1001)
    auth.set_password(cfg, "another password")  # bumps the epoch
    assert not auth.session_valid(cfg, s, now=1001)
    assert not auth.session_valid(cfg, None) and not auth.session_valid(cfg, "nonsense")


def test_tokens_are_stored_hashed_and_revocable(tmp_path):
    cfg = make_cfg(tmp_path)
    row, token = auth.create_token(cfg, "align-remote")
    assert token.startswith("hnd_") and token not in auth.auth_file(cfg).read_text()
    assert auth.token_valid(cfg, f"Bearer {token}")
    assert not auth.token_valid(cfg, "Bearer hnd_nope") and not auth.token_valid(cfg, None)
    assert [t["name"] for t in auth.list_tokens(cfg)] == ["align-remote"]
    assert auth.list_tokens(cfg)[0]["last_used"]
    assert auth.revoke_token(cfg, row["id"]) and not auth.token_valid(cfg, f"Bearer {token}")
    assert not auth.revoke_token(cfg, row["id"])


def test_the_login_throttle_backs_off():
    t = auth.Throttle()
    for _ in range(5):
        assert t.wait("1.2.3.4", now=100) == 0
        t.failed("1.2.3.4", now=100)
    assert t.wait("1.2.3.4", now=100) >= 1 and t.wait("5.6.7.8", now=100) == 0
    t.failed("1.2.3.4", now=100)
    assert t.wait("1.2.3.4", now=100) == 2
    assert t.wait("1.2.3.4", now=200) == 0
    t.succeeded("1.2.3.4")
    assert t.wait("1.2.3.4", now=100) == 0


def test_every_route_has_a_decided_policy(server):
    """New route? Decide whether it is admin-only (auth.ADMIN_READ_PREFIXES /
    OPEN_WRITES) and update tests/fixtures/route_policy.json in the same change."""
    got = {}
    for r in server.app.routes:
        methods = getattr(r, "methods", None)
        if not methods or not hasattr(r, "dependant"):
            continue
        for m in sorted(methods - {"HEAD"}) or ["GET"]:
            got[f"{m} {r.path}"] = "admin" if auth.needs_admin(m, r.path) else "open"
    assert got == json.loads(POLICY.read_text())


# ── the gate, end to end ──────────────────────────────────────────────────────

def test_without_a_password_everything_is_open(client):
    assert client.get("/api/auth/state").json() == {"password_set": False, "admin": True}
    assert client.get("/admin").status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_with_a_password_the_admin_surface_needs_it(server, client):
    _set_password(client)
    stranger = TestClient(server.app)
    assert stranger.get("/api/auth/state").json() == {"password_set": True, "admin": False}
    r = stranger.get("/admin?tab=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin%3Ftab%3D1"
    assert stranger.get("/api/settings").status_code == 401
    assert stranger.get("/api/settings").json()["detail"] == "admin_required"
    assert stranger.post("/api/admin/run/scan").status_code == 401
    assert stranger.delete("/api/books/nope").status_code == 401
    # Browsing, the reader's own state and the phone stay open.
    assert stranger.get("/").status_code == 200 and stranger.get("/api/books").status_code == 200
    assert stranger.get("/login").status_code == 200
    assert stranger.post("/api/reader/events", json={"events": []}).status_code == 200
    assert stranger.post("/api/books/nope/read", json={"read": True}).status_code == 404
    assert stranger.post("/api/known-words/mark", json={"words": []}).status_code != 401
    # The browser that set it stays signed in.
    assert client.get("/api/settings").status_code == 200


def test_signing_in_and_out(server, client):
    _set_password(client)
    other = TestClient(server.app)
    assert other.post("/api/auth/login", json={"password": "wrong one!"}).status_code == 401
    r = other.post("/api/auth/login", json={"password": "correct horse"})
    assert r.status_code == 200 and auth.COOKIE in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert other.get("/api/settings").status_code == 200
    other.post("/api/auth/logout")
    assert other.get("/api/settings").status_code == 401


def test_too_many_wrong_passwords_wait(server, client):
    _set_password(client)
    other = TestClient(server.app)
    for _ in range(auth.Throttle.FREE):
        assert other.post("/api/auth/login", json={"password": "wrong one!"}).status_code == 401
    r = other.post("/api/auth/login", json={"password": "correct horse"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_a_script_token_works_where_a_session_would(server, client):
    _set_password(client)
    made = client.post("/api/auth/tokens", json={"name": "mac"}).json()
    script = TestClient(server.app, headers={"Authorization": f"Bearer {made['token']}"})
    assert script.get("/api/settings").status_code == 200
    assert client.delete(f"/api/auth/tokens/{made['id']}").status_code == 200
    assert script.get("/api/settings").status_code == 401


def test_clearing_the_password_from_the_cli_opens_everything(server, client):
    _set_password(client)
    stranger = TestClient(server.app)
    assert stranger.get("/api/settings").status_code == 401
    auth.clear_password(server.cfg)
    assert stranger.get("/api/settings").status_code == 200


def test_cross_site_writes_are_refused_with_or_without_a_password(client):
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/reader/events", json={"events": []}, headers=evil).status_code == 403
    assert client.post("/api/reader/events", json={"events": []},
                       headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/reader/events", json={"events": []}).status_code == 200  # no Origin: the phone
    assert client.get("/api/books", headers=evil).status_code == 200  # reads are not writes


def test_a_trusted_origin_may_write(server, client):
    server.cfg.trusted_origins = ["https://books.example"]
    assert client.post("/api/reader/events", json={"events": []},
                       headers={"Origin": "https://books.example"}).status_code == 200


# ── settings API ──────────────────────────────────────────────────────────────

@pytest.fixture
def started(server, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_start_stage", lambda stage: calls.append(stage) or True)
    return calls


def _field(doc, key):
    return next(f for f in doc["fields"] if f["key"] == key)


def test_settings_list_values_sources_and_locks(client, monkeypatch):
    doc = client.get("/api/settings").json()
    assert _field(doc, "epub_dir")["source"] == "file" and _field(doc, "epub_dir")["editable"]
    assert _field(doc, "port")["source"] == "default" and _field(doc, "port")["restart"]
    assert all(f["key"] != "alignment.subplz_cmd" or not f["editable"] for f in doc["fields"])
    assert _field(doc, "google_books_api_key")["value"] == ""
    monkeypatch.setenv("HONDANA_PORT", "9001")
    port = _field(client.get("/api/settings").json(), "port")
    assert port["env_locked"] and not port["editable"] and port["env"] == "HONDANA_PORT"


def test_saving_a_library_folder_applies_at_once_and_scans(server, client, started, tmp_path):
    books = tmp_path / "other-books"
    books.mkdir()
    r = client.put("/api/settings", json={"values": {"epub_dir": str(books), "natively.delay": 1.5}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["applied"]) == {"epub_dir", "natively.delay"} and body["started"] == "scan"
    assert started == ["scan"]
    assert server.cfg.epub_dir == books and server.cfg.natively_delay == 1.5
    assert server._book_jobs._cfg is server.cfg  # the job workers see it too
    saved = json.loads((server.cfg.data_dir / "settings.json").read_text())
    assert saved["values"]["epub_dir"] == str(books)
    assert _field(body, "epub_dir")["source"] == "settings"
    # null takes it back to the file's value
    r = client.put("/api/settings", json={"values": {"epub_dir": None}})
    assert server.cfg.epub_dir == tmp_path / "epubs" and _field(r.json(), "epub_dir")["source"] == "file"


def test_a_restart_setting_waits_for_a_restart(server, client, started):
    r = client.put("/api/settings", json={"values": {"port": 8124}})
    body = r.json()
    assert server.cfg.port != 8124 and body["applied"] == []
    assert body["restart_pending"] == ["Port"] and _field(body, "port")["restart_pending"]
    assert started == []


def test_bad_values_are_refused_and_nothing_is_saved(server, client, started, tmp_path, monkeypatch):
    for values, field in [
        ({"epub_dir": str(tmp_path / "missing")}, "epub_dir"),
        ({"alignment.subplz_cmd": "rm -rf /"}, "alignment.subplz_cmd"),
        ({"data_dir": str(tmp_path)}, "data_dir"),
        ({"port": 70000}, "port"),
        ({"public_url": "books.local"}, "public_url"),
        ({"no.such": 1}, "no.such"),
    ]:
        r = client.put("/api/settings", json={"values": values})
        assert r.status_code == 400 and field in r.json()["errors"], (values, r.text)
    monkeypatch.setenv("HONDANA_NATIVELY_DELAY", "2")
    r = client.put("/api/settings", json={"values": {"natively.delay": 1}})
    assert r.status_code == 400 and "HONDANA_NATIVELY_DELAY" in r.json()["errors"]["natively.delay"]
    assert not (server.cfg.data_dir / "settings.json").exists()


def test_a_save_waits_for_a_running_stage(server, client, monkeypatch):
    monkeypatch.setattr(server, "SETTINGS_LOCK_WAIT", 0.05)
    with server._index_lock:
        r = client.put("/api/settings", json={"values": {"natively.delay": 1.0}})
    assert r.status_code == 409


def test_check_path_counts_what_is_there(client, tmp_path):
    d = tmp_path / "lib"
    (d / "a").mkdir(parents=True)
    (d / "a" / "x.epub").write_bytes(b"x")
    (d / "b.m4b").write_bytes(b"x")
    r = client.post("/api/settings/check-path", json={"path": str(d)}).json()
    assert r["is_dir"] and r["epubs"] == 1 and r["audio"] == 1 and r["problem"] is None
    assert client.post("/api/settings/check-path", json={"path": str(d / "nope")}).json()["problem"]
    assert client.post("/api/settings/check-path", json={"path": "relative"}).json()["problem"]


def test_restart_is_refused_without_a_supervisor(client, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert client.post("/api/admin/restart").status_code == 409


def test_the_cli_sets_and_clears_the_password(server, monkeypatch, capsys):
    import io

    from epublib import cli
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin pw\n"))
    assert cli.main(["admin-password", "set", "--stdin"]) == 0
    assert auth.password_set(server.cfg)
    assert cli.main(["admin-password", "status"]) == 0 and "set" in capsys.readouterr().out
    assert cli.main(["token", "create", "mac"]) == 0 and "hnd_" in capsys.readouterr().out
    assert cli.main(["admin-password", "clear"]) == 0 and not auth.password_set(server.cfg)


def test_sessions_survive_a_restart_but_not_a_new_secret(tmp_path):
    cfg = make_cfg(tmp_path)
    auth.set_password(cfg, "correct horse")
    s = auth.make_session(cfg, now=time.time())
    assert auth.session_valid(cfg, s)
    (cfg.data_dir / "secret.key").unlink()
    assert not auth.session_valid(cfg, s)
