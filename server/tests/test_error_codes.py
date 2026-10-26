from datetime import UTC, datetime

import pytest
from starlette.exceptions import HTTPException
from starlette.routing import Route


def assert_error(response, status, code, detail):
    assert response.status_code == status
    assert response.json() == {"error": detail, "code": code}


def test_invalid_password(client, settings):
    response = client.post(
        "/api/admin/login",
        headers={"Origin": settings.public_origin},
        json={"password": "wrong"},
    )
    assert_error(response, 401, "invalid_password", "Неверный пароль.")


def test_login_origin(client):
    response = client.post("/api/admin/login", json={"password": "wrong"})
    assert_error(response, 403, "invalid_origin", "Недопустимый источник запроса.")


@pytest.mark.parametrize("path", ["/api/admin/messages", "/api/admin/logout"])
@pytest.mark.parametrize("header,code,detail", [
    ("Origin", "invalid_origin", "Недопустимый источник запроса."),
    ("X-CSRF-Token", "invalid_csrf", "Недействительный CSRF-токен."),
])
@pytest.mark.parametrize("value", [None, "wrong"])
def test_mutation_guards(client, admin, path, header, code, detail, value):
    headers = dict(admin)
    if value is None:
        del headers[header]
    else:
        headers[header] = value
    response = client.post(path, headers=headers, json={"text": "test"})
    assert_error(response, 403, code, detail)


def test_missing_auth(client):
    assert_error(
        client.get("/api/admin/state"), 401, "auth_required", "Необходим вход."
    )


def test_invalid_session(client):
    client.cookies.set("psst_admin", "v4.local.broken", path="/api/admin")
    assert_error(
        client.get("/api/admin/state"),
        401, "invalid_session", "Недействительная сессия.",
    )


def test_expired_session(client, app, admin):
    token = client.cookies.get("psst_admin")
    claims = app.state.auth.jam.authenticate(token, via="paseto").claims
    app.state.now = lambda: datetime.fromtimestamp(claims["exp"], UTC)
    assert_error(
        client.get("/api/admin/state"),
        401, "invalid_session", "Сессия истекла или недействительна.",
    )


@pytest.mark.parametrize("body,status,code,detail", [
    (b"{", 400, "invalid_json", "Некорректный JSON."),
    (b'{"text":"\xff"}', 400, "invalid_json", "Некорректный JSON."),
    (b"[]", 400, "json_object_required", "Ожидается JSON-объект."),
    (b"null", 400, "json_object_required", "Ожидается JSON-объект."),
    (b'{"text":"\\ud800"}', 400, "invalid_unicode", "Поле text содержит некорректный Unicode."),
    (b" " * 32769, 413, "payload_too_large", "Слишком большой запрос."),
])
def test_json_errors(client, admin, body, status, code, detail):
    response = client.post(
        "/api/admin/messages", content=body,
        headers={**admin, "Content-Type": "application/json"},
    )
    assert_error(response, status, code, detail)


def test_unsupported_media_type(client, admin):
    response = client.post("/api/admin/messages", content="text", headers=admin)
    assert_error(response, 415, "unsupported_media_type", "Ожидается application/json.")


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": 12}])
def test_invalid_field(client, admin, payload):
    response = client.post("/api/admin/messages", headers=admin, json=payload)
    assert_error(response, 400, "invalid_field", "Поле text должно быть строкой.")


@pytest.mark.parametrize("text", ["", " \n", "a" * 2001])
def test_invalid_message(client, admin, text):
    response = client.post("/api/admin/messages", headers=admin, json={"text": text})
    assert_error(
        response, 400, "invalid_message",
        "Сообщение должно содержать от 1 до 2000 символов.",
    )


def test_rate_limit_preserves_header(client, settings, monkeypatch):
    monkeypatch.setattr("auth.time.monotonic", lambda: 1000)
    for _ in range(5):
        client.post(
            "/api/admin/login", headers={"Origin": settings.public_origin},
            json={"password": "wrong"},
        )
    response = client.post(
        "/api/admin/login", headers={"Origin": settings.public_origin},
        json={"password": settings.admin_password},
    )
    assert_error(
        response, 429, "rate_limited",
        "Слишком много попыток входа. Попробуйте через минуту.",
    )
    assert response.headers["retry-after"] == "60"


@pytest.mark.parametrize("path", ["/api/admin", "/api/admin/missing"])
def test_admin_not_found(client, path):
    assert_error(client.get(path), 404, "not_found", "Not Found")


def test_admin_method_not_allowed(client):
    response = client.get("/api/admin/login")
    assert_error(response, 405, "method_not_allowed", "Method Not Allowed")
    assert response.headers["allow"] == "POST"


@pytest.mark.parametrize("path,is_admin", [
    ("/api/admin/failure", True),
    ("/failure", False),
])
def test_framework_fallback_preserves_headers(client, app, path, is_admin):
    async def failure(request):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    app.router.routes.append(Route(path, failure))
    response = client.get(path)
    assert response.status_code == 401
    expected = {"error": "Unauthorized"}
    if is_admin:
        expected["code"] = "request_failed"
    assert response.json() == expected
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ["/missing", "/api/administrator", "/api/admin-other"])
def test_non_admin_not_found_unchanged(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"error": "Not Found"}


@pytest.mark.parametrize("method,path", [
    ("POST", "/ping"), ("GET", "/message"), ("POST", "/message/ack"),
])
def test_device_auth_errors_unchanged(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.json() == {"error": "Недействительный токен устройства."}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("body,content_type,status,detail", [
    (b"{", "application/json", 400, "Некорректный JSON."),
    (b"[]", "application/json", 400, "Ожидается JSON-объект."),
    (b"text", "text/plain", 415, "Ожидается application/json."),
    (b" " * 32769, "application/json", 413, "Слишком большой запрос."),
    (b'{"id":0}', "application/json", 400, "Поле id должно быть положительным 64-битным целым числом."),
    (b'{"id":1}', "application/json", 404, "Сообщение не найдено."),
])
def test_device_validation_errors_unchanged(client, device, body, content_type, status, detail):
    response = client.post(
        "/message/ack", content=body,
        headers={**device, "Content-Type": content_type},
    )
    assert response.status_code == status
    assert response.json() == {"error": detail}


def test_device_method_error_unchanged(client):
    response = client.get("/ping")
    assert response.status_code == 405
    assert response.json() == {"error": "Method Not Allowed"}
    assert response.headers["allow"] == "POST"


def test_ping_success_unchanged(client, device, admin):
    response = client.post("/ping", headers=device)
    assert response.status_code == 200
    assert response.content == b'{"unread":false,"ping":"pong"}'
    assert client.post(
        "/api/admin/messages", headers=admin, json={"text": "test"}
    ).status_code == 201
    response = client.post("/ping", headers=device)
    assert response.status_code == 200
    assert response.content == b'{"unread":true,"ping":"pong"}'
