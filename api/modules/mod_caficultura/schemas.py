from __future__ import annotations

from datetime import date, datetime
from typing import Any, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

ROLES = {"gerente", "operario", "administrativo", "supervisor_finca"}
PROCESOS = {"miel", "natural", "semilavado"}
ESTADOS_OT = {"abierta", "en_proceso", "alerta", "pendiente_aprobacion", "finalizada", "vendido", "unido", "anulada"}
TIPOS_PERSONA = {"personal", "juridica"}
CATEGORIAS_CLIENTE = {"personal", "agro", "comercial", "industria"}

NOTA_CATEGORIAS = {"general", "proveedor", "recibo", "actividad", "cliente", "insumo", "compra", "venta", "pendiente"}
NOTA_ESTADOS = {"pendiente", "procesando", "procesada", "archivada"}
NOTA_PRIORIDADES = {"baja", "normal", "alta", "urgente"}
NOTA_VISIBILIDADES = {"personal", "equipo"}


def normalize_role(value: str | None) -> str:
    raw = (value or "operario").strip().lower()
    aliases = {
        "gerente": "gerente",
        "admin": "gerente",
        "administrador": "gerente",
        "superadmin": "gerente",
        "operario": "operario",
        "operator": "operario",
        "trabajador": "operario",
        "administrativo": "administrativo",
        "oficina": "administrativo",
        "supervisor_finca": "supervisor_finca",
        "supervisor de finca": "supervisor_finca",
        "supervisor": "supervisor_finca",
        "finca": "supervisor_finca",
    }
    return aliases.get(raw, "operario")


class LoginIn(BaseModel):
    correo: EmailStr
    password: str = Field(min_length=1, max_length=128)


class LoginOut(BaseModel):
    token: Optional[str] = None
    token_type: str = "bearer"
    expires_at: datetime
    user: Any


class ForgotPasswordIn(BaseModel):
    correo: EmailStr


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=32, max_length=512)
    password: str = Field(min_length=10, max_length=128)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class MessageOut(BaseModel):
    ok: bool = True
    message: str


class UsuarioBase(BaseModel):
    nombre: str = Field(min_length=2, max_length=180)
    correo: EmailStr
    rol: str = "operario"
    activo: bool = True
    is_superadmin: bool = False
    firma_url: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("rol")
    @classmethod
    def rol_ok(cls, value: str) -> str:
        rol = normalize_role(value)
        if rol not in ROLES:
            raise ValueError("Rol inválido")
        return rol


class UsuarioCreate(UsuarioBase):
    password: str = Field(min_length=8, max_length=128)


class UsuarioUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=2, max_length=180)
    correo: Optional[EmailStr] = None
    rol: Optional[str] = None
    activo: Optional[bool] = None
    is_superadmin: Optional[bool] = None
    firma_url: Optional[str] = Field(default=None, max_length=1000)
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)

    @field_validator("rol")
    @classmethod
    def rol_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_role(value)


class UsuarioOut(BaseModel):
    id: int
    nombre: str
    correo: EmailStr
    rol: str
    activo: bool
    is_superadmin: bool = False
    firma_url: Optional[str] = None
    permisos: List[str] = Field(default_factory=list)
    onboarding_version: int = 0
    onboarding_current_version: int = 1
    onboarding_required: bool = True
    onboarding_completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TrainingCompleteIn(BaseModel):
    version: int = Field(ge=1, le=100)


class ConfiguracionOut(BaseModel):
    session_days: int = 30
    app_name: str = "NAVIA"
    app_logo_url: Optional[str] = None
    alert_whatsapp: Optional[str] = None
    alert_email: Optional[str] = None
    can_export_backup: bool = False
    can_import_backup: bool = False
    can_full_reset: bool = False


