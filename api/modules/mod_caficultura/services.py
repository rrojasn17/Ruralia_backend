
from __future__ import annotations
import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session, selectinload

from config import DEFAULT_SESSION_DAYS, TRAINING_CURRENT_VERSION
from modules.mod_caficultura.models import (
    ActividadFinca,
    Cliente,
    Finca,
    InsumoFinca,
    OrdenTrabajo,
    OrdenTrabajoRecibo,
    OrdenTrabajoUnionOrigen,
    ReciboCafe,
    ReciboConsecutivo,
    SeguimientoOT,
    TrabajadorFinca,
    DocumentoLote,
    ComentarioLote,
    CotizacionVenta,
    CotizacionVentaLinea,
    SolicitudSalidaVenta,
    SolicitudSalidaVentaLinea,
    VentaLeadLote,
    VentaSeguimiento,
    SolicitudVenta,
    SolicitudVentaLinea,
    SolicitudVentaLineaLiquidacion,
    SolicitudVentaSeguimiento,
    SolicitudVentaDocumento,
)
from core.models import ConfiguracionSistema, PasswordResetToken, SesionUsuario, Usuario
from modules.mod_caficultura.schemas import ClienteOut, FincaOut, UsuarioOut, ReciboOut, OTOut, OTReciboOut, SeguimientoOut, DocumentoLoteOut, ComentarioLoteOut, OTUnionOrigenOut, CotizacionVentaOut, CotizacionVentaLineaOut, SolicitudSalidaVentaOut, SolicitudSalidaVentaLineaOut, VentaLeadOut, VentaSeguimientoOut, SolicitudVentaOut, SolicitudVentaSeguimientoOut, SolicitudVentaDocumentoOut, SolicitudVentaLineaOut, SolicitudVentaLineaLiquidacionOut
from security import generar_token, hash_token, hashear_contrasena, verificar_contrasena

ROLE_PERMISSIONS = {
    "gerente": ["*"],
    "operario": [
        "dashboard:view",
        "clientes:view",
        "fincas:view",
        "recibos:view",
        "recibos:create",
        "ot:view",
        "ot:followup",
        "offline:sync",
    ],
    "administrativo": [
        "dashboard:view",
        "clientes:view",
        "clientes:create",
        "clientes:update",
        "fincas:view",
        "fincas:create",
        "fincas:update",
        "recibos:view",
        "recibos:create",
        "recibos:update",
        "ot:view",
        "insumos:view",
        "offline:sync",
    ],
    "supervisor_finca": [
        "dashboard:view",
        "clientes:view",
        "fincas:view",
        "gestionfincas:view",
        "gestionfincas:create",
        "gestionfincas:update",
        "insumos:view",
        "offline:sync",
    ],
}

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def user_permissions(role: str) -> list[str]:
    return ROLE_PERMISSIONS.get((role or "operario").lower(), ROLE_PERMISSIONS["operario"])


def usuario_out(u: Usuario) -> UsuarioOut:
    is_superadmin = bool(getattr(u, "is_superadmin", False))
    onboarding_version = int(getattr(u, "onboarding_version", 0) or 0)
    return UsuarioOut(
        id=u.id,
        nombre=u.nombre,
        correo=u.correo,
        rol=u.rol,
        activo=bool(u.activo),
        is_superadmin=is_superadmin,
        firma_url=getattr(u, "firma_url", None),
        permisos=user_permissions(u.rol),
        onboarding_version=onboarding_version,
        onboarding_current_version=TRAINING_CURRENT_VERSION,
        onboarding_required=onboarding_version < TRAINING_CURRENT_VERSION,
        onboarding_completed_at=getattr(u, "onboarding_completed_at", None),
        created_at=u.created_at,
    )


def get_config_int(db: Session, key: str, default: int) -> int:
    row = db.query(ConfiguracionSistema).filter(ConfiguracionSistema.clave == key).first()
    if not row:
        return default
    try:
        return int(row.valor)
    except Exception:
        return default


def set_config(db: Session, key: str, value: str, descripcion: str | None = None) -> ConfiguracionSistema:
    row = db.query(ConfiguracionSistema).filter(ConfiguracionSistema.clave == key).first()
    if not row:
        row = ConfiguracionSistema(clave=key, valor=str(value), descripcion=descripcion)
        db.add(row)
    else:
        row.valor = str(value)
        if descripcion is not None:
            row.descripcion = descripcion
    return row


def create_session(db: Session, usuario: Usuario) -> tuple[SesionUsuario, str]:
    days = get_config_int(db, "session_days", DEFAULT_SESSION_DAYS)
    raw_token = generar_token(48)
    session = SesionUsuario(
        usuario_id=usuario.id,
        token=hash_token(raw_token),
        expires_at=utcnow() + timedelta(days=days),
    )
    db.add(session)
    db.flush()
    return session, raw_token


def estimated_fanegas(cajuelas: float | None, cuartillos: float | None) -> float:
    # Referencia operativa: 20 cajuelas ≈ 1 fanega, 4 cuartillos = 1 cajuela.
    total_cajuelas = float(cajuelas or 0) + (float(cuartillos or 0) / 4)
    return round(total_cajuelas / 20, 3)


def cosecha_from_date(value: date) -> str:
    year = value.year
    if value.month >= 10:
        return f"{year}-{year + 1}"
    return f"{year - 1}-{year}"


UNIDADES = [
    "", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
    "diez", "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve",
]
DECENAS = {
    20: "veinte", 30: "treinta", 40: "cuarenta", 50: "cincuenta",
    60: "sesenta", 70: "setenta", 80: "ochenta", 90: "noventa",
}
CENTENAS = {
    100: "cien", 200: "doscientos", 300: "trescientos", 400: "cuatrocientos",
    500: "quinientos", 600: "seiscientos", 700: "setecientos", 800: "ochocientos", 900: "novecientos",
}


