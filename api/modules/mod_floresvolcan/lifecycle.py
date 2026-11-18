from __future__ import annotations
import inspect as pyinspect
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from database import Base
from modules.mod_floresvolcan import models
from modules.mod_floresvolcan.models import PurchaseOrder, PlantingOrder, Requisition, FlowerCrop, CropCatalogValue, Supplier, ProductionSpace, ModuleSetting, SapProduct, ProductionBatch


def _tables():
    tables=[]
    for obj in vars(models).values():
        if pyinspect.isclass(obj) and getattr(obj,"__module__",None)==models.__name__:
            table=getattr(obj,"__table__",None)
            if table is not None: tables.append(table)
    return tables


# Migración aditiva v0.1 -> v0.2. No elimina ni renombra datos existentes.
_ADDITIVE_COLUMNS = {
    "fv_sap_products": {
        "expected_stems_per_bulb": "NUMERIC(12,3) DEFAULT 1",
        "crop_id": "INTEGER",
        "material_type": "VARCHAR(60) DEFAULT 'bulbo'",
        "purchase_unit": "VARCHAR(40) DEFAULT 'caja'", "inventory_unit": "VARCHAR(40) DEFAULT 'bulbo'",
        "units_per_purchase_unit": "NUMERIC(18,3)",
        "variety_catalog_id": "INTEGER", "caliber_catalog_id": "INTEGER", "common_catalog_id": "INTEGER", "color_catalog_id": "INTEGER",
        "variety": "VARCHAR(180)", "caliber": "VARCHAR(60)", "common_code": "VARCHAR(20)",
        "color": "VARCHAR(30)", "default_density": "NUMERIC(12,3)", "default_cycle_weeks": "INTEGER",
    },
    "fv_purchase_orders": {"paid_at":"DATE", "shipped_at":"DATE"},
    "fv_purchase_order_lines": {
        "variety_snapshot": "VARCHAR(180)", "caliber_snapshot": "VARCHAR(60)",
        "common_code_snapshot": "VARCHAR(20)", "color_snapshot": "VARCHAR(30)",
        "purchase_unit_snapshot": "VARCHAR(40) DEFAULT 'caja'", "inventory_unit_snapshot": "VARCHAR(40) DEFAULT 'bulbo'",
        "units_per_purchase_unit_snapshot": "NUMERIC(18,3)", "currency_snapshot": "VARCHAR(8) DEFAULT 'USD'",
    },
    "fv_goods_receipt_lines": {
        "supplier_lot_code": "VARCHAR(100)", "harvest_year": "INTEGER",
        "boxes_received": "NUMERIC(18,3)", "units_per_box": "NUMERIC(18,3)",
        "purchase_quantity_received": "NUMERIC(18,3)", "inventory_quantity_received": "NUMERIC(18,3)",
    },
    "fv_inventory_lots": {
        "supplier_lot_code": "VARCHAR(100)", "container_number": "VARCHAR(120)", "harvest_year": "INTEGER",
        "variety_snapshot": "VARCHAR(180)", "caliber_snapshot": "VARCHAR(60)",
        "common_code_snapshot": "VARCHAR(20)", "color_snapshot": "VARCHAR(30)",
        "boxes_received": "NUMERIC(18,3)", "units_per_box": "NUMERIC(18,3)",
        "purchase_quantity_received": "NUMERIC(18,3)", "purchase_unit_snapshot": "VARCHAR(40)", "inventory_unit_snapshot": "VARCHAR(40)",
        "quantity_issued": "NUMERIC(18,3) DEFAULT 0",
    },
    "fv_production_spaces": {"google_maps_url": "VARCHAR(1000)","length_m": "NUMERIC(18,3)", "width_m": "NUMERIC(18,3)"},
    "fv_workers": {"legacy_employee_code": "VARCHAR(50)","user_id": "INTEGER", "job_title": "VARCHAR(120)"},
    "fv_work_crews": {"planting_order_id": "INTEGER"},
    "fv_planting_orders": {"approved_at": "TIMESTAMP", "crop_id": "INTEGER",
        "bed_profile_id": "INTEGER", "planned_quantity": "INTEGER", "qr_token": "VARCHAR(64)", "bed_area_snapshot": "NUMERIC(18,3)",
        "bed_capacity_snapshot": "INTEGER", "density_snapshot": "NUMERIC(18,6)",
        "material_type_snapshot": "VARCHAR(60)", "crop_name_snapshot": "VARCHAR(120)"},
    "fv_requisition_lines": {"quantity_planted": "NUMERIC(18,3) DEFAULT 0"},
    "fv_production_batches": {
        "bed_cycle_id": "INTEGER", "history_review_required": "BOOLEAN DEFAULT FALSE",
        "expected_stems_per_bulb": "NUMERIC(12,3) DEFAULT 1",
        "crop_id": "INTEGER", "inventory_lot_id": "INTEGER", "greenhouse_space_id": "INTEGER", "bed_space_id": "INTEGER",
        "side": "VARCHAR(4)", "planting_week": "VARCHAR(12)", "projected_harvest_week": "VARCHAR(12)",
        "cycle_weeks": "INTEGER",
    },
    "fv_planting_records": {"bed_capacity_units_snapshot": "INTEGER","worker_id": "INTEGER",
        "request_hash": "VARCHAR(64)",
        "client_token": "VARCHAR(80)", "greenhouse_space_id": "INTEGER", "bed_space_id": "INTEGER",
        "side": "VARCHAR(4)", "density": "NUMERIC(12,3)", "area_m2": "NUMERIC(18,4)",
        "cycle_weeks": "INTEGER", "planting_week": "VARCHAR(12)", "projected_harvest_week": "VARCHAR(12)",
        "sealed_at": "DATE",
    },
    "fv_harvest_records": {"worker_id": "INTEGER",
        "voided_at": "TIMESTAMP", "voided_by_id": "INTEGER", "void_reason": "VARCHAR(1000)",
        "request_hash": "VARCHAR(64)",
        "harvest_order_id": "INTEGER",
        "client_token": "VARCHAR(80)", "planting_record_id": "INTEGER", "bed_space_id": "INTEGER",
        "bloom_1_qty": "NUMERIC(18,3) DEFAULT 0", "bloom_2_qty": "NUMERIC(18,3) DEFAULT 0",
        "bloom_3_5_qty": "NUMERIC(18,3) DEFAULT 0", "discard_qty": "NUMERIC(18,3) DEFAULT 0",
        "discard_reason": "VARCHAR(180)", "total_output": "NUMERIC(18,3) DEFAULT 0",
        "actual_harvest_week": "VARCHAR(12)",
    },
    "fv_technical_closures": {
        "total_expected": "NUMERIC(18,3) DEFAULT 0", "total_loss": "NUMERIC(18,3) DEFAULT 0",
        "previous_summary_json": "TEXT",
        "total_sale": "NUMERIC(18,3) DEFAULT 0", "total_discarded": "NUMERIC(18,3) DEFAULT 0",
        "sale_yield_percentage": "NUMERIC(10,3) DEFAULT 0",
    },
    "fv_harvest_orders": {"qr_token": "VARCHAR(64)","overage_quantity": "NUMERIC(18,3) DEFAULT 0"},
    "fv_material_resolutions": {"client_token": "VARCHAR(80)", "request_hash": "VARCHAR(64)"},
}
_ADDITIVE_COLUMNS["fv_requisition_lines"].update({
    "quantity_returned": "NUMERIC(18,3) DEFAULT 0", "quantity_field_loss": "NUMERIC(18,3) DEFAULT 0",
    "quantity_released": "NUMERIC(18,3) DEFAULT 0",
})


