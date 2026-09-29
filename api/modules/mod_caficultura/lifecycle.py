from __future__ import annotations

import inspect as pyinspect

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.orm import Session

from config import ADMIN_EMAIL, ADMIN_NAME, ADMIN_PASSWORD, DEFAULT_SESSION_DAYS, SEED_DEMO_DATA
from database import Base
from modules.mod_caficultura import (
    model_ai_consulting,
    model_ai_knowledge,
    model_iot,
    model_notifications,
    models,
)
from modules.mod_caficultura.models import OrdenTrabajo, ReciboCafe, SeguimientoOT
from modules.mod_caficultura.services import bootstrap



def legacy_schema_exists(db: Session) -> bool:
    """Detecta una instalación histórica previa al catálogo modular."""
    existing_tables = set(sa_inspect(db.get_bind()).get_table_names())
    return any(
        table in existing_tables
        for table in ("navia_recibos_cafe", "navia_ordenes_trabajo", "navia_clientes", "navia_fincas")
    )

def _module_tables():
    """Devuelve solo tablas declaradas por Caficultura.

    ``Base.metadata`` puede contener tablas de otros runtimes ya importados. No se
    usa ``create_all`` global para evitar que instalar Caficultura instale también
    GestiónFV u otros módulos.
    """
    tables = []
    seen: set[str] = set()
    for module in (models, model_ai_consulting, model_ai_knowledge, model_iot, model_notifications):
        for obj in vars(module).values():
            if not pyinspect.isclass(obj) or getattr(obj, "__module__", None) != module.__name__:
                continue
            table = getattr(obj, "__table__", None)
            if table is not None and table.name not in seen:
                tables.append(table)
                seen.add(table.name)
    return tables


def _run_statements(db: Session, statements: list[str]) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    for statement in statements:
        db.execute(text(statement))
    db.commit()


def install(db: Session) -> None:
    """Crea/migra únicamente el esquema propiedad de Caficultura."""
    Base.metadata.create_all(bind=db.get_bind(), tables=_module_tables())
    bootstrap(
        db,
        ADMIN_EMAIL,
        ADMIN_PASSWORD,
        ADMIN_NAME,
        DEFAULT_SESSION_DAYS,
        sync_admin_password=False,
        seed_demo_data=SEED_DEMO_DATA,
        manage_core_identity=False,
    )