def numero_a_letras_es(value: float | int | None) -> str:
    """Convierte montos enteros a letras en español para recibos.

    Pensado para colones sin céntimos: 120000 -> "ciento veinte mil".
    """
    try:
        n = int(round(float(value or 0)))
    except Exception:
        n = 0

    if n <= 0:
        return "cero"

    def hasta_999(num: int) -> str:
        if num < 20:
            return UNIDADES[num]
        if num < 30:
            if num == 20:
                return "veinte"
            return "veinti" + UNIDADES[num - 20]
        if num < 100:
            decena = (num // 10) * 10
            unidad = num % 10
            return DECENAS[decena] if unidad == 0 else f"{DECENAS[decena]} y {UNIDADES[unidad]}"
        if num in CENTENAS:
            return CENTENAS[num]
        centena = (num // 100) * 100
        resto = num % 100
        prefijo = "ciento" if centena == 100 else CENTENAS[centena]
        return f"{prefijo} {hasta_999(resto)}"

    def convertir(num: int) -> str:
        if num < 1000:
            return hasta_999(num)
        if num < 1_000_000:
            miles = num // 1000
            resto = num % 1000
            prefijo = "mil" if miles == 1 else f"{convertir(miles)} mil"
            return prefijo if resto == 0 else f"{prefijo} {hasta_999(resto)}"
        millones = num // 1_000_000
        resto = num % 1_000_000
        prefijo = "un millón" if millones == 1 else f"{convertir(millones)} millones"
        return prefijo if resto == 0 else f"{prefijo} {convertir(resto)}"

    return convertir(n).strip()


def next_code(db: Session, model, field: str, prefix: str, width: int = 5) -> str:
    col = getattr(model, field)
    total = db.query(model).count() or 0
    n = total + 1
    while True:
        code = f"{prefix}{n:0{width}d}"
        if not db.query(model).filter(col == code).first():
            return code
        n += 1


RECEIPT_COUNTER_ID = 1


def format_receipt_number(prefix: str, number: int, width: int) -> str:
    return f"{prefix}{number:0{width}d}"


def receipt_number_value(value: str | None, prefix: str) -> int | None:
    raw = str(value or "").strip()
    if not raw.startswith(prefix):
        return None
    suffix = raw[len(prefix):]
    if not suffix.isdigit():
        return None
    return int(suffix)


def last_receipt_number(db: Session, prefix: str) -> int | None:
    values = db.query(ReciboCafe.numero_recibo).filter(
        ReciboCafe.numero_recibo.like(f"{prefix}%")
    ).all()
    numbers = [
        parsed
        for (value,) in values
        if (parsed := receipt_number_value(value, prefix)) is not None
    ]
    return max(numbers) if numbers else None


def get_receipt_counter(db: Session, *, lock: bool = False) -> ReciboConsecutivo:
    query = db.query(ReciboConsecutivo).filter(ReciboConsecutivo.id == RECEIPT_COUNTER_ID)
    if lock:
        query = query.with_for_update()
    counter = query.first()
    if counter:
        return counter

    # bootstrap crea esta fila antes de recibir tráfico. Este fallback cubre
    # instalaciones nuevas y mantiene un valor coherente con datos existentes.
    prefix = "RC-"
    last_used = last_receipt_number(db, prefix) or 0
    counter = ReciboConsecutivo(
        id=RECEIPT_COUNTER_ID,
        prefijo=prefix,
        siguiente_numero=last_used + 1,
        ancho=5,
    )
    db.add(counter)
    db.flush()
    return counter


def preview_receipt_number(db: Session) -> str:
    counter = get_receipt_counter(db)
    candidate = max(1, int(counter.siguiente_numero or 1))
    while db.query(ReciboCafe.id).filter(
        ReciboCafe.numero_recibo == format_receipt_number(counter.prefijo, candidate, counter.ancho)
    ).first():
        candidate += 1
    return format_receipt_number(counter.prefijo, candidate, counter.ancho)


def allocate_receipt_number(db: Session) -> str:
    """Reserva el siguiente número dentro de la transacción actual.

    El bloqueo de la única fila serializa emisores concurrentes en PostgreSQL;
    el índice UNIQUE de recibos permanece como la última barrera de integridad.
    """
    counter = get_receipt_counter(db, lock=True)
    candidate = max(1, int(counter.siguiente_numero or 1))
    while True:
        receipt_number = format_receipt_number(counter.prefijo, candidate, counter.ancho)
        exists = db.query(ReciboCafe.id).filter(ReciboCafe.numero_recibo == receipt_number).first()
        if not exists:
            counter.siguiente_numero = candidate + 1
            db.flush()
            return receipt_number
        candidate += 1


def advance_receipt_counter(db: Session, receipt_number: str) -> None:
    """Avanza el contador si se registra/importa un número de su misma serie."""
    counter = get_receipt_counter(db, lock=True)
    parsed = receipt_number_value(receipt_number, counter.prefijo)
    if parsed is not None and parsed >= int(counter.siguiente_numero or 1):
        counter.siguiente_numero = parsed + 1
        db.flush()
def serialize_cliente(row: Cliente) -> ClienteOut:
    return ClienteOut(
        id=row.id,
        codigo=row.codigo,
        nombre_completo=row.nombre_completo,
        tipo_persona=row.tipo_persona,
        categoria=row.categoria,
        numero_identificacion=row.numero_identificacion,
        direccion=row.direccion,
        provincia=row.provincia,
        canton=row.canton,
        distrito=row.distrito,
        telefono=getattr(row, "telefono", None),
        correo=getattr(row, "correo", None),
        activo=bool(row.activo),
        created_at=row.created_at,
    )


def calcular_produccion_finca(db: Session, finca: Finca) -> float | None:
    if not finca.area or finca.area <= 0:
        return None

    recibos = (
        db.query(ReciboCafe)
        .filter(ReciboCafe.finca_id == finca.id, ReciboCafe.estado != "anulado")
        .all()
    )

    total_fanegas = sum(estimated_fanegas(r.cajuelas, r.cuartillos) for r in recibos)
    return round(total_fanegas / float(finca.area), 2)


def serialize_finca(db: Session, row: Finca) -> FincaOut:
    return FincaOut(
        id=row.id,
        cliente_id=row.cliente_id,
        cliente_nombre=row.cliente.nombre_completo if row.cliente else None,
        cliente_correo=getattr(row.cliente, "correo", None) if row.cliente else None,
        cliente_telefono=getattr(row.cliente, "telefono", None) if row.cliente else None,
        codigo=row.codigo,
        nombre=row.nombre,
        ubicacion=row.ubicacion,
        google_maps_url=row.google_maps_url,
        provincia=row.provincia,
        canton=row.canton,
        distrito=row.distrito,
        area=row.area,
        cultivo=row.cultivo,
        caracteristicas=row.caracteristicas,
        propietario=row.propietario,
        altitud=row.altitud,
        variedades=row.variedades or [],
        activa=bool(row.activa),
        gestion_fincas_habilitada=bool(getattr(row, "gestion_fincas_habilitada", False)),
        produccion_fanegas_ha=calcular_produccion_finca(db, row),
        created_at=row.created_at,
    )

def recibo_payload(row: ReciboCafe) -> str:
    return f"NAVIA|RECIBO|{row.numero_recibo}|PRODUCTOR={row.productor_nombre}|FECHA={row.fecha.isoformat()}"


def ot_payload(row: OrdenTrabajo) -> str:
    return f"NAVIA|OT|{row.codigo_lote}|PROCESO={row.proceso}|INICIO={row.fecha_inicio.isoformat()}"


def serialize_recibo(row: ReciboCafe) -> ReciboOut:
    fanegas = estimated_fanegas(row.cajuelas, row.cuartillos)
    monto_estimado = round(fanegas * float(row.precio_fanega or 0), 2)
    liquidado = bool(getattr(row, "liquidado", False))
    return ReciboOut(
        id=row.id,
        numero_recibo=row.numero_recibo,
        client_uuid=row.client_uuid,
        fecha=row.fecha,
        cosecha=row.cosecha,
        cliente_id=row.cliente_id,
        cliente_nombre=row.cliente.nombre_completo if row.cliente else None,
        cliente_correo=getattr(row.cliente, "correo", None) if row.cliente else None,
        cliente_telefono=getattr(row.cliente, "telefono", None) if row.cliente else None,
        finca_id=row.finca_id,
        finca_nombre=row.finca.nombre if row.finca else None,
        productor_nombre=row.productor_nombre,
        productor_cedula=row.productor_cedula,
        provincia=row.provincia,
        canton=row.canton,
        distrito=row.distrito,
        zona=row.zona,
        cajuelas=float(row.cajuelas or 0),
        cuartillos=float(row.cuartillos or 0),
        fanegas_estimadas=fanegas,
        porcentaje_flote=row.porcentaje_flote,
        porcentaje_verde=row.porcentaje_verde,
        peso_promedio_cajuela=(row.peso_promedio_cajuela if getattr(row, "peso_promedio_cajuela", None) is not None else row.precio_promedio_cajuela),
        precio_promedio_cajuela=row.precio_promedio_cajuela,
        precio_fanega=row.precio_fanega,
        precio_fanega_letras=row.precio_fanega_letras or (numero_a_letras_es(row.precio_fanega) if row.precio_fanega else None),
        precio_adelanto_letras=row.precio_adelanto_letras,
        beneficio_recibe_usuario_id=row.beneficio_recibe_usuario_id,
        beneficio_recibe=row.beneficio_recibe,
        beneficio_firma_url=row.beneficio_firma_url or (row.beneficio_usuario.firma_url if getattr(row, "beneficio_usuario", None) else None),
        productor_entrega=row.productor_entrega,
        estado=row.estado,
        observaciones=row.observaciones,
        qr_payload=row.qr_payload,
        monto_estimado=monto_estimado,
        liquidado=liquidado,
        estado_liquidacion="liquidado" if liquidado else "pendiente",
        liquidado_at=getattr(row, "liquidado_at", None),
        liquidado_por_id=getattr(row, "liquidado_por_id", None),
        liquidado_por_nombre=(
            getattr(row, "liquidado_por_nombre_snapshot", None)
            or (row.liquidado_por.nombre if getattr(row, "liquidado_por", None) else None)
        ),
        liquidacion_nota=getattr(row, "liquidacion_nota", None),
        liquidacion_numero_transferencia=getattr(row, "liquidacion_numero_transferencia", None),
        liquidacion_monto=getattr(row, "liquidacion_monto", None),
        liquidacion_comprobante_nombre=getattr(row, "liquidacion_comprobante_nombre", None),
        liquidacion_comprobante_tipo=getattr(row, "liquidacion_comprobante_tipo", None),
        liquidacion_comprobante_tamano=getattr(row, "liquidacion_comprobante_tamano", None),
        created_by_id=row.created_by_id,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_followup(row: SeguimientoOT) -> SeguimientoOut:
    return SeguimientoOut(
        id=row.id,
        client_uuid=row.client_uuid,
        fecha=row.fecha,
        actividad_realizada=row.actividad_realizada,
        horas_implementadas=row.horas_implementadas,
        temperatura=row.temperatura,
        humedad=row.humedad,
        comentario=row.comentario,
        marca_tiempo=getattr(row, "marca_tiempo", None),
        estado_lote_resultante=getattr(row, "estado_lote_resultante", None),
        dar_alerta=bool(getattr(row, "dar_alerta", False)),
        alerta_mensaje=getattr(row, "alerta_mensaje", None),
        alerta_canal=getattr(row, "alerta_canal", None),
        alerta_whatsapp_url=getattr(row, "alerta_whatsapp_url", None),
        alerta_correo_url=getattr(row, "alerta_correo_url", None),
        estado_revision=getattr(row, "estado_revision", "pendiente"),
        comentario_revision=getattr(row, "comentario_revision", None),
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_lote_documento(row: DocumentoLote) -> DocumentoLoteOut:
    return DocumentoLoteOut(
        id=row.id,
        ot_id=row.ot_id,
        titulo=row.titulo,
        tipo=row.tipo,
        descripcion=row.descripcion,
        file_url=row.file_url,
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_lote_comentario(row: ComentarioLote) -> ComentarioLoteOut:
    return ComentarioLoteOut(
        id=row.id,
        ot_id=row.ot_id,
        tipo=row.tipo,
        comentario=row.comentario,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


OT_PROCESS_STAGES = [
    "Recepción en beneficio",
    "Fermentación",
    "Secado en patio",
    "Secado en guardiola",
    "Pelado",
    "Clasificadora por tamaño",
    "Decimétrica",
    "Clasificado por color",
    "Selección manual",
]


def normalize_trace_stage(value: object) -> str:
    raw = str(value or "").strip().lower()
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n",
    }
    for source, target in replacements.items():
        raw = raw.replace(source, target)
    return "".join(ch for ch in raw if ch.isalnum())


def calculate_ot_progress_percent(row: OrdenTrabajo | None) -> int:
    if not row:
        return 0
    estado = str(getattr(row, "estado", "") or "").lower()
    if estado in {"finalizada", "vendido"}:
        return 100
    if not OT_PROCESS_STAGES:
        return 0
    completadas = {normalize_trace_stage(getattr(seg, "actividad_realizada", None)) for seg in (getattr(row, "seguimientos", []) or [])}
    total = sum(1 for stage in OT_PROCESS_STAGES if normalize_trace_stage(stage) in completadas)
    return min(100, round((total / len(OT_PROCESS_STAGES)) * 100))


def ot_quintales_base(row: OrdenTrabajo | None) -> float:
    """Base comercial del lote en quintales operativos.

    Mantiene una sola fuente visible para frontend: si el lote tiene recibos
    asignados, calcula 20 cajuelas = 1 qq/fanega operativa; si no, usa
    fanegas_estimadas como respaldo.
    """
    if not row:
        return 0.0
    cajuelas = 0.0
    for rel in (getattr(row, "recibos_rel", []) or []):
        cajuelas += float(getattr(rel, "cajuelas_asignadas", 0) or 0)
        cajuelas += float(getattr(rel, "cuartillos_asignados", 0) or 0) / 4
    if cajuelas > 0:
        return round(cajuelas / 20, 3)
    return round(float(getattr(row, "fanegas_estimadas", 0) or 0), 3)


def ot_quintales_vendidos(row: OrdenTrabajo | None) -> float:
    if not row:
        return 0.0
    vendido = 0.0
    for rel in (getattr(row, "solicitudes_salida_venta", []) or []):
        solicitud = getattr(rel, "solicitud", None)
        if solicitud and str(getattr(solicitud, "estado", "") or "").lower() == "anulada":
            continue
        vendido += float(getattr(rel, "cantidad_quintales", 0) or 0)
    return round(vendido, 3)


def ot_quintales_disponibles(row: OrdenTrabajo | None) -> float:
    return round(max(ot_quintales_base(row) - ot_quintales_vendidos(row), 0), 3)


def serialize_ot_recibos(row: OrdenTrabajo | None) -> list[OTReciboOut]:
    recibos: list[OTReciboOut] = []
    if not row:
        return recibos

    for rel in getattr(row, "recibos_rel", []) or []:
        r = rel.recibo
        if not r:
            continue
        recibos.append(OTReciboOut(
            id=r.id,
            numero_recibo=r.numero_recibo,
            productor_nombre=r.productor_nombre,
            cajuelas=float(r.cajuelas or 0),
            cuartillos=float(r.cuartillos or 0),
            fanegas_estimadas=estimated_fanegas(r.cajuelas, r.cuartillos),
            cajuelas_asignadas=float(rel.cajuelas_asignadas or 0),
            cuartillos_asignados=float(rel.cuartillos_asignados or 0),
            fanegas_asignadas=float(rel.fanegas_asignadas or 0),
        ))

    return recibos


def serialize_cotizacion_linea(row: CotizacionVentaLinea) -> CotizacionVentaLineaOut:
    return CotizacionVentaLineaOut(
        id=row.id,
        cotizacion_id=row.cotizacion_id,
        ot_id=row.ot_id,
        codigo_lote=row.ot.codigo_lote if row.ot else None,
        descripcion=row.descripcion,
        cantidad_quintales=float(row.cantidad_quintales or 0),
        precio_unitario=float(row.precio_unitario or 0),
        impuesto_porcentaje=float(row.impuesto_porcentaje or 0),
        subtotal=float(row.subtotal or 0),
        impuesto=float(row.impuesto or 0),
        total=float(row.total or 0),
    )


def serialize_cotizacion_venta(row: CotizacionVenta) -> CotizacionVentaOut:
    lineas = [serialize_cotizacion_linea(linea) for linea in (row.lineas or [])]
    return CotizacionVentaOut(
        id=row.id,
        codigo=row.codigo,
        cliente_id=row.cliente_id,
        cliente_nombre=row.cliente.nombre_completo if row.cliente else None,
        cliente_correo=getattr(row.cliente, "correo", None) if row.cliente else None,
        cliente_telefono=getattr(row.cliente, "telefono", None) if row.cliente else None,
        estado=row.estado,
        fecha=row.fecha,
        validez_dias=int(row.validez_dias or 8),
        moneda=row.moneda,
        condiciones=row.condiciones,
        observaciones=row.observaciones,
        subtotal=sum(float(linea.subtotal or 0) for linea in (row.lineas or [])),
        impuesto=sum(float(linea.impuesto or 0) for linea in (row.lineas or [])),
        total=sum(float(linea.total or 0) for linea in (row.lineas or [])),
        lineas=lineas,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_solicitud_salida_linea(row: SolicitudSalidaVentaLinea) -> SolicitudSalidaVentaLineaOut:
    return SolicitudSalidaVentaLineaOut(
        id=row.id,
        solicitud_id=row.solicitud_id,
        ot_id=row.ot_id,
        codigo_lote=row.ot.codigo_lote if row.ot else None,
        descripcion=row.descripcion,
        cantidad_quintales=float(row.cantidad_quintales or 0),
        precio_unitario=float(row.precio_unitario or 0),
        impuesto_porcentaje=float(row.impuesto_porcentaje or 0),
        subtotal=float(row.subtotal or 0),
        impuesto=float(row.impuesto or 0),
        total=float(row.total or 0),
    )


def serialize_solicitud_salida_venta(row: SolicitudSalidaVenta) -> SolicitudSalidaVentaOut:
    lineas = [serialize_solicitud_salida_linea(linea) for linea in (row.lineas or [])]
    return SolicitudSalidaVentaOut(
        id=row.id,
        codigo=row.codigo,
        cotizacion_id=row.cotizacion_id,
        codigo_cotizacion=row.cotizacion.codigo if row.cotizacion else None,
        cliente_id=row.cliente_id,
        cliente_nombre=row.cliente.nombre_completo if row.cliente else None,
        cliente_correo=getattr(row.cliente, "correo", None) if row.cliente else None,
        cliente_telefono=getattr(row.cliente, "telefono", None) if row.cliente else None,
        estado=row.estado,
        fecha=row.fecha,
        moneda=row.moneda,
        tipo_envio=row.tipo_envio,
        direccion_entrega=row.direccion_entrega,
        observaciones=row.observaciones,
        subtotal=float(row.subtotal or 0),
        impuesto=float(row.impuesto or 0),
        total=float(row.total or 0),
        lineas=lineas,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_venta_seguimiento(row: VentaSeguimiento) -> VentaSeguimientoOut:
    return VentaSeguimientoOut(
        id=row.id,
        lead_id=row.lead_id,
        fecha=row.fecha,
        canal=row.canal,
        asunto=row.asunto,
        comentario=row.comentario,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_venta_lead(row: VentaLeadLote) -> VentaLeadOut:
    return VentaLeadOut(
        id=row.id,
        ot_id=row.ot_id,
        cliente_id=row.cliente_id,
        cliente_nombre=row.cliente.nombre_completo if row.cliente else None,
        cliente_correo=getattr(row.cliente, "correo", None) if row.cliente else None,
        cliente_telefono=getattr(row.cliente, "telefono", None) if row.cliente else None,
        estado=row.estado,
        cantidad_quintales=row.cantidad_quintales,
        precio_unitario=row.precio_unitario,
        moneda=row.moneda,
        notas=row.notas,
        seguimientos=[serialize_venta_seguimiento(s) for s in (row.seguimientos or [])],
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_solicitud_documento(row: SolicitudVentaDocumento) -> SolicitudVentaDocumentoOut:
    return SolicitudVentaDocumentoOut(
        id=row.id,
        solicitud_id=row.solicitud_id,
        titulo=row.titulo,
        tipo=row.tipo,
        descripcion=row.descripcion,
        file_url=row.file_url,
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_solicitud_seguimiento(row: SolicitudVentaSeguimiento) -> SolicitudVentaSeguimientoOut:
    return SolicitudVentaSeguimientoOut(
        id=row.id,
        solicitud_id=row.solicitud_id,
        fecha=row.fecha,
        canal=row.canal,
        asunto=row.asunto,
        comentario=row.comentario,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_solicitud_linea_liquidacion(row: SolicitudVentaLineaLiquidacion) -> SolicitudVentaLineaLiquidacionOut:
    return SolicitudVentaLineaLiquidacionOut(
        id=row.id,
        solicitud_id=row.solicitud_id,
        linea_id=row.linea_id,
        salida_id=row.salida_id,
        codigo_salida=row.salida.codigo if row.salida else None,
        salida_linea_id=row.salida_linea_id,
        ot_id=row.ot_id,
        codigo_lote=row.ot.codigo_lote if row.ot else None,
        cantidad_quintales=float(row.cantidad_quintales or 0),
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_solicitud_linea(row: SolicitudVentaLinea) -> SolicitudVentaLineaOut:
    liquidaciones = [serialize_solicitud_linea_liquidacion(item) for item in (row.liquidaciones or [])]
    cantidad_liquidada = round(sum(float(item.cantidad_quintales or 0) for item in (row.liquidaciones or [])), 3)
    cantidad_total = float(row.cantidad_quintales or 0)
    return SolicitudVentaLineaOut(
        id=row.id,
        solicitud_id=row.solicitud_id,
        descripcion=row.descripcion,
        proceso_preferido=row.proceso_preferido,
        cantidad_quintales=cantidad_total,
        cantidad_liquidada=cantidad_liquidada,
        cantidad_pendiente=round(max(cantidad_total - cantidad_liquidada, 0), 3),
        precio_objetivo=row.precio_objetivo,
        moneda=row.moneda or "USD",
        observaciones=row.observaciones,
        liquidaciones=liquidaciones,
        created_at=row.created_at,
    )


def serialize_solicitud_venta(row: SolicitudVenta) -> SolicitudVentaOut:
    lineas = [serialize_solicitud_linea(linea) for linea in (row.lineas or [])]
    cantidad_total = sum(float(linea.cantidad_quintales or 0) for linea in (row.lineas or []))
    if not lineas:
        cantidad_total = float(row.cantidad_quintales or 0)
    return SolicitudVentaOut(
        id=row.id,
        codigo=row.codigo,
        cliente_id=row.cliente_id,
        cliente_nombre=row.cliente.nombre_completo if row.cliente else None,
        cliente_correo=getattr(row.cliente, "correo", None) if row.cliente else None,
        cliente_telefono=getattr(row.cliente, "telefono", None) if row.cliente else None,
        ot_id=row.ot_id,
        codigo_lote=row.ot.codigo_lote if row.ot else None,
        estado=row.estado,
        cantidad_quintales=round(float(cantidad_total or 0), 3),
        proceso_preferido=row.proceso_preferido,
        precio_objetivo=row.precio_objetivo,
        moneda=row.moneda,
        observaciones=row.observaciones,
        seguimientos=[serialize_solicitud_seguimiento(s) for s in (row.seguimientos or [])],
        documentos=[serialize_solicitud_documento(d) for d in (row.documentos or [])],
        lineas=lineas,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
    )


def serialize_ot_origen(row: OrdenTrabajoUnionOrigen) -> OTUnionOrigenOut:
    origen = getattr(row, "ot_origen", None)

    return OTUnionOrigenOut(
        id=row.id,
        ot_origen_id=row.ot_origen_id,
        codigo_lote_origen=row.codigo_lote_origen,
        estado_origen=row.estado_origen,
        estado_actual_origen=origen.estado if origen else row.estado_origen,
        proceso_origen=row.proceso_origen,
        fanegas_origen=row.fanegas_origen,
        progreso_origen_porcentaje=calculate_ot_progress_percent(origen),
        motivo=row.motivo,
        created_by_nombre=row.created_by.nombre if row.created_by else None,
        created_at=row.created_at,
        recibos=serialize_ot_recibos(origen),
        seguimientos=[
            serialize_followup(s)
            for s in sorted(getattr(origen, "seguimientos", []) or [], key=lambda x: (x.fecha or date.min, x.created_at or datetime.min.replace(tzinfo=timezone.utc), x.id or 0))
        ] if origen else [],
        documentos=[
            serialize_lote_documento(d)
            for d in sorted(getattr(origen, "documentos", []) or [], key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc))
        ] if origen else [],
        comentarios=[
            serialize_lote_comentario(c)
            for c in sorted(getattr(origen, "comentarios", []) or [], key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc))
        ] if origen else [],
    )

def serialize_ot(row: OrdenTrabajo) -> OTOut:
    recibos = serialize_ot_recibos(row)
    return OTOut(
        id=row.id,
        codigo_lote=row.codigo_lote,
        client_uuid=row.client_uuid,
        proceso=row.proceso,
        estado=row.estado,
        fecha_inicio=row.fecha_inicio,
        finca_id=row.finca_id,
        finca_nombre=row.finca.nombre if row.finca else None,
        operario_id=row.operario_id,
        operario_nombre=row.operario.nombre if row.operario else None,
        fanegas_estimadas=float(row.fanegas_estimadas or 0),
        quintales_base=ot_quintales_base(row),
        quintales_vendidos=ot_quintales_vendidos(row),
        quintales_disponibles=ot_quintales_disponibles(row),
        objetivo_cajuelas=row.objetivo_cajuelas,
        objetivo_fanegas=row.objetivo_fanegas,
        qr_token=row.qr_token,
        qr_public_url=row.qr_public_url or (lote_public_url(row.qr_token) if row.qr_token else None),
        qr_payload=row.qr_payload,
        observaciones=row.observaciones,
        recibos=recibos,
        seguimientos=[serialize_followup(s) for s in (row.seguimientos or [])],
        documentos=[serialize_lote_documento(d) for d in (row.documentos or [])],
        comentarios=[serialize_lote_comentario(c) for c in (row.comentarios or [])],
        ventas_leads=[serialize_venta_lead(v) for v in (row.ventas_leads or [])],
        cotizaciones_venta=[serialize_cotizacion_venta(rel.cotizacion) for rel in (row.cotizaciones_venta or []) if rel.cotizacion],
        solicitudes_salida_venta=[serialize_solicitud_salida_venta(rel.solicitud) for rel in (row.solicitudes_salida_venta or []) if rel.solicitud],
        origenes_unidos=[serialize_ot_origen(origen) for origen in (getattr(row, "origenes_unidos", []) or [])],
        created_by_id=row.created_by_id,
        created_at=row.created_at,
    )

def create_recibo_from_payload(db: Session, payload, current: Usuario | None = None) -> ReciboCafe:
    if payload.client_uuid:
        existing = db.query(ReciboCafe).filter(ReciboCafe.client_uuid == payload.client_uuid).first()
        if existing:
            return existing

    cliente = None
    finca = None

    if payload.cliente_id:
        cliente = db.query(Cliente).filter(Cliente.id == payload.cliente_id, Cliente.activo).first()
        if not cliente:
            raise ValueError("Cliente no encontrado o inactivo")

    if payload.finca_id:
        finca = db.query(Finca).filter(Finca.id == payload.finca_id, Finca.activa).first()
        if not finca:
            raise ValueError("Finca no encontrada o inactiva")

        if cliente and finca.cliente_id != cliente.id:
            raise ValueError("La finca seleccionada no pertenece al cliente indicado")

        if not cliente and finca.cliente:
            cliente = finca.cliente

    explicit_number = (payload.numero_recibo or "").strip()
    if explicit_number:
        if db.query(ReciboCafe.id).filter(ReciboCafe.numero_recibo == explicit_number).first():
            raise ValueError("El número de recibo ya existe")
        advance_receipt_counter(db, explicit_number)
        numero = explicit_number
    else:
        numero = allocate_receipt_number(db)

    productor_nombre = (payload.productor_nombre or "").strip()
    productor_cedula = payload.productor_cedula

    if cliente:
        productor_nombre = productor_nombre or cliente.nombre_completo
        productor_cedula = productor_cedula or cliente.numero_identificacion

    if not productor_nombre:
        raise ValueError("Debe seleccionar un cliente o indicar el nombre del productor")

    provincia = payload.provincia or (finca.provincia if finca else None) or (cliente.provincia if cliente else None)
    canton = payload.canton or (finca.canton if finca else None) or (cliente.canton if cliente else None)
    distrito = payload.distrito or (finca.distrito if finca else None) or (cliente.distrito if cliente else None)

    beneficio_usuario = None
    if getattr(payload, "beneficio_recibe_usuario_id", None):
        beneficio_usuario = db.query(Usuario).filter(
            Usuario.id == payload.beneficio_recibe_usuario_id,
            Usuario.activo
        ).first()
        if not beneficio_usuario:
            raise ValueError("El responsable de beneficio no existe o está inactivo")

    precio_fanega = getattr(payload, "precio_fanega", None)
    precio_fanega_letras = (getattr(payload, "precio_fanega_letras", None) or "").strip() or None
    if precio_fanega and not precio_fanega_letras:
        precio_fanega_letras = numero_a_letras_es(precio_fanega)

    peso_promedio_cajuela = getattr(payload, "peso_promedio_cajuela", None)
    if peso_promedio_cajuela is None:
        peso_promedio_cajuela = getattr(payload, "precio_promedio_cajuela", None)

    row = ReciboCafe(
        numero_recibo=numero,
        client_uuid=payload.client_uuid,
        fecha=payload.fecha,
        cosecha=payload.cosecha or cosecha_from_date(payload.fecha),
        cliente_id=cliente.id if cliente else payload.cliente_id,
        finca_id=finca.id if finca else payload.finca_id,
        productor_nombre=productor_nombre,
        productor_cedula=productor_cedula,
        provincia=provincia,
        canton=canton,
        distrito=distrito,
        zona=payload.zona,
        cajuelas=payload.cajuelas,
        cuartillos=payload.cuartillos,
        porcentaje_flote=payload.porcentaje_flote,
        porcentaje_verde=payload.porcentaje_verde,
        peso_promedio_cajuela=peso_promedio_cajuela,
        precio_promedio_cajuela=peso_promedio_cajuela,
        precio_fanega=precio_fanega,
        precio_fanega_letras=precio_fanega_letras,
        precio_adelanto_letras=payload.precio_adelanto_letras,
        beneficio_recibe_usuario_id=beneficio_usuario.id if beneficio_usuario else getattr(payload, "beneficio_recibe_usuario_id", None),
        beneficio_recibe=(beneficio_usuario.nombre if beneficio_usuario else payload.beneficio_recibe),
        beneficio_firma_url=(beneficio_usuario.firma_url if beneficio_usuario else getattr(payload, "beneficio_firma_url", None)),
        productor_entrega=payload.productor_entrega,
        estado=payload.estado or "recibido",
        observaciones=payload.observaciones,
        created_by_id=current.id if current else None,
    )

    row.qr_payload = recibo_payload(row)
    db.add(row)
    db.flush()
    return row

def frontend_public_url() -> str:
    return os.getenv("FRONTEND_PUBLIC_URL", "http://localhost:3000").rstrip("/")


def lote_public_url(qr_token: str) -> str:
    return f"{frontend_public_url()}/trazabilidad/{qr_token}"


def lote_internal_path(codigo_lote: str) -> str:
    return f"/ot/{codigo_lote}"


def generar_qr_token_lote(db: Session) -> str:
    """
    Token público, no secuencial e irrepetible.
    No usar id incremental como QR público.
    """
    for _ in range(20):
        token = secrets.token_urlsafe(32)
        exists = db.query(OrdenTrabajo).filter(OrdenTrabajo.qr_token == token).first()
        if not exists:
            return token

    raise ValueError("No se pudo generar un token QR único")


def build_ot_qr_payload(row: OrdenTrabajo) -> str:
    data = {
        "tipo": "navia_lote",
        "codigo_lote": row.codigo_lote,
        "qr_token": row.qr_token,
        "public_url": row.qr_public_url,
        "internal_path": lote_internal_path(row.codigo_lote),
        "version": 1,
    }

    return json.dumps(data, ensure_ascii=False)

def create_ot_from_payload(db: Session, payload, current: Usuario | None = None) -> OrdenTrabajo:
    if payload.client_uuid:
        existing = db.query(OrdenTrabajo).filter(OrdenTrabajo.client_uuid == payload.client_uuid).first()
        if existing:
            return existing

    if not getattr(payload, "operario_id", None):
        raise ValueError("Debe asignar un operario o gerente responsable antes de crear el lote")

    responsable = db.query(Usuario).filter(
        Usuario.id == payload.operario_id,
        Usuario.activo,
        (Usuario.rol.in_(["operario", "gerente", "admin"]) | Usuario.is_superadmin.is_(True)),
    ).first()

    if not responsable:
        raise ValueError("El responsable asignado debe ser un operario, gerente o administrador activo")

    asignaciones = []

    if payload.recibos:
        asignaciones = payload.recibos
    elif payload.recibo_ids:
        # Compatibilidad temporal: si llega el formato viejo, asigna todo lo disponible.
        for recibo_id in payload.recibo_ids:
            recibo_tmp = db.query(ReciboCafe).filter(ReciboCafe.id == recibo_id).first()
            if recibo_tmp:
                disponible = recibo_disponible_cajuelas(db, recibo_tmp)
                asignaciones.append(type("AsignacionTmp", (), {
                    "recibo_id": recibo_id,
                    "cajuelas_asignadas": disponible,
                    "cuartillos_asignados": 0,
                })())

    if not asignaciones:
        raise ValueError("Debe seleccionar al menos un recibo con cantidad asignada")

    recibos = []
    total_cajuelas = 0.0

    seen_receipts = set()
    for item in sorted(asignaciones, key=lambda item: item.recibo_id):
        if item.recibo_id in seen_receipts:
            raise ValueError("No se puede asignar el mismo recibo dos veces al lote")
        seen_receipts.add(item.recibo_id)
        recibo = (
            db.query(ReciboCafe)
            .filter(ReciboCafe.id == item.recibo_id)
            .with_for_update()
            .first()
        )

        if not recibo:
            raise ValueError(f"Recibo {item.recibo_id} no encontrado")

        if str(recibo.estado or "").lower() == "anulado":
            raise ValueError("No se puede crear un lote con un recibo anulado")
        disponible = recibo_disponible_cajuelas(db, recibo)
        solicitado = float(item.cajuelas_asignadas or 0) + float(item.cuartillos_asignados or 0) / 4

        if solicitado <= 0:
            raise ValueError(f"La cantidad asignada al recibo {recibo.numero_recibo} debe ser mayor a cero")

        if solicitado > disponible:
            raise ValueError(
                f"El recibo {recibo.numero_recibo} solo tiene {disponible} cajuelas disponibles "
                f"y se intentaron asignar {round(solicitado, 2)}"
            )

        recibos.append((recibo, item, solicitado))
        total_cajuelas += solicitado

    fanegas = round(total_cajuelas / 20, 3)

    codigo = (payload.codigo_lote or "").strip() or next_code(db, OrdenTrabajo, "codigo_lote", "LOT-", 5)

    qr_token = generar_qr_token_lote(db)
    qr_public_url = lote_public_url(qr_token)

    row = OrdenTrabajo(
        codigo_lote=codigo,
        client_uuid=payload.client_uuid,
        proceso=payload.proceso,
        estado="abierta",
        fecha_inicio=payload.fecha_inicio,
        finca_id=payload.finca_id or (recibos[0][0].finca_id if recibos else None),
        operario_id=payload.operario_id,
        fanegas_estimadas=fanegas,
        objetivo_cajuelas=payload.objetivo_cajuelas,
        objetivo_fanegas=payload.objetivo_fanegas or ((payload.objetivo_cajuelas or 0) / 20 if payload.objetivo_cajuelas else None),
        qr_token=qr_token,
        qr_public_url=qr_public_url,
        observaciones=payload.observaciones,
        created_by_id=current.id if current else None,
    )

    row.qr_payload = build_ot_qr_payload(row)

    db.add(row)
    db.flush()

    db.add(ComentarioLote(
        ot_id=row.id,
        tipo="estado",
        comentario="Lote creado y QR único generado para trazabilidad pública.",
        created_by_id=current.id if current else None,
    ))

    for recibo, item, solicitado in recibos:
        db.add(OrdenTrabajoRecibo(
            ot_id=row.id,
            recibo_id=recibo.id,
            cajuelas_asignadas=float(item.cajuelas_asignadas or 0),
            cuartillos_asignados=float(item.cuartillos_asignados or 0),
            fanegas_asignadas=round(solicitado / 20, 3),
        ))

        disponible_post = recibo_disponible_cajuelas(db, recibo) - solicitado

        if disponible_post <= 0:
            recibo.estado = "asignado_parcial" if disponible_post > 0 else "en_proceso"
        else:
            recibo.estado = "recibido"

    db.flush()
    return row

def ot_query(db: Session):
    return db.query(OrdenTrabajo).options(
        selectinload(OrdenTrabajo.finca),
        selectinload(OrdenTrabajo.operario),
        selectinload(OrdenTrabajo.recibos_rel).selectinload(OrdenTrabajoRecibo.recibo),
        selectinload(OrdenTrabajo.seguimientos).selectinload(SeguimientoOT.created_by),
        selectinload(OrdenTrabajo.documentos).selectinload(DocumentoLote.created_by),
        selectinload(OrdenTrabajo.comentarios).selectinload(ComentarioLote.created_by),
        selectinload(OrdenTrabajo.ventas_leads).selectinload(VentaLeadLote.cliente),
        selectinload(OrdenTrabajo.ventas_leads).selectinload(VentaLeadLote.created_by),
        selectinload(OrdenTrabajo.ventas_leads).selectinload(VentaLeadLote.seguimientos).selectinload(VentaSeguimiento.created_by),
        selectinload(OrdenTrabajo.cotizaciones_venta).selectinload(CotizacionVentaLinea.cotizacion).selectinload(CotizacionVenta.cliente),
        selectinload(OrdenTrabajo.cotizaciones_venta).selectinload(CotizacionVentaLinea.cotizacion).selectinload(CotizacionVenta.created_by),
        selectinload(OrdenTrabajo.solicitudes_salida_venta).selectinload(SolicitudSalidaVentaLinea.solicitud).selectinload(SolicitudSalidaVenta.cliente),
        selectinload(OrdenTrabajo.solicitudes_salida_venta).selectinload(SolicitudSalidaVentaLinea.solicitud).selectinload(SolicitudSalidaVenta.created_by),
        selectinload(OrdenTrabajo.origenes_unidos).selectinload(OrdenTrabajoUnionOrigen.created_by),
        selectinload(OrdenTrabajo.origenes_unidos).selectinload(OrdenTrabajoUnionOrigen.ot_origen),
        selectinload(OrdenTrabajo.origenes_unidos).selectinload(OrdenTrabajoUnionOrigen.ot_origen).selectinload(OrdenTrabajo.recibos_rel).selectinload(OrdenTrabajoRecibo.recibo),
        selectinload(OrdenTrabajo.origenes_unidos).selectinload(OrdenTrabajoUnionOrigen.ot_origen).selectinload(OrdenTrabajo.seguimientos).selectinload(SeguimientoOT.created_by),
        selectinload(OrdenTrabajo.origenes_unidos).selectinload(OrdenTrabajoUnionOrigen.ot_origen).selectinload(OrdenTrabajo.documentos).selectinload(DocumentoLote.created_by),
        selectinload(OrdenTrabajo.origenes_unidos).selectinload(OrdenTrabajoUnionOrigen.ot_origen).selectinload(OrdenTrabajo.comentarios).selectinload(ComentarioLote.created_by),
    )


def recibo_query(db: Session):
    return db.query(ReciboCafe).options(
        selectinload(ReciboCafe.cliente),
        selectinload(ReciboCafe.finca),
        selectinload(ReciboCafe.created_by),
        selectinload(ReciboCafe.beneficio_usuario),
        selectinload(ReciboCafe.liquidado_por),
    )


def ensure_usuario_firma_column(db: Session) -> None:
    try:
        db.execute(text(
            "ALTER TABLE navia_usuarios "
            "ADD COLUMN IF NOT EXISTS firma_url VARCHAR(1000)"
        ))
        db.commit()
    except Exception:
        db.rollback()


def ensure_usuario_training_columns(db: Session) -> None:
    """Mantiene el progreso de capacitación en instalaciones existentes."""
    statements = [
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS onboarding_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMP WITH TIME ZONE",
    ]
    try:
        for statement in statements:
            db.execute(text(statement))
        db.commit()
    except Exception:
        db.rollback()


def ensure_recibos_nuevos_campos(db: Session) -> None:
    """Agrega campos nuevos de recibo en bases ya existentes.

    Mantiene precio_promedio_cajuela como campo legado, pero copia su valor a
    peso_promedio_cajuela cuando corresponde.
    """
    statements = [
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS peso_promedio_cajuela DOUBLE PRECISION",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS precio_fanega DOUBLE PRECISION",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS precio_fanega_letras VARCHAR(255)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS beneficio_recibe_usuario_id INTEGER",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS beneficio_firma_url VARCHAR(1000)",
        "ALTER TABLE navia_clientes ADD COLUMN IF NOT EXISTS portal_token VARCHAR(255)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado_por_id INTEGER",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado_por_nombre_snapshot VARCHAR(180)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_nota TEXT",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_numero_transferencia VARCHAR(180)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_monto NUMERIC(18, 2)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_storage_key VARCHAR(255)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_nombre VARCHAR(255)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_tipo VARCHAR(120)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_tamano INTEGER",
        "UPDATE navia_recibos_cafe SET peso_promedio_cajuela = precio_promedio_cajuela WHERE peso_promedio_cajuela IS NULL AND precio_promedio_cajuela IS NOT NULL",
    ]

    try:
        for statement in statements:
            db.execute(text(statement))
        db.commit()
    except Exception:
        db.rollback()


def ensure_finca_gestion_column(db: Session) -> None:
    """Agrega la columna si la base ya existía antes de este módulo.

    Base.metadata.create_all crea columnas nuevas solo cuando la tabla no existe;
    este ajuste evita errores en instalaciones que ya tienen navia_fincas creada.
    """
    try:
        db.execute(text(
            "ALTER TABLE navia_fincas "
            "ADD COLUMN IF NOT EXISTS gestion_fincas_habilitada BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        db.commit()
    except Exception:
        db.rollback()


def ensure_actividad_ambito_column(db: Session) -> None:
    try:
        db.execute(text(
            "ALTER TABLE navia_gf_actividades "
            "ADD COLUMN IF NOT EXISTS ambito VARCHAR(30) NOT NULL DEFAULT 'finca'"
        ))
        db.commit()
    except Exception:
        db.rollback()


def ensure_navia_operational_columns(db: Session) -> None:
    """Ajustes no destructivos para bases existentes de NAVIA.

    create_all crea tablas nuevas, pero no agrega columnas sobre tablas ya existentes.
    Estos ALTER mantienen compatibilidad al desplegar el paquete sobre una instancia en uso.
    """
    statements = [
        "ALTER TABLE navia_clientes ADD COLUMN IF NOT EXISTS telefono VARCHAR(80)",
        "ALTER TABLE navia_clientes ADD COLUMN IF NOT EXISTS correo VARCHAR(180)",
        "ALTER TABLE navia_gf_insumos ADD COLUMN IF NOT EXISTS codigo_fabricante VARCHAR(120)",
        "ALTER TABLE navia_gf_insumos ADD COLUMN IF NOT EXISTS precio_unitario DOUBLE PRECISION",
        "ALTER TABLE navia_gf_insumos ADD COLUMN IF NOT EXISTS impuesto_porcentaje DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE navia_gf_insumos ADD COLUMN IF NOT EXISTS stock_actual DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE navia_gf_insumos ADD COLUMN IF NOT EXISTS stock_minimo DOUBLE PRECISION",
        "UPDATE navia_gf_insumos SET precio_unitario = costo_unitario WHERE precio_unitario IS NULL AND costo_unitario IS NOT NULL",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS marca_tiempo TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS estado_lote_resultante VARCHAR(40)",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS dar_alerta BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS alerta_mensaje TEXT",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS alerta_canal VARCHAR(40)",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS alerta_whatsapp_url VARCHAR(1000)",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS alerta_correo_url VARCHAR(1000)",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS estado_revision VARCHAR(30) NOT NULL DEFAULT 'pendiente'",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS comentario_revision TEXT",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS revisado_por_id INTEGER",
        "ALTER TABLE navia_seguimientos_ot ADD COLUMN IF NOT EXISTS revisado_at TIMESTAMP WITH TIME ZONE",
    ]

    try:
        for statement in statements:
            db.execute(text(statement))
        db.commit()
    except Exception:
        db.rollback()


def bootstrap(
    db: Session,
    admin_email: str,
    admin_password: str,
    admin_name: str,
    session_days: int,
    sync_admin_password: bool = False,
    seed_demo_data: bool = True,
    manage_core_identity: bool = True,
) -> None:
    # Las migraciones de compatibilidad pueden ejecutar rollback cuando una
    # columna ya existe o el motor no soporta una sentencia. Deben correr antes
    # de agregar datos pendientes para no descartar el superadmin ni la config.
    ensure_usuario_firma_column(db)
    ensure_usuario_training_columns(db)
    ensure_recibos_nuevos_campos(db)
    ensure_finca_gestion_column(db)
    ensure_actividad_ambito_column(db)
    ensure_navia_operational_columns(db)

    # El consecutivo pertenece al dominio Caficultura y se inicializa al instalarlo.
    get_receipt_counter(db)

    if manage_core_identity:
        set_config(db, "session_days", str(session_days), "Duración de sesión en días")

        admin = db.query(Usuario).filter(Usuario.correo == admin_email).first()

        if not admin:
            admin = Usuario(
                nombre=admin_name,
                correo=admin_email,
                hash_contrasena=hashear_contrasena(admin_password),
                rol="gerente",
                activo=True,
                is_superadmin=True,
            )
            db.add(admin)
        else:
            admin.nombre = admin_name or admin.nombre
            admin.rol = "gerente"
            admin.activo = True
            admin.is_superadmin = True
            if sync_admin_password and not verificar_contrasena(admin_password, admin.hash_contrasena):
                admin.hash_contrasena = hashear_contrasena(admin_password)
                now = utcnow()
                for session in db.query(SesionUsuario).filter(
                    SesionUsuario.usuario_id == admin.id,
                    SesionUsuario.revoked_at.is_(None),
                ).all():
                    session.revoked_at = now

        session_cleanup_cutoff = utcnow() - timedelta(days=30)
        db.query(SesionUsuario).filter(
            (SesionUsuario.expires_at < session_cleanup_cutoff)
            | (SesionUsuario.revoked_at < session_cleanup_cutoff)
        ).delete(synchronize_session=False)
        db.query(PasswordResetToken).filter(
            PasswordResetToken.created_at < utcnow() - timedelta(days=7)
        ).delete(synchronize_session=False)

    if seed_demo_data:
        cliente_demo = db.query(Cliente).filter(Cliente.codigo == "CL-00001").first()

        if not cliente_demo:
            cliente_demo = Cliente(
                codigo="CL-00001",
                nombre_completo="Inversiones Coffee Nace S.A",
                tipo_persona="juridica",
                categoria="agro",
                numero_identificacion="3-101-719291",
                direccion="Río Conejo, Corralillo, Cartago",
                provincia="Cartago",
                canton="Central",
                distrito="Río Conejo",
                activo=True,
            )
            db.add(cliente_demo)
            db.flush()

        if not db.query(Finca).first():
            db.add(Finca(
                cliente_id=cliente_demo.id,
                nombre="Finca demostrativa",
                codigo="FIN-00001",
                propietario=cliente_demo.nombre_completo,
                ubicacion="Río Conejo, Corralillo, Cartago",
                provincia="Cartago",
                canton="Central",
                distrito="Río Conejo",
                area=1,
                cultivo="Café",
                caracteristicas="Finca demostrativa para pruebas de trazabilidad NAVIA.",
                activa=True,
                gestion_fincas_habilitada=True,
            ))

    activity_definitions = [
        ("Fertilizar", "fertilizacion", "finca", "Aplicación de fertilizante granular.", True, "sacos"),
        ("Atomizar", "atomizacion", "finca", "Aplicación foliar o fitosanitaria.", True, "litros"),
        ("Encalar", "encalado", "finca", "Aplicación de cal o enmienda.", True, "sacos"),
        ("Chapia", "chapia", "finca", "Control mecánico de malezas.", False, "jornales"),
        ("Poda", "poda", "finca", "Manejo de tejido productivo.", False, "plantas"),
        ("Recepción en beneficio", "recepcion", "beneficio", "Recepción y pesaje de café en fruta.", False, "lote"),
        ("Despulpado", "beneficiado", "beneficio", "Despulpado del lote según proceso definido.", False, "lote"),
        ("Secado y volteo", "secado", "beneficio", "Secado, volteo y control del lote.", False, "horas"),
        ("Fermentación", "beneficiado", "beneficio", "Control de fermentación del lote.", False, "lote"),
        ("Secado en patio", "secado", "beneficio", "Secado solar, volteo y control en patio.", False, "lote"),
        ("Secado en guardiola", "secado", "beneficio", "Secado mecánico y control de humedad.", False, "lote"),
        ("Pelado", "preparacion", "beneficio", "Pelado o trillado del lote.", False, "lote"),
        ("Clasificadora por tamaño", "preparacion", "beneficio", "Clasificación granulométrica por tamaño.", False, "lote"),
        ("Decimétrica", "preparacion", "beneficio", "Clasificación densimétrica del lote.", False, "lote"),
        ("Clasificado por color", "preparacion", "beneficio", "Clasificación óptica o por color.", False, "lote"),
        ("Selección manual", "preparacion", "beneficio", "Selección manual final del café.", False, "lote"),
        ("Clasificación y preparación", "preparacion", "beneficio", "Clasificación, empaque o preparación para venta.", False, "lote"),
    ]
    existing_activity_names = {
        row[0] for row in db.query(ActividadFinca.nombre).all()
    }
    for nombre, tipo, ambito, descripcion, requiere_insumo, unidad in activity_definitions:
        if nombre in existing_activity_names:
            continue
        db.add(ActividadFinca(
            nombre=nombre,
            tipo=tipo,
            ambito=ambito,
            descripcion=descripcion,
            requiere_insumo=requiere_insumo,
            unidad_referencia=unidad,
            activa=True,
        ))
        existing_activity_names.add(nombre)

    if seed_demo_data and not db.query(InsumoFinca).first():
        db.add_all([
            InsumoFinca(codigo="INS-001", nombre="Fertilizante 10-30-10", tipo="fertilizante", unidad="saco", costo_unitario=25000, activo=True),
            InsumoFinca(codigo="INS-002", nombre="Cal dolomita", tipo="enmienda", unidad="saco", costo_unitario=4500, activo=True),
            InsumoFinca(codigo="INS-003", nombre="Producto foliar", tipo="foliar", unidad="litro", costo_unitario=8500, activo=True),
        ])

    if seed_demo_data and not db.query(TrabajadorFinca).first():
        db.add(TrabajadorFinca(
            codigo="TR-001",
            nombre="Trabajador demostrativo",
            puesto="Trabajador agrícola",
            jornal_diario=15000,
            activo=True,
        ))

    db.commit()

def dias_entre(fecha: date | None) -> int:
    if not fecha:
        return 0

    return max(0, (date.today() - fecha).days)


def serialize_public_lote(row: OrdenTrabajo):
    from modules.mod_caficultura.schemas import (
        PublicLoteDocumentoOut,
        PublicLoteEventoOut,
        PublicLoteOrigenOut,
        PublicLoteOut,
        PublicLoteReciboOut,
        PublicLoteSeguimientoOut,
    )

    def public_recibos_for(lote: OrdenTrabajo | None) -> list[PublicLoteReciboOut]:
        if not lote:
            return []
        recibos = []
        for link in (lote.recibos_rel or lote.recibos or []):
            recibo = getattr(link, "recibo", None)
            if not recibo:
                continue
            cajuelas_equiv = float(getattr(link, "cajuelas_asignadas", None) or recibo.cajuelas or 0) + (
                float(getattr(link, "cuartillos_asignados", None) or recibo.cuartillos or 0) / 4
            )
            recibos.append(PublicLoteReciboOut(
                numero_recibo=recibo.numero_recibo,
                fecha=recibo.fecha,
                dias_desde_ingreso=dias_entre(recibo.fecha),
                finca_nombre=recibo.finca.nombre if recibo.finca else None,
                provincia=recibo.provincia,
                canton=recibo.canton,
                distrito=recibo.distrito,
                cajuelas_equivalentes=round(cajuelas_equiv, 2),
                fanegas_equivalentes=round(cajuelas_equiv / 20, 2),
            ))
        return recibos

    def public_seguimientos_for(lote: OrdenTrabajo | None) -> list[PublicLoteSeguimientoOut]:
        if not lote:
            return []
        return [
            PublicLoteSeguimientoOut(
                fecha=s.fecha,
                marca_tiempo=getattr(s, "marca_tiempo", None),
                actividad_realizada=s.actividad_realizada,
                horas_implementadas=s.horas_implementadas,
                temperatura=s.temperatura,
                humedad=s.humedad,
                comentario=s.comentario,
                dar_alerta=bool(getattr(s, "dar_alerta", False)),
            )
            for s in sorted(lote.seguimientos or [], key=lambda x: (x.fecha, x.id or 0))
        ]

    def public_documentos_for(lote: OrdenTrabajo | None) -> list[PublicLoteDocumentoOut]:
        if not lote:
            return []
        return [
            PublicLoteDocumentoOut(
                titulo=d.titulo,
                tipo=d.tipo,
                descripcion=d.descripcion,
                file_url=d.file_url,
                file_name=d.file_name,
                created_at=d.created_at,
            )
            for d in sorted(lote.documentos or [], key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc))
        ]

    def public_eventos_for_lote(lote: OrdenTrabajo | None, prefijo: str = "") -> list[PublicLoteEventoOut]:
        if not lote:
            return []
        lote_codigo = getattr(lote, "codigo_lote", "") or "lote original"
        prefix = f"{prefijo} · " if prefijo else ""
        eventos_lote: list[PublicLoteEventoOut] = [PublicLoteEventoOut(
            tipo="creacion",
            titulo=f"{prefix}Lote creado",
            descripcion=f"Se abrió el lote {lote_codigo} y se generó su trazabilidad.",
            fecha=lote.fecha_inicio,
            created_at=lote.created_at,
        )]

        for s in sorted(lote.seguimientos or [], key=lambda x: (x.fecha, x.id or 0)):
            actor = s.created_by.nombre if getattr(s, "created_by", None) else "Operario"
            detalle = f"{actor} registró seguimiento productivo en {lote_codigo}."
            if getattr(s, "dar_alerta", False):
                detalle = f"{detalle} Se registró alerta operativa."
            if s.comentario:
                detalle = f"{detalle} {s.comentario}"
            eventos_lote.append(PublicLoteEventoOut(
                tipo="alerta" if getattr(s, "dar_alerta", False) else "seguimiento",
                titulo=f"{prefix}{s.actividad_realizada}",
                descripcion=detalle,
                fecha=s.fecha,
                created_at=s.created_at,
            ))

        for c in sorted(lote.comentarios or [], key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc)):
            tipo = (c.tipo or "nota").lower()
            titulo_map = {
                "estado": "Cambio de estado",
                "supervision": "Supervisión gerencial",
                "comercial": "Movimiento comercial",
                "garantia": "Nota de garantía",
                "union": "Registro de unión",
                "nota": "Nota del lote",
            }
            actor = c.created_by.nombre if getattr(c, "created_by", None) else None
            descripcion = f"{actor}: {c.comentario}" if actor else c.comentario
            eventos_lote.append(PublicLoteEventoOut(
                tipo=tipo,
                titulo=f"{prefix}{titulo_map.get(tipo, 'Registro del lote')}",
                descripcion=descripcion,
                fecha=c.created_at.date() if c.created_at else None,
                created_at=c.created_at,
            ))

        for d in sorted(lote.documentos or [], key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc)):
            eventos_lote.append(PublicLoteEventoOut(
                tipo="documento",
                titulo=f"{prefix}Documento agregado: {d.titulo}",
                descripcion=d.descripcion or d.file_name,
                fecha=d.created_at.date() if d.created_at else None,
                created_at=d.created_at,
            ))
        return sorted(eventos_lote, key=lambda e: (e.created_at.isoformat() if e.created_at else f"{e.fecha or lote.fecha_inicio}T00:00:00"))

    recibos_publicos = public_recibos_for(row)

    # El nuevo lote unido consolida recibos; las líneas de origen conservan su historia separada.
    seguimientos_publicos = public_seguimientos_for(row)

    documentos_publicos = public_documentos_for(row)

    eventos = public_eventos_for_lote(row)

    for salida_linea in sorted(row.solicitudes_salida_venta or [], key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc)):
        salida = salida_linea.solicitud
        if not salida or (salida.estado or "").lower() == "anulada":
            continue
        cliente_nombre = salida.cliente.nombre_completo if salida.cliente else "cliente no registrado"
        cantidad = round(float(salida_linea.cantidad_quintales or 0), 3)
        eventos.append(PublicLoteEventoOut(
            tipo="comercial",
            titulo="Salida comercial registrada",
            descripcion=(
                f"Se asignaron {cantidad} qq de este lote al cliente {cliente_nombre} "
                f"mediante la solicitud de salida {salida.codigo}."
            ),
            fecha=salida.fecha,
            created_at=salida_linea.created_at or salida.created_at,
        ))


    origenes_publicos = []
    for origen in sorted(getattr(row, "origenes_unidos", []) or [], key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc)):
        lote_origen = getattr(origen, "ot_origen", None)
        eventos_origen = public_eventos_for_lote(lote_origen, f"Historia original {origen.codigo_lote_origen}")
        origenes_publicos.append(PublicLoteOrigenOut(
            codigo_lote_origen=origen.codigo_lote_origen,
            estado_origen=origen.estado_origen,
            estado_actual_origen=lote_origen.estado if lote_origen else origen.estado_origen,
            proceso_origen=origen.proceso_origen,
            fanegas_origen=round(float(origen.fanegas_origen or 0), 3),
            progreso_origen_porcentaje=calculate_ot_progress_percent(lote_origen),
            motivo=origen.motivo,
            created_at=origen.created_at,
            recibos=public_recibos_for(lote_origen),
            seguimientos=public_seguimientos_for(lote_origen),
            documentos=public_documentos_for(lote_origen),
            eventos=eventos_origen,
        ))
        eventos.extend(eventos_origen)
        eventos.append(PublicLoteEventoOut(
            tipo="union",
            titulo=f"Punto de unión: {origen.codigo_lote_origen} se integró a {row.codigo_lote}",
            descripcion=origen.motivo or "Este lote conserva trazabilidad de un lote original unido al proceso.",
            fecha=origen.created_at.date() if origen.created_at else None,
            created_at=origen.created_at,
        ))

    if row.estado == "finalizada" and row.fecha_cierre and not any(e.tipo == "supervision" for e in eventos):
        eventos.append(PublicLoteEventoOut(
            tipo="supervision",
            titulo="Lote aprobado",
            descripcion="El lote fue aprobado por gerencia.",
            fecha=row.fecha_cierre.date(),
            created_at=row.fecha_cierre,
        ))

    if row.estado == "vendido" and not any(e.tipo == "comercial" and "vend" in (e.descripcion or "").lower() for e in eventos):
        eventos.append(PublicLoteEventoOut(
            tipo="comercial",
            titulo="Lote vendido",
            descripcion="El lote fue marcado como vendido mediante solicitud de salida.",
            fecha=row.updated_at.date() if row.updated_at else None,
            created_at=row.updated_at,
        ))

    eventos = sorted(
        eventos,
        key=lambda e: (e.created_at.isoformat() if e.created_at else f"{e.fecha or row.fecha_inicio}T00:00:00"),
    )

    proceso_label = {
        "miel": "Café miel",
        "natural": "Café natural",
        "semilavado": "Café semilavado",
    }.get(row.proceso, row.proceso)

    resumen = (
        f"Lote {row.codigo_lote} procesado como {proceso_label}, "
        f"con trazabilidad desde la recepción del café hasta sus registros de proceso, aprobación y salida comercial."
    )

    return PublicLoteOut(
        codigo_lote=row.codigo_lote,
        proceso=row.proceso,
        estado=row.estado,
        fecha_inicio=row.fecha_inicio,
        dias_desde_inicio=dias_entre(row.fecha_inicio),
        fanegas_estimadas=round(float(row.fanegas_estimadas or 0), 2),
        objetivo_cajuelas=row.objetivo_cajuelas,
        objetivo_fanegas=row.objetivo_fanegas,
        qr_token=row.qr_token,
        qr_public_url=row.qr_public_url or lote_public_url(row.qr_token),
        resumen=resumen,
        recibos=recibos_publicos,
        seguimientos=seguimientos_publicos,
        documentos=documentos_publicos,
        eventos=eventos,
        origenes_unidos=origenes_publicos,
    )   

def recibo_total_cajuelas(row: ReciboCafe) -> float:
    return round(float(row.cajuelas or 0) + float(row.cuartillos or 0) / 4, 2)


def recibo_asignado_cajuelas(db: Session, recibo_id: int) -> float:
    rows = (
        db.query(OrdenTrabajoRecibo)
        .join(OrdenTrabajo, OrdenTrabajoRecibo.ot_id == OrdenTrabajo.id)
        .filter(
            OrdenTrabajoRecibo.recibo_id == recibo_id,
            OrdenTrabajo.estado != "anulada",
        )
        .all()
    )

    total = 0.0

    for row in rows:
        total += float(row.cajuelas_asignadas or 0)
        total += float(row.cuartillos_asignados or 0) / 4

    return round(total, 2)


def recibo_disponible_cajuelas(db: Session, row: ReciboCafe) -> float:
    total = recibo_total_cajuelas(row)
    asignado = recibo_asignado_cajuelas(db, row.id)
    return round(max(0, total - asignado), 2)


def dias_desde(fecha: date | None) -> int:
    if not fecha:
        return 0
    return max(0, (date.today() - fecha).days)


def serialize_recibo_disponible_ot(db: Session, row: ReciboCafe):
    from modules.mod_caficultura.schemas import ReciboDisponibleOTOut

    total = recibo_total_cajuelas(row)
    asignado = recibo_asignado_cajuelas(db, row.id)
    disponible = max(0, total - asignado)

    return ReciboDisponibleOTOut(
        id=row.id,
        numero_recibo=row.numero_recibo,
        fecha=row.fecha,
        cliente_nombre=row.cliente.nombre_completo if row.cliente else None,
        productor_nombre=row.productor_nombre,
        finca_nombre=row.finca.nombre if row.finca else None,
        provincia=row.provincia,
        canton=row.canton,
        distrito=row.distrito,
        cajuelas=float(row.cajuelas or 0),
        cuartillos=float(row.cuartillos or 0),
        cajuelas_totales=round(total, 2),
        cajuelas_asignadas=round(asignado, 2),
        cajuelas_disponibles=round(disponible, 2),
        fanegas_disponibles=round(disponible / 20, 2),
        dias_desde_ingreso=dias_desde(row.fecha),
    )
