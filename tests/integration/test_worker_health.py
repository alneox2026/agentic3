from fastapi.testclient import TestClient

from services.agent_persistence_worker_v3.app.main import app


client = TestClient(app)


def test_worker_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_worker_ready() -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
