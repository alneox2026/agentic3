from fastapi.testclient import TestClient

from services.billing_api_v3.app.main import app


client = TestClient(app)


def test_billing_api_health_reports_the_server_owned_catalog() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "billing-api",
        "status": "healthy",
        "catalog_schema_version": 1,
        "catalog_environment": "test",
    }


def test_billing_api_ready_validates_the_catalog() -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
