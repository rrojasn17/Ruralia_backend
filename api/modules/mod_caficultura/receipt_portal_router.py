from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote
import re

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session, selectinload

from config import (
    FRONTEND_URL,
    RECEIPT_PORTAL_RATE_WINDOW_SECONDS,
    RECEIPT_PORTAL_VERIFY_LIMIT,
)
from database import get_db
from modules.mod_caficultura.models import Cliente, ClientePortalSession, ReciboCafe
from core.models import Usuario
from rate_limit import client_ip, enforce_rate_limit
from core.routers.auth import get_current_user, require_roles
from modules.mod_caficultura.schemas import (
    PortalRecibosOut,
    PortalRecibosVerifyIn,
    PortalRecibosVerifyOut,
    ReciboOut,
    ReciboPortalLinkOut,
)
from modules.mod_caficultura.receipt_portal import (
    build_portal_payload,
    build_receipt_pdf,
    create_portal_session,
    delete_private_proof,
    ensure_client_portal_token,
    find_portal_client,
    identification_matches,
    resolve_private_proof_path,
    save_private_receipt_proof,
    utcnow,
    validate_portal_session,
)
from modules.mod_caficultura.services import recibo_query, serialize_recibo


router = APIRouter(tags=["receipt-portal"])


@router.post("/recibos/{recibo_id}/portal-link", response_model=ReciboPortalLinkOut)
def create_receipt_portal_link(
    recibo_id: int,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del current
    row = recibo_query(db).filter(ReciboCafe.id == recibo_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recibo no encontrado")
    if not row.cliente or not row.cliente_id:
        raise HTTPException(status_code=422, detail="Vincule el recibo con un cliente antes de compartirlo")
    if not str(row.cliente.numero_identificacion or "").strip():
        raise HTTPException(status_code=422, detail="El cliente debe tener una identificación registrada")
    if not row.cliente.activo:
        raise HTTPException(status_code=422, detail="El cliente está inactivo")

    token = ensure_client_portal_token(row.cliente)
    db.commit()
    url = f"{FRONTEND_URL}/portal-recibos/{quote(token, safe='')}"
    return ReciboPortalLinkOut(
        url=url,
        cliente_id=row.cliente.id,
        cliente_nombre=row.cliente.nombre_completo,
    )


@router.post("/clientes/{cliente_id}/portal/regenerar", response_model=ReciboPortalLinkOut)
def regenerate_client_portal_link(
    cliente_id: int,
    current: Usuario = Depends(require_roles("gerente", "administrativo")),
    db: Session = Depends(get_db),
):
    del current
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id, Cliente.activo.is_(True)).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    cliente.portal_token = None
    token = ensure_client_portal_token(cliente)
    now = utcnow()
    sessions = db.query(ClientePortalSession).filter(
        ClientePortalSession.cliente_id == cliente.id,
        ClientePortalSession.revoked_at.is_(None),
    ).all()
    for portal_session in sessions:
        portal_session.revoked_at = now
    db.commit()
    return ReciboPortalLinkOut(
        url=f"{FRONTEND_URL}/portal-recibos/{quote(token, safe='')}",
        cliente_id=cliente.id,
        cliente_nombre=cliente.nombre_completo,
    )


@router.post("/recibos/{recibo_id}/liquidar", response_model=ReciboOut)
async def liquidate_receipt(
    recibo_id: int,
    nota: str = Form(..., min_length=3, max_length=2000),
    numero_transferencia: str = Form(..., min_length=3, max_length=180),
    monto: Decimal = Form(..., gt=0, max_digits=18, decimal_places=2),
    comprobante: UploadFile = File(...),
    current: Usuario = Depends(require_roles("gerente", "administrativo")),
    db: Session = Depends(get_db),
):
    row = recibo_query(db).filter(ReciboCafe.id == recibo_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="Recibo no encontrado")
    if bool(row.liquidado):
        raise HTTPException(status_code=409, detail="El recibo ya está liquidado")
    if str(row.estado or "").lower() == "anulado":
        raise HTTPException(status_code=409, detail="No se puede liquidar un recibo anulado")

    clean_note = nota.strip()
    clean_transfer = numero_transferencia.strip()
    if len(clean_note) < 3:
        raise HTTPException(status_code=422, detail="Indique una nota de liquidación")
    if len(clean_transfer) < 3:
        raise HTTPException(status_code=422, detail="Indique el número de transferencia")
    if monto <= 0:
        raise HTTPException(status_code=422, detail="El monto pagado debe ser mayor que cero")

    storage_key: str | None = None
    try:
        storage_key, original_name, content_type, size = await save_private_receipt_proof(comprobante, row.id)
        row.liquidado = True
        row.liquidado_at = utcnow()
        row.liquidado_por_id = current.id
        row.liquidado_por_nombre_snapshot = current.nombre
        row.liquidacion_nota = clean_note
        row.liquidacion_numero_transferencia = clean_transfer
        row.liquidacion_monto = monto.quantize(Decimal("0.01"))
        row.liquidacion_comprobante_storage_key = storage_key
        row.liquidacion_comprobante_nombre = original_name
        row.liquidacion_comprobante_tipo = content_type
        row.liquidacion_comprobante_tamano = size
        db.commit()
    except HTTPException:
        db.rollback()
        delete_private_proof(storage_key)
        raise
    except Exception as exc:
        db.rollback()
        delete_private_proof(storage_key)
        raise HTTPException(status_code=500, detail="No se pudo registrar la liquidación") from exc

    refreshed = recibo_query(db).filter(ReciboCafe.id == recibo_id).first()
    return serialize_recibo(refreshed)


@router.get("/recibos/{recibo_id}/liquidacion/comprobante")
def download_receipt_payment_proof(
    recibo_id: int,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del current
    row = db.query(ReciboCafe).filter(ReciboCafe.id == recibo_id).first()
    if not row or not row.liquidacion_comprobante_storage_key:
        raise HTTPException(status_code=404, detail="Comprobante no disponible")
    path = resolve_private_proof_path(row.liquidacion_comprobante_storage_key)
    return FileResponse(
        path,
        media_type=row.liquidacion_comprobante_tipo or "application/octet-stream",
        filename=row.liquidacion_comprobante_nombre or path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/public/recibos/{access_token}/verify", response_model=PortalRecibosVerifyOut)
def verify_receipt_portal(
    access_token: str,
    payload: PortalRecibosVerifyIn,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(
        request,
        "receipt-portal-verify",
        RECEIPT_PORTAL_VERIFY_LIMIT,
        RECEIPT_PORTAL_RATE_WINDOW_SECONDS,
    )
    cliente = find_portal_client(db, access_token)
    if not cliente or not identification_matches(cliente, payload.numero_identificacion):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Datos de acceso inválidos")

    raw_token, portal_session = create_portal_session(db, cliente, client_ip(request))
    db.commit()
    return PortalRecibosVerifyOut(
        session_token=raw_token,
        expires_at=portal_session.expires_at,
        cliente_nombre=cliente.nombre_completo,
    )


@router.get("/public/recibos/{access_token}", response_model=PortalRecibosOut)
def get_receipt_portal(
    access_token: str,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    cliente, _ = validate_portal_session(db, access_token, x_portal_token)
    rows = db.query(ReciboCafe).options(
        selectinload(ReciboCafe.finca),
    ).filter(
        ReciboCafe.cliente_id == cliente.id,
        ReciboCafe.estado != "anulado",
    ).order_by(ReciboCafe.fecha.desc(), ReciboCafe.id.desc()).all()
    return build_portal_payload(cliente, rows)


@router.get("/public/recibos/{access_token}/{recibo_id}/pdf")
def download_public_receipt_pdf(
    access_token: str,
    recibo_id: int,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    cliente, _ = validate_portal_session(db, access_token, x_portal_token)
    row = recibo_query(db).filter(
        ReciboCafe.id == recibo_id,
        ReciboCafe.cliente_id == cliente.id,
        ReciboCafe.estado != "anulado",
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recibo no encontrado")

    pdf = build_receipt_pdf(row, cliente)
    safe_number = re.sub(r"[^A-Za-z0-9._-]+", "-", str(row.numero_recibo or "recibo")).strip(".-")
    filename = f"recibo-{safe_number or row.id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/public/recibos/{access_token}/{recibo_id}/oficial", response_model=ReciboOut)
def get_public_official_receipt(
    access_token: str,
    recibo_id: int,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    """Entrega los mismos datos que alimentan el formato oficial interno."""
    cliente, _ = validate_portal_session(db, access_token, x_portal_token)
    row = recibo_query(db).filter(
        ReciboCafe.id == recibo_id,
        ReciboCafe.cliente_id == cliente.id,
        ReciboCafe.estado != "anulado",
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recibo no encontrado")
    return serialize_recibo(row)


@router.post("/public/recibos/{access_token}/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout_receipt_portal(
    access_token: str,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    _, portal_session = validate_portal_session(db, access_token, x_portal_token)
    portal_session.revoked_at = utcnow()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
