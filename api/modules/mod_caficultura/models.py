from __future__ import annotations

from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


from core.models import Usuario

class ReciboConsecutivo(Base):
    __tablename__ = "navia_recibo_consecutivo"
    __table_args__ = (
        CheckConstraint("siguiente_numero > 0", name="ck_recibo_consecutivo_positivo"),
        CheckConstraint("ancho BETWEEN 1 AND 12", name="ck_recibo_consecutivo_ancho"),
    )

    # Una sola fila funciona como cerrojo transaccional en PostgreSQL.
    id = Column(Integer, primary_key=True, default=1)
    prefijo = Column(String(20), nullable=False, default="RC-", server_default="RC-")
    siguiente_numero = Column(Integer, nullable=False, default=1, server_default="1")
    ancho = Column(Integer, nullable=False, default=5, server_default="5")
    updated_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Cliente(Base):
    __tablename__ = "navia_clientes"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(80), nullable=False, unique=True, index=True)
    nombre_completo = Column(String(220), nullable=False, index=True)
    tipo_persona = Column(String(30), nullable=False, default="personal", index=True)  # personal, juridica
    categoria = Column(String(40), nullable=False, default="personal", index=True)  # personal, agro, comercial, industria
    numero_identificacion = Column(String(80), nullable=False, unique=True, index=True)
    direccion = Column(String(500), nullable=True)
    provincia = Column(String(80), nullable=True)
    canton = Column(String(80), nullable=True)
    distrito = Column(String(80), nullable=True)
    telefono = Column(String(80), nullable=True)
    correo = Column(String(180), nullable=True, index=True)
    portal_token = Column(String(255), nullable=True, unique=True, index=True)
    activo = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    fincas = relationship("Finca", back_populates="cliente", cascade="all, delete-orphan")
    recibos = relationship("ReciboCafe", back_populates="cliente")
    cotizaciones_venta = relationship("CotizacionVenta", back_populates="cliente")
    solicitudes_salida_venta = relationship("SolicitudSalidaVenta", back_populates="cliente")
    portal_sessions = relationship("ClientePortalSession", back_populates="cliente", cascade="all, delete-orphan")


class ClientePortalSession(Base):
    __tablename__ = "navia_cliente_portal_sessions"

    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("navia_clientes.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    requested_ip = Column(String(80), nullable=True)

    cliente = relationship("Cliente", back_populates="portal_sessions")



class Finca(Base):
    __tablename__ = "navia_fincas"

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("navia_clientes.id", ondelete="CASCADE"), nullable=False, index=True)
    codigo = Column(String(80), nullable=False, unique=True, index=True)
    nombre = Column(String(180), nullable=False, index=True)

    ubicacion = Column(String(500), nullable=True)
    google_maps_url = Column(String(1000), nullable=True)
    provincia = Column(String(80), nullable=True)
    canton = Column(String(80), nullable=True)
    distrito = Column(String(80), nullable=True)

    area = Column(Float, nullable=True)  # hectáreas
    cultivo = Column(String(120), nullable=True, default="Café")
    caracteristicas = Column(Text, nullable=True)

    # Compatibilidad con datos anteriores
    propietario = Column(String(180), nullable=True)
    altitud = Column(Integer, nullable=True)
    variedades = Column(JSON, nullable=False, default=list)

    activa = Column(Boolean, nullable=False, default=True)
    gestion_fincas_habilitada = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    cliente = relationship("Cliente", back_populates="fincas")
    recibos = relationship("ReciboCafe", back_populates="finca")
    ots = relationship("OrdenTrabajo", back_populates="finca")


class NotaRapida(Base):
    __tablename__ = "navia_notas_rapidas"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_nota_rapida_public_id"),
        UniqueConstraint("client_uuid", name="uq_nota_rapida_client_uuid"),
        CheckConstraint("version > 0", name="ck_nota_rapida_version_positiva"),
    )

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(40), nullable=False, default=lambda: uuid.uuid4().hex, index=True)
    client_uuid = Column(String(120), nullable=True, index=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True, index=True)
    finca_id = Column(Integer, ForeignKey("navia_fincas.id", ondelete="SET NULL"), nullable=True, index=True)

    titulo = Column(String(180), nullable=True)
    contenido = Column(Text, nullable=False)
    categoria = Column(String(40), nullable=False, default="general", server_default="general", index=True)
    estado = Column(String(40), nullable=False, default="pendiente", server_default="pendiente", index=True)
    prioridad = Column(String(20), nullable=False, default="normal", server_default="normal", index=True)
    visibilidad = Column(String(20), nullable=False, default="personal", server_default="personal", index=True)
    origen = Column(String(30), nullable=False, default="manual", server_default="manual", index=True)
    fijada = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    etiquetas = Column(JSON, nullable=False, default=list)
    contexto = Column(JSON, nullable=False, default=dict)

    linked_entity_type = Column(String(60), nullable=True, index=True)
    linked_entity_id = Column(Integer, nullable=True, index=True)
    procesada_at = Column(DateTime(timezone=True), nullable=True, index=True)
    procesada_por_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    finca = relationship("Finca")
    created_by = relationship("Usuario", foreign_keys=[created_by_id])
    procesada_por = relationship("Usuario", foreign_keys=[procesada_por_id])