def _add_missing_columns(db: Session) -> None:
    bind=db.get_bind(); inspector=inspect(bind); existing=set(inspector.get_table_names())
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in existing: continue
        current={c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in current:
                db.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))
    db.commit()


def _seed_defaults(db: Session) -> None:
    """Precarga editable derivada de los Excel del cliente. Idempotente."""
    from modules.mod_floresvolcan.seed_data import DEFAULT_SUPPLIERS, LILY_CATALOGS, LILY_GREENHOUSE_BEDS

    if db.query(ModuleSetting.id).filter(ModuleSetting.key == "excel.exploration", ModuleSetting.value_text == "true").first():
        # The chosen workbook remains the catalog source across restarts.
        return

    crop = db.query(FlowerCrop).filter(FlowerCrop.code == "LIRIO").first()
    if crop is None:
        crop = FlowerCrop(code="LIRIO", name="Lirios", material_type="bulbo", active=True, is_default=True)
        db.add(crop); db.flush()

    # Catálogos observados: se cargan una sola vez y luego quedan bajo control del administrador.
    existing = {(r.category, r.code) for r in db.query(CropCatalogValue).filter(CropCatalogValue.crop_id == crop.id).all()}
    for category, values in LILY_CATALOGS.items():
        for order, value in enumerate(values):
            code = str(value).strip().upper()
            if (category, code) in existing:
                continue
            db.add(CropCatalogValue(crop_id=crop.id, category=category, code=code, label=str(value).strip(), sort_order=order, active=True, source="excel_default"))

    # Los Excel solo identifican proveedores por código. No se inventan país/dirección: quedan editables.
    for code in DEFAULT_SUPPLIERS:
        if not db.query(Supplier.id).filter(Supplier.code == code).first():
            db.add(Supplier(code=code, name=code, country="Por definir", active=True))

    root = db.query(ProductionSpace).filter(ProductionSpace.code == "LIRIO-ROOT").first()
    if root is None:
        root = ProductionSpace(code="LIRIO-ROOT", name="Lirios", type="farm", status="active", area_unit="m2")
        db.add(root); db.flush()
    for greenhouse_name, beds in LILY_GREENHOUSE_BEDS.items():
        gh_code = "LIRIO-GH-" + greenhouse_name.replace(" ", "-")
        gh = db.query(ProductionSpace).filter(ProductionSpace.code == gh_code).first()
        if gh is None:
            gh = ProductionSpace(parent_id=root.id, code=gh_code, name=greenhouse_name, type="greenhouse", status="active", area_unit="m2")
            db.add(gh); db.flush()
        for bed in beds:
            bed_code = f"{gh_code}-BED-{bed}"
            if not db.query(ProductionSpace.id).filter(ProductionSpace.code == bed_code).first():
                db.add(ProductionSpace(parent_id=gh.id, code=bed_code, name=f"Cama {bed}", type="bed", status="active", area_unit="m2"))

    defaults = {
        "backup.enabled": "false",
        "backup.frequency": "daily",
        "backup.hour": "2",
        "backup.retention_days": "30",
        "backup.destination": "local",
        "backup.drive_folder_id": "",
        "backup.drive_client_id": "",
        "backup.drive_client_secret": "",
        "backup.drive_refresh_token": "",
    }
    for key, value in defaults.items():
        if not db.query(ModuleSetting.id).filter(ModuleSetting.key == key).first():
            db.add(ModuleSetting(key=key, value_text=value, is_secret=key.endswith(("client_secret", "refresh_token"))))

    # Migración aditiva v0.5: los datos creados antes de introducir cultivos
    # pertenecen a Lirios. No se sobrescribe ningún crop_id ya definido.
    db.query(SapProduct).filter(SapProduct.crop_id.is_(None)).update({SapProduct.crop_id: crop.id}, synchronize_session=False)
    db.query(PlantingOrder).filter(PlantingOrder.crop_id.is_(None)).update({PlantingOrder.crop_id: crop.id}, synchronize_session=False)
    db.query(ProductionBatch).filter(ProductionBatch.crop_id.is_(None)).update({ProductionBatch.crop_id: crop.id}, synchronize_session=False)

    # v0.5.1: enlaza textos SAP existentes con los catálogos normalizados de Lirios.
    catalogs = db.query(CropCatalogValue).filter(CropCatalogValue.crop_id == crop.id).all()
    by_category = {}
    for item in catalogs:
        by_category.setdefault(item.category, {})[str(item.label).strip().casefold()] = item
        by_category.setdefault(item.category, {})[str(item.code).strip().casefold()] = item
    for product in db.query(SapProduct).filter(SapProduct.crop_id == crop.id).all():
        product.material_type = product.material_type or "bulbo"
        product.purchase_unit = product.purchase_unit or "caja"
        product.inventory_unit = product.inventory_unit or product.unit or "bulbo"
        product.unit = product.inventory_unit
        for category, text_value, attr in [
            ("variety", product.variety, "variety_catalog_id"),
            ("caliber", product.caliber, "caliber_catalog_id"),
            ("common", product.common_code, "common_catalog_id"),
            ("color", product.color, "color_catalog_id"),
        ]:
            if getattr(product, attr, None) or not text_value:
                continue
            match = by_category.get(category, {}).get(str(text_value).strip().casefold())
            if match:
                setattr(product, attr, match.id)

    # Completa snapshots nuevos en líneas históricas sin alterar precios/cantidades.
    from modules.mod_floresvolcan.models import PurchaseOrderLine, GoodsReceiptLine, InventoryLot
    for line in db.query(PurchaseOrderLine).all():
        product = db.query(SapProduct).filter(SapProduct.id == line.sap_product_id).first()
        if not product:
            continue
        line.variety_snapshot = line.variety_snapshot or product.variety
        line.caliber_snapshot = line.caliber_snapshot or product.caliber
        line.common_code_snapshot = line.common_code_snapshot or product.common_code
        line.color_snapshot = line.color_snapshot or product.color
        line.purchase_unit_snapshot = line.purchase_unit_snapshot or product.purchase_unit or "caja"
        line.inventory_unit_snapshot = line.inventory_unit_snapshot or product.inventory_unit or "bulbo"
        line.units_per_purchase_unit_snapshot = line.units_per_purchase_unit_snapshot or product.units_per_purchase_unit
        line.currency_snapshot = line.currency_snapshot or product.currency or "USD"
    for receipt_line in db.query(GoodsReceiptLine).all():
        if receipt_line.inventory_quantity_received is None:
            receipt_line.inventory_quantity_received = receipt_line.quantity_received
        if receipt_line.purchase_quantity_received is None:
            receipt_line.purchase_quantity_received = receipt_line.boxes_received or receipt_line.quantity_received
    for lot in db.query(InventoryLot).all():
        if lot.purchase_quantity_received is None:
            lot.purchase_quantity_received = lot.boxes_received or lot.quantity_received
        product = db.query(SapProduct).filter(SapProduct.id == lot.sap_product_id).first()
        if product:
            lot.purchase_unit_snapshot = lot.purchase_unit_snapshot or product.purchase_unit
            lot.inventory_unit_snapshot = lot.inventory_unit_snapshot or product.inventory_unit
    db.commit()


