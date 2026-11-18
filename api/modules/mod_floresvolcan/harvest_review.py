"""Solicitud y autorización de cierres con diferencias; conserva la evidencia."""

import hashlib
import json
from datetime import datetime, timezone
from fastapi import HTTPException
from .models import ModuleSetting


def read_review(db, batch_id):
    row = db.query(ModuleSetting).filter_by(key=f"harvest_close.{batch_id}").first()
    return json.loads(row.value_text) if row else None


def save_review(db, batch_id, value):
    row = db.query(ModuleSetting).filter_by(key=f"harvest_close.{batch_id}").first()
    if not row:
        row = ModuleSetting(key=f"harvest_close.{batch_id}")
        db.add(row)
    row.value_text = json.dumps(value, ensure_ascii=False, default=str)


def fingerprint(metrics, segments):
    value = {
        "expected": metrics["expected_stems"],
        "harvested": metrics["harvested"],
        "segments": [
            {
                k: s[k]
                for k in ("id", "expected_stems", "harvested", "remaining", "overage")
            }
            for s in segments
        ],
    }
    return hashlib.sha256(
        json.dumps(value, default=str, sort_keys=True).encode()
    ).hexdigest()


def closing_gate(db, batch, current, payload, metrics, segments):
    if all(s["expected_stems"] == s["harvested"] for s in segments) or current.is_superadmin:
        return None
    review = read_review(db, batch.id)
    digest = fingerprint(metrics, segments)
    if (
        review
        and review["status"] == "approved"
        and review.get("approved_by") == current.id
        and review.get("fingerprint") == digest
    ):
        return None
    if review and review["status"] == "approved":
        raise HTTPException(
            409, "El balance cambió; solicite nuevamente la revisión del cierre"
        )
    if (
        not review
        or review.get("status") != "pending"
        or review.get("fingerprint") != digest
        or review.get("payload") != payload.model_dump(mode="json")
    ):
        history = (
            (
                review.get("history", [])
                + [{k: v for k, v in review.items() if k != "history"}]
            )
            if review
            else []
        )
        review = {
            "status": "pending",
            "requested_by": current.id,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "comments": payload.comments,
            "payload": payload.model_dump(mode="json"),
            "fingerprint": digest,
            "difference": str(metrics["expected_stems"] - metrics["harvested"]),
            "history": history,
        }
        save_review(db, batch.id, review)
    db.commit()
    return {
        "status": "under_review",
        "review_required": True,
        "message": "Diferencia enviada a gerencia; el lote sigue abierto",
    }