class ReciboCafe(Base):
    __tablename__ = "navia_recibos_cafe"
    __table_args__ = (
        UniqueConstraint("client_uuid", name="uq_recibo_client_uuid"),
        CheckConstraint(
            "liquidacion_monto IS NULL OR liquidacion_monto > 0",
            name="ck_recibo_liquidacion_monto_positivo",
        ),
        CheckConstraint(
            "NOT liquidado OR (liquidado_at IS NOT NULL "
            "AND liquidacion_nota IS NOT NULL "
            "AND liquidacion_numero_transferencia IS NOT NULL "
            "AND liquidacion_monto IS NOT NULL "
            "AND liquidacion_comprobante_storage_key IS NOT NULL)",
            name="ck_recibo_liquidacion_completa",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    numero_recibo = Column(String(80), nullable=False, unique=True, index=True)
    client_uuid = Column(String(120), nullable=True, index=True)
    fecha = Column(Date, nullable=False, index=True)
    cosecha = Column(String(30), nullable=True, index=True)
    cliente_id = Column(Integer, ForeignKey("navia_clientes.id", ondelete="SET NULL"), nullable=True, index=True)
    finca_id = Column(Integer, ForeignKey("navia_fincas.id", ondelete="SET NULL"), nullable=True, index=True)
    productor_nombre = Column(String(180), nullable=False)
    productor_cedula = Column(String(80), nullable=True)
    provincia = Column(String(80), nullable=True)
    canton = Column(String(80), nullable=True)
    distrito = Column(String(80), nullable=True)
    zona = Column(String(40), nullable=True)
    cajuelas = Column(Float, nullable=False, default=0)
    cuartillos = Column(Float, nullable=False, default=0)
    porcentaje_flote = Column(Float, nullable=True)
    porcentaje_verde = Column(Float, nullable=True)
    peso_promedio_cajuela = Column(Float, nullable=True)
    # Campo legado: se conserva para no romper bases existentes; ahora corresponde a peso_promedio_cajuela.
    precio_promedio_cajuela = Column(Float, nullable=True)
    precio_fanega = Column(Float, nullable=True)
    precio_fanega_letras = Column(String(255), nullable=True)
    precio_adelanto_letras = Column(String(255), nullable=True)
    beneficio_recibe_usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True, index=True)
    beneficio_recibe = Column(String(180), nullable=True)
    beneficio_firma_url = Column(String(1000), nullable=True)
    productor_entrega = Column(String(180), nullable=True)
    estado = Column(String(40), nullable=False, default="recibido", index=True)  # recibido, en_lote, anulado
    observaciones = Column(Text, nullable=True)
    qr_payload = Column(Text, nullable=True)
    liquidado = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    liquidado_at = Column(DateTime(timezone=True), nullable=True, index=True)
    liquidado_por_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True, index=True)
    liquidado_por_nombre_snapshot = Column(String(180), nullable=True)
    liquidacion_nota = Column(Text, nullable=True)
    liquidacion_numero_transferencia = Column(String(180), nullable=True, index=True)
    liquidacion_monto = Column(Numeric(18, 2), nullable=True)
    liquidacion_comprobante_storage_key = Column(String(255), nullable=True)
    liquidacion_comprobante_nombre = Column(String(255), nullable=True)
    liquidacion_comprobante_tipo = Column(String(120), nullable=True)
    liquidacion_comprobante_tamano = Column(Integer, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    cliente = relationship("Cliente", back_populates="recibos")
    finca = relationship("Finca", back_populates="recibos")
    created_by = relationship("Usuario", foreign_keys=[created_by_id])
    liquidado_por = relationship("Usuario", foreign_keys=[liquidado_por_id])
    beneficio_usuario = relationship("Usuario", foreign_keys=[beneficio_recibe_usuario_id])
    recibos_ot = relationship("OrdenTrabajoRecibo", back_populates="recibo", cascade="all, delete-orphan")


class OrdenTrabajo(Base):
    __tablename__ = "navia_ordenes_trabajo"
    __table_args__ = (
        UniqueConstraint("client_uuid", name="uq_ot_client_uuid"),
    )

    id = Column(Integer, primary_key=True, index=True)
    codigo_lote = Column(String(80), nullable=False, unique=True, index=True)
    client_uuid = Column(String(120), nullable=True, index=True)
    proceso = Column(String(40), nullable=False, default="miel", index=True)  # miel, natural, semilavado
    estado = Column(String(40), nullable=False, default="abierta", index=True)  # abierta, en_proceso, pendiente_aprobacion, finalizada, anulada
    fecha_inicio = Column(Date, nullable=False, index=True)
    finca_id = Column(Integer, ForeignKey("navia_fincas.id", ondelete="SET NULL"), nullable=True, index=True)
    operario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True, index=True)
    fanegas_estimadas = Column(Float, nullable=False, default=0)
    objetivo_cajuelas = Column(Float, nullable=True)
    objetivo_fanegas = Column(Float, nullable=True)

    qr_token = Column(String(160), nullable=True, unique=True, index=True)
    qr_public_url = Column(String(1000), nullable=True)
    qr_payload = Column(Text, nullable=True)
    observaciones = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    gerente_cierra_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    fecha_cierre = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    finca = relationship("Finca", back_populates="ots")
    operario = relationship("Usuario", foreign_keys=[operario_id])
    created_by = relationship("Usuario", foreign_keys=[created_by_id])
    gerente_cierra = relationship("Usuario", foreign_keys=[gerente_cierra_id])
    recibos_rel = relationship("OrdenTrabajoRecibo", back_populates="ot", cascade="all, delete-orphan", overlaps="recibos")
    recibos = relationship("OrdenTrabajoRecibo", viewonly=True, overlaps="recibos_rel,ot")
    seguimientos = relationship("SeguimientoOT", back_populates="orden_trabajo", cascade="all, delete-orphan")
    documentos = relationship("DocumentoLote", back_populates="ot", cascade="all, delete-orphan")
    comentarios = relationship("ComentarioLote", back_populates="ot", cascade="all, delete-orphan")
    ventas_leads = relationship("VentaLeadLote", back_populates="ot", cascade="all, delete-orphan")
    solicitudes_venta = relationship("SolicitudVenta", back_populates="ot")
    solicitudes_venta_liquidaciones = relationship("SolicitudVentaLineaLiquidacion", back_populates="ot")
    cotizaciones_venta = relationship("CotizacionVentaLinea", back_populates="ot")
    solicitudes_salida_venta = relationship("SolicitudSalidaVentaLinea", back_populates="ot")
    origenes_unidos = relationship("OrdenTrabajoUnionOrigen", foreign_keys="OrdenTrabajoUnionOrigen.ot_destino_id", back_populates="ot_destino", cascade="all, delete-orphan")
    destinos_union = relationship("OrdenTrabajoUnionOrigen", foreign_keys="OrdenTrabajoUnionOrigen.ot_origen_id", back_populates="ot_origen")


