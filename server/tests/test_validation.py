import base64

import pytest

from config import Settings


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": 12}, {"text": ""}, {"text": " \n"}, {"text": "a" * 2001}])
def test_invalid_text(client, admin, payload):
    response = client.post("/api/admin/messages", json=payload, headers=admin)
    assert response.status_code == 400
    assert "error" in response.json()
    assert client.get("/api/admin/state").json()["messages"] == []


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "1", 2**63])
def test_invalid_ack(client, device, value):
    assert client.post("/message/ack", json={"id": value}, headers=device).status_code == 400


@pytest.mark.parametrize("body", [b"{", b"null", b"[]", b"true", b'{"text":"\\ud800"}', b'{"text":"\xff"}'])
def test_invalid_json(client, admin, body):
    response = client.post(
        "/api/admin/messages", content=body,
        headers={**admin, "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_body_size_and_content_type(client, admin):
    assert client.post("/api/admin/messages", content="text", headers=admin).status_code == 415
    assert client.post("/api/admin/messages", json={"text": "a" * 32768}, headers=admin).status_code == 413


def test_static_assets_and_host(client):
    for path in ["/", "/static/app.js", "/static/style.css"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "content-security-policy" in response.headers
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_configuration_from_env(settings, monkeypatch):
    values = {
        "ADMIN_PASSWORD": settings.admin_password,
        "DEVICE_TOKEN": settings.device_token,
        "PASETO_SECRET_KEY": base64.urlsafe_b64encode(settings.paseto_secret_key).decode().rstrip("="),
        "DATABASE_PATH": str(settings.database_path),
        "PUBLIC_ORIGIN": settings.public_origin,
        "PING_INTERVAL_SECONDS": "15",
        "SESSION_TTL_SECONDS": "600",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    result = Settings.from_env()
    assert result.paseto_secret_key == settings.paseto_secret_key
    assert result.database_path == settings.database_path
    assert result.ping_interval_seconds == 15
    assert result.session_ttl_seconds == 600
    assert settings.admin_password not in repr(result)
    for name in ["ADMIN_PASSWORD", "DEVICE_TOKEN", "PASETO_SECRET_KEY"]:
        with monkeypatch.context() as context:
            context.delenv(name)
            with pytest.raises(ValueError, match=name):
                Settings.from_env()


@pytest.mark.parametrize("name,value", [
    ("PASETO_SECRET_KEY", "not-base64!"),
    ("PASETO_SECRET_KEY", "YQ"),
    ("PUBLIC_ORIGIN", "http://example.com"),
    ("PUBLIC_ORIGIN", "https://example.com/"),
    ("PUBLIC_ORIGIN", "https://example.com:443"),
    ("PUBLIC_ORIGIN", "http://localhost:80"),
    ("PUBLIC_ORIGIN", "https://EXAMPLE.COM"),
    ("PUBLIC_ORIGIN", "HTTPS://example.com"),
    ("PUBLIC_ORIGIN", "http://[::1]:8000"),
    ("PUBLIC_ORIGIN", "https://user:password@example.com"),
    ("PING_INTERVAL_SECONDS", "0"),
    ("SESSION_TTL_SECONDS", "-1"),
])
def test_invalid_configuration(settings, monkeypatch, name, value):
    monkeypatch.setenv("ADMIN_PASSWORD", settings.admin_password)
    monkeypatch.setenv("DEVICE_TOKEN", settings.device_token)
    monkeypatch.setenv("PASETO_SECRET_KEY", base64.urlsafe_b64encode(settings.paseto_secret_key).decode())
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()
