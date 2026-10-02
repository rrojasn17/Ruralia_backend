"""Quick notes: capture, offline idempotency, visibility and optimistic locking."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from core.models import Usuario
from core.routers.auth import get_current_user
from database import SessionLocal
from main import app


def _user(role: str, name: str) -> Usuario:
    with SessionLocal() as db:
        row = Usuario(
            nombre=name,
            correo=f"{uuid.uuid4().hex}@example.com",
            hash_contrasena="unused",
            rol=role,
            activo=True,
            is_superadmin=False,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row


def test_quick_notes_idempotent_sync_locking_and_visibility(db_session):
    del db_session
    owner = _user("admin", "Dueño nota")
    teammate = _user("operario", "Operario campo")
    current = {"user": owner}
    app.dependency_overrides[get_current_user] = lambda: current["user"]

    try:
        with TestClient(app) as client:
            with SessionLocal() as db:
                from core.models import AppModule
                from core.module_manager import BUILTIN_MANIFESTS, activate_module, install_module
                if not db.query(AppModule).filter_by(key="mod_caficultura").first():
                    manifest = BUILTIN_MANIFESTS["mod_caficultura"]
                    db.add(AppModule(
                        key="mod_caficultura", name=manifest["name"], version=manifest["version"],
                        module_type="industry", status="imported", manifest=manifest,
                    ))
                    db.commit()
                install_module(db, "mod_caficultura")
                activate_module(db, "mod_caficultura")

            client_uuid = f"nota-{uuid.uuid4().hex}"
            payload = {
                "client_uuid": client_uuid,
                "contenido": "Proveedor Cafetalero, teléfono 8888-9999. Pedir cotización.",
                "categoria": "proveedor",
                "prioridad": "alta",
                "visibilidad": "personal",
                "origen": "movil_offline",
                "etiquetas": ["Proveedor", "cotizar", "proveedor"],
                "contexto": {"capture_mode": "primary_mobile"},
            }
            created_response = client.post("/notas-rapidas", json=payload)
            assert created_response.status_code == 201, created_response.text
            created = created_response.json()
            assert created["client_uuid"] == client_uuid
            assert created["categoria"] == "proveedor"
            assert created["estado"] == "pendiente"
            assert created["etiquetas"] == ["proveedor", "cotizar"]
            assert created["version"] == 1

            # Retrying an offline payload must never duplicate the note.
            retry = client.post("/notas-rapidas", json=payload)
            assert retry.status_code == 201, retry.text
            assert retry.json()["id"] == created["id"]
            rows = client.get("/notas-rapidas?incluir_archivadas=true").json()
            assert [row["id"] for row in rows].count(created["id"]) == 1

            conflict = client.patch(
                f"/notas-rapidas/{created['id']}",
                json={"estado": "procesada", "expected_version": 99},
            )
            assert conflict.status_code == 409

            updated_response = client.patch(
                f"/notas-rapidas/{created['id']}",
                json={
                    "estado": "procesada",
                    "linked_entity_type": "provider",
                    "linked_entity_id": 123,
                    "expected_version": created["version"],
                },
            )
            assert updated_response.status_code == 200, updated_response.text
            updated = updated_response.json()
            assert updated["estado"] == "procesada"
            assert updated["linked_entity_type"] == "provider"
            assert updated["linked_entity_id"] == 123
            assert updated["procesada_por_id"] == owner.id
            assert updated["version"] == 2

            private_id = created["id"]
            team_response = client.post("/notas-rapidas", json={
                "contenido": "Dato compartido con el equipo",
                "categoria": "pendiente",
                "visibilidad": "equipo",
            })
            assert team_response.status_code == 201, team_response.text
            team = team_response.json()

            current["user"] = teammate
            assert client.get(f"/notas-rapidas/{private_id}").status_code == 404
            visible_team = client.get(f"/notas-rapidas/{team['id']}")
            assert visible_team.status_code == 200
            assert visible_team.json()["visibilidad"] == "equipo"
            denied = client.patch(
                f"/notas-rapidas/{team['id']}",
                json={"titulo": "No autorizado", "expected_version": team["version"]},
            )
            assert denied.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_provider_action_from_field_note_requires_confirmation(db_session):
    from modules.mod_caficultura.ai_actions import confirm_pending_action, prepare_provider_action
    from modules.mod_caficultura.ai_agent import ensure_default_agent
    from modules.mod_caficultura.model_ai_consulting import AIConversation, AIMessage
    from modules.mod_caficultura.models import Proveedor

    user = Usuario(
        nombre="Administrativo notas",
        correo=f"{uuid.uuid4().hex}@example.com",
        hash_contrasena="unused",
        rol="admin",
        activo=True,
        is_superadmin=False,
    )
    db_session.add(user)
    db_session.flush()
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Procesar nota de proveedor",
        channel="web",
        status="active",
    )
    db_session.add(conversation)
    db_session.flush()
    message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content="Agro Montaña, cédula 3-101-999999, 8888-0000, compras@agromontana.test",
        status="completed",
    )
    db_session.add(message)
    db_session.flush()

    prepared = prepare_provider_action(
        db_session,
        conversation,
        user,
        message,
        {
            "legal_name": "Agro Montaña",
            "commercial_name": "Agro Montaña",
            "identification": "3-101-999999",
            "identification_type": "juridica",
            "email": "compras@agromontana.test",
            "phone": "8888-0000",
            "website": None,
            "address": None,
            "province": None,
            "canton": None,
            "district": None,
            "notes": "Dato capturado desde nota rápida",
            "contacts": [],
        },
    )
    db_session.commit()
    assert prepared["requires_confirmation"] is True
    assert db_session.query(Proveedor).count() == 0

    action = confirm_pending_action(
        db_session,
        prepared["action_public_id"],
        user,
        prepared["action_version"],
    )
    db_session.commit()
    assert action.status == "executed"
    assert action.result["entity"] == "provider"
    provider = db_session.query(Proveedor).one()
    assert provider.nombre_legal == "Agro Montaña"
    assert provider.identificacion == "3-101-999999"