class OrdenTrabajoUnionOrigen(Base):
    __tablename__ = "navia_ot_uniones_origenes"

    id = Column(Integer, primary_key=True, index=True)
    ot_destino_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="CASCADE"), nullable=False, index=True)
    ot_origen_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="SET NULL"), nullable=True, index=True)
    codigo_lote_origen = Column(String(80), nullable=False, index=True)
    estado_origen = Column(String(40), nullable=True)
    proceso_origen = Column(String(40), nullable=True)
    fanegas_origen = Column(Float, nullable=True)
    motivo = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ot_destino = relationship("OrdenTrabajo", foreign_keys=[ot_destino_id], back_populates="origenes_unidos")
    ot_origen = relationship("OrdenTrabajo", foreign_keys=[ot_origen_id], back_populates="destinos_union")
    created_by = relationship("Usuario")


class OrdenTrabajoRecibo(Base):
    __tablename__ = "navia_ot_recibos"

    id = Column(Integer, primary_key=True, index=True)

    ot_id = Column(
        Integer,
        ForeignKey("navia_ordenes_trabajo.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    recibo_id = Column(
        Integer,
        ForeignKey("navia_recibos_cafe.id", ondelete="RESTRICT"),
        nullable=False,
        index=True
    )

    cajuelas_asignadas = Column(Float, nullable=False, default=0)
    cuartillos_asignados = Column(Float, nullable=False, default=0)
    fanegas_asignadas = Column(Float, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ot = relationship("OrdenTrabajo", back_populates="recibos_rel", overlaps="recibos")
    recibo = relationship("ReciboCafe")


class ActividadFinca(Base):
    __tablename__ = "navia_gf_actividades"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(180), nullable=False, unique=True, index=True)
    tipo = Column(String(60), nullable=False, default="mantenimiento", index=True)
    ambito = Column(String(30), nullable=False, default="finca", server_default="finca", index=True)  # finca, beneficio
    descripcion = Column(Text, nullable=True)
    requiere_insumo = Column(Boolean, nullable=False, default=False)
    unidad_referencia = Column(String(60), nullable=True)
    activa = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    registros = relationship("RegistroFinca", back_populates="actividad")


class TrabajadorFinca(Base):
    __tablename__ = "navia_gf_trabajadores"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(80), nullable=True, unique=True, index=True)
    nombre = Column(String(180), nullable=False, index=True)
    identificacion = Column(String(80), nullable=True, index=True)
    telefono = Column(String(80), nullable=True)
    puesto = Column(String(120), nullable=True)
    jornal_diario = Column(Float, nullable=True)
    activo = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    registros = relationship("RegistroFincaTrabajador", back_populates="trabajador")




