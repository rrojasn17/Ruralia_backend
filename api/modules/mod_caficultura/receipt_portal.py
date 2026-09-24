from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
import secrets
import uuid
from urllib.request import Request as UrlRequest, urlopen

from fastapi import HTTPException, UploadFile, status
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from config import (
    RECEIPT_PORTAL_SESSION_MINUTES,
    RECEIPT_OFFICIAL_LOGO_URL,
    RECEIPT_PRIVATE_UPLOAD_DIR,
    RECEIPT_PROOF_MAX_MB,
)
from modules.mod_caficultura.models import Cliente, ClientePortalSession, ReciboCafe
from modules.mod_caficultura.schemas import (
    PortalReciboItemOut,
    PortalReciboResumenOut,
    PortalRecibosOut,
)
from security import generar_token, hash_token
from modules.mod_caficultura.services import cosecha_from_date, numero_a_letras_es


_IDENTIFICATION_CLEANER = re.compile(r"[^0-9A-Z]")
_STORAGE_KEY = re.compile(r"^[1-9][0-9]*/[a-f0-9]{32}\.(?:pdf|png|jpg|webp)$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_identification(value: str | None) -> str:
    return _IDENTIFICATION_CLEANER.sub("", str(value or "").strip().upper())


def receipt_cajuelas(row: ReciboCafe) -> float:
    return round(float(row.cajuelas or 0) + (float(row.cuartillos or 0) / 4), 4)


def receipt_fanegas(row: ReciboCafe) -> float:
    return round(receipt_cajuelas(row) / 20, 4)


def receipt_amount(row: ReciboCafe) -> float:
    return round(receipt_fanegas(row) * float(row.precio_fanega or 0), 2)


def effective_receipt_amount(row: ReciboCafe) -> float:
    if bool(row.liquidado) and row.liquidacion_monto is not None:
        return round(float(row.liquidacion_monto), 2)
    return receipt_amount(row)


def ensure_client_portal_token(cliente: Cliente) -> str:
    token = str(cliente.portal_token or "").strip()
    if not token:
        token = generar_token(32)
        cliente.portal_token = token
    return token


def find_portal_client(db: Session, access_token: str) -> Cliente | None:
    token = str(access_token or "").strip()
    if len(token) < 32 or len(token) > 255:
        return None
    return db.query(Cliente).filter(Cliente.portal_token == token, Cliente.activo.is_(True)).first()


def create_portal_session(db: Session, cliente: Cliente, requested_ip: str | None) -> tuple[str, ClientePortalSession]:
    now = utcnow()
    db.query(ClientePortalSession).filter(
        ClientePortalSession.cliente_id == cliente.id,
        ClientePortalSession.expires_at <= now,
    ).delete(synchronize_session=False)

    raw_token = generar_token(40)
    session = ClientePortalSession(
        cliente_id=cliente.id,
        token_hash=hash_token(raw_token),
        expires_at=now + timedelta(minutes=RECEIPT_PORTAL_SESSION_MINUTES),
        requested_ip=(requested_ip or "")[:80] or None,
    )
    db.add(session)
    db.flush()
    return raw_token, session


def validate_portal_session(
    db: Session,
    access_token: str,
    raw_session_token: str | None,
) -> tuple[Cliente, ClientePortalSession]:
    cliente = find_portal_client(db, access_token)
    token = str(raw_session_token or "").strip()
    if not cliente or len(token) < 32:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión pública inválida o vencida")

    session = db.query(ClientePortalSession).filter(
        ClientePortalSession.cliente_id == cliente.id,
        ClientePortalSession.token_hash == hash_token(token),
        ClientePortalSession.revoked_at.is_(None),
    ).first()
    now = utcnow()
    if not session or _normalize_datetime(session.expires_at) <= now:
        if session and session.revoked_at is None:
            session.revoked_at = now
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión pública inválida o vencida")

    last_used = _normalize_datetime(session.last_used_at) if session.last_used_at else None
    if last_used is None or (now - last_used).total_seconds() >= 300:
        session.last_used_at = now
        db.commit()
    return cliente, session


def identification_matches(cliente: Cliente, candidate: str) -> bool:
    expected = normalize_identification(cliente.numero_identificacion)
    supplied = normalize_identification(candidate)
    if len(expected) < 3 or len(supplied) < 3:
        return False
    return secrets.compare_digest(expected, supplied)


def build_portal_payload(cliente: Cliente, rows: list[ReciboCafe]) -> PortalRecibosOut:
    items: list[PortalReciboItemOut] = []
    total_cajuelas = 0.0
    total_fanegas = 0.0
    total_amount = 0.0
    paid_amount = 0.0
    paid_count = 0

    for row in rows:
        cajuelas = receipt_cajuelas(row)
        fanegas = receipt_fanegas(row)
        amount = effective_receipt_amount(row)
        liquidado = bool(row.liquidado)
        total_cajuelas += cajuelas
        total_fanegas += fanegas
        total_amount += amount
        if liquidado:
            paid_count += 1
            paid_amount += amount

        items.append(PortalReciboItemOut(
            id=row.id,
            numero_recibo=row.numero_recibo,
            fecha=row.fecha,
            cosecha=row.cosecha,
            finca_nombre=row.finca.nombre if row.finca else None,
            cajuelas=float(row.cajuelas or 0),
            cuartillos=float(row.cuartillos or 0),
            fanegas=fanegas,
            precio_fanega=float(row.precio_fanega or 0),
            monto=amount,
            liquidado=liquidado,
            estado_liquidacion="liquidado" if liquidado else "pendiente",
            liquidado_at=row.liquidado_at,
            liquidacion_monto=float(row.liquidacion_monto) if row.liquidacion_monto is not None else None,
            liquidacion_numero_transferencia=row.liquidacion_numero_transferencia,
        ))

    pending_count = max(0, len(items) - paid_count)
    return PortalRecibosOut(
        cliente_nombre=cliente.nombre_completo,
        resumen=PortalReciboResumenOut(
            total_recibos=len(items),
            total_cajuelas=round(total_cajuelas, 2),
            total_fanegas=round(total_fanegas, 3),
            monto_total=round(total_amount, 2),
            monto_cobrado=round(paid_amount, 2),
            monto_por_cobrar=round(max(0, total_amount - paid_amount), 2),
            recibos_liquidados=paid_count,
            recibos_pendientes=pending_count,
        ),
        recibos=items,
    )


async def save_private_receipt_proof(file: UploadFile, recibo_id: int) -> tuple[str, str, str, int]:
    max_bytes = RECEIPT_PROOF_MAX_MB * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if not content:
        raise HTTPException(status_code=422, detail="El comprobante está vacío")
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El comprobante no debe superar {RECEIPT_PROOF_MAX_MB} MB",
        )

    extension, content_type = _detect_proof_type(content)
    original_name = _safe_original_name(file.filename, extension)
    storage_key = f"{recibo_id}/{uuid.uuid4().hex}{extension}"
    path = resolve_private_proof_path(storage_key, require_exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as buffer:
        buffer.write(content)
    return storage_key, original_name, content_type, len(content)


def resolve_private_proof_path(storage_key: str, *, require_exists: bool = True) -> Path:
    key = str(storage_key or "").strip()
    if not _STORAGE_KEY.fullmatch(key):
        raise HTTPException(status_code=404, detail="Comprobante no disponible")

    root = Path(RECEIPT_PRIVATE_UPLOAD_DIR).resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=404, detail="Comprobante no disponible")
    if require_exists and not path.is_file():
        raise HTTPException(status_code=404, detail="Comprobante no disponible")
    return path


