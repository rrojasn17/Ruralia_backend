from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from main import app


ADMIN_EMAIL = "ai-tests-admin@example.com"
ADMIN_PASSWORD = "AiTestsAdmin2026!"


def _login(client: TestClient, correo: str, password: str) -> dict:
    response = client.post(
        "/auth/login",
        json={"correo": correo, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


def test_training_progress_is_private_versioned_and_persistent_per_user():
    suffix = uuid.uuid4().hex[:10]
    worker_email = f"training-{suffix}@example.com"
    worker_password = "TrainingUser2026!"

    with TestClient(app) as client:
        unauthenticated = client.post("/auth/training-complete", json={"version": 1})
        assert unauthenticated.status_code == 401

        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        created = client.post(
            "/usuarios",
            json={
                "nombre": "Usuario en capacitación",
                "correo": worker_email,
                "password": worker_password,
                "rol": "operario",
                "activo": True,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["onboarding_version"] == 0
        assert created.json()["onboarding_required"] is True

        client.post("/auth/logout")
        worker = _login(client, worker_email, worker_password)
        current_version = worker["onboarding_current_version"]
        assert current_version >= 1
        assert worker["onboarding_required"] is True
        assert worker["onboarding_completed_at"] is None

        invalid_version = client.post(
            "/auth/training-complete",
            json={"version": current_version + 1},
        )
        assert invalid_version.status_code == 409
        assert client.get("/auth/me").json()["onboarding_required"] is True

        completed = client.post(
            "/auth/training-complete",
            json={"version": current_version},
        )
        assert completed.status_code == 200, completed.text
        completed_user = completed.json()
        assert completed_user["onboarding_version"] == current_version
        assert completed_user["onboarding_required"] is False
        assert completed_user["onboarding_completed_at"] is not None

        completed_again = client.post(
            "/auth/training-complete",
            json={"version": current_version},
        )
        assert completed_again.status_code == 200
        assert completed_again.json()["onboarding_completed_at"] == completed_user["onboarding_completed_at"]

        client.post("/auth/logout")
        relogged = _login(client, worker_email, worker_password)
        assert relogged["onboarding_required"] is False
        assert relogged["onboarding_version"] == current_version