class Proveedor(Base):
    __tablename__ = "navia_proveedores"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(80), nullable=True, unique=True, index=True)
    nombre_legal = Column(String(220), nullable=False, index=True)
    nombre_comercial = Column(String(220), nullable=True, index=True)
    identificacion = Column(String(100), nullable=True, unique=True, index=True)
    tipo_identificacion = Column(String(40), nullable=True)
    correo = Column(String(180), nullable=True, index=True)
    telefono = Column(String(80), nullable=True)
    sitio_web = Column(String(500), nullable=True)
    direccion = Column(String(500), nullable=True)
    provincia = Column(String(80), nullable=True)
    canton = Column(String(80), nullable=True)
    distrito = Column(String(80), nullable=True)
    notas = Column(Text, nullable=True)
    activo = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    contactos = relationship("ProveedorContacto", back_populates="proveedor", cascade="all, delete-orphan", order_by="ProveedorContacto.principal.desc(), ProveedorContacto.nombre.asc()")
    facturas = relationship("CompraInsumoFactura", back_populates="proveedor_rel")


class ProveedorContacto(Base):
    __tablename__ = "navia_proveedor_contactos"

    id = Column(Integer, primary_key=True, index=True)
    proveedor_id = Column(Integer, ForeignKey("navia_proveedores.id", ondelete="CASCADE"), nullable=False, index=True)
    nombre = Column(String(180), nullable=False)
    puesto = Column(String(140), nullable=True)
    correo = Column(String(180), nullable=True)
    telefono = Column(String(80), nullable=True)
    principal = Column(Boolean, nullable=False, default=False, server_default="false")
    activo = Column(Boolean, nullable=False, default=True, server_default="true")
    notas = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    proveedor = relationship("Proveedor", back_populates="contactos")


class InsumoFinca(Base):
    __tablename__ = "navia_gf_insumos"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(80), nullable=True, unique=True, index=True)
    nombre = Column(String(180), nullable=False, unique=True, index=True)
    tipo = Column(String(80), nullable=False, default="general", index=True)
    unidad = Column(String(60), nullable=False, default="unidad")
    codigo_fabricante = Column(String(120), nullable=True, index=True)
    costo_unitario = Column(Float, nullable=True)
    precio_unitario = Column(Float, nullable=True)
    impuesto_porcentaje = Column(Float, nullable=False, default=0, server_default="0")
    stock_actual = Column(Float, nullable=False, default=0, server_default="0")
    stock_minimo = Column(Float, nullable=True)
    observaciones = Column(Text, nullable=True)
    activo = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    usos = relationship("RegistroFincaInsumo", back_populates="insumo")
    compras_lineas = relationship("CompraInsumoLinea", back_populates="insumo")