def delete_private_proof(storage_key: str | None) -> None:
    if not storage_key:
        return
    try:
        resolve_private_proof_path(storage_key).unlink(missing_ok=True)
    except HTTPException:
        return


def build_receipt_pdf(row: ReciboCafe, cliente: Cliente) -> bytes:
    """Genera el formato oficial usado por el botón Imprimir de NAVIA.

    El documento conserva la composición apaisada del recibo físico: encabezado
    comercial, consecutivo, fecha, controles de calidad, cantidades y firmas.
    No agrega widgets, estados de pago ni diseños propios del portal.
    """
    buffer = BytesIO()
    page_width, page_height = landscape(letter)
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    pdf.setTitle(f"Recibo {row.numero_recibo}")
    pdf.setAuthor("INVERSIONES COFFEE NACE S.A.")

    paper_width = 257 * mm
    paper_height = 152 * mm
    paper_x = (page_width - paper_width) / 2
    paper_y = page_height - paper_height - 9 * mm
    ink = HexColor("#111827")
    muted = HexColor("#6B7280")
    red = HexColor("#D94848")
    line = HexColor("#9CA3AF")

    pdf.setStrokeColor(HexColor("#D4D4D4"))
    pdf.setFillColor(HexColor("#FFFFFF"))
    pdf.rect(paper_x, paper_y, paper_width, paper_height, fill=1, stroke=1)

    inner_x = paper_x + 8 * mm
    top = paper_y + paper_height - 7 * mm
    logo_width = 49 * mm
    logo_height = 42 * mm
    logo = _official_logo_reader()
    if logo:
        pdf.drawImage(
            logo,
            inner_x + 2 * mm,
            top - logo_height,
            width=45 * mm,
            height=41 * mm,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
    else:
        pdf.setFillColor(HexColor("#111A36"))
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawCentredString(inner_x + logo_width / 2, top - 17 * mm, "CAFÉ NAVARRO")
        pdf.setFont("Helvetica", 8)
        pdf.drawCentredString(inner_x + logo_width / 2, top - 23 * mm, "Desde 1927 · Costa Rica")

    center_x = inner_x + 56 * mm
    ribbon_y = top - 13 * mm
    pdf.setStrokeColor(line)
    pdf.setLineWidth(1.1)
    pdf.roundRect(center_x + 38 * mm, ribbon_y, 49 * mm, 12 * mm, 2 * mm, fill=0, stroke=1)
    pdf.line(center_x + 2 * mm, ribbon_y + 2 * mm, center_x + 38 * mm, ribbon_y + 2 * mm)
    pdf.line(center_x + 2 * mm, ribbon_y + 10 * mm, center_x + 38 * mm, ribbon_y + 10 * mm)
    pdf.line(center_x + 87 * mm, ribbon_y + 2 * mm, center_x + 123 * mm, ribbon_y + 2 * mm)
    pdf.line(center_x + 87 * mm, ribbon_y + 10 * mm, center_x + 123 * mm, ribbon_y + 10 * mm)
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 14)
    pdf.drawCentredString(center_x + 62.5 * mm, ribbon_y + 4 * mm, "Recibo por café")

    company_y = ribbon_y - 5 * mm
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(center_x + 11 * mm, company_y, "INVERSIONES COFFEE NACE S.A.")
    pdf.line(center_x + 11 * mm, company_y - 1, center_x + 72 * mm, company_y - 1)
    company_lines = [
        "Río Conejo, Corralillo, Cartago",
        "Cédula Jurídica: 3-101-719291",
        "Cel: 506 83 79 33 78",
        f"Cosecha de Café {row.cosecha or cosecha_from_date(row.fecha)}",
    ]
    pdf.setFont("Helvetica", 10.5)
    for index, value in enumerate(company_lines, start=1):
        pdf.drawString(center_x + 11 * mm, company_y - index * 5 * mm, value)

    number_x = inner_x + 190 * mm
    pdf.setFillColor(red)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawCentredString(number_x + 27 * mm, top - 14 * mm, f"N° {row.numero_recibo}")
    date_x = number_x + 4 * mm
    date_y = top - 34 * mm
    cell_width = 15 * mm
    cell_height = 7 * mm
    pdf.setStrokeColor(ink)
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica", 9)
    for index, label in enumerate(("Día", "Mes", "Año")):
        x = date_x + index * cell_width
        pdf.rect(x, date_y + cell_height, cell_width, cell_height, fill=0, stroke=1)
        pdf.rect(x, date_y, cell_width, cell_height, fill=0, stroke=1)
        pdf.drawCentredString(x + cell_width / 2, date_y + cell_height + 2.2 * mm, label)
    for index, value in enumerate((f"{row.fecha.day:02d}", f"{row.fecha.month:02d}", str(row.fecha.year))):
        pdf.drawCentredString(date_x + index * cell_width + cell_width / 2, date_y + 2.2 * mm, value)

    body_top = top - 51 * mm
    quality_width = 20 * mm
    quality_height = 17 * mm
    quality_labels = (
        ("Peso promedio", "por cajuela", _number(row.peso_promedio_cajuela or row.precio_promedio_cajuela, 2)),
        ("% Flote", "", _number(row.porcentaje_flote, 2)),
        ("% Verde", "", _number(row.porcentaje_verde, 2)),
    )
    pdf.setStrokeColor(ink)
    for index, (label, secondary, value) in enumerate(quality_labels):
        y = body_top - (index + 1) * quality_height
        pdf.rect(inner_x, y, quality_width, quality_height, fill=0, stroke=1)
        pdf.setFont("Helvetica", 6.7)
        pdf.drawString(inner_x + 1.5 * mm, y + 11 * mm, label)
        if secondary:
            pdf.drawString(inner_x + 1.5 * mm, y + 8 * mm, secondary)
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawCentredString(inner_x + quality_width / 2, y + 3 * mm, value)

    data_x = inner_x + quality_width + 5 * mm
    data_right = paper_x + paper_width - 8 * mm
    cursor_y = body_top - 3 * mm

    def field_line(label: str, value: object, x: float, width: float, y: float, label_width: float) -> None:
        pdf.setFillColor(ink)
        pdf.setFont("Helvetica-Bold", 9.5)
        pdf.drawString(x, y, label)
        pdf.setFont("Helvetica", 9.5)
        _draw_fitted(pdf, str(value or ""), x + label_width, y, width - label_width, 9.5)
        pdf.setStrokeColor(line)
        pdf.line(x + label_width, y - 1.5 * mm, x + width, y - 1.5 * mm)

    field_line("Recibí del sr:", row.productor_nombre, data_x, data_right - data_x, cursor_y, 25 * mm)
    cursor_y -= 10 * mm
    half = (data_right - data_x - 5 * mm) / 2
    field_line("Ced:", row.productor_cedula or "N/D", data_x, half, cursor_y, 11 * mm)
    field_line("Zona:", row.zona or "", data_x + half + 5 * mm, half, cursor_y, 13 * mm)
    cursor_y -= 10 * mm
    third = (data_right - data_x - 8 * mm) / 3
    field_line("Provincia:", row.provincia or "N/D", data_x, third, cursor_y, 20 * mm)
    field_line("Cantón:", row.canton or "N/D", data_x + third + 4 * mm, third, cursor_y, 16 * mm)
    field_line("Distrito:", row.distrito or "N/D", data_x + 2 * (third + 4 * mm), third, cursor_y, 17 * mm)
    cursor_y -= 10 * mm
    field_line("Cajuelas:", _number(row.cajuelas, 2), data_x, third, cursor_y, 18 * mm)
    field_line("Cuartillos:", _number(row.cuartillos, 2), data_x + third + 4 * mm, third, cursor_y, 20 * mm)
    field_line("Fanegas:", _number(receipt_fanegas(row), 3), data_x + 2 * (third + 4 * mm), third, cursor_y, 18 * mm)
    cursor_y -= 10 * mm
    price_words = row.precio_fanega_letras or row.precio_adelanto_letras or (
        numero_a_letras_es(row.precio_fanega) if row.precio_fanega else ""
    )
    field_line("Precio de la fanega en letras:", price_words, data_x, data_right - data_x, cursor_y, 57 * mm)
    cursor_y -= 11 * mm
    field_line("Firma operario beneficio:", row.beneficio_recibe or "", data_x, half, cursor_y, 47 * mm)
    field_line("P/Productor:", row.productor_entrega or "", data_x + half + 5 * mm, half, cursor_y, 25 * mm)
    cursor_y -= 10 * mm
    if row.observaciones:
        field_line("Observaciones:", row.observaciones, data_x, data_right - data_x, cursor_y, 27 * mm)

    footer = (
        "El Precio del Café será conforme a la regulación establecida por la Ley N°1967 "
        "y sus respectivas reformas y reglamentos."
    )
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawCentredString(paper_x + paper_width / 2, paper_y + 5 * mm, footer)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


