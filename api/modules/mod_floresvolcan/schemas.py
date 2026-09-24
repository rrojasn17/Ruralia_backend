from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class ContactIn(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=80)
    position: str | None = Field(default=None, max_length=120)
    active: bool = True

    @field_validator("email", "phone", "position", mode="before")
    @classmethod
    def normalize_optional_text(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class SupplierIn(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    name: str = Field(min_length=2, max_length=180)
    country: str = Field(min_length=2, max_length=100)
    department: str | None = Field(default=None, max_length=120)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=80)
    email: EmailStr | None = None
    website: str | None = Field(default=None, max_length=500)
    active: bool = True
    contacts: list[ContactIn] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_supplier_payload(cls, data):
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for key in ("code", "department", "address", "phone", "email", "website"):
            value = cleaned.get(key)
            if isinstance(value, str):
                value = value.strip()
                cleaned[key] = value or None
        for key in ("name", "country"):
            value = cleaned.get(key)
            if isinstance(value, str):
                cleaned[key] = value.strip()
        contacts = []
        for item in cleaned.get("contacts") or []:
            if isinstance(item, dict):
                # Una fila de contacto completamente vacía no debe bloquear la creación del proveedor.
                if not any(str(item.get(k) or "").strip() for k in ("name", "email", "phone", "position")):
                    continue
            contacts.append(item)
        cleaned["contacts"] = contacts
        return cleaned


class SapProductIn(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=2, max_length=500)
    material_type: str = Field(default="bulbo", min_length=1, max_length=60)
    last_price: Decimal | None = Field(default=None, ge=0)
    currency: Literal["USD", "EUR", "CRC"] = "USD"
    purchase_unit: str = Field(default="caja", min_length=1, max_length=40)
    inventory_unit: str = Field(default="bulbo", min_length=1, max_length=40)
    units_per_purchase_unit: Decimal | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=40)
    active: bool = True
    variety_catalog_id: int | None = None
    caliber_catalog_id: int | None = None
    common_catalog_id: int | None = None
    color_catalog_id: int | None = None
    # Campos legacy opcionales para clientes anteriores a 0.5.1.
    variety: str | None = Field(default=None, max_length=180)
    caliber: str | None = Field(default=None, max_length=60)
    common_code: str | None = Field(default=None, max_length=20)
    color: str | None = Field(default=None, max_length=30)
    default_density: Decimal | None = Field(default=None, gt=0)
    default_cycle_weeks: int | None = Field(default=None, ge=1, le=52)


class SapProductStatusIn(BaseModel):
    active: bool


class PurchaseOrderLineIn(BaseModel):
    sap_product_id: int
    quantity: Decimal = Field(gt=0)
    unit_price_estimated: Decimal = Field(ge=0)
    apply_tax: bool = False
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class PurchaseOrderIn(BaseModel):
    supplier_id: int
    approver_user_id: int
    warehouse_due_date: date
    comments: str | None = None
    lines: list[PurchaseOrderLineIn] = Field(min_length=1)


class EventIn(BaseModel):
    event_type: Literal["comment", "shipping_update", "container_update", "document"] = "comment"
    message: str = Field(min_length=1, max_length=4000)
    event_date: datetime | None = None


class ReceiptLineIn(BaseModel):
    purchase_order_line_id: int
    # 0.5.1: la OC se recibe en su unidad comercial (p.ej. cajas) y el inventario en unidades físicas (bulbos).
    purchase_quantity_received: Decimal | None = Field(default=None, gt=0)
    quantity_received: Decimal | None = Field(default=None, gt=0)  # compatibilidad: unidades físicas
    supplier_lot_code: str | None = Field(default=None, max_length=100)
    harvest_year: int | None = Field(default=None, ge=2000, le=2200)
    boxes_received: Decimal | None = Field(default=None, ge=0)
    units_per_box: Decimal | None = Field(default=None, gt=0)
    units_per_purchase_unit: Decimal | None = Field(default=None, gt=0)


class GoodsReceiptIn(BaseModel):
    purchase_order_id: int
    container_number: str = Field(min_length=1, max_length=120)
    comments: str | None = None
    lines: list[ReceiptLineIn] = Field(min_length=1)


class InventoryWriteoffIn(BaseModel):
    quantity: Decimal = Field(gt=0)
    comments: str = Field(min_length=3, max_length=1000)