class CompraInsumoFactura(Base):
    __tablename__ = "navia_insumo_compras_facturas"

    id = Column(Integer, primary_key=True, index=True)
    numero_factura = Column(String(120), nullable=True, index=True)
    proveedor_id = Column(Integer, ForeignKey("navia_proveedores.id", ondelete="SET NULL"), nullable=True, index=True)
    # Snapshot histórico para conservar el nombre mostrado aunque cambie el proveedor.
    proveedor = Column(String(180), nullable=True, index=True)
    fecha = Column(Date, nullable=False, index=True)
    moneda = Column(String(12), nullable=False, default="CRC")
    observaciones = Column(Text, nullable=True)
    descuento = Column(Float, nullable=False, default=0, server_default="0")
    subtotal = Column(Float, nullable=False, default=0, server_default="0")
    impuesto = Column(Float, nullable=False, default=0, server_default="0")
    total = Column(Float, nullable=False, default=0, server_default="0")
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    created_by = relationship("Usuario")
    proveedor_rel = relationship("Proveedor", back_populates="facturas")
    lineas = relationship("CompraInsumoLinea", back_populates="factura", cascade="all, delete-orphan")


class CompraInsumoLinea(Base):
    __tablename__ = "navia_insumo_compras_lineas"

    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(Integer, ForeignKey("navia_insumo_compras_facturas.id", ondelete="CASCADE"), nullable=False, index=True)
    insumo_id = Column(Integer, ForeignKey("navia_gf_insumos.id", ondelete="SET NULL"), nullable=True, index=True)
    codigo_fabricante = Column(String(120), nullable=True)
    nombre_snapshot = Column(String(180), nullable=False)
    cantidad = Column(Float, nullable=False, default=0)
    unidad = Column(String(60), nullable=True)
    precio_unitario = Column(Float, nullable=False, default=0)
    impuesto_porcentaje = Column(Float, nullable=False, default=0)
    subtotal = Column(Float, nullable=False, default=0)
    impuesto = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    factura = relationship("CompraInsumoFactura", back_populates="lineas")
    insumo = relationship("InsumoFinca", back_populates="compras_lineas")


class RegistroFinca(Base):
    __tablename__ = "navia_gf_registros"

    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(Date, nullable=False, index=True)
    ispublic = Column(Boolean, nullable=False, default=False, server_default="false")
    semana_inicio = Column(Date, nullable=False, index=True)
    semana_fin = Column(Date, nullable=False, index=True)
    finca_id = Column(Integer, ForeignKey("navia_fincas.id", ondelete="SET NULL"), nullable=True, index=True)
    actividad_id = Column(Integer, ForeignKey("navia_gf_actividades.id", ondelete="SET NULL"), nullable=True, index=True)
    descripcion = Column(String(255), nullable=True)
    estado = Column(String(40), nullable=False, default="registrado", index=True)
    observaciones = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    finca = relationship("Finca")
    actividad = relationship("ActividadFinca", back_populates="registros")
    created_by = relationship("Usuario")
    trabajadores = relationship("RegistroFincaTrabajador", back_populates="registro", cascade="all, delete-orphan")
    insumos = relationship("RegistroFincaInsumo", back_populates="registro", cascade="all, delete-orphan")


class RegistroFincaTrabajador(Base):
    __tablename__ = "navia_gf_registro_trabajadores"

    id = Column(Integer, primary_key=True, index=True)
    registro_id = Column(Integer, ForeignKey("navia_gf_registros.id", ondelete="CASCADE"), nullable=False, index=True)
    trabajador_id = Column(Integer, ForeignKey("navia_gf_trabajadores.id", ondelete="SET NULL"), nullable=True, index=True)
    nombre_snapshot = Column(String(180), nullable=False)
    horas = Column(Float, nullable=True)
    jornal = Column(Float, nullable=True)
    costo = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    registro = relationship("RegistroFinca", back_populates="trabajadores")
    trabajador = relationship("TrabajadorFinca", back_populates="registros")


class RegistroFincaInsumo(Base):
    __tablename__ = "navia_gf_registro_insumos"

    id = Column(Integer, primary_key=True, index=True)
    registro_id = Column(Integer, ForeignKey("navia_gf_registros.id", ondelete="CASCADE"), nullable=False, index=True)
    insumo_id = Column(Integer, ForeignKey("navia_gf_insumos.id", ondelete="SET NULL"), nullable=True, index=True)
    nombre_snapshot = Column(String(180), nullable=False)
    cantidad = Column(Float, nullable=False, default=0)
    unidad = Column(String(60), nullable=True)
    costo_unitario = Column(Float, nullable=True)
    costo_total = Column(Float, nullable=True)
    comentario = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    registro = relationship("RegistroFinca", back_populates="insumos")
    insumo = relationship("InsumoFinca", back_populates="usos")