class ConfiguracionUpdate(BaseModel):
    session_days: Optional[int] = Field(default=None, ge=1, le=365)
    app_name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    alert_whatsapp: Optional[str] = Field(default=None, max_length=80)
    alert_email: Optional[str] = Field(default=None, max_length=180)

    @field_validator("app_name")
    @classmethod
    def app_name_clean(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if len(clean) < 2:
            raise ValueError("El nombre de la plataforma debe tener al menos 2 caracteres")
        return clean


class BrandingOut(BaseModel):
    app_name: str = "NAVIA"
    app_logo_url: Optional[str] = None


class ReciboConsecutivoUpdate(BaseModel):
    prefijo: str = Field(default="RC-", max_length=20)
    siguiente_numero: int = Field(ge=1, le=2_000_000_000)
    ancho: int = Field(default=5, ge=1, le=12)

    @field_validator("prefijo")
    @classmethod
    def prefijo_seguro(cls, value: str) -> str:
        clean = str(value or "").strip().upper()
        if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/." for ch in clean):
            raise ValueError("El prefijo solo admite letras, números, guion, punto, diagonal o guion bajo")
        return clean


class ReciboConsecutivoOut(BaseModel):
    prefijo: str
    siguiente_numero: int
    ancho: int
    proximo_recibo: str
    ultimo_numero_usado: Optional[int] = None
    updated_at: Optional[datetime] = None


class ClienteBase(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    nombre_completo: str = Field(min_length=2, max_length=220)
    tipo_persona: str = "personal"
    categoria: str = "personal"
    numero_identificacion: str = Field(min_length=2, max_length=80)
    direccion: Optional[str] = Field(default=None, max_length=500)
    provincia: Optional[str] = Field(default=None, max_length=80)
    canton: Optional[str] = Field(default=None, max_length=80)
    distrito: Optional[str] = Field(default=None, max_length=80)
    telefono: Optional[str] = Field(default=None, max_length=80)
    correo: Optional[EmailStr] = None
    activo: bool = True

    @field_validator("tipo_persona")
    @classmethod
    def tipo_persona_ok(cls, value: str) -> str:
        val = (value or "personal").strip().lower()
        if val not in TIPOS_PERSONA:
            raise ValueError("Tipo de persona inválido")
        return val

    @field_validator("categoria")
    @classmethod
    def categoria_ok(cls, value: str) -> str:
        val = (value or "personal").strip().lower()
        if val not in CATEGORIAS_CLIENTE:
            raise ValueError("Categoría inválida")
        return val


class ClienteCreate(ClienteBase):
    pass


class ClienteUpdate(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    nombre_completo: Optional[str] = Field(default=None, min_length=2, max_length=220)
    tipo_persona: Optional[str] = None
    categoria: Optional[str] = None
    numero_identificacion: Optional[str] = Field(default=None, min_length=2, max_length=80)
    direccion: Optional[str] = Field(default=None, max_length=500)
    provincia: Optional[str] = Field(default=None, max_length=80)
    canton: Optional[str] = Field(default=None, max_length=80)
    distrito: Optional[str] = Field(default=None, max_length=80)
    telefono: Optional[str] = Field(default=None, max_length=80)
    correo: Optional[EmailStr] = None
    activo: Optional[bool] = None

    @field_validator("tipo_persona")
    @classmethod
    def tipo_persona_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        val = value.strip().lower()
        if val not in TIPOS_PERSONA:
            raise ValueError("Tipo de persona inválido")
        return val

    @field_validator("categoria")
    @classmethod
    def categoria_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        val = value.strip().lower()
        if val not in CATEGORIAS_CLIENTE:
            raise ValueError("Categoría inválida")
        return val


class ClienteOut(ClienteBase):
    id: int
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class FincaBase(BaseModel):
    # Opcional para permitir crear fincas desde /clientes/{cliente_id}/fincas,
    # donde el cliente se toma de la ruta. El endpoint directo /fincas lo valida explícitamente.
    cliente_id: Optional[int] = None
    codigo: Optional[str] = Field(default=None, max_length=80)
    nombre: str = Field(min_length=2, max_length=180)
    ubicacion: Optional[str] = Field(default=None, max_length=500)
    google_maps_url: Optional[str] = Field(default=None, max_length=1000)
    provincia: Optional[str] = Field(default=None, max_length=80)
    canton: Optional[str] = Field(default=None, max_length=80)
    distrito: Optional[str] = Field(default=None, max_length=80)
    area: Optional[float] = Field(default=None, ge=0)
    cultivo: Optional[str] = Field(default="Café", max_length=120)
    caracteristicas: Optional[str] = None

    propietario: Optional[str] = Field(default=None, max_length=180)
    altitud: Optional[int] = None
    variedades: List[str] = Field(default_factory=list)
    activa: bool = True
    gestion_fincas_habilitada: bool = False


class FincaCreate(FincaBase):
    pass


class FincaUpdate(BaseModel):
    cliente_id: Optional[int] = None
    codigo: Optional[str] = None
    nombre: Optional[str] = None
    ubicacion: Optional[str] = None
    google_maps_url: Optional[str] = None
    provincia: Optional[str] = None
    canton: Optional[str] = None
    distrito: Optional[str] = None
    area: Optional[float] = Field(default=None, ge=0)
    cultivo: Optional[str] = None
    caracteristicas: Optional[str] = None

    propietario: Optional[str] = None
    altitud: Optional[int] = None
    variedades: Optional[List[str]] = None
    activa: Optional[bool] = None
    gestion_fincas_habilitada: Optional[bool] = None


class FincaOut(FincaBase):
    id: int
    cliente_nombre: Optional[str] = None
    produccion_fanegas_ha: Optional[float] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class MapLocationResolveIn(BaseModel):
    value: str = Field(min_length=3, max_length=2000)


class MapLocationResolveOut(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    canonical_url: str
    source: str


class ReciboBase(BaseModel):
    numero_recibo: Optional[str] = None
    client_uuid: Optional[str] = None
    fecha: date
    cosecha: Optional[str] = None
    cliente_id: Optional[int] = None
    finca_id: Optional[int] = None
    productor_nombre: Optional[str] = Field(default=None, max_length=180)
    productor_cedula: Optional[str] = Field(default=None, max_length=80)
    provincia: Optional[str] = Field(default=None, max_length=80)
    canton: Optional[str] = Field(default=None, max_length=80)
    distrito: Optional[str] = Field(default=None, max_length=80)
    zona: Optional[str] = Field(default=None, max_length=40)
    cajuelas: float = Field(default=0, ge=0)
    cuartillos: float = Field(default=0, ge=0)
    porcentaje_flote: Optional[float] = Field(default=None, ge=0, le=100)
    porcentaje_verde: Optional[float] = Field(default=None, ge=0, le=100)
    peso_promedio_cajuela: Optional[float] = Field(default=None, ge=0)
    # Legado: se acepta para compatibilidad con clientes antiguos.
    precio_promedio_cajuela: Optional[float] = Field(default=None, ge=0)
    precio_fanega: Optional[float] = Field(default=None, ge=0)
    precio_fanega_letras: Optional[str] = Field(default=None, max_length=255)
    precio_adelanto_letras: Optional[str] = Field(default=None, max_length=255)
    beneficio_recibe_usuario_id: Optional[int] = None
    beneficio_recibe: Optional[str] = Field(default=None, max_length=180)
    beneficio_firma_url: Optional[str] = Field(default=None, max_length=1000)
    productor_entrega: Optional[str] = Field(default=None, max_length=180)
    estado: str = "recibido"
    observaciones: Optional[str] = None


class ReciboCreate(ReciboBase):
    pass


class ReciboUpdate(BaseModel):
    numero_recibo: Optional[str] = None
    fecha: Optional[date] = None
    cosecha: Optional[str] = None
    cliente_id: Optional[int] = None
    finca_id: Optional[int] = None
    productor_nombre: Optional[str] = None
    productor_cedula: Optional[str] = None
    provincia: Optional[str] = None
    canton: Optional[str] = None
    distrito: Optional[str] = None
    zona: Optional[str] = None
    cajuelas: Optional[float] = Field(default=None, ge=0)
    cuartillos: Optional[float] = Field(default=None, ge=0)
    porcentaje_flote: Optional[float] = Field(default=None, ge=0, le=100)
    porcentaje_verde: Optional[float] = Field(default=None, ge=0, le=100)
    peso_promedio_cajuela: Optional[float] = Field(default=None, ge=0)
    precio_promedio_cajuela: Optional[float] = Field(default=None, ge=0)
    precio_fanega: Optional[float] = Field(default=None, ge=0)
    precio_fanega_letras: Optional[str] = None
    precio_adelanto_letras: Optional[str] = None
    beneficio_recibe_usuario_id: Optional[int] = None
    beneficio_recibe: Optional[str] = None
    beneficio_firma_url: Optional[str] = None
    productor_entrega: Optional[str] = None
    estado: Optional[str] = None
    observaciones: Optional[str] = None


class ReciboOut(BaseModel):
    id: int
    numero_recibo: str
    client_uuid: Optional[str] = None
    fecha: date
    cosecha: Optional[str] = None
    cliente_id: Optional[int] = None
    cliente_nombre: Optional[str] = None
    cliente_correo: Optional[str] = None
    cliente_telefono: Optional[str] = None
    finca_id: Optional[int] = None
    finca_nombre: Optional[str] = None
    productor_nombre: str
    productor_cedula: Optional[str] = None
    provincia: Optional[str] = None
    canton: Optional[str] = None
    distrito: Optional[str] = None
    zona: Optional[str] = None
    cajuelas: float = 0
    cuartillos: float = 0
    fanegas_estimadas: float = 0
    porcentaje_flote: Optional[float] = None
    porcentaje_verde: Optional[float] = None
    peso_promedio_cajuela: Optional[float] = None
    precio_promedio_cajuela: Optional[float] = None
    precio_fanega: Optional[float] = None
    precio_fanega_letras: Optional[str] = None
    precio_adelanto_letras: Optional[str] = None
    beneficio_recibe_usuario_id: Optional[int] = None
    beneficio_recibe: Optional[str] = None
    beneficio_firma_url: Optional[str] = None
    productor_entrega: Optional[str] = None
    estado: str = "recibido"
    observaciones: Optional[str] = None
    qr_payload: Optional[str] = None
    monto_estimado: float = 0
    liquidado: bool = False
    estado_liquidacion: str = "pendiente"
    liquidado_at: Optional[datetime] = None
    liquidado_por_id: Optional[int] = None
    liquidado_por_nombre: Optional[str] = None
    liquidacion_nota: Optional[str] = None
    liquidacion_numero_transferencia: Optional[str] = None
    liquidacion_monto: Optional[float] = None
    liquidacion_comprobante_nombre: Optional[str] = None
    liquidacion_comprobante_tipo: Optional[str] = None
    liquidacion_comprobante_tamano: Optional[int] = None
    created_by_id: Optional[int] = None
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class ReciboPortalLinkOut(BaseModel):
    url: str
    cliente_id: int
    cliente_nombre: str


class PortalRecibosVerifyIn(BaseModel):
    numero_identificacion: str = Field(min_length=3, max_length=80)


class PortalRecibosVerifyOut(BaseModel):
    session_token: str
    expires_at: datetime
    cliente_nombre: str


class PortalReciboResumenOut(BaseModel):
    total_recibos: int = 0
    total_cajuelas: float = 0
    total_fanegas: float = 0
    monto_total: float = 0
    monto_cobrado: float = 0
    monto_por_cobrar: float = 0
    recibos_liquidados: int = 0
    recibos_pendientes: int = 0
    moneda: str = "CRC"


class PortalReciboItemOut(BaseModel):
    id: int
    numero_recibo: str
    fecha: date
    cosecha: Optional[str] = None
    finca_nombre: Optional[str] = None
    cajuelas: float = 0
    cuartillos: float = 0
    fanegas: float = 0
    precio_fanega: float = 0
    monto: float = 0
    liquidado: bool = False
    estado_liquidacion: str = "pendiente"
    liquidado_at: Optional[datetime] = None
    liquidacion_monto: Optional[float] = None
    liquidacion_numero_transferencia: Optional[str] = None


class PortalRecibosOut(BaseModel):
    cliente_nombre: str
    resumen: PortalReciboResumenOut
    recibos: List[PortalReciboItemOut] = Field(default_factory=list)


class OTCreate(BaseModel):
    codigo_lote: Optional[str] = None
    client_uuid: Optional[str] = None
    proceso: str = "miel"
    fecha_inicio: date
    finca_id: Optional[int] = None
    operario_id: Optional[int] = None

    # Nuevo formato correcto: recibos con cantidad parcial asignada.
    recibos: List[OTReciboAsignacionCreate] = Field(default_factory=list)

    # Compatibilidad temporal con formato viejo.
    recibo_ids: List[int] = Field(default_factory=list)

    objetivo_cajuelas: Optional[float] = Field(default=None, ge=0)
    objetivo_fanegas: Optional[float] = Field(default=None, ge=0)
    observaciones: Optional[str] = None

    @field_validator("proceso")
    @classmethod
    def proceso_ok(cls, value: str) -> str:
        val = (value or "miel").strip().lower()
        if val not in PROCESOS:
            raise ValueError("Proceso inválido")
        return val

class OTUpdate(BaseModel):
    proceso: Optional[str] = None
    estado: Optional[str] = None
    fecha_inicio: Optional[date] = None
    finca_id: Optional[int] = None
    operario_id: Optional[int] = None
    observaciones: Optional[str] = None
    comentario_gerencial: Optional[str] = None

    @field_validator("proceso")
    @classmethod
    def proceso_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        val = value.strip().lower()
        if val not in PROCESOS:
            raise ValueError("Proceso inválido")
        return val

    @field_validator("estado")
    @classmethod
    def estado_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        val = value.strip().lower()
        if val not in ESTADOS_OT:
            raise ValueError("Estado inválido")
        return val


class SeguimientoCreate(BaseModel):
    client_uuid: Optional[str] = None
    fecha: date
    marca_tiempo: Optional[datetime] = None
    actividad_realizada: str = Field(min_length=2, max_length=180)
    horas_implementadas: Optional[float] = Field(default=None, ge=0)
    temperatura: Optional[float] = None
    humedad: Optional[float] = Field(default=None, ge=0, le=100)
    comentario: Optional[str] = None
    estado_lote_resultante: Optional[str] = None
    dar_alerta: bool = False
    alerta_mensaje: Optional[str] = None
    alerta_canal: Optional[str] = Field(default="whatsapp", max_length=40)

    @field_validator("estado_lote_resultante")
    @classmethod
    def estado_lote_resultante_ok(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        val = value.strip().lower()
        if val not in ESTADOS_OT:
            raise ValueError("Estado de lote inválido")
        return val


class SeguimientoOut(BaseModel):
    id: int
    client_uuid: Optional[str] = None
    fecha: date
    marca_tiempo: Optional[datetime] = None
    actividad_realizada: str
    horas_implementadas: Optional[float] = None
    temperatura: Optional[float] = None
    humedad: Optional[float] = None
    comentario: Optional[str] = None
    estado_lote_resultante: Optional[str] = None
    dar_alerta: bool = False
    alerta_mensaje: Optional[str] = None
    alerta_canal: Optional[str] = None
    alerta_whatsapp_url: Optional[str] = None
    alerta_correo_url: Optional[str] = None
    estado_revision: str = "pendiente"
    comentario_revision: Optional[str] = None
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class OTReciboOut(BaseModel):
    id: int
    numero_recibo: str
    productor_nombre: str
    cajuelas: float = 0
    cuartillos: float = 0
    fanegas_estimadas: float = 0
    cajuelas_asignadas: float = 0
    cuartillos_asignados: float = 0
    fanegas_asignadas: float = 0


class DocumentoLoteOut(BaseModel):
    id: int
    ot_id: int
    titulo: str
    tipo: str = "documento"
    descripcion: Optional[str] = None
    file_url: str
    file_name: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class OTUnionOrigenOut(BaseModel):
    id: int
    ot_origen_id: Optional[int] = None
    codigo_lote_origen: str
    estado_origen: Optional[str] = None
    estado_actual_origen: Optional[str] = None
    proceso_origen: Optional[str] = None
    fanegas_origen: Optional[float] = None
    progreso_origen_porcentaje: Optional[int] = None
    motivo: Optional[str] = None
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None
    recibos: List[OTReciboOut] = Field(default_factory=list)
    seguimientos: List[SeguimientoOut] = Field(default_factory=list)
    documentos: List[DocumentoLoteOut] = Field(default_factory=list)
    comentarios: List[ComentarioLoteOut] = Field(default_factory=list)


class OTMergePayload(BaseModel):
    ot_ids: List[int] = Field(min_length=2)
    codigo_lote: Optional[str] = None
    motivo: str = Field(min_length=3)
    proceso: Optional[str] = None
    fecha_inicio: Optional[date] = None
    operario_id: Optional[int] = None
    observaciones: Optional[str] = None


class ComentarioLoteCreate(BaseModel):
    tipo: str = Field(default="nota", max_length=40)
    comentario: str = Field(min_length=2)


class ComentarioLoteOut(BaseModel):
    id: int
    ot_id: int
    tipo: str = "nota"
    comentario: str
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class CotizacionVentaLineaCreate(BaseModel):
    ot_id: Optional[int] = None
    descripcion: str = Field(min_length=2, max_length=500)
    cantidad_quintales: float = Field(ge=0)
    precio_unitario: float = Field(ge=0)
    impuesto_porcentaje: float = Field(default=0, ge=0)


class CotizacionVentaLineaOut(BaseModel):
    id: int
    cotizacion_id: Optional[int] = None
    ot_id: Optional[int] = None
    codigo_lote: Optional[str] = None
    descripcion: str
    cantidad_quintales: float = 0
    precio_unitario: float = 0
    impuesto_porcentaje: float = 0
    subtotal: float = 0
    impuesto: float = 0
    total: float = 0


class CotizacionVentaCreate(BaseModel):
    cliente_id: Optional[int] = None
    fecha: date
    validez_dias: int = Field(default=8, ge=1)
    moneda: str = Field(default="USD", max_length=12)
    condiciones: Optional[str] = None
    observaciones: Optional[str] = None
    lineas: List[CotizacionVentaLineaCreate] = Field(default_factory=list)


class CotizacionVentaUpdate(BaseModel):
    cliente_id: Optional[int] = None
    estado: Optional[str] = Field(default=None, max_length=40)
    fecha: Optional[date] = None
    validez_dias: Optional[int] = Field(default=None, ge=1)
    moneda: Optional[str] = Field(default=None, max_length=12)
    condiciones: Optional[str] = None
    observaciones: Optional[str] = None
    lineas: Optional[List[CotizacionVentaLineaCreate]] = None


class CotizacionVentaOut(BaseModel):
    id: int
    codigo: str
    cliente_id: Optional[int] = None
    cliente_nombre: Optional[str] = None
    cliente_correo: Optional[str] = None
    cliente_telefono: Optional[str] = None
    estado: str
    fecha: date
    validez_dias: int = 8
    moneda: str = "USD"
    condiciones: Optional[str] = None
    observaciones: Optional[str] = None
    subtotal: float = 0
    impuesto: float = 0
    total: float = 0
    lineas: List[CotizacionVentaLineaOut] = Field(default_factory=list)
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class SolicitudSalidaVentaLineaCreate(BaseModel):
    ot_id: int
    descripcion: Optional[str] = Field(default=None, max_length=500)
    cantidad_quintales: float = Field(ge=0)
    precio_unitario: float = Field(ge=0)
    impuesto_porcentaje: float = Field(default=0, ge=0)


class SolicitudSalidaVentaLineaOut(BaseModel):
    id: int
    solicitud_id: Optional[int] = None
    ot_id: Optional[int] = None
    codigo_lote: Optional[str] = None
    descripcion: str
    cantidad_quintales: float = 0
    precio_unitario: float = 0
    impuesto_porcentaje: float = 0
    subtotal: float = 0
    impuesto: float = 0
    total: float = 0


class SolicitudSalidaVentaCreate(BaseModel):
    cotizacion_id: Optional[int] = None
    cliente_id: Optional[int] = None
    fecha: date
    moneda: str = Field(default="USD", max_length=12)
    tipo_envio: Optional[str] = Field(default=None, max_length=80)
    direccion_entrega: Optional[str] = Field(default=None, max_length=500)
    observaciones: Optional[str] = None
    lineas: List[SolicitudSalidaVentaLineaCreate] = Field(default_factory=list)


class SolicitudSalidaVentaOut(BaseModel):
    id: int
    codigo: str
    cotizacion_id: Optional[int] = None
    codigo_cotizacion: Optional[str] = None
    cliente_id: Optional[int] = None
    cliente_nombre: Optional[str] = None
    cliente_correo: Optional[str] = None
    cliente_telefono: Optional[str] = None
    estado: str
    fecha: date
    moneda: str = "USD"
    tipo_envio: Optional[str] = None
    direccion_entrega: Optional[str] = None
    observaciones: Optional[str] = None
    subtotal: float = 0
    impuesto: float = 0
    total: float = 0
    lineas: List[SolicitudSalidaVentaLineaOut] = Field(default_factory=list)
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class VentaSeguimientoCreate(BaseModel):
    fecha: date
    canal: str = Field(default="nota", max_length=40)
    asunto: Optional[str] = Field(default=None, max_length=180)
    comentario: str = Field(min_length=2)


class VentaSeguimientoOut(BaseModel):
    id: int
    lead_id: int
    fecha: date
    canal: str
    asunto: Optional[str] = None
    comentario: str
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class VentaLeadCreate(BaseModel):
    cliente_id: Optional[int] = None
    cantidad_quintales: Optional[float] = Field(default=None, ge=0)
    precio_unitario: Optional[float] = Field(default=None, ge=0)
    moneda: str = Field(default="USD", max_length=12)
    notas: Optional[str] = None


class VentaLeadUpdate(BaseModel):
    cliente_id: Optional[int] = None
    estado: Optional[str] = Field(default=None, max_length=40)
    cantidad_quintales: Optional[float] = Field(default=None, ge=0)
    precio_unitario: Optional[float] = Field(default=None, ge=0)
    moneda: Optional[str] = Field(default=None, max_length=12)
    notas: Optional[str] = None


class VentaLeadOut(BaseModel):
    id: int
    ot_id: int
    cliente_id: Optional[int] = None
    cliente_nombre: Optional[str] = None
    cliente_correo: Optional[str] = None
    cliente_telefono: Optional[str] = None
    estado: str
    cantidad_quintales: Optional[float] = None
    precio_unitario: Optional[float] = None
    moneda: str = "USD"
    notas: Optional[str] = None
    seguimientos: List[VentaSeguimientoOut] = Field(default_factory=list)
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class SolicitudVentaSeguimientoCreate(BaseModel):
    fecha: date
    canal: str = Field(default="nota", max_length=40)
    asunto: Optional[str] = Field(default=None, max_length=180)
    comentario: str = Field(min_length=2)


class SolicitudVentaSeguimientoOut(BaseModel):
    id: int
    solicitud_id: int
    fecha: date
    canal: str
    asunto: Optional[str] = None
    comentario: str
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class SolicitudVentaDocumentoOut(BaseModel):
    id: int
    solicitud_id: int
    titulo: str
    tipo: str = "documento"
    descripcion: Optional[str] = None
    file_url: str
    file_name: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class SolicitudVentaLineaLiquidacionOut(BaseModel):
    id: int
    solicitud_id: int
    linea_id: int
    salida_id: int
    codigo_salida: Optional[str] = None
    salida_linea_id: Optional[int] = None
    ot_id: Optional[int] = None
    codigo_lote: Optional[str] = None
    cantidad_quintales: float = 0
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class SolicitudVentaLineaCreate(BaseModel):
    descripcion: Optional[str] = Field(default=None, max_length=500)
    proceso_preferido: Optional[str] = Field(default=None, max_length=40)
    cantidad_quintales: float = Field(gt=0)
    precio_objetivo: Optional[float] = Field(default=None, ge=0)
    moneda: Optional[str] = Field(default=None, max_length=12)
    observaciones: Optional[str] = None


class SolicitudVentaLineaOut(BaseModel):
    id: int
    solicitud_id: int
    descripcion: str
    proceso_preferido: Optional[str] = None
    cantidad_quintales: float = 0
    cantidad_liquidada: float = 0
    cantidad_pendiente: float = 0
    precio_objetivo: Optional[float] = None
    moneda: str = "USD"
    observaciones: Optional[str] = None
    liquidaciones: List[SolicitudVentaLineaLiquidacionOut] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class SolicitudVentaLiquidarLineaPayload(BaseModel):
    solicitud_linea_id: int
    ot_id: int
    cantidad_quintales: float = Field(gt=0)
    precio_unitario: float = Field(ge=0)
    impuesto_porcentaje: float = Field(default=0, ge=0)
    descripcion: Optional[str] = Field(default=None, max_length=500)


class SolicitudVentaLiquidarPayload(BaseModel):
    cotizacion_id: Optional[int] = None
    cliente_id: Optional[int] = None
    fecha: date
    moneda: str = Field(default="USD", max_length=12)
    tipo_envio: Optional[str] = Field(default=None, max_length=80)
    direccion_entrega: Optional[str] = Field(default=None, max_length=500)
    observaciones: Optional[str] = None
    lineas: List[SolicitudVentaLiquidarLineaPayload] = Field(default_factory=list)


class SolicitudVentaCreate(BaseModel):
    cliente_id: Optional[int] = None
    cantidad_quintales: Optional[float] = Field(default=None, ge=0)
    proceso_preferido: Optional[str] = Field(default=None, max_length=40)
    precio_objetivo: Optional[float] = Field(default=None, ge=0)
    moneda: str = Field(default="USD", max_length=12)
    observaciones: Optional[str] = None
    lineas: List[SolicitudVentaLineaCreate] = Field(default_factory=list)


class SolicitudVentaUpdate(BaseModel):
    cliente_id: Optional[int] = None
    ot_id: Optional[int] = None
    estado: Optional[str] = Field(default=None, max_length=40)
    cantidad_quintales: Optional[float] = Field(default=None, ge=0)
    proceso_preferido: Optional[str] = Field(default=None, max_length=40)
    precio_objetivo: Optional[float] = Field(default=None, ge=0)
    moneda: Optional[str] = Field(default=None, max_length=12)
    observaciones: Optional[str] = None
    lineas: Optional[List[SolicitudVentaLineaCreate]] = None


class SolicitudVentaOut(BaseModel):
    id: int
    codigo: str
    cliente_id: Optional[int] = None
    cliente_nombre: Optional[str] = None
    cliente_correo: Optional[str] = None
    cliente_telefono: Optional[str] = None
    ot_id: Optional[int] = None
    codigo_lote: Optional[str] = None
    estado: str
    cantidad_quintales: float = 0
    proceso_preferido: Optional[str] = None
    precio_objetivo: Optional[float] = None
    moneda: str = "USD"
    observaciones: Optional[str] = None
    seguimientos: List[SolicitudVentaSeguimientoOut] = Field(default_factory=list)
    documentos: List[SolicitudVentaDocumentoOut] = Field(default_factory=list)
    lineas: List[SolicitudVentaLineaOut] = Field(default_factory=list)
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class AssignSolicitudVentaPayload(BaseModel):
    ot_id: int
    comentario: Optional[str] = None


class OTOut(BaseModel):
    id: int
    codigo_lote: str
    client_uuid: Optional[str] = None
    proceso: str
    estado: str
    fecha_inicio: date
    finca_id: Optional[int] = None
    finca_nombre: Optional[str] = None
    operario_id: Optional[int] = None
    operario_nombre: Optional[str] = None
    fanegas_estimadas: float = 0
    quintales_base: float = 0
    quintales_vendidos: float = 0
    quintales_disponibles: float = 0
    objetivo_cajuelas: Optional[float] = None
    objetivo_fanegas: Optional[float] = None
    qr_token: Optional[str] = None
    qr_public_url: Optional[str] = None
    qr_payload: Optional[str] = None
    observaciones: Optional[str] = None
    recibos: List[OTReciboOut] = Field(default_factory=list)
    seguimientos: List[SeguimientoOut] = Field(default_factory=list)
    documentos: List[DocumentoLoteOut] = Field(default_factory=list)
    comentarios: List[ComentarioLoteOut] = Field(default_factory=list)
    ventas_leads: List[VentaLeadOut] = Field(default_factory=list)
    cotizaciones_venta: List[CotizacionVentaOut] = Field(default_factory=list)
    solicitudes_salida_venta: List[SolicitudSalidaVentaOut] = Field(default_factory=list)
    origenes_unidos: List[OTUnionOrigenOut] = Field(default_factory=list)
    created_by_id: Optional[int] = None
    created_at: Optional[datetime] = None


class DashboardKPI(BaseModel):
    key: str
    label: str
    value: float | int | str


class ChartPoint(BaseModel):
    label: str
    value: float


class DashboardOut(BaseModel):
    kpis: List[DashboardKPI] = Field(default_factory=list)
    recibos_por_mes: List[ChartPoint] = Field(default_factory=list)
    ots_por_estado: List[ChartPoint] = Field(default_factory=list)
    sugerencias: List[str] = Field(default_factory=list)


class SyncPush(BaseModel):
    recibos: List[ReciboCreate] = Field(default_factory=list)
    ots: List[OTCreate] = Field(default_factory=list)
    seguimientos: List[dict[str, Any]] = Field(default_factory=list)
    notas: List[dict[str, Any]] = Field(default_factory=list)


class SyncResultItem(BaseModel):
    client_uuid: Optional[str] = None
    id: Optional[int] = None
    status: str
    detail: Optional[str] = None


class SyncResult(BaseModel):
    recibos: List[SyncResultItem] = Field(default_factory=list)
    ots: List[SyncResultItem] = Field(default_factory=list)
    seguimientos: List[SyncResultItem] = Field(default_factory=list)
    notas: List[SyncResultItem] = Field(default_factory=list)

class PublicLoteReciboOut(BaseModel):
    numero_recibo: str
    fecha: date
    dias_desde_ingreso: int
    finca_nombre: Optional[str] = None
    provincia: Optional[str] = None
    canton: Optional[str] = None
    distrito: Optional[str] = None
    cajuelas_equivalentes: float
    fanegas_equivalentes: float


class PublicLoteSeguimientoOut(BaseModel):
    fecha: date
    marca_tiempo: Optional[datetime] = None
    actividad_realizada: str
    horas_implementadas: Optional[float] = None
    temperatura: Optional[float] = None
    humedad: Optional[float] = None
    comentario: Optional[str] = None
    dar_alerta: bool = False


class PublicLoteDocumentoOut(BaseModel):
    titulo: str
    tipo: str = "documento"
    descripcion: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    created_at: Optional[datetime] = None


class PublicLoteEventoOut(BaseModel):
    tipo: str
    titulo: str
    descripcion: Optional[str] = None
    fecha: Optional[date] = None
    created_at: Optional[datetime] = None


class PublicLoteOrigenOut(BaseModel):
    codigo_lote_origen: str
    estado_origen: Optional[str] = None
    estado_actual_origen: Optional[str] = None
    proceso_origen: Optional[str] = None
    fanegas_origen: Optional[float] = None
    progreso_origen_porcentaje: Optional[int] = None
    motivo: Optional[str] = None
    created_at: Optional[datetime] = None
    recibos: List[PublicLoteReciboOut] = Field(default_factory=list)
    seguimientos: List[PublicLoteSeguimientoOut] = Field(default_factory=list)
    documentos: List[PublicLoteDocumentoOut] = Field(default_factory=list)
    eventos: List[PublicLoteEventoOut] = Field(default_factory=list)


class PublicLoteOut(BaseModel):
    codigo_lote: str
    proceso: str
    estado: str
    fecha_inicio: date
    dias_desde_inicio: int
    fanegas_estimadas: float
    objetivo_cajuelas: Optional[float] = None
    objetivo_fanegas: Optional[float] = None
    qr_token: str
    qr_public_url: str
    resumen: str
    recibos: List[PublicLoteReciboOut] = Field(default_factory=list)
    seguimientos: List[PublicLoteSeguimientoOut] = Field(default_factory=list)
    documentos: List[PublicLoteDocumentoOut] = Field(default_factory=list)
    eventos: List[PublicLoteEventoOut] = Field(default_factory=list)
    origenes_unidos: List[PublicLoteOrigenOut] = Field(default_factory=list)
class OTReciboAsignacionCreate(BaseModel):
    recibo_id: int
    cajuelas_asignadas: float = Field(gt=0)
    cuartillos_asignados: float = Field(default=0, ge=0, le=3)


class ReciboDisponibleOTOut(BaseModel):
    id: int
    numero_recibo: str
    fecha: date
    cliente_nombre: Optional[str] = None
    productor_nombre: Optional[str] = None
    finca_nombre: Optional[str] = None
    provincia: Optional[str] = None
    canton: Optional[str] = None
    distrito: Optional[str] = None
    cajuelas: float = 0
    cuartillos: float = 0
    cajuelas_totales: float = 0
    cajuelas_asignadas: float = 0
    cajuelas_disponibles: float = 0
    fanegas_disponibles: float = 0
    dias_desde_ingreso: int = 0


class EstadoOTPayload(BaseModel):
    comentario: Optional[str] = None


class SeguimientoRevisionPayload(BaseModel):
    estado_revision: str = Field(pattern="^(aprobado|rechazado)$")
    comentario_revision: Optional[str] = None

class ActividadFincaBase(BaseModel):
    nombre: str = Field(min_length=2, max_length=180)
    tipo: str = Field(default="mantenimiento", max_length=60)
    ambito: str = Field(default="finca", max_length=30)
    descripcion: Optional[str] = None
    requiere_insumo: bool = False
    unidad_referencia: Optional[str] = Field(default=None, max_length=60)
    activa: bool = True


class ActividadFincaCreate(ActividadFincaBase):
    pass


class ActividadFincaUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=2, max_length=180)
    tipo: Optional[str] = Field(default=None, max_length=60)
    ambito: Optional[str] = Field(default=None, max_length=30)
    descripcion: Optional[str] = None
    requiere_insumo: Optional[bool] = None
    unidad_referencia: Optional[str] = Field(default=None, max_length=60)
    activa: Optional[bool] = None


class ActividadFincaOut(ActividadFincaBase):
    id: int
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TrabajadorFincaBase(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    nombre: str = Field(min_length=2, max_length=180)
    identificacion: Optional[str] = Field(default=None, max_length=80)
    telefono: Optional[str] = Field(default=None, max_length=80)
    puesto: Optional[str] = Field(default=None, max_length=120)
    jornal_diario: Optional[float] = Field(default=None, ge=0)
    activo: bool = True


class TrabajadorFincaCreate(TrabajadorFincaBase):
    pass


class TrabajadorFincaUpdate(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    nombre: Optional[str] = Field(default=None, min_length=2, max_length=180)
    identificacion: Optional[str] = Field(default=None, max_length=80)
    telefono: Optional[str] = Field(default=None, max_length=80)
    puesto: Optional[str] = Field(default=None, max_length=120)
    jornal_diario: Optional[float] = Field(default=None, ge=0)
    activo: Optional[bool] = None


class TrabajadorFincaOut(TrabajadorFincaBase):
    id: int
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}




class ProveedorContactoIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=180)
    puesto: Optional[str] = Field(default=None, max_length=140)
    correo: Optional[EmailStr] = None
    telefono: Optional[str] = Field(default=None, max_length=80)
    principal: bool = False
    activo: bool = True
    notas: Optional[str] = None


class ProveedorContactoOut(ProveedorContactoIn):
    id: int
    created_at: Optional[datetime] = None


class ProveedorBase(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    nombre_legal: str = Field(min_length=2, max_length=220)
    nombre_comercial: Optional[str] = Field(default=None, max_length=220)
    identificacion: Optional[str] = Field(default=None, max_length=100)
    tipo_identificacion: Optional[str] = Field(default=None, max_length=40)
    correo: Optional[EmailStr] = None
    telefono: Optional[str] = Field(default=None, max_length=80)
    sitio_web: Optional[str] = Field(default=None, max_length=500)
    direccion: Optional[str] = Field(default=None, max_length=500)
    provincia: Optional[str] = Field(default=None, max_length=80)
    canton: Optional[str] = Field(default=None, max_length=80)
    distrito: Optional[str] = Field(default=None, max_length=80)
    notas: Optional[str] = None
    activo: bool = True


class ProveedorCreate(ProveedorBase):
    contactos: List[ProveedorContactoIn] = Field(default_factory=list)


class ProveedorUpdate(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    nombre_legal: Optional[str] = Field(default=None, min_length=2, max_length=220)
    nombre_comercial: Optional[str] = Field(default=None, max_length=220)
    identificacion: Optional[str] = Field(default=None, max_length=100)
    tipo_identificacion: Optional[str] = Field(default=None, max_length=40)
    correo: Optional[EmailStr] = None
    telefono: Optional[str] = Field(default=None, max_length=80)
    sitio_web: Optional[str] = Field(default=None, max_length=500)
    direccion: Optional[str] = Field(default=None, max_length=500)
    provincia: Optional[str] = Field(default=None, max_length=80)
    canton: Optional[str] = Field(default=None, max_length=80)
    distrito: Optional[str] = Field(default=None, max_length=80)
    notas: Optional[str] = None
    activo: Optional[bool] = None
    contactos: Optional[List[ProveedorContactoIn]] = None


class ProveedorOut(ProveedorBase):
    id: int
    contactos: List[ProveedorContactoOut] = Field(default_factory=list)
    facturas_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class NotaRapidaCreate(BaseModel):
    client_uuid: Optional[str] = Field(default=None, max_length=120)
    finca_id: Optional[int] = None
    titulo: Optional[str] = Field(default=None, max_length=180)
    contenido: str = Field(min_length=1, max_length=20000)
    categoria: str = Field(default="general", max_length=40)
    prioridad: str = Field(default="normal", max_length=20)
    visibilidad: str = Field(default="personal", max_length=20)
    origen: str = Field(default="manual", max_length=30)
    fijada: bool = False
    etiquetas: List[str] = Field(default_factory=list, max_length=12)
    contexto: dict[str, Any] = Field(default_factory=dict)

    @field_validator("categoria")
    @classmethod
    def categoria_ok(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in NOTA_CATEGORIAS:
            raise ValueError("Categoría de nota inválida")
        return value

    @field_validator("prioridad")
    @classmethod
    def prioridad_ok(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in NOTA_PRIORIDADES:
            raise ValueError("Prioridad de nota inválida")
        return value

    @field_validator("visibilidad")
    @classmethod
    def visibilidad_ok(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in NOTA_VISIBILIDADES:
            raise ValueError("Visibilidad de nota inválida")
        return value

    @field_validator("etiquetas")
    @classmethod
    def etiquetas_ok(cls, values: List[str]) -> List[str]:
        cleaned: list[str] = []
        for raw in values:
            tag = str(raw or "").strip().lower()[:40]
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return cleaned[:12]


class NotaRapidaUpdate(BaseModel):
    expected_version: Optional[int] = Field(default=None, ge=1)
    finca_id: Optional[int] = None
    titulo: Optional[str] = Field(default=None, max_length=180)
    contenido: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    categoria: Optional[str] = Field(default=None, max_length=40)
    estado: Optional[str] = Field(default=None, max_length=40)
    prioridad: Optional[str] = Field(default=None, max_length=20)
    visibilidad: Optional[str] = Field(default=None, max_length=20)
    fijada: Optional[bool] = None
    etiquetas: Optional[List[str]] = Field(default=None, max_length=12)
    contexto: Optional[dict[str, Any]] = None
    linked_entity_type: Optional[str] = Field(default=None, max_length=60)
    linked_entity_id: Optional[int] = None

    @field_validator("categoria")
    @classmethod
    def categoria_ok(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip().lower()
        if value not in NOTA_CATEGORIAS:
            raise ValueError("Categoría de nota inválida")
        return value

    @field_validator("estado")
    @classmethod
    def estado_ok(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip().lower()
        if value not in NOTA_ESTADOS:
            raise ValueError("Estado de nota inválido")
        return value

    @field_validator("prioridad")
    @classmethod
    def prioridad_ok(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip().lower()
        if value not in NOTA_PRIORIDADES:
            raise ValueError("Prioridad de nota inválida")
        return value

    @field_validator("visibilidad")
    @classmethod
    def visibilidad_ok(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip().lower()
        if value not in NOTA_VISIBILIDADES:
            raise ValueError("Visibilidad de nota inválida")
        return value

    @field_validator("etiquetas")
    @classmethod
    def etiquetas_ok(cls, values: Optional[List[str]]) -> Optional[List[str]]:
        if values is None:
            return values
        cleaned: list[str] = []
        for raw in values:
            tag = str(raw or "").strip().lower()[:40]
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return cleaned[:12]


class NotaRapidaOut(BaseModel):
    id: int
    public_id: str
    client_uuid: Optional[str] = None
    finca_id: Optional[int] = None
    finca_nombre: Optional[str] = None
    titulo: Optional[str] = None
    contenido: str
    categoria: str
    estado: str
    prioridad: str
    visibilidad: str
    origen: str
    fijada: bool = False
    etiquetas: List[str] = Field(default_factory=list)
    contexto: dict[str, Any] = Field(default_factory=dict)
    linked_entity_type: Optional[str] = None
    linked_entity_id: Optional[int] = None
    procesada_at: Optional[datetime] = None
    procesada_por_id: Optional[int] = None
    procesada_por_nombre: Optional[str] = None
    created_by_id: Optional[int] = None
    created_by_nombre: Optional[str] = None
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class InsumoFincaBase(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    codigo_fabricante: Optional[str] = Field(default=None, max_length=120)
    nombre: str = Field(min_length=2, max_length=180)
    tipo: str = Field(default="general", max_length=80)
    unidad: str = Field(default="unidad", max_length=60)
    costo_unitario: Optional[float] = Field(default=None, ge=0)
    precio_unitario: Optional[float] = Field(default=None, ge=0)
    impuesto_porcentaje: float = Field(default=0, ge=0, le=100)
    stock_actual: float = Field(default=0, ge=0)
    stock_minimo: Optional[float] = Field(default=None, ge=0)
    observaciones: Optional[str] = None
    activo: bool = True


class InsumoFincaCreate(InsumoFincaBase):
    pass


class InsumoFincaUpdate(BaseModel):
    codigo: Optional[str] = Field(default=None, max_length=80)
    codigo_fabricante: Optional[str] = Field(default=None, max_length=120)
    nombre: Optional[str] = Field(default=None, min_length=2, max_length=180)
    tipo: Optional[str] = Field(default=None, max_length=80)
    unidad: Optional[str] = Field(default=None, max_length=60)
    costo_unitario: Optional[float] = Field(default=None, ge=0)
    precio_unitario: Optional[float] = Field(default=None, ge=0)
    impuesto_porcentaje: Optional[float] = Field(default=None, ge=0, le=100)
    stock_actual: Optional[float] = Field(default=None, ge=0)
    stock_minimo: Optional[float] = Field(default=None, ge=0)
    observaciones: Optional[str] = None
    activo: Optional[bool] = None


class InsumoFincaOut(InsumoFincaBase):
    id: int
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class CompraInsumoLineaCreate(BaseModel):
    insumo_id: Optional[int] = None
    codigo: Optional[str] = Field(default=None, max_length=80)
    codigo_fabricante: Optional[str] = Field(default=None, max_length=120)
    nombre: str = Field(min_length=2, max_length=180)
    tipo: str = Field(default="general", max_length=80)
    unidad: str = Field(default="unidad", max_length=60)
    cantidad: float = Field(gt=0)
    precio_unitario: float = Field(ge=0)
    impuesto_porcentaje: float = Field(default=0, ge=0, le=100)


class CompraInsumoLineaOut(BaseModel):
    id: int
    insumo_id: Optional[int] = None
    codigo_fabricante: Optional[str] = None
    nombre: str
    cantidad: float
    unidad: Optional[str] = None
    precio_unitario: float
    impuesto_porcentaje: float
    subtotal: float
    impuesto: float
    total: float


class CompraInsumoCreate(BaseModel):
    descuento: float = Field(default=0, ge=0, allow_inf_nan=False)
    numero_factura: Optional[str] = Field(default=None, max_length=120)
    proveedor_id: Optional[int] = None
    # Compatibilidad con clientes antiguos; nuevas UIs deben enviar proveedor_id.
    proveedor: Optional[str] = Field(default=None, max_length=180)
    fecha: date
    moneda: str = Field(default="CRC", max_length=12)
    observaciones: Optional[str] = None
    lineas: List[CompraInsumoLineaCreate] = Field(min_length=1)


class CompraInsumoOut(BaseModel):
    descuento: float = 0
    id: int
    numero_factura: Optional[str] = None
    proveedor_id: Optional[int] = None
    proveedor: Optional[str] = None
    fecha: date
    moneda: str = "CRC"
    observaciones: Optional[str] = None
    subtotal: float = 0
    impuesto: float = 0
    total: float = 0
    lineas: List[CompraInsumoLineaOut] = Field(default_factory=list)
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None


class RegistroFincaTrabajadorIn(BaseModel):
    trabajador_id: int
    horas: Optional[float] = Field(default=8, ge=0)
    jornal: Optional[float] = Field(default=None, ge=0)


class RegistroFincaTrabajadorOut(BaseModel):
    id: int
    trabajador_id: Optional[int] = None
    nombre: str
    horas: Optional[float] = None
    jornal: Optional[float] = None
    costo: Optional[float] = None


class RegistroFincaInsumoIn(BaseModel):
    insumo_id: int
    cantidad: float = Field(gt=0)
    unidad: Optional[str] = Field(default=None, max_length=60)
    costo_unitario: Optional[float] = Field(default=None, ge=0)
    comentario: Optional[str] = None


class RegistroFincaInsumoOut(BaseModel):
    id: int
    insumo_id: Optional[int] = None
    nombre: str
    cantidad: float = 0
    unidad: Optional[str] = None
    costo_unitario: Optional[float] = None
    costo_total: Optional[float] = None
    comentario: Optional[str] = None


class RegistroFincaCreate(BaseModel):
    ispublic: bool = False
    fecha: date
    finca_id: int
    actividad_id: int
    descripcion: Optional[str] = Field(default=None, max_length=255)
    estado: str = Field(default="registrado", max_length=40)
    observaciones: Optional[str] = None
    trabajadores: List[RegistroFincaTrabajadorIn] = Field(default_factory=list)
    insumos: List[RegistroFincaInsumoIn] = Field(default_factory=list)


class RegistroFincaUpdate(BaseModel):
    ispublic: bool = False
    fecha: Optional[date] = None
    finca_id: Optional[int] = None
    actividad_id: Optional[int] = None
    descripcion: Optional[str] = Field(default=None, max_length=255)
    estado: Optional[str] = Field(default=None, max_length=40)
    observaciones: Optional[str] = None
    trabajadores: Optional[List[RegistroFincaTrabajadorIn]] = None
    insumos: Optional[List[RegistroFincaInsumoIn]] = None


class RegistroFincaOut(BaseModel):
    ispublic: bool = False
    id: int
    fecha: date
    semana_inicio: date
    semana_fin: date
    semana_label: str
    finca_id: Optional[int] = None
    finca_nombre: Optional[str] = None
    cliente_nombre: Optional[str] = None
    actividad_id: Optional[int] = None
    actividad_nombre: Optional[str] = None
    actividad_tipo: Optional[str] = None
    descripcion: Optional[str] = None
    estado: str
    observaciones: Optional[str] = None
    trabajadores: List[RegistroFincaTrabajadorOut] = Field(default_factory=list)
    insumos: List[RegistroFincaInsumoOut] = Field(default_factory=list)
    total_trabajadores: int = 0
    total_horas: float = 0
    costo_mano_obra: float = 0
    costo_insumos: float = 0
    costo_total: float = 0
    created_by_id: Optional[int] = None
    created_by_nombre: Optional[str] = None
    created_at: Optional[datetime] = None
