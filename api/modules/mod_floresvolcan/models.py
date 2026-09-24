from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)




class FlowerCrop(Base):
    """Cultivo configurable dentro de FloresVolcan. Lirio es el primer perfil precargado."""
    __tablename__ = "fv_flower_crops"
    id = Column(Integer, primary_key=True)
    code = Column(String(40), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    material_type = Column(String(60), nullable=False, default="bulbo")
    active = Column(Boolean, nullable=False, default=True, index=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class CropCatalogValue(Base):
    """Catálogos editables por cultivo: variedad, color, calibre, densidad, etc."""
    __tablename__ = "fv_crop_catalog_values"
    id = Column(Integer, primary_key=True)
    crop_id = Column(Integer, ForeignKey("fv_flower_crops.id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String(60), nullable=False, index=True)
    code = Column(String(120), nullable=False)
    label = Column(String(220), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True, index=True)
    source = Column(String(40), nullable=False, default="manual")
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("crop_id", "category", "code", name="uq_fv_crop_catalog_code"),)


class ModuleSetting(Base):
    __tablename__ = "fv_module_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String(100), nullable=False, unique=True, index=True)
    value_text = Column(Text, nullable=True)
    is_secret = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class BackupRun(Base):
    __tablename__ = "fv_backup_runs"
    id = Column(Integer, primary_key=True)
    status = Column(String(30), nullable=False, default="running", index=True)
    destination = Column(String(30), nullable=False, default="local")
    filename = Column(String(255), nullable=True)
    storage_key = Column(String(700), nullable=True)
    drive_file_id = Column(String(180), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class Supplier(Base):
    __tablename__ = "fv_suppliers"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(180), nullable=False, index=True)
    country = Column(String(100), nullable=False)
    department = Column(String(120), nullable=True)
    address = Column(Text, nullable=True)
    phone = Column(String(80), nullable=True)
    email = Column(String(180), nullable=True)
    website = Column(String(500), nullable=True)
    active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    contacts = relationship("SupplierContact", back_populates="supplier", cascade="all, delete-orphan")


class SupplierContact(Base):
    __tablename__ = "fv_supplier_contacts"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, ForeignKey("fv_suppliers.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(180), nullable=False)
    email = Column(String(180), nullable=True)
    phone = Column(String(80), nullable=True)
    position = Column(String(120), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    supplier = relationship("Supplier", back_populates="contacts")


class SapProduct(Base):
    __tablename__ = "fv_sap_products"
    id = Column(Integer, primary_key=True)
    crop_id = Column(Integer, ForeignKey("fv_flower_crops.id", ondelete="SET NULL"), nullable=True, index=True)
    code = Column(String(80), nullable=False, unique=True, index=True)
    description = Column(String(500), nullable=False)
    material_type = Column(String(60), nullable=False, default="bulbo")
    last_price = Column(Numeric(18, 4), nullable=True)
    currency = Column(String(8), nullable=False, default="USD")
    # Unidad comercial de la OC vs. unidad física del inventario.
    purchase_unit = Column(String(40), nullable=False, default="caja")
    inventory_unit = Column(String(40), nullable=False, default="bulbo")
    units_per_purchase_unit = Column(Numeric(18, 3), nullable=True)
    # Campo legacy: se conserva para compatibilidad; refleja inventory_unit.
    unit = Column(String(40), nullable=False, default="bulbo")
    active = Column(Boolean, nullable=False, default=True, index=True)

    # Referencias formales a catálogos editables del cultivo.
    variety_catalog_id = Column(Integer, ForeignKey("fv_crop_catalog_values.id", ondelete="SET NULL"), nullable=True, index=True)
    caliber_catalog_id = Column(Integer, ForeignKey("fv_crop_catalog_values.id", ondelete="SET NULL"), nullable=True, index=True)
    common_catalog_id = Column(Integer, ForeignKey("fv_crop_catalog_values.id", ondelete="SET NULL"), nullable=True, index=True)
    color_catalog_id = Column(Integer, ForeignKey("fv_crop_catalog_values.id", ondelete="SET NULL"), nullable=True, index=True)

    # Snapshots/textos legacy para exportación de Excel e históricos.
    variety = Column(String(180), nullable=True, index=True)
    caliber = Column(String(60), nullable=True)
    common_code = Column(String(20), nullable=True)
    color = Column(String(30), nullable=True)
    default_density = Column(Numeric(12, 3), nullable=True)
    default_cycle_weeks = Column(Integer, nullable=True)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class PurchaseOrder(Base):
    __tablename__ = "fv_purchase_orders"
    id = Column(Integer, primary_key=True)
    number = Column(String(40), nullable=False, unique=True, index=True)
    supplier_id = Column(Integer, ForeignKey("fv_suppliers.id"), nullable=False, index=True)
    requested_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    approver_user_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    warehouse_due_date = Column(Date, nullable=False)
    status = Column(String(32), nullable=False, default="pending_approval", index=True)
    comments = Column(Text, nullable=True)
    subtotal = Column(Numeric(18, 4), nullable=False, default=0)
    tax_total = Column(Numeric(18, 4), nullable=False, default=0)
    total_estimated = Column(Numeric(18, 4), nullable=False, default=0)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    supplier = relationship("Supplier")
    lines = relationship("PurchaseOrderLine", back_populates="purchase_order", cascade="all, delete-orphan")
    events = relationship("PurchaseOrderEvent", back_populates="purchase_order", cascade="all, delete-orphan")


class PurchaseOrderLine(Base):
    __tablename__ = "fv_purchase_order_lines"
    id = Column(Integer, primary_key=True)
    purchase_order_id = Column(Integer, ForeignKey("fv_purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    sap_product_id = Column(Integer, ForeignKey("fv_sap_products.id"), nullable=False, index=True)
    product_code_snapshot = Column(String(80), nullable=False)
    description_snapshot = Column(String(500), nullable=False)
    variety_snapshot = Column(String(180), nullable=True)
    caliber_snapshot = Column(String(60), nullable=True)
    common_code_snapshot = Column(String(20), nullable=True)
    color_snapshot = Column(String(30), nullable=True)
    purchase_unit_snapshot = Column(String(40), nullable=False, default="caja")
    inventory_unit_snapshot = Column(String(40), nullable=False, default="bulbo")
    units_per_purchase_unit_snapshot = Column(Numeric(18, 3), nullable=True)
    currency_snapshot = Column(String(8), nullable=False, default="USD")
    quantity = Column(Numeric(18, 3), nullable=False)  # cantidad en unidad de compra
    unit_price_estimated = Column(Numeric(18, 4), nullable=False)
    apply_tax = Column(Boolean, nullable=False, default=False)
    tax_rate = Column(Numeric(8, 4), nullable=False, default=0)
    line_subtotal = Column(Numeric(18, 4), nullable=False, default=0)
    line_tax = Column(Numeric(18, 4), nullable=False, default=0)
    line_total = Column(Numeric(18, 4), nullable=False, default=0)
    purchase_order = relationship("PurchaseOrder", back_populates="lines")
    product = relationship("SapProduct")


class PurchaseOrderEvent(Base):
    __tablename__ = "fv_purchase_order_events"
    id = Column(Integer, primary_key=True)
    purchase_order_id = Column(Integer, ForeignKey("fv_purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String(40), nullable=False, default="comment", index=True)
    message = Column(Text, nullable=False)
    event_date = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    purchase_order = relationship("PurchaseOrder", back_populates="events")
    attachments = relationship("PurchaseOrderEventAttachment", back_populates="event", cascade="all, delete-orphan")


class PurchaseOrderEventAttachment(Base):
    __tablename__ = "fv_purchase_order_event_attachments"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("fv_purchase_order_events.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    storage_key = Column(String(700), nullable=False)
    mime_type = Column(String(120), nullable=True)
    size = Column(Integer, nullable=True)
    event = relationship("PurchaseOrderEvent", back_populates="attachments")


class GoodsReceipt(Base):
    __tablename__ = "fv_goods_receipts"
    id = Column(Integer, primary_key=True)
    number = Column(String(40), nullable=False, unique=True, index=True)
    purchase_order_id = Column(Integer, ForeignKey("fv_purchase_orders.id"), nullable=False, index=True)
    container_number = Column(String(120), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    received_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    comments = Column(Text, nullable=True)
    lines = relationship("GoodsReceiptLine", back_populates="receipt", cascade="all, delete-orphan")


class GoodsReceiptLine(Base):
    __tablename__ = "fv_goods_receipt_lines"
    id = Column(Integer, primary_key=True)
    goods_receipt_id = Column(Integer, ForeignKey("fv_goods_receipts.id", ondelete="CASCADE"), nullable=False, index=True)
    purchase_order_line_id = Column(Integer, ForeignKey("fv_purchase_order_lines.id"), nullable=False, index=True)
    # quantity_received conserva la semántica histórica: unidades físicas de inventario (bulbos).
    quantity_received = Column(Numeric(18, 3), nullable=False)
    purchase_quantity_received = Column(Numeric(18, 3), nullable=True)
    inventory_quantity_received = Column(Numeric(18, 3), nullable=True)
    supplier_lot_code = Column(String(100), nullable=True, index=True)
    harvest_year = Column(Integer, nullable=True)
    boxes_received = Column(Numeric(18, 3), nullable=True)
    units_per_box = Column(Numeric(18, 3), nullable=True)
    receipt = relationship("GoodsReceipt", back_populates="lines")


class InventoryLot(Base):
    __tablename__ = "fv_inventory_lots"
    id = Column(Integer, primary_key=True)
    lot_code = Column(String(80), nullable=False, unique=True, index=True)
    supplier_lot_code = Column(String(100), nullable=True, index=True)
    sap_product_id = Column(Integer, ForeignKey("fv_sap_products.id"), nullable=False, index=True)
    goods_receipt_line_id = Column(Integer, ForeignKey("fv_goods_receipt_lines.id"), nullable=False, index=True)
    container_number = Column(String(120), nullable=True, index=True)
    harvest_year = Column(Integer, nullable=True)
    variety_snapshot = Column(String(180), nullable=True)
    caliber_snapshot = Column(String(60), nullable=True)
    common_code_snapshot = Column(String(20), nullable=True)
    color_snapshot = Column(String(30), nullable=True)
    boxes_received = Column(Numeric(18, 3), nullable=True)
    units_per_box = Column(Numeric(18, 3), nullable=True)
    purchase_quantity_received = Column(Numeric(18, 3), nullable=True)
    purchase_unit_snapshot = Column(String(40), nullable=True)
    inventory_unit_snapshot = Column(String(40), nullable=True)
    quantity_received = Column(Numeric(18, 3), nullable=False)
    quantity_available = Column(Numeric(18, 3), nullable=False)
    quantity_reserved = Column(Numeric(18, 3), nullable=False, default=0)
    quantity_issued = Column(Numeric(18, 3), nullable=False, default=0)
    quantity_consumed = Column(Numeric(18, 3), nullable=False, default=0)
    quantity_written_off = Column(Numeric(18, 3), nullable=False, default=0)
    storage_status = Column(String(32), nullable=False, default="refrigerated", index=True)
    received_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    product = relationship("SapProduct")


class InventoryMovement(Base):
    __tablename__ = "fv_inventory_movements"
    id = Column(Integer, primary_key=True)
    inventory_lot_id = Column(Integer, ForeignKey("fv_inventory_lots.id"), nullable=False, index=True)
    movement_type = Column(String(40), nullable=False, index=True)
    quantity = Column(Numeric(18, 3), nullable=False)
    reference_type = Column(String(60), nullable=True)
    reference_id = Column(Integer, nullable=True)
    comments = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class Worker(Base):
    __tablename__ = "fv_workers"
    id = Column(Integer, primary_key=True)
    employee_code = Column(String(50), nullable=False, unique=True, index=True)
    identification = Column(String(80), nullable=False, unique=True, index=True)
    name = Column(String(180), nullable=False, index=True)
    hire_date = Column(Date, nullable=False)
    nationality = Column(String(100), nullable=True)
    birth_date = Column(Date, nullable=True)
    daily_salary = Column(Numeric(18, 2), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    user_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True, index=True)


class WorkCrew(Base):
    __tablename__ = "fv_work_crews"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, unique=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    planting_order_id = Column(Integer, nullable=True, index=True)
    members = relationship("WorkCrewMember", back_populates="crew", cascade="all, delete-orphan")


class WorkCrewMember(Base):
    __tablename__ = "fv_work_crew_members"
    id = Column(Integer, primary_key=True)
    crew_id = Column(Integer, ForeignKey("fv_work_crews.id", ondelete="CASCADE"), nullable=False, index=True)
    worker_id = Column(Integer, ForeignKey("fv_workers.id"), nullable=False, index=True)
    valid_from = Column(Date, nullable=False, default=date.today)
    valid_until = Column(Date, nullable=True)
    crew = relationship("WorkCrew", back_populates="members")
    worker = relationship("Worker")
    __table_args__ = (UniqueConstraint("crew_id", "worker_id", "valid_from", name="uq_fv_crew_worker_from"),)


class ProductionSpace(Base):
    __tablename__ = "fv_production_spaces"
    id = Column(Integer, primary_key=True)
    parent_id = Column(Integer, ForeignKey("fv_production_spaces.id", ondelete="SET NULL"), nullable=True, index=True)
    code = Column(String(60), nullable=False, unique=True, index=True)
    name = Column(String(180), nullable=False)
    type = Column(String(32), nullable=False, index=True)  # farm/greenhouse/land/bed (+ legacy table/row)
    latitude = Column(Numeric(10, 7), nullable=True)
    longitude = Column(Numeric(10, 7), nullable=True)
    area = Column(Numeric(18, 3), nullable=True)
    area_unit = Column(String(20), nullable=False, default="m2")
    capacity = Column(Numeric(18, 3), nullable=True)  # para bed: m2 utilizables
    status = Column(String(32), nullable=False, default="active", index=True)
    parent = relationship("ProductionSpace", remote_side=[id])


class PlantingOrder(Base):
    __tablename__ = "fv_planting_orders"
    id = Column(Integer, primary_key=True)
    crop_id = Column(Integer, ForeignKey("fv_flower_crops.id", ondelete="SET NULL"), nullable=True, index=True)
    number = Column(String(50), nullable=False, unique=True, index=True)
    production_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=False, index=True)
    crew_id = Column(Integer, ForeignKey("fv_work_crews.id"), nullable=False, index=True)
    requested_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    approved_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), nullable=False, default="pending_approval", index=True)
    planned_start_date = Column(Date, nullable=True)
    actual_start_date = Column(Date, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    comments = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class Requisition(Base):
    __tablename__ = "fv_requisitions"
    id = Column(Integer, primary_key=True)
    number = Column(String(50), nullable=False, unique=True, index=True)
    planting_order_id = Column(Integer, ForeignKey("fv_planting_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    approved_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    comments = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    lines = relationship("RequisitionLine", back_populates="requisition", cascade="all, delete-orphan")


class RequisitionLine(Base):
    __tablename__ = "fv_requisition_lines"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("fv_requisitions.id", ondelete="CASCADE"), nullable=False, index=True)
    inventory_lot_id = Column(Integer, ForeignKey("fv_inventory_lots.id"), nullable=False, index=True)
    sap_product_id = Column(Integer, ForeignKey("fv_sap_products.id"), nullable=False, index=True)
    quantity_requested = Column(Numeric(18, 3), nullable=False)
    quantity_approved = Column(Numeric(18, 3), nullable=False, default=0)
    quantity_delivered = Column(Numeric(18, 3), nullable=False, default=0)  # legacy v0.3; no se usa en el flujo nuevo
    quantity_planted = Column(Numeric(18, 3), nullable=False, default=0)
    requisition = relationship("Requisition", back_populates="lines")
    inventory_lot = relationship("InventoryLot")


class RequisitionDelivery(Base):
    """Entrega física parcial de una línea aprobada de requisición.

    Sustituye la pantalla independiente de "Salida Bulbo": el movimiento existe,
    pero vive dentro de la requisición y alimenta el respaldo Excel.
    """
    __tablename__ = "fv_requisition_deliveries"
    id = Column(Integer, primary_key=True)
    number = Column(String(50), nullable=False, unique=True, index=True)
    requisition_line_id = Column(Integer, ForeignKey("fv_requisition_lines.id", ondelete="CASCADE"), nullable=False, index=True)
    inventory_lot_id = Column(Integer, ForeignKey("fv_inventory_lots.id"), nullable=False, index=True)
    boxes = Column(Numeric(18, 3), nullable=True)
    units_per_box = Column(Numeric(18, 3), nullable=True)
    quantity = Column(Numeric(18, 3), nullable=False)
    delivery_week = Column(String(12), nullable=True, index=True)
    delivered_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    delivered_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    comments = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    requisition_line = relationship("RequisitionLine")
    inventory_lot = relationship("InventoryLot")


class InventoryIssue(Base):
    """Tabla histórica v0.3. Desde v0.4 la requisición aprobada reserva y la siembra consume directamente."""
    __tablename__ = "fv_inventory_issues"
    id = Column(Integer, primary_key=True)
    number = Column(String(50), nullable=False, unique=True, index=True)
    requisition_line_id = Column(Integer, ForeignKey("fv_requisition_lines.id"), nullable=False, index=True)
    inventory_lot_id = Column(Integer, ForeignKey("fv_inventory_lots.id"), nullable=False, index=True)
    boxes = Column(Numeric(18, 3), nullable=True)
    units_per_box = Column(Numeric(18, 3), nullable=True)
    quantity = Column(Numeric(18, 3), nullable=False)
    issue_week = Column(String(12), nullable=True, index=True)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    issued_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    comments = Column(Text, nullable=True)


class ProductionBatch(Base):
    """Lote productivo: lote de origen + cama + lado + semana de siembra."""
    __tablename__ = "fv_production_batches"
    id = Column(Integer, primary_key=True)
    crop_id = Column(Integer, ForeignKey("fv_flower_crops.id", ondelete="SET NULL"), nullable=True, index=True)
    code = Column(String(90), nullable=False, unique=True, index=True)
    planting_order_id = Column(Integer, ForeignKey("fv_planting_orders.id"), nullable=False, index=True)
    sap_product_id = Column(Integer, ForeignKey("fv_sap_products.id"), nullable=False, index=True)
    inventory_lot_id = Column(Integer, ForeignKey("fv_inventory_lots.id"), nullable=True, index=True)
    production_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=False, index=True)
    greenhouse_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    bed_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    side = Column(String(4), nullable=True)
    planting_week = Column(String(12), nullable=True, index=True)
    projected_harvest_week = Column(String(12), nullable=True)
    cycle_weeks = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False, default="open", index=True)
    opened_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    closed_at = Column(DateTime(timezone=True), nullable=True)


class PlantingRecord(Base):
    """Evento/segmento de siembra; una cama puede contener varios registros y lotes."""
    __tablename__ = "fv_planting_records"
    id = Column(Integer, primary_key=True)
    client_token = Column(String(80), nullable=True, unique=True, index=True)
    production_batch_id = Column(Integer, ForeignKey("fv_production_batches.id"), nullable=False, index=True)
    planting_order_id = Column(Integer, ForeignKey("fv_planting_orders.id"), nullable=False, index=True)
    requisition_line_id = Column(Integer, ForeignKey("fv_requisition_lines.id"), nullable=False, index=True)
    inventory_lot_id = Column(Integer, ForeignKey("fv_inventory_lots.id"), nullable=False, index=True)
    greenhouse_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    bed_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=False, index=True)
    side = Column(String(4), nullable=True)
    # Campos legacy mantenidos para compatibilidad con 0.1.
    table_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    row_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    quantity_planted = Column(Numeric(18, 3), nullable=False)
    density = Column(Numeric(12, 3), nullable=True)
    area_m2 = Column(Numeric(18, 4), nullable=True)
    cycle_weeks = Column(Integer, nullable=True)
    planting_week = Column(String(12), nullable=True, index=True)
    projected_harvest_week = Column(String(12), nullable=True)
    sealed_at = Column(Date, nullable=True)
    crew_id = Column(Integer, ForeignKey("fv_work_crews.id"), nullable=False)
    operator_user_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    planted_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    notes = Column(Text, nullable=True)


class HarvestRecord(Base):
    """Corte parcial. Se permiten múltiples eventos por lote productivo."""
    __tablename__ = "fv_harvest_records"
    id = Column(Integer, primary_key=True)
    client_token = Column(String(80), nullable=True, unique=True, index=True)
    production_batch_id = Column(Integer, ForeignKey("fv_production_batches.id"), nullable=False, index=True)
    planting_record_id = Column(Integer, ForeignKey("fv_planting_records.id"), nullable=True, index=True)
    bed_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    table_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    row_space_id = Column(Integer, ForeignKey("fv_production_spaces.id"), nullable=True, index=True)
    crew_id = Column(Integer, ForeignKey("fv_work_crews.id"), nullable=False)
    operator_user_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    bloom_1_qty = Column(Numeric(18, 3), nullable=False, default=0)
    bloom_2_qty = Column(Numeric(18, 3), nullable=False, default=0)
    bloom_3_5_qty = Column(Numeric(18, 3), nullable=False, default=0)
    discard_qty = Column(Numeric(18, 3), nullable=False, default=0)
    discard_reason = Column(String(250), nullable=True)
    quantity_harvested = Column(Numeric(18, 3), nullable=False, default=0)  # total venta
    total_output = Column(Numeric(18, 3), nullable=False, default=0)  # venta + descarte
    actual_harvest_week = Column(String(12), nullable=True, index=True)
    harvested_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    notes = Column(Text, nullable=True)


class TechnicalClosure(Base):
    __tablename__ = "fv_technical_closures"
    id = Column(Integer, primary_key=True)
    production_batch_id = Column(Integer, ForeignKey("fv_production_batches.id"), nullable=False, unique=True, index=True)
    total_planted = Column(Numeric(18, 3), nullable=False)
    total_harvested = Column(Numeric(18, 3), nullable=False)  # salida total contabilizada
    total_sale = Column(Numeric(18, 3), nullable=False, default=0)
    total_discarded = Column(Numeric(18, 3), nullable=False, default=0)
    difference = Column(Numeric(18, 3), nullable=False)
    yield_percentage = Column(Numeric(9, 3), nullable=False)
    sale_yield_percentage = Column(Numeric(9, 3), nullable=False, default=0)
    closed_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    comments = Column(Text, nullable=False)
