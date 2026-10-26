import secrets

import pytest
from starlette.testclient import TestClient

from config import Settings
from main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        admin_password=secrets.token_urlsafe(32),
        device_token=secrets.token_urlsafe(32),
        paseto_secret_key=secrets.token_bytes(32),
        database_path=tmp_path / "psst.db",
        public_origin="https://testserver",
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app, settings):
    with TestClient(app, base_url=settings.public_origin) as client:
        yield client


@pytest.fixture
def admin(client, settings):
    response = client.post(
        "/api/admin/login",
        json={"password": settings.admin_password},
        headers={"Origin": settings.public_origin},
    )
    assert response.status_code == 200
    return {
        "Origin": settings.public_origin,
        "X-CSRF-Token": response.json()["csrf_token"],
    }


@pytest.fixture
def device(settings):
    return {"Authorization": f"Bearer {settings.device_token}"}
