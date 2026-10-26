from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient

from main import create_app


def send_message(client, admin, text="Привет 💛"):
    response = client.post("/api/admin/messages", headers=admin, json={"text": text})
    assert response.status_code == 201
    return response.json()


def get_state(client):
    response = client.get("/api/admin/state")
    assert response.status_code == 200
    return response.json()


def test_message_lifecycle(client, admin, device):
    assert client.get("/message", headers=device).status_code == 204
    assert client.post("/ping", headers=device).json() == {"unread": False, "ping": "pong"}
    message = send_message(client, admin)
    assert message["received_at"] is None
    assert client.post("/ping", headers=device).json() == {"unread": True, "ping": "pong"}
    assert client.get("/message", headers=device).json() == {
        "id": message["id"], "text": message["text"],
    }
    assert get_state(client)["messages"][0]["received_at"] is None
    assert client.post("/ping", headers=device).json()["unread"] is True
    response = client.post("/message/ack", headers=device, json={"id": message["id"]})
    assert response.status_code == 204
    assert response.content == b""
    assert client.post("/ping", headers=device).json() == {"unread": False, "ping": "pong"}
    assert get_state(client)["messages"][0]["received_at"] is not None
    # A rebooted display can restore the current text even after acknowledgement.
    assert client.get("/message", headers=device).json()["id"] == message["id"]


def test_latest_only_and_late_ack(client, admin, device):
    first = send_message(client, admin, "A")
    second = send_message(client, admin, "B")
    assert client.get("/message", headers=device).json()["id"] == second["id"]
    third = send_message(client, admin, "C")
    assert client.post("/message/ack", headers=device, json={"id": second["id"]}).status_code == 204
    assert client.post("/ping", headers=device).json()["unread"] is True
    assert client.get("/message", headers=device).json()["id"] == third["id"]
    history = get_state(client)["messages"]
    assert [row["id"] for row in history] == [third["id"], second["id"], first["id"]]
    assert [row["is_current"] for row in history] == [True, False, False]
    assert history[0]["received_at"] is None
    assert history[1]["received_at"] is not None
    assert history[2]["received_at"] is None


def test_ack_is_idempotent(client, app, admin, device):
    message = send_message(client, admin)
    now = datetime.now(UTC)
    app.state.now = lambda: now
    assert client.post("/message/ack", headers=device, json={"id": message["id"]}).status_code == 204
    received = get_state(client)["messages"][0]["received_at"]
    app.state.now = lambda: now + timedelta(seconds=10)
    assert client.post("/message/ack", headers=device, json={"id": message["id"]}).status_code == 204
    assert get_state(client)["messages"][0]["received_at"] == received
    assert client.post("/message/ack", headers=device, json={"id": message["id"] + 1}).status_code == 404


def test_device_availability(client, app, admin, device, settings):
    assert get_state(client)["device"] == {
        "status": "never_seen",
        "last_seen": None,
        "ping_interval_seconds": settings.ping_interval_seconds,
    }
    message = send_message(client, admin)
    client.get("/message", headers=device)
    client.post("/message/ack", headers=device, json={"id": message["id"]})
    assert get_state(client)["device"]["status"] == "never_seen"
    now = datetime.now(UTC)
    app.state.now = lambda: now
    client.post("/ping", headers=device)
    assert get_state(client)["device"]["last_seen"] == now.isoformat()
    app.state.now = lambda: now + timedelta(seconds=3 * settings.ping_interval_seconds)
    assert get_state(client)["device"]["status"] == "online"
    app.state.now = lambda: now + timedelta(seconds=3 * settings.ping_interval_seconds + 1)
    assert get_state(client)["device"]["status"] == "offline"
    client.post("/ping", headers=device)
    assert get_state(client)["device"]["status"] == "online"


def test_database_survives_app_restart(settings, device):
    def logged_in(client):
        response = client.post(
            "/api/admin/login", headers={"Origin": settings.public_origin},
            json={"password": settings.admin_password},
        )
        assert response.status_code == 200
        return {"Origin": settings.public_origin, "X-CSRF-Token": response.json()["csrf_token"]}

    with TestClient(create_app(settings), base_url=settings.public_origin) as first:
        message = send_message(first, logged_in(first))
        first.post("/ping", headers=device)
        first.post("/message/ack", headers=device, json={"id": message["id"]})
        original = get_state(first)
    with TestClient(create_app(settings), base_url=settings.public_origin) as second:
        admin = logged_in(second)
        restored = get_state(second)
        assert restored["messages"] == original["messages"]
        assert restored["device"] == original["device"]
        assert second.get("/message", headers=device).json()["id"] == message["id"]
        assert second.post("/ping", headers=device).json()["unread"] is False
        newer = send_message(second, admin)
        assert newer["id"] > message["id"]
        assert second.post("/ping", headers=device).json()["unread"] is True


def test_history_is_limited_and_unicode_is_preserved(client, admin, device):
    for index in range(51):
        send_message(client, admin, str(index))
    text = "💛" * 2000
    latest = send_message(client, admin, f"  {text}\n")
    assert client.get("/message", headers=device).json() == {"id": latest["id"], "text": text}
    history = get_state(client)["messages"]
    assert len(history) == 50
    assert sum(row["is_current"] for row in history) == 1
    assert history[0]["id"] == latest["id"]