def upgrade_schema(db: Session) -> None:
    install(db)

    # Migraciones/indexes históricos que pertenecen al módulo, no al core.
    statements = [
        "ALTER TABLE navia_gf_registros ADD COLUMN IF NOT EXISTS ispublic BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE navia_insumo_compras_facturas ADD COLUMN IF NOT EXISTS descuento DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE navia_clientes ADD COLUMN IF NOT EXISTS portal_token VARCHAR(255)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado_por_id INTEGER",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidado_por_nombre_snapshot VARCHAR(180)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_nota TEXT",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_numero_transferencia VARCHAR(180)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_monto NUMERIC(18, 2)",
        "ALTER TABLE navia_recibos_cafe ALTER COLUMN liquidacion_monto TYPE NUMERIC(18, 2) USING ROUND(liquidacion_monto::numeric, 2)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_storage_key VARCHAR(255)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_nombre VARCHAR(255)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_tipo VARCHAR(120)",
        "ALTER TABLE navia_recibos_cafe ADD COLUMN IF NOT EXISTS liquidacion_comprobante_tamano INTEGER",
        "ALTER TABLE navia_ai_conversations ADD COLUMN IF NOT EXISTS finca_id INTEGER",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_navia_ai_conversation_finca'
                  AND conrelid = 'navia_ai_conversations'::regclass
            ) THEN
                ALTER TABLE navia_ai_conversations
                ADD CONSTRAINT fk_navia_ai_conversation_finca
                FOREIGN KEY (finca_id) REFERENCES navia_fincas(id) ON DELETE SET NULL;
            END IF;
        END
        $$
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_navia_recibo_liquidado_por'
                  AND conrelid = 'navia_recibos_cafe'::regclass
            ) THEN
                ALTER TABLE navia_recibos_cafe
                ADD CONSTRAINT fk_navia_recibo_liquidado_por
                FOREIGN KEY (liquidado_por_id) REFERENCES navia_usuarios(id) ON DELETE SET NULL;
            END IF;
        END
        $$
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_recibo_liquidacion_monto_positivo'
                  AND conrelid = 'navia_recibos_cafe'::regclass
            ) THEN
                ALTER TABLE navia_recibos_cafe
                ADD CONSTRAINT ck_recibo_liquidacion_monto_positivo
                CHECK (liquidacion_monto IS NULL OR liquidacion_monto > 0);
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_recibo_liquidacion_completa'
                  AND conrelid = 'navia_recibos_cafe'::regclass
            ) THEN
                ALTER TABLE navia_recibos_cafe
                ADD CONSTRAINT ck_recibo_liquidacion_completa
                CHECK (
                    NOT liquidado OR (
                        liquidado_at IS NOT NULL
                        AND liquidacion_nota IS NOT NULL
                        AND liquidacion_numero_transferencia IS NOT NULL
                        AND liquidacion_monto IS NOT NULL
                        AND liquidacion_comprobante_storage_key IS NOT NULL
                    )
                );
            END IF;
        END
        $$
        """,
        "CREATE INDEX IF NOT EXISTS ix_navia_ot_estado_updated ON navia_ordenes_trabajo (estado, updated_at)",
        "CREATE INDEX IF NOT EXISTS ix_navia_seguimiento_alerta_revision ON navia_seguimientos_ot (dar_alerta, estado_revision)",
        "CREATE INDEX IF NOT EXISTS ix_navia_solicitud_venta_estado_created ON navia_solicitudes_venta (estado, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_navia_recibo_estado_fecha ON navia_recibos_cafe (estado, fecha)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_navia_recibos_numero_recibo ON navia_recibos_cafe (numero_recibo)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_navia_clientes_portal_token ON navia_clientes (portal_token) WHERE portal_token IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_navia_recibo_liquidado_fecha ON navia_recibos_cafe (liquidado, fecha)",
        "CREATE INDEX IF NOT EXISTS ix_navia_recibo_liquidado_por ON navia_recibos_cafe (liquidado_por_id)",
        "CREATE INDEX IF NOT EXISTS ix_navia_recibo_transferencia ON navia_recibos_cafe (liquidacion_numero_transferencia)",
        "ALTER TABLE navia_insumo_compras_facturas ADD COLUMN IF NOT EXISTS proveedor_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_navia_insumo_factura_proveedor ON navia_insumo_compras_facturas (proveedor_id)",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_navia_insumo_factura_proveedor'
                  AND conrelid = 'navia_insumo_compras_facturas'::regclass
            ) THEN
                ALTER TABLE navia_insumo_compras_facturas
                ADD CONSTRAINT fk_navia_insumo_factura_proveedor
                FOREIGN KEY (proveedor_id) REFERENCES navia_proveedores(id) ON DELETE SET NULL;
            END IF;
        END
        $$
        """,
        "CREATE INDEX IF NOT EXISTS ix_navia_insumo_activo_stock ON navia_gf_insumos (activo, stock_actual)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_navia_ai_single_default_agent ON navia_ai_agents (is_default) WHERE is_default = true",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_navia_ai_automation_scheduled_run ON navia_ai_automation_runs (automation_id, scheduled_for)",
        "CREATE INDEX IF NOT EXISTS ix_navia_ai_pending_expiry ON navia_ai_pending_actions (status, expires_at)",
        "CREATE INDEX IF NOT EXISTS ix_navia_ai_conversation_finca ON navia_ai_conversations (finca_id)",
        "ALTER TABLE navia_iot_nodes ADD COLUMN IF NOT EXISTS integration_id INTEGER",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_navia_iot_node_integration'
                  AND conrelid = 'navia_iot_nodes'::regclass
            ) THEN
                ALTER TABLE navia_iot_nodes
                ADD CONSTRAINT fk_navia_iot_node_integration
                FOREIGN KEY (integration_id) REFERENCES navia_iot_integrations(id) ON DELETE SET NULL;
            END IF;
        END
        $$
        """,
        "CREATE INDEX IF NOT EXISTS ix_navia_iot_node_finca_activo ON navia_iot_nodes (finca_id, activo)",
        "CREATE INDEX IF NOT EXISTS ix_navia_iot_node_integration_activo ON navia_iot_nodes (integration_id, activo)",
        "CREATE INDEX IF NOT EXISTS ix_navia_iot_integration_provider_active ON navia_iot_integrations (provider, activo)",
        "CREATE INDEX IF NOT EXISTS ix_navia_iot_reading_node_recorded ON navia_iot_readings (node_id, recorded_at)",
    ]
    _run_statements(db, statements)


def activate(db: Session) -> None:
    upgrade_schema(db)
    from modules.mod_caficultura.sequence_repair import repair_sequences
    repair_sequences(db)
    db.commit()


def deactivate(db: Session) -> None:
    del db


def uninstall(db: Session) -> None:
    # Política no destructiva: no se borran tablas ni historia al desinstalar.
    del db


def user_delete_blockers(db: Session, user_id: int) -> list[str]:
    blockers: list[str] = []
    checks = [
        (OrdenTrabajo.operario_id, "OT asignada(s) como operario"),
        (OrdenTrabajo.created_by_id, "OT creada(s)"),
        (OrdenTrabajo.gerente_cierra_id, "OT cerrada(s) por este usuario"),
        (ReciboCafe.created_by_id, "recibo(s) creado(s)"),
        (SeguimientoOT.created_by_id, "seguimiento(s) creado(s)"),
        (SeguimientoOT.revisado_por_id, "seguimiento(s) revisado(s)"),
    ]
    for column, label in checks:
        try:
            count = db.query(column.class_).filter(column == user_id).count()
        except Exception:
            db.rollback()
            count = 0
        if count:
            blockers.append(f"{count} {label}")
    return blockers