class WorkerIn(BaseModel):
    employee_code: str = Field(min_length=1, max_length=50)
    identification: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=2, max_length=180)
    hire_date: date
    nationality: str | None = Field(default=None, max_length=100)
    birth_date: date | None = None
    daily_salary: Decimal | None = Field(default=None, ge=0)
    active: bool = True
    user_id: int | None = None


class CrewIn(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    worker_ids: list[int] = Field(min_length=1)


class ProductionSpaceIn(BaseModel):
    parent_id: int | None = None
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=2, max_length=180)
    type: Literal["farm", "greenhouse", "land", "bed", "table", "row"]
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    area: Decimal | None = Field(default=None, ge=0)
    area_unit: str = Field(default="m2", max_length=20)
    capacity: Decimal | None = Field(default=None, ge=0)
    status: Literal["active", "inactive", "maintenance"] = "active"


class PlantingOrderIn(BaseModel):
    production_space_id: int
    worker_ids: list[int] = Field(min_length=1)
    planned_start_date: date | None = None
    comments: str | None = None


class RequisitionLineIn(BaseModel):
    inventory_lot_id: int
    quantity_requested: Decimal = Field(gt=0)


class RequisitionIn(BaseModel):
    planting_order_id: int
    comments: str | None = None
    lines: list[RequisitionLineIn] = Field(min_length=1)


class PlantingRecordIn(BaseModel):
    client_token: str | None = Field(default=None, max_length=80)
    production_batch_id: int | None = None
    planting_order_id: int
    requisition_line_id: int | None = None
    greenhouse_space_id: int | None = None
    bed_space_id: int
    side: Literal["A", "B"] | None = None
    quantity_planted: Decimal = Field(gt=0)
    density: Decimal | None = Field(default=None, gt=0)
    cycle_weeks: int | None = Field(default=None, ge=1, le=52)
    sealed_at: date | None = None
    planted_at: datetime | None = None
    notes: str | None = None


class HarvestRecordIn(BaseModel):
    client_token: str | None = Field(default=None, max_length=80)
    production_batch_id: int
    planting_record_id: int | None = None
    bloom_1_qty: Decimal = Field(default=Decimal("0"), ge=0)
    bloom_2_qty: Decimal = Field(default=Decimal("0"), ge=0)
    bloom_3_5_qty: Decimal = Field(default=Decimal("0"), ge=0)
    discard_qty: Decimal = Field(default=Decimal("0"), ge=0)
    discard_reason: str | None = Field(default=None, max_length=250)
    harvested_at: datetime | None = None
    notes: str | None = None


class TechnicalClosureIn(BaseModel):
    comments: str = Field(min_length=3, max_length=4000)


class FieldInterpretIn(BaseModel):
    operation: Literal["planting", "harvest"]
    text: str = Field(min_length=2, max_length=2000)
    planting_order_id: int | None = None
    production_batch_id: int | None = None
    current_space_id: int | None = None


class CropCatalogValueIn(BaseModel):
    category: str = Field(min_length=2, max_length=60)
    code: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=220)
    sort_order: int = 0
    active: bool = True


class CropCatalogValuePatch(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=120)
    label: str | None = Field(default=None, min_length=1, max_length=220)
    sort_order: int | None = None
    active: bool | None = None


class RequisitionDeliveryIn(BaseModel):
    requisition_line_id: int
    quantity: Decimal = Field(gt=0)
    boxes: Decimal | None = Field(default=None, ge=0)
    units_per_box: Decimal | None = Field(default=None, gt=0)
    delivered_at: datetime | None = None
    comments: str | None = Field(default=None, max_length=1000)


class BackupSettingsIn(BaseModel):
    enabled: bool = False
    frequency: Literal["daily", "weekly", "manual"] = "daily"
    hour: int = Field(default=2, ge=0, le=23)
    weekday: int = Field(default=0, ge=0, le=6)
    retention_days: int = Field(default=30, ge=1, le=3650)
    destination: Literal["local", "drive", "both"] = "local"
    drive_folder_id: str | None = Field(default=None, max_length=300)
    drive_client_id: str | None = Field(default=None, max_length=500)
    drive_client_secret: str | None = Field(default=None, max_length=1000)
    drive_refresh_token: str | None = Field(default=None, max_length=2000)
