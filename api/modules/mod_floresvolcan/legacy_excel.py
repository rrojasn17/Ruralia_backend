from __future__ import annotations

from io import BytesIO
from decimal import Decimal
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import func
from sqlalchemy.orm import Session

from modules.mod_floresvolcan.models import (
    CropCatalogValue, FlowerCrop, GoodsReceipt, GoodsReceiptLine, HarvestRecord,
    InventoryLot, PlantingOrder, PlantingRecord, ProductionBatch, ProductionSpace,
    PurchaseOrder, RequisitionDelivery, RequisitionLine, SapProduct, Supplier,
    WorkCrewMember,
)
from .production_control import batch_segments, local_date

HEADERS = {
    "Entradas": ["COD","Variedad","Calibre","Comun","Fecha de Ingreso","Contenedor","Lote","Cosecha","Proveedor","Cajas","Cantidad por Caja","Total Bulbos"],
    "Salida Bulbo": ["COD","Variedad","Calibre","Comun","Contenedor","Lote","Proveedor","Stock","Cajas","Semana Salida","Fecha de Salida","Cantidad por Caja","Bulbos"],
    "Siembra": ["KEY","LOTE","INVERNADERO","LADO","CAMA","COMUN","PROVEEDOR","COLOR","VARIEDAD","CANTIDAD","CICLO","SEM. SIEMBRA","FECHA DE SIEMBRA","SEM. COSECHA","CALIBRE","DENSIDAD","FECHA SELLADO","SEMBRADOR","M2"],
    "Corta": ["KEY","LOTE","INV.","CAMA","COMUN","PROVEEDOR","COLOR","VARIEDAD","CANTIDAD","SEM. DE SIEMBRA","SEM. COSECHA PROG.","SEM. COSECHA REAL","FECHA DE CORTA","DES. CANTIDAD","DES. MOTIVO","1 BL","2 BL","3-5 BL","TOTAL VENTA","TOTAL "],
}

COMMAND_CATEGORIES = [
    ("INVERNADERO", "greenhouse"), ("COMÚN", "common"), ("PROVEEDOR", "supplier"),
    ("COLOR", "color"), ("VARIEDAD", "variety"), ("CALIBRE", "caliber"),
    ("DENSIDAD", "density"), ("LADO", "side"), ("SEMBRADOR", "legacy_planter"),
]


def _n(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _legacy_code(variety, caliber, container, lot) -> str:
    return f"{variety or ''}{caliber or ''}{container or ''}{lot or ''}".replace(" ", "")


def _supplier_for_lot(db: Session, lot: InventoryLot) -> Supplier | None:
    line = db.query(GoodsReceiptLine).filter(GoodsReceiptLine.id == lot.goods_receipt_line_id).first()
    receipt = db.query(GoodsReceipt).filter(GoodsReceipt.id == line.goods_receipt_id).first() if line else None
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == receipt.purchase_order_id).first() if receipt else None
    return db.query(Supplier).filter(Supplier.id == po.supplier_id).first() if po else None


def _product(db: Session, lot: InventoryLot | None, product_id: int | None = None) -> SapProduct | None:
    pid = product_id or (lot.sap_product_id if lot else None)
    return db.query(SapProduct).filter(SapProduct.id == pid).first() if pid else None


def _space(db: Session, sid: int | None) -> ProductionSpace | None:
    return db.query(ProductionSpace).filter(ProductionSpace.id == sid).first() if sid else None


def _crew_label(db: Session, crew_id: int | None) -> str:
    if not crew_id:
        return ""
    members = db.query(WorkCrewMember).filter(WorkCrewMember.crew_id == crew_id).all()
    names = [m.worker.name for m in members if getattr(m, "worker", None)]
    return "-".join(names)