class SeguimientoOT(Base):
    __tablename__ = "navia_seguimientos_ot"

    id = Column(Integer, primary_key=True, index=True)

    orden_trabajo_id = Column(
        Integer,
        ForeignKey("navia_ordenes_trabajo.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    client_uuid = Column(String(120), nullable=True, unique=True, index=True)
    fecha = Column(Date, nullable=False)
    marca_tiempo = Column(DateTime(timezone=True), nullable=True, index=True)
    actividad_realizada = Column(String(255), nullable=False)
    horas_implementadas = Column(Float, nullable=True)
    temperatura = Column(Float, nullable=True)
    humedad = Column(Float, nullable=True)
    comentario = Column(Text, nullable=True)
    estado_lote_resultante = Column(String(40), nullable=True, index=True)
    dar_alerta = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    alerta_mensaje = Column(Text, nullable=True)
    alerta_canal = Column(String(40), nullable=True)
    alerta_whatsapp_url = Column(String(1000), nullable=True)
    alerta_correo_url = Column(String(1000), nullable=True)

    estado_revision = Column(String(30), nullable=False, default="pendiente")
    comentario_revision = Column(Text, nullable=True)
    revisado_por_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    revisado_at = Column(DateTime(timezone=True), nullable=True)

    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    orden_trabajo = relationship("OrdenTrabajo", back_populates="seguimientos")
    created_by = relationship("Usuario", foreign_keys=[created_by_id])
    revisado_por = relationship("Usuario", foreign_keys=[revisado_por_id])

class DocumentoLote(Base):
    __tablename__ = "navia_lote_documentos"

    id = Column(Integer, primary_key=True, index=True)
    ot_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="CASCADE"), nullable=False, index=True)
    titulo = Column(String(180), nullable=False)
    tipo = Column(String(60), nullable=False, default="documento", index=True)
    descripcion = Column(Text, nullable=True)
    file_url = Column(String(1000), nullable=False)
    file_name = Column(String(255), nullable=False)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ot = relationship("OrdenTrabajo", back_populates="documentos")
    created_by = relationship("Usuario")


class ComentarioLote(Base):
    __tablename__ = "navia_lote_comentarios"

    id = Column(Integer, primary_key=True, index=True)
    ot_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="CASCADE"), nullable=False, index=True)
    tipo = Column(String(40), nullable=False, default="nota", index=True)  # nota, garantia, supervision, comercial
    comentario = Column(Text, nullable=False)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ot = relationship("OrdenTrabajo", back_populates="comentarios")
    created_by = relationship("Usuario")


class CotizacionVenta(Base):
    __tablename__ = "navia_cotizaciones_venta"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(80), nullable=False, unique=True, index=True)
    cliente_id = Column(Integer, ForeignKey("navia_clientes.id", ondelete="SET NULL"), nullable=True, index=True)
    estado = Column(String(40), nullable=False, default="borrador", index=True)  # borrador, enviada, aceptada, cerrada, cancelada
    fecha = Column(Date, nullable=False, index=True)
    validez_dias = Column(Integer, nullable=False, default=8)
    moneda = Column(String(12), nullable=False, default="USD")
    condiciones = Column(Text, nullable=True)
    observaciones = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    cliente = relationship("Cliente", back_populates="cotizaciones_venta")
    created_by = relationship("Usuario")
    lineas = relationship("CotizacionVentaLinea", back_populates="cotizacion", cascade="all, delete-orphan")


class CotizacionVentaLinea(Base):
    __tablename__ = "navia_cotizaciones_venta_lineas"

    id = Column(Integer, primary_key=True, index=True)
    cotizacion_id = Column(Integer, ForeignKey("navia_cotizaciones_venta.id", ondelete="CASCADE"), nullable=False, index=True)
    ot_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="SET NULL"), nullable=True, index=True)
    descripcion = Column(String(500), nullable=False)
    cantidad_quintales = Column(Float, nullable=False, default=0)
    precio_unitario = Column(Float, nullable=False, default=0)
    impuesto_porcentaje = Column(Float, nullable=False, default=0)
    subtotal = Column(Float, nullable=False, default=0)
    impuesto = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    cotizacion = relationship("CotizacionVenta", back_populates="lineas")
    ot = relationship("OrdenTrabajo", back_populates="cotizaciones_venta")


