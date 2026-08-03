import pytest
from fastapi.testclient import TestClient

from gateway.app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_login_returns_tokens(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "changeme123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert "refresh_token" in body


def test_rate_limit_returns_429(client: TestClient) -> None:
    for _ in range(61):
        response = client.get("/health")
        if response.status_code == 429:
            break
    assert response.status_code == 429