def _style_sheet(ws) -> None:
    fill = PatternFill("solid", fgColor="107C5C")
    for cell in ws[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        width = min(35, max(10, max(len(str(c.value or "")) for c in list(col)[:120]) + 2))
        ws.column_dimensions[col[0].column_letter].width = width


def build_lirio_workbook(db: Session, title: str = "Lirio") -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    # Entradas
    ws = wb.create_sheet("Entradas"); ws.append(HEADERS["Entradas"])
    lots = db.query(InventoryLot).order_by(InventoryLot.received_at, InventoryLot.id).all()
    for lot in lots:
        product = _product(db, lot); supplier = _supplier_for_lot(db, lot)
        variety = lot.variety_snapshot or (product.variety if product else "")
        caliber = lot.caliber_snapshot or (product.caliber if product else "")
        common = lot.common_code_snapshot or (product.common_code if product else "")
        ws.append([
            _legacy_code(variety, caliber, lot.container_number, lot.supplier_lot_code), variety, caliber, common,
            local_date(lot.received_at) if lot.received_at else None, lot.container_number, lot.supplier_lot_code, lot.harvest_year,
            supplier.code if supplier else "", _n(lot.boxes_received), _n(lot.units_per_box), _n(lot.quantity_received),
        ])
    _style_sheet(ws)

    # Salida Bulbo = entregas parciales dentro de requisiciones.
    ws = wb.create_sheet("Salida Bulbo"); ws.append(HEADERS["Salida Bulbo"])
    deliveries = db.query(RequisitionDelivery).order_by(RequisitionDelivery.delivered_at, RequisitionDelivery.id).all()
    for d in deliveries:
        line = db.query(RequisitionLine).filter(RequisitionLine.id == d.requisition_line_id).first()
        lot = db.query(InventoryLot).filter(InventoryLot.id == d.inventory_lot_id).first()
        product = _product(db, lot, line.sap_product_id if line else None); supplier = _supplier_for_lot(db, lot) if lot else None
        variety = (lot.variety_snapshot if lot else None) or (product.variety if product else "")
        caliber = (lot.caliber_snapshot if lot else None) or (product.caliber if product else "")
        common = (lot.common_code_snapshot if lot else None) or (product.common_code if product else "")
        units_per_box = _n(d.units_per_box or (lot.units_per_box if lot else None))
        boxes = _n(d.boxes) or (_n(d.quantity) / units_per_box if units_per_box else 0)
        ws.append([
            _legacy_code(variety, caliber, lot.container_number if lot else "", lot.supplier_lot_code if lot else ""),
            variety, caliber, common, lot.container_number if lot else "", lot.supplier_lot_code if lot else "",
            supplier.code if supplier else "", _n(lot.boxes_received if lot else 0), boxes, d.delivery_week,
            local_date(d.delivered_at) if d.delivered_at else None, units_per_box, _n(d.quantity),
        ])
    _style_sheet(ws)

    # Siembra
    ws = wb.create_sheet("Siembra"); ws.append(HEADERS["Siembra"] + ["ID SIEMBRA", "ID LOTE PRODUCTIVO", "ID CICLO MESA", "ID INVENTARIO"])
    plantings = db.query(PlantingRecord).order_by(PlantingRecord.planted_at, PlantingRecord.id).all()
    for pr in plantings:
        lot = db.query(InventoryLot).filter(InventoryLot.id == pr.inventory_lot_id).first()
        line = db.query(RequisitionLine).filter(RequisitionLine.id == pr.requisition_line_id).first()
        product = _product(db, lot, line.sap_product_id if line else None); supplier = _supplier_for_lot(db, lot) if lot else None
        greenhouse = _space(db, pr.greenhouse_space_id); bed = _space(db, pr.bed_space_id)
        batch = db.query(ProductionBatch).filter(ProductionBatch.id == pr.production_batch_id).first()
        variety = (lot.variety_snapshot if lot else None) or (product.variety if product else "")
        caliber = (lot.caliber_snapshot if lot else None) or (product.caliber if product else "")
        common = (lot.common_code_snapshot if lot else None) or (product.common_code if product else "")
        color = (lot.color_snapshot if lot else None) or (product.color if product else "")
        inv = greenhouse.name if greenhouse else ""; bed_label = (bed.name.replace("Cama ", "") if bed else "")
        key = f"SIEMBRA-{pr.id}"
        ws.append([
            key, lot.supplier_lot_code if lot else "", inv, pr.side, bed_label, common, supplier.code if supplier else "",
            color, variety, _n(pr.quantity_planted), pr.cycle_weeks, pr.planting_week,
            local_date(pr.planted_at) if pr.planted_at else None, pr.projected_harvest_week, caliber, _n(pr.density),
            pr.sealed_at, _crew_label(db, pr.crew_id), _n(pr.area_m2),
            pr.id, pr.production_batch_id, batch.bed_cycle_id if batch else None, pr.inventory_lot_id,
        ])
    _style_sheet(ws)

    # Corta
    ws = wb.create_sheet("Corta"); ws.append(HEADERS["Corta"] + ["ID CORTA", "ID SIEMBRA", "ID LOTE PRODUCTIVO", "ID CICLO MESA", "LADO", "ESTADO VINCULO"])
    ws.cell(1, 9, "SEMBRADO DEL SEGMENTO (NO SUMAR)")
    cuts = db.query(HarvestRecord).filter(HarvestRecord.voided_at.is_(None)).order_by(HarvestRecord.harvested_at, HarvestRecord.id).all()
    for cut in cuts:
        batch = db.query(ProductionBatch).filter(ProductionBatch.id == cut.production_batch_id).first()
        lot = db.query(InventoryLot).filter(InventoryLot.id == batch.inventory_lot_id).first() if batch and batch.inventory_lot_id else None
        product = _product(db, lot, batch.sap_product_id if batch else None); supplier = _supplier_for_lot(db, lot) if lot else None
        greenhouse = _space(db, batch.greenhouse_space_id if batch else None); bed = _space(db, batch.bed_space_id if batch else None)
        segment = db.query(PlantingRecord).filter(PlantingRecord.id == cut.planting_record_id).first() if cut.planting_record_id else None
        planted = segment.quantity_planted if segment else None
        variety = (lot.variety_snapshot if lot else None) or (product.variety if product else "")
        common = (lot.common_code_snapshot if lot else None) or (product.common_code if product else "")
        color = (lot.color_snapshot if lot else None) or (product.color if product else "")
        inv = greenhouse.name if greenhouse else ""; bed_label = bed.name.replace("Cama ", "") if bed else ""
        key = f"SIEMBRA-{segment.id}" if segment else f"SIN-ASIGNAR-{cut.id}"
        ws.append([
            key, lot.supplier_lot_code if lot else "", inv, bed_label, common, supplier.code if supplier else "", color,
            variety, _n(planted) if planted is not None else None, segment.planting_week if segment else None, segment.projected_harvest_week if segment else None,
            cut.actual_harvest_week, local_date(cut.harvested_at) if cut.harvested_at else None, _n(cut.discard_qty),
            cut.discard_reason, _n(cut.bloom_1_qty), _n(cut.bloom_2_qty), _n(cut.bloom_3_5_qty),
            _n(cut.quantity_harvested), _n(cut.total_output),
            cut.id, cut.planting_record_id, cut.production_batch_id, batch.bed_cycle_id if batch else None,
            segment.side if segment else None, "VINCULADO" if segment else "PENDIENTE DE ASIGNACION",
        ])
    _style_sheet(ws)

    ws = wb.create_sheet("Cortas anuladas")
    ws.append(["ID CORTA", "ID SIEMBRA", "ID LOTE PRODUCTIVO", "FECHA CORTA", "VENTA ORIGINAL", "DESCARTE ORIGINAL", "SALIDA ORIGINAL", "FECHA ANULACION", "USUARIO ANULACION", "MOTIVO"])
    for cut in db.query(HarvestRecord).filter(HarvestRecord.voided_at.isnot(None)).order_by(HarvestRecord.id).all():
        ws.append([cut.id, cut.planting_record_id, cut.production_batch_id, local_date(cut.harvested_at),
                   _n(cut.quantity_harvested), _n(cut.discard_qty), _n(cut.total_output),
                   local_date(cut.voided_at), cut.voided_by_id, cut.void_reason])
    _style_sheet(ws)

    # Única base sumable de comparación: una fila por segmento, nunca por visita.
    ws = wb.create_sheet("Conciliacion")
    ws.append(["ID SIEMBRA", "ID LOTE PRODUCTIVO", "ID CICLO MESA", "CAMA", "LADO", "VARIEDAD", "LOTE ORIGEN", "PROVEEDOR", "CONTENEDOR", "FECHA SIEMBRA", "SEMBRADOS", "FACTOR TALLOS/UNIDAD", "TALLOS ESPERADOS", "VENTA", "DESCARTE", "SALIDA TOTAL", "MERMA POTENCIAL JUSTIFICADA", "MOTIVO MERMA", "SALDO TECNICO", "EXCEDENTE", "ESTADO LOTE"])
    for batch in db.query(ProductionBatch).order_by(ProductionBatch.id).all():
        lot = db.query(InventoryLot).filter(InventoryLot.id == batch.inventory_lot_id).first()
        supplier = _supplier_for_lot(db, lot) if lot else None
        product = _product(db, lot, batch.sap_product_id)
        bed = _space(db, batch.bed_space_id)
        for segment in batch_segments(db, batch):
            ws.append([segment["id"], batch.id, batch.bed_cycle_id, bed.code if bed else None, segment["side"],
                       lot.variety_snapshot if lot else product.variety if product else None,
                       lot.supplier_lot_code if lot else None, supplier.code if supplier else None, lot.container_number if lot else None,
                       local_date(segment["planted_at"]), _n(segment["quantity_planted"]), _n(batch.expected_stems_per_bulb),
                       _n(segment["expected_stems"]), _n(segment["sale"]), _n(segment["discarded"]), _n(segment["harvested"]),
                       _n(segment["loss_quantity"]), segment["loss_reason"], _n(segment["remaining"]), _n(segment["overage"]), batch.status])
    _style_sheet(ws)

    # COMANDOS familiar: columnas separadas por una columna vacía.
    ws = wb.create_sheet("COMANDOS")
    crop = db.query(FlowerCrop).filter(FlowerCrop.code == "LIRIO").first()
    columns = []
    for label, category in COMMAND_CATEGORIES:
        if category == "greenhouse":
            values = [x.name for x in db.query(ProductionSpace).filter(ProductionSpace.type == "greenhouse", ProductionSpace.status == "active").order_by(ProductionSpace.name).all()]
        elif category == "supplier":
            values = [x.code for x in db.query(Supplier).filter(Supplier.active.is_(True)).order_by(Supplier.code).all()]
        else:
            values = [x.label for x in db.query(CropCatalogValue).filter(CropCatalogValue.crop_id == crop.id, CropCatalogValue.category == category, CropCatalogValue.active.is_(True)).order_by(CropCatalogValue.sort_order, CropCatalogValue.label).all()] if crop else []
        columns.append((label, values))
    max_len = max([len(v) for _, v in columns] + [1])
    for i, (label, values) in enumerate(columns):
        col = 2 + i * 2
        ws.cell(1, col, label)
        for row_idx, value in enumerate(values, start=2):
            ws.cell(row_idx, col, value)
    for cell in ws[1]:
        if cell.value:
            cell.font = Font(bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor="107C5C")
    for col in range(1, ws.max_column + 1):
        ws.column_dimensions[ws.cell(1, col).column_letter].width = 22
    ws.freeze_panes = "A2"

    bio = BytesIO(); wb.save(bio); return bio.getvalue()


def build_excel_bundle(db: Session) -> bytes:
    data = build_lirio_workbook(db)
    bio = BytesIO()
    with ZipFile(bio, "w", ZIP_DEFLATED) as zf:
        zf.writestr("Lirio.xlsx", data)
        zf.writestr("Liriodoc.xlsx", data)
    return bio.getvalue()