class SolicitudSalidaVenta(Base):
    __tablename__ = "navia_solicitudes_salida_venta"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(80), nullable=False, unique=True, index=True)
    cotizacion_id = Column(Integer, ForeignKey("navia_cotizaciones_venta.id", ondelete="SET NULL"), nullable=True, index=True)
    cliente_id = Column(Integer, ForeignKey("navia_clientes.id", ondelete="SET NULL"), nullable=True, index=True)
    estado = Column(String(40), nullable=False, default="vendida", index=True)  # vendida, anulada
    fecha = Column(Date, nullable=False, index=True)
    moneda = Column(String(12), nullable=False, default="USD")
    tipo_envio = Column(String(80), nullable=True)
    direccion_entrega = Column(String(500), nullable=True)
    observaciones = Column(Text, nullable=True)
    subtotal = Column(Float, nullable=False, default=0)
    impuesto = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    cotizacion = relationship("CotizacionVenta")
    cliente = relationship("Cliente", back_populates="solicitudes_salida_venta")
    created_by = relationship("Usuario")
    lineas = relationship("SolicitudSalidaVentaLinea", back_populates="solicitud", cascade="all, delete-orphan")


class SolicitudSalidaVentaLinea(Base):
    __tablename__ = "navia_solicitudes_salida_venta_lineas"

    id = Column(Integer, primary_key=True, index=True)
    solicitud_id = Column(Integer, ForeignKey("navia_solicitudes_salida_venta.id", ondelete="CASCADE"), nullable=False, index=True)
    ot_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="SET NULL"), nullable=True, index=True)
    descripcion = Column(String(500), nullable=False)
    cantidad_quintales = Column(Float, nullable=False, default=0)
    precio_unitario = Column(Float, nullable=False, default=0)
    impuesto_porcentaje = Column(Float, nullable=False, default=0)
    subtotal = Column(Float, nullable=False, default=0)
    impuesto = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    solicitud = relationship("SolicitudSalidaVenta", back_populates="lineas")
    ot = relationship("OrdenTrabajo", back_populates="solicitudes_salida_venta")


class VentaLeadLote(Base):
    __tablename__ = "navia_lote_ventas_leads"

    id = Column(Integer, primary_key=True, index=True)
    ot_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="CASCADE"), nullable=False, index=True)
    cliente_id = Column(Integer, ForeignKey("navia_clientes.id", ondelete="SET NULL"), nullable=True, index=True)
    estado = Column(String(40), nullable=False, default="lead", index=True)  # lead, seguimiento, solicitud_salida, vendido, cancelado
    cantidad_quintales = Column(Float, nullable=True)
    precio_unitario = Column(Float, nullable=True)
    moneda = Column(String(12), nullable=False, default="USD")
    notas = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    ot = relationship("OrdenTrabajo", back_populates="ventas_leads")
    cliente = relationship("Cliente")
    created_by = relationship("Usuario")
    seguimientos = relationship("VentaSeguimiento", back_populates="lead", cascade="all, delete-orphan")


class VentaSeguimiento(Base):
    __tablename__ = "navia_lote_ventas_seguimientos"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("navia_lote_ventas_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    fecha = Column(Date, nullable=False, index=True)
    canal = Column(String(40), nullable=False, default="nota", index=True)  # llamada, correo, whatsapp, reunion, nota
    asunto = Column(String(180), nullable=True)
    comentario = Column(Text, nullable=False)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    lead = relationship("VentaLeadLote", back_populates="seguimientos")
    created_by = relationship("Usuario")


class SolicitudVenta(Base):
    __tablename__ = "navia_solicitudes_venta"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(80), nullable=False, unique=True, index=True)
    cliente_id = Column(Integer, ForeignKey("navia_clientes.id", ondelete="SET NULL"), nullable=True, index=True)
    ot_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="SET NULL"), nullable=True, index=True)
    estado = Column(String(40), nullable=False, default="pendiente", index=True)  # pendiente, asignada, salida_solicitada, vendida, cancelada
    cantidad_quintales = Column(Float, nullable=False, default=0)
    proceso_preferido = Column(String(40), nullable=True, index=True)
    precio_objetivo = Column(Float, nullable=True)
    moneda = Column(String(12), nullable=False, default="USD")
    observaciones = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    cliente = relationship("Cliente")
    ot = relationship("OrdenTrabajo", back_populates="solicitudes_venta")
    created_by = relationship("Usuario")
    lineas = relationship("SolicitudVentaLinea", back_populates="solicitud", cascade="all, delete-orphan")
    seguimientos = relationship("SolicitudVentaSeguimiento", back_populates="solicitud", cascade="all, delete-orphan")
    documentos = relationship("SolicitudVentaDocumento", back_populates="solicitud", cascade="all, delete-orphan")