def install(db: Session) -> None:
    # Primero migra tablas v0.1 que pudieran existir y luego crea las nuevas (p.ej. fv_inventory_issues).
    _add_missing_columns(db)
    Base.metadata.create_all(bind=db.get_bind(), tables=_tables())
    if db.get_bind().dialect.name == "postgresql":
        # Historical sheets have names, but no national IDs or hire dates.
        # New manual records still require both through WorkerIn.
        db.execute(text('ALTER TABLE fv_workers ALTER COLUMN identification DROP NOT NULL'))
        db.execute(text('ALTER TABLE fv_workers ALTER COLUMN hire_date DROP NOT NULL'))
        db.execute(text('ALTER TABLE fv_planting_records ALTER COLUMN density TYPE NUMERIC(18,6)'))
    _seed_defaults(db)
    _upgrade_production_control(db)
    from .worker_codes import normalize_worker_codes, install_worker_code_guard
    install_worker_code_guard(db)
    normalize_worker_codes(db)
    from .material_defaults import upgrade_material_defaults
    upgrade_material_defaults(db)
    import secrets
    from .models import HarvestOrder
    for model in (PlantingOrder,HarvestOrder):
        for order in db.query(model).filter(model.status.in_(['approved','in_progress']),model.qr_token.is_(None)):
            order.qr_token=secrets.token_urlsafe(32)
    db.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_fv_harvest_qr_v11 ON fv_harvest_orders (qr_token)'))
    db.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_fv_planting_qr_v11 ON fv_planting_orders (qr_token)'))
    db.commit()