@lru_cache(maxsize=1)
def _official_logo_bytes() -> bytes | None:
    url = str(RECEIPT_OFFICIAL_LOGO_URL or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        request = UrlRequest(url, headers={"User-Agent": "NAVIA-Receipt/1.0"})
        with urlopen(request, timeout=2.5) as response:  # noqa: S310 - URL is controlled by server configuration.
            content_type = str(response.headers.get("Content-Type") or "").lower()
            content = response.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024 or not content_type.startswith("image/"):
            return None
        return content
    except Exception:
        return None


def _official_logo_reader() -> ImageReader | None:
    content = _official_logo_bytes()
    if not content:
        return None
    try:
        return ImageReader(BytesIO(content))
    except Exception:
        return None


def _draw_fitted(pdf: canvas.Canvas, value: str, x: float, y: float, width: float, font_size: float) -> None:
    clean = " ".join(str(value or "").split())
    if not clean:
        return
    selected_size = font_size
    while selected_size > 6.5 and stringWidth(clean, "Helvetica", selected_size) > width:
        selected_size -= 0.5
    if stringWidth(clean, "Helvetica", selected_size) > width:
        while clean and stringWidth(f"{clean}…", "Helvetica", selected_size) > width:
            clean = clean[:-1]
        clean = f"{clean.rstrip()}…"
    pdf.setFont("Helvetica", selected_size)
    pdf.drawString(x, y, clean)


def _number(value: object, decimals: int) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")


def _detect_proof_type(content: bytes) -> tuple[str, str]:
    if content.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise HTTPException(status_code=422, detail="Use un comprobante PDF, PNG, JPG o WEBP válido")


def _safe_original_name(value: str | None, extension: str) -> str:
    raw = Path(value or f"comprobante{extension}").name
    cleaned = "".join(ch if ch.isalnum() or ch in {".", "-", "_", " "} else "_" for ch in raw).strip(" ._")
    if not cleaned:
        return f"comprobante{extension}"
    current_extension = Path(cleaned).suffix.lower()
    compatible = {extension}
    if extension == ".jpg":
        compatible.add(".jpeg")
    if current_extension not in compatible:
        raise HTTPException(status_code=422, detail="La extensión del comprobante no coincide con su contenido")
    return cleaned[:255]


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
