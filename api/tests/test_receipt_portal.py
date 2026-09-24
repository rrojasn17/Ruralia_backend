from __future__ import annotations

from datetime import date
from urllib.parse import urlparse
import uuid

from fastapi.testclient import TestClient

from main import app


ADMIN_EMAIL = "ai-tests-admin@example.com"
ADMIN_PASSWORD = "AiTestsAdmin2026!"


def _login(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        json={"correo": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text


def test_customer_receipt_portal_and_irreversible_liquidation_flow():
    suffix = uuid.uuid4().hex[:10]
    identification = f"1-2345-{suffix[:4]}"

    with TestClient(app) as client:
        _login(client)

        created_client = client.post(
            "/clientes",
            json={
                "codigo": f"CL-PORTAL-{suffix}",
                "nombre_completo": "Productor Portal Seguro",
                "tipo_persona": "personal",
                "categoria": "agro",
                "numero_identificacion": identification,
                "telefono": "88887777",
                "correo": f"portal-{suffix}@example.com",
                "activo": True,
            },
        )
        assert created_client.status_code == 201, created_client.text
        cliente_id = created_client.json()["id"]

        created_receipt = client.post(
            "/recibos",
            json={
                "numero_recibo": f"RC-PORTAL-{suffix}",
                "fecha": date.today().isoformat(),
                "cliente_id": cliente_id,
                "cajuelas": 20,
                "cuartillos": 0,
                "precio_fanega": 100000,
                "estado": "recibido",
            },
        )
        assert created_receipt.status_code == 201, created_receipt.text
        receipt_id = created_receipt.json()["id"]
        assert created_receipt.json()["monto_estimado"] == 100000
        assert created_receipt.json()["estado_liquidacion"] == "pendiente"

        link_response = client.post(f"/recibos/{receipt_id}/portal-link")
        assert link_response.status_code == 200, link_response.text
        portal_url = link_response.json()["url"]
        access_token = urlparse(portal_url).path.rstrip("/").rsplit("/", 1)[-1]
        assert len(access_token) >= 32
        assert f"/portal-recibos/{access_token}" in portal_url

        client.post("/auth/logout")
        assert client.get(f"/recibos/{receipt_id}/liquidacion/comprobante").status_code == 401

        wrong_verification = client.post(
            f"/public/recibos/{access_token}/verify",
            json={"numero_identificacion": "000000000"},
        )
        assert wrong_verification.status_code == 401
        assert wrong_verification.json()["detail"] == "Datos de acceso inválidos"

        verification = client.post(
            f"/public/recibos/{access_token}/verify",
            json={"numero_identificacion": identification.replace("-", " ")},
        )
        assert verification.status_code == 200, verification.text
        portal_session = verification.json()["session_token"]
        portal_headers = {"X-Portal-Token": portal_session}

        pending_portal = client.get(
            f"/public/recibos/{access_token}",
            headers=portal_headers,
        )
        assert pending_portal.status_code == 200, pending_portal.text
        assert pending_portal.json()["resumen"] == {
            "total_recibos": 1,
            "total_cajuelas": 20.0,
            "total_fanegas": 1.0,
            "monto_total": 100000.0,
            "monto_cobrado": 0.0,
            "monto_por_cobrar": 100000.0,
            "recibos_liquidados": 0,
            "recibos_pendientes": 1,
            "moneda": "CRC",
        }

        _login(client)

        rejected_file = client.post(
            f"/recibos/{receipt_id}/liquidar",
            data={
                "nota": "Pago completo por transferencia",
                "numero_transferencia": "SINPE-001",
                "monto": "97500",
            },
            files={"comprobante": ("comprobante.pdf", b"not-a-real-pdf", "application/pdf")},
        )
        assert rejected_file.status_code == 422, rejected_file.text
        assert client.get(f"/recibos/{receipt_id}").json()["liquidado"] is False

        liquidation = client.post(
            f"/recibos/{receipt_id}/liquidar",
            data={
                "nota": "Pago completo por transferencia bancaria",
                "numero_transferencia": "SINPE-001",
                "monto": "97500",
            },
            files={
                "comprobante": (
                    "transferencia.pdf",
                    b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF",
                    "application/pdf",
                )
            },
        )
        assert liquidation.status_code == 200, liquidation.text
        paid = liquidation.json()
        assert paid["liquidado"] is True
        assert paid["estado_liquidacion"] == "liquidado"
        assert paid["liquidacion_monto"] == 97500
        assert paid["liquidacion_numero_transferencia"] == "SINPE-001"
        assert paid["liquidacion_comprobante_nombre"] == "transferencia.pdf"
        assert paid["liquidado_por_nombre"] == "AI Tests Admin"

        duplicate = client.post(
            f"/recibos/{receipt_id}/liquidar",
            data={
                "nota": "Intento duplicado",
                "numero_transferencia": "SINPE-002",
                "monto": "97500",
            },
            files={"comprobante": ("transferencia.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert duplicate.status_code == 409

        immutable_update = client.patch(
            f"/recibos/{receipt_id}",
            json={"observaciones": "Intento de cambio posterior al pago"},
        )
        assert immutable_update.status_code == 409
        assert client.delete(f"/recibos/{receipt_id}").status_code == 409

        private_proof = client.get(f"/recibos/{receipt_id}/liquidacion/comprobante")
        assert private_proof.status_code == 200
        assert private_proof.content.startswith(b"%PDF-")
        assert private_proof.headers["cache-control"] == "private, no-store"

        client.post("/auth/logout")
        paid_portal = client.get(
            f"/public/recibos/{access_token}",
            headers=portal_headers,
        )
        assert paid_portal.status_code == 200, paid_portal.text
        summary = paid_portal.json()["resumen"]
        assert summary["monto_total"] == 97500
        assert summary["monto_cobrado"] == 97500
        assert summary["monto_por_cobrar"] == 0
        assert summary["recibos_liquidados"] == 1
        assert paid_portal.json()["recibos"][0]["liquidado"] is True

        official_receipt = client.get(
            f"/public/recibos/{access_token}/{receipt_id}/oficial",
            headers=portal_headers,
        )
        assert official_receipt.status_code == 200, official_receipt.text
        assert official_receipt.json()["numero_recibo"] == f"RC-PORTAL-{suffix}"
        assert official_receipt.json()["productor_nombre"] == "Productor Portal Seguro"
        assert official_receipt.json()["fanegas_estimadas"] == 1

        public_pdf = client.get(
            f"/public/recibos/{access_token}/{receipt_id}/pdf",
            headers=portal_headers,
        )
        assert public_pdf.status_code == 200, public_pdf.text
        assert public_pdf.content.startswith(b"%PDF-")
        assert public_pdf.headers["content-type"] == "application/pdf"
        assert "attachment" in public_pdf.headers["content-disposition"]

        logout = client.post(
            f"/public/recibos/{access_token}/logout",
            headers=portal_headers,
        )
        assert logout.status_code == 204
        assert client.get(
            f"/public/recibos/{access_token}",
            headers=portal_headers,
        ).status_code == 401