class SolicitudVentaLinea(Base):
    __tablename__ = "navia_solicitudes_venta_lineas"

    id = Column(Integer, primary_key=True, index=True)
    solicitud_id = Column(Integer, ForeignKey("navia_solicitudes_venta.id", ondelete="CASCADE"), nullable=False, index=True)
    descripcion = Column(String(500), nullable=False)
    proceso_preferido = Column(String(40), nullable=True, index=True)
    cantidad_quintales = Column(Float, nullable=False, default=0)
    precio_objetivo = Column(Float, nullable=True)
    moneda = Column(String(12), nullable=False, default="USD")
    observaciones = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    solicitud = relationship("SolicitudVenta", back_populates="lineas")
    liquidaciones = relationship("SolicitudVentaLineaLiquidacion", back_populates="linea", cascade="all, delete-orphan")


class SolicitudVentaLineaLiquidacion(Base):
    __tablename__ = "navia_solicitudes_venta_linea_liquidaciones"

    id = Column(Integer, primary_key=True, index=True)
    solicitud_id = Column(Integer, ForeignKey("navia_solicitudes_venta.id", ondelete="CASCADE"), nullable=False, index=True)
    linea_id = Column(Integer, ForeignKey("navia_solicitudes_venta_lineas.id", ondelete="CASCADE"), nullable=False, index=True)
    salida_id = Column(Integer, ForeignKey("navia_solicitudes_salida_venta.id", ondelete="CASCADE"), nullable=False, index=True)
    salida_linea_id = Column(Integer, ForeignKey("navia_solicitudes_salida_venta_lineas.id", ondelete="CASCADE"), nullable=True, index=True)
    ot_id = Column(Integer, ForeignKey("navia_ordenes_trabajo.id", ondelete="SET NULL"), nullable=True, index=True)
    cantidad_quintales = Column(Float, nullable=False, default=0)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    solicitud = relationship("SolicitudVenta")
    linea = relationship("SolicitudVentaLinea", back_populates="liquidaciones")
    salida = relationship("SolicitudSalidaVenta")
    salida_linea = relationship("SolicitudSalidaVentaLinea")
    ot = relationship("OrdenTrabajo", back_populates="solicitudes_venta_liquidaciones")
    created_by = relationship("Usuario")


class SolicitudVentaSeguimiento(Base):
    __tablename__ = "navia_solicitudes_venta_seguimientos"

    id = Column(Integer, primary_key=True, index=True)
    solicitud_id = Column(Integer, ForeignKey("navia_solicitudes_venta.id", ondelete="CASCADE"), nullable=False, index=True)
    fecha = Column(Date, nullable=False, index=True)
    canal = Column(String(40), nullable=False, default="nota", index=True)
    asunto = Column(String(180), nullable=True)
    comentario = Column(Text, nullable=False)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    solicitud = relationship("SolicitudVenta", back_populates="seguimientos")
    created_by = relationship("Usuario")


class SolicitudVentaDocumento(Base):
    __tablename__ = "navia_solicitudes_venta_documentos"

    id = Column(Integer, primary_key=True, index=True)
    solicitud_id = Column(Integer, ForeignKey("navia_solicitudes_venta.id", ondelete="CASCADE"), nullable=False, index=True)
    titulo = Column(String(180), nullable=False)
    tipo = Column(String(60), nullable=False, default="documento", index=True)
    descripcion = Column(Text, nullable=True)
    file_url = Column(String(1000), nullable=False)
    file_name = Column(String(255), nullable=False)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    solicitud = relationship("SolicitudVenta", back_populates="documentos")
    created_by = relationship("Usuario")

# Extension models are part of the Caficultura module as well. Importing this
# module registers the complete module schema in SQLAlchemy metadata.
from .model_notifications import (  # noqa: E402,F401
    NotificationDelivery,
    NotificationEvent,
    NotificationPreference,
    NotificationRun,
    ScheduledNotification,
)
from .model_ai_consulting import (  # noqa: E402,F401
    AIAgent,
    AIAttachment,
    AIAuditLog,
    AIAutomation,
    AIAutomationRun,
    AIConversation,
    AIMessage,
    AIPendingAction,
    AITelegramConnection,
    AITelegramPairing,
)
from .model_iot import IoTDashboardPreset, IoTNode, IoTReading  # noqa: E402,F401
from .model_ai_knowledge import AIKnowledgeChunk, AIKnowledgeDocument  # noqa: E402,F401
