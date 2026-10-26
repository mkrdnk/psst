import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from main import create_app


def test_cookie_and_logout(client, admin):
    cookie = client.cookies.get("psst_admin")
    assert cookie.startswith("v4.local.")
    response = client.get("/api/admin/state")
    assert response.status_code == 200
    assert response.json()["csrf_token"] == admin["X-CSRF-Token"]
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    response = client.post("/api/admin/logout", json={}, headers=admin)
    assert response.status_code == 204
    assert not client.cookies.get("psst_admin")
    assert client.get("/api/admin/state").status_code == 401


def test_login_cookie_flags(client, settings):
    response = client.post(
        "/api/admin/login", headers={"Origin": settings.public_origin},
        json={"password": settings.admin_password},
    )
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/api/admin" in cookie
    assert f"Max-Age={settings.session_ttl_seconds}" in cookie


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null"])
def test_login_rejects_wrong_origin(client, settings, origin):
    headers = {"Origin": origin} if origin else {}
    response = client.post("/api/admin/login", headers=headers, json={"password": settings.admin_password})
    assert response.status_code == 403
    assert not client.cookies.get("psst_admin")


def test_login_rate_limit(client, settings, monkeypatch):
    now = 1000
    monkeypatch.setattr("auth.time.monotonic", lambda: now)
    headers = {"Origin": settings.public_origin}
    for _ in range(5):
        assert client.post("/api/admin/login", headers=headers, json={"password": "wrong"}).status_code == 401
    limited = client.post("/api/admin/login", headers=headers, json={"password": settings.admin_password})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    now += 60
    assert client.post("/api/admin/login", headers=headers, json={"password": settings.admin_password}).status_code == 200


@pytest.mark.parametrize("path", ["/api/admin/messages", "/api/admin/logout"])
@pytest.mark.parametrize("missing", ["Origin", "X-CSRF-Token"])
def test_admin_mutations_require_origin_and_csrf(client, admin, path, missing):
    headers = {key: value for key, value in admin.items() if key != missing}
    assert client.post(path, headers=headers, json={"text": "no"}).status_code == 403
    headers[missing] = "wrong"
    assert client.post(path, headers=headers, json={"text": "no"}).status_code == 403
    state = client.get("/api/admin/state")
    assert state.status_code == 200
    assert state.json()["messages"] == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/message"), ("POST", "/ping"), ("POST", "/message/ack"),
])
def test_device_auth_is_separate(client, admin, method, path, settings):
    for token in [None, settings.admin_password, client.cookies.get("psst_admin")]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        assert client.request(method, path, headers=headers, json={"id": 1}).status_code == 401
    assert client.get("/api/admin/state").json()["device"]["last_seen"] is None


def test_device_token_cannot_access_admin(client, device):
    assert client.get("/api/admin/state", headers=device).status_code == 401
    assert client.post("/api/admin/messages", headers=device, json={"text": "no"}).status_code == 401


@pytest.mark.parametrize("token", ["v4.local.", "v4.local.broken", "v4.public.bad", "jwt", "v4.local.%%%%"])
def test_malformed_cookie_rejected(client, token):
    client.cookies.set("psst_admin", token, path="/api/admin")
    assert client.get("/api/admin/state").status_code == 401


def test_tampered_token_rejected(client, admin):
    token = client.cookies.get("psst_admin")
    index = len("v4.local.") + 10
    changed = token[:index] + ("A" if token[index] != "A" else "B") + token[index + 1:]
    client.cookies.clear()
    client.cookies.set("psst_admin", changed, path="/api/admin")
    assert client.get("/api/admin/state").status_code == 401


@pytest.mark.parametrize("override", [
    {"exp": -1}, {"exp": None}, {"aud": "device"}, {"iss": "elsewhere"},
    {"subject": {"id": "device"}}, {"password_version": "old"}, {"csrf": ""},
])
def test_valid_crypto_does_not_bypass_claim_checks(client, app, override):
    claims = {
        "subject": {"id": "admin"},
        "via": "paseto",
        "exp": 3600,
        "iss": "psst",
        "aud": "psst-admin",
        "csrf": secrets.token_urlsafe(),
        "password_version": app.state.auth.password_version,
    }
    claims.update(override)
    token = app.state.auth.jam.issue(**claims)
    client.cookies.set("psst_admin", token, path="/api/admin")
    assert client.get("/api/admin/state").status_code == 401


def test_password_rotation_invalidates_session(client, admin, settings):
    cookie = client.cookies.get("psst_admin")
    changed = replace(settings, admin_password=secrets.token_urlsafe())
    with TestClient(create_app(changed), base_url=settings.public_origin) as other:
        other.cookies.set("psst_admin", cookie, path="/api/admin")
        assert other.get("/api/admin/state").status_code == 401


def test_session_expires_at_its_deadline(client, app, admin):
    token = client.cookies.get("psst_admin")
    claims = app.state.auth.jam.authenticate(token, via="paseto").claims
    deadline = datetime.fromtimestamp(claims["exp"], UTC)
    app.state.now = lambda: deadline - timedelta(microseconds=1)
    assert client.get("/api/admin/state").status_code == 200
    for now in [deadline, deadline + timedelta(seconds=1)]:
        app.state.now = lambda: now
        assert client.get("/api/admin/state").status_code == 401
        assert client.post("/api/admin/messages", headers=admin, json={"text": "no"}).status_code == 401


def test_local_http_login(settings):
    local = replace(settings, public_origin="http://localhost:8000")
    with TestClient(create_app(local), base_url=local.public_origin) as client:
        response = client.post(
            "/api/admin/login", headers={"Origin": local.public_origin},
            json={"password": local.admin_password},
        )
        assert response.status_code == 200
        assert "Secure" not in response.headers["set-cookie"]
        assert client.get("/api/admin/state").status_code == 200