def _upgrade_production_control(db: Session) -> None:
    """Conserva las cantidades previas. Ninguna corta ambigua se asigna automáticamente."""
    from sqlalchemy import func
    from .models import BedCycle, HarvestRecord, PlantingRecord, MaterialResolution
    for model in (PlantingRecord, HarvestRecord, MaterialResolution):
        duplicates = (db.query(model.client_token).filter(model.client_token.isnot(None))
                      .group_by(model.client_token).having(func.count(model.id) > 1).first())
        if duplicates:
            raise RuntimeError("Flores Volcán: existen identificadores de campo duplicados; revise el histórico antes de actualizar")
        db.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{model.__tablename__}_token_v07" ON "{model.__tablename__}" (client_token)'))
    marker = db.query(ModuleSetting).filter(ModuleSetting.key == "production_control.v07").first()
    if not marker:
        for cut in db.query(HarvestRecord).all():
            # Las versiones anteriores no tenían total_output; quantity_harvested ya era venta.
            if not cut.total_output and (cut.quantity_harvested or cut.discard_qty):
                cut.total_output = (cut.quantity_harvested or 0) + (cut.discard_qty or 0)
        groups = {}
        for batch in db.query(ProductionBatch).filter(ProductionBatch.bed_cycle_id.is_(None)).all():
            if batch.bed_space_id:
                groups.setdefault(batch.bed_space_id, []).append(batch)
        for bed_id, batches in groups.items():
            cycle = db.query(BedCycle).filter(BedCycle.bed_space_id == bed_id, BedCycle.status != "cleaned").first()
            if not cycle:
                ids = [b.id for b in batches]
                has_cuts = db.query(HarvestRecord.id).filter(HarvestRecord.production_batch_id.in_(ids)).first()
                state = "pending_cleaning" if all(b.status == "closed" for b in batches) else "harvesting" if has_cuts else "growing"
                cycle = BedCycle(bed_space_id=bed_id, status=state)
                db.add(cycle); db.flush()
            for batch in batches:
                batch.bed_cycle_id = cycle.id
        db.add(ModuleSetting(key="production_control.v07", value_text="migrated"))
    db.commit()


def upgrade_schema(db: Session) -> None:
    install(db)


def activate(db: Session) -> None:
    upgrade_schema(db)


def deactivate(db: Session) -> None:
    del db


def uninstall(db: Session) -> None:
    # Desinstalar el runtime no destruye datos de producción.
    del db


def user_delete_blockers(db: Session, user_id: int) -> list[str]:
    blockers=[]
    checks=[
        (PurchaseOrder.requested_by_id,"orden(es) de compra solicitada(s)"),
        (PurchaseOrder.approved_by_id,"orden(es) de compra aprobada(s)"),
        (PlantingOrder.requested_by_id,"orden(es) de siembra solicitada(s)"),
        (PlantingOrder.approved_by_id,"orden(es) de siembra aprobada(s)"),
        (Requisition.requested_by_id,"requisición(es) solicitada(s)"),
        (Requisition.approved_by_id,"requisición(es) aprobada(s)"),
    ]
    for column,label in checks:
        count=db.query(column.class_).filter(column==user_id).count()
        if count: blockers.append(f"{count} {label}")
    return blockers
