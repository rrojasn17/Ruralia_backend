"""Historical XLSX preview and conservative preload. No formulas or macros run.

One workbook is authoritative for this preload. The second is provenance only,
never appended. Original rows remain available even when they cannot safely
become operational transactions. Operational orders are explicitly reconstructed.
"""

from __future__ import annotations

import collections
import hashlib
import json
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from . import models as m

SHEETS = ("Entradas", "Salida Bulbo", "Siembra", "Corta")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
TZ = ZoneInfo("America/Costa_Rica")
ZERO = Decimal(0)


def normalized(value):
    return " ".join(
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode()
        .upper()
        .split()
    )


def label(value):
    return str(value if value is not None else "").strip()


def numeric(value, *, blank_zero=False):
    if value is None and blank_zero:
        return ZERO
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def real_date(value):
    if isinstance(value, (int, float)):
        try:
            return (datetime(1899, 12, 30) + timedelta(days=value)).date()
        except (OverflowError, ValueError):
            return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return None


def stamp(value):
    return datetime.combine(value, time(12), tzinfo=TZ).astimezone(timezone.utc)


def week(value):
    y, w, _ = value.isocalendar()
    return f"{y % 100:02d}-{w:02d}"


def read_workbook(path):
    """Read stored cell values, including formula caches, without recalculation."""
    result = {}
    with ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for _, node in ET.iterparse(z.open("xl/sharedStrings.xml"), events=["end"]):
                if node.tag == f"{{{NS['m']}}}si":
                    strings.append(
                        "".join(t.text or "" for t in node.iter(f"{{{NS['m']}}}t"))
                    )
                    node.clear()
        rels = {
            n.attrib["Id"]: n.attrib["Target"]
            for n in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        }
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS):
            name = sheet.attrib["name"]
            if name not in (*SHEETS, "COMANDOS"):
                continue
            target = rels[
                sheet.attrib[
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                ]
            ]
            target = (
                posixpath.normpath("xl/" + target)
                if not target.startswith("/")
                else target[1:]
            )
            rows = []
            for _, node in ET.iterparse(z.open(target), events=["end"]):
                if node.tag != f"{{{NS['m']}}}row":
                    continue
                row = {"row": int(node.attrib["r"])}
                for cell in node.findall("m:c", NS):
                    v = cell.find("m:v", NS)
                    value = v.text if v is not None else None
                    kind = cell.attrib.get("t")
                    if kind == "s" and value is not None:
                        value = strings[int(value)]
                    elif kind == "inlineStr":
                        value = "".join(
                            t.text or "" for t in cell.findall(".//m:t", NS)
                        )
                    elif value is not None and kind not in {"str", "e"}:
                        try:
                            value = (
                                float(value)
                                if "." in value or "E" in value.upper()
                                else int(value)
                            )
                        except ValueError:
                            pass
                    if value is not None:
                        row[re.sub(r"\d", "", cell.attrib["r"])] = value
                if len(row) > 1:
                    rows.append(row)
                node.clear()
            result[name] = rows
    missing = set(SHEETS) - result.keys()
    if missing:
        raise ValueError("Faltan hojas: " + ", ".join(sorted(missing)))
    return result


@dataclass
class SourceRow:
    sheet: str
    raw: dict
    issues: list[str] = field(default_factory=list)
    links: dict = field(default_factory=dict)
    destination: str | None = None
    origin: int | None = None

    @property
    def number(self):
        return self.raw["row"]

    def hold(self, message):
        if message not in self.issues:
            self.issues.append(message)
        self.destination = None


@dataclass
class ImportPlan:
    path: Path
    digest: str
    rows: dict[str, list[SourceRow]]
    headers: dict
    commands: list[dict]

    def summary(self):
        return {
            "filename": self.path.name,
            "sha256": self.digest,
            "rows": {s: len(self.rows[s]) for s in SHEETS},
            "operational": {
                s: sum(bool(r.destination) for r in self.rows[s]) for s in SHEETS
            },
            "review": {s: sum(not r.destination for r in self.rows[s]) for s in SHEETS},
            "issues": dict(
                collections.Counter(
                    i for rows in self.rows.values() for r in rows for i in r.issues
                )
            ),
            "assumptions": [
                "Copia elegida para explorar; el cliente no ha designado una versión oficial.",
                "Pedidos y solicitudes XL son enlaces reconstruidos, no órdenes originales del Excel.",
                "No se conocen precios, identificaciones, fechas de ingreso del personal ni limpiezas.",
                "Factor 1 usado solo como referencia de exploración; BL y rendimiento aún requieren confirmación.",
                "Saldos históricos calculados; no son un inventario físico confirmado.",
            ],
            "headers": self.headers,
        }


def prepare_import(path, *, today=None):
    path = Path(path)
    today = today or datetime.now(TZ).date()
    book = read_workbook(path)
    rows = {s: [SourceRow(s, r) for r in book[s] if r["row"] > 1] for s in SHEETS}
    plan = ImportPlan(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        rows,
        {s: {k: v for k, v in book[s][0].items() if k != "row"} for s in SHEETS},
        book.get("COMANDOS", []),
    )
    by_code = collections.defaultdict(list)
    by_identity = collections.defaultdict(list)
    receipts = {}
    for r in rows["Entradas"]:
        v = r.raw
        by_code[normalized(v.get("A"))].append(r)
        by_identity[tuple(normalized(v.get(c)) for c in ("G", "B", "C", "I"))].append(r)
        dt, qty = real_date(v.get("E")), numeric(v.get("L"))
        boxes, each = numeric(v.get("J")), numeric(v.get("K"))
        if not all(label(v.get(c)) for c in ("A", "B", "C", "G", "I")):
            r.hold("Entrada sin identidad completa")
        elif dt is None or dt > today:
            r.hold("Fecha de entrada inválida o futura")
        elif (
            qty is None
            or qty <= 0
            or qty != qty.to_integral_value()
            or boxes is None
            or each is None
            or boxes * each != qty
        ):
            r.hold("Entrada: cajas y bulbos no coinciden")
        else:
            r.destination = "inventory_lot"
            receipts[r.number] = r
    outgoing = collections.defaultdict(list)
    for r in rows["Salida Bulbo"]:
        v = r.raw
        candidates = by_code[normalized(v.get("A"))]
        qty, boxes, each = numeric(v.get("M")), numeric(v.get("I")), numeric(v.get("L"))
        dt = real_date(v.get("K"))
        if len(candidates) != 1 or not candidates[0].destination:
            r.hold("Salida sin entrada única y validada")
        elif (
            qty is None
            or qty <= 0
            or qty != qty.to_integral_value()
            or boxes is None
            or each is None
            or boxes * each != qty
        ):
            r.hold("Salida: cajas y bulbos no coinciden")
        elif dt is None or dt > today or dt < real_date(candidates[0].raw.get("E")):
            r.hold("Fecha de salida incompatible con la recepción")
        else:
            r.origin = candidates[0].number
            r.destination = "delivery"
            outgoing[r.origin].append(r)
    for origin, group in outgoing.items():
        if sum((numeric(r.raw["M"]) for r in group), ZERO) > numeric(
            receipts[origin].raw["L"]
        ):
            for r in group:
                r.hold("Las salidas del origen superan lo recibido")
    sowings = collections.defaultdict(list)
    by_key = collections.defaultdict(list)
    for r in rows["Siembra"]:
        v = r.raw
        by_key[normalized(v.get("A"))].append(r)
        candidates = by_identity[
            tuple(normalized(v.get(c)) for c in ("B", "I", "O", "G"))
        ]
        dt, qty, density = (
            real_date(v.get("M")),
            numeric(v.get("J")),
            numeric(v.get("P")),
        )
        cycle = numeric(v.get("K"))
        if len(candidates) != 1 or not candidates[0].destination:
            r.hold("Siembra sin origen único: lote, variedad, calibre y proveedor")
        elif (
            not all(label(v.get(c)) for c in ("C", "E", "L"))
            or len(label(v.get("D"))) > 4
        ):
            r.hold("Siembra sin ubicación o semana")
        elif (
            qty is None
            or qty <= 0
            or qty != qty.to_integral_value()
            or density is None
            or density <= 0
            or cycle is None
            or not 1 <= cycle <= 52
            or cycle != cycle.to_integral_value()
        ):
            r.hold("Cantidad, densidad o ciclo de siembra inválidos")
        elif (
            dt is None
            or dt > today
            or dt < real_date(candidates[0].raw.get("E"))
            or week(dt) != label(v.get("L"))
        ):
            r.hold("Fecha y semana de siembra requieren revisión")
        else:
            r.origin = candidates[0].number
            r.destination = "planting"
            sowings[r.origin].append(r)
    for origin, group in sowings.items():
        delivered = sum(
            (numeric(r.raw["M"]) for r in outgoing[origin] if r.destination), ZERO
        )
        if sum((numeric(r.raw["J"]) for r in group), ZERO) > delivered:
            for r in group:
                r.hold(
                    "No hay suficiente salida validada para respaldar estas siembras"
                )
    cut_groups = collections.defaultdict(list)
    signatures = collections.defaultdict(list)
    for r in rows["Corta"]:
        signatures[
            json.dumps({k: v for k, v in r.raw.items() if k != "row"}, sort_keys=True)
        ].append(r)
    duplicates = {
        r.number for group in signatures.values() if len(group) > 1 for r in group
    }
    for r in rows["Corta"]:
        v = r.raw
        candidates = by_key[normalized(v.get("A"))]
        dt = real_date(v.get("M"))
        counts = [
            numeric(v.get(c), blank_zero=True) for c in ("N", "P", "Q", "R", "S", "T")
        ]
        if len(candidates) != 1 or not candidates[0].destination:
            r.hold("Corta sin siembra única y validada")
        elif r.number in duplicates:
            r.hold("Posible corta duplicada; conservar ambas para revisión")
        elif any(q is None or q != q.to_integral_value() for q in counts):
            r.hold("Corta con texto o conteo inválido")
        elif sum(counts[1:4]) != counts[4] or counts[0] + counts[4] != counts[5]:
            r.hold("Los componentes y totales de la corta no coinciden")
        elif (
            dt is None
            or dt > today
            or dt < real_date(candidates[0].raw.get("M"))
            or week(dt) != label(v.get("L"))
        ):
            r.hold("Fecha de corta incompatible con la siembra")
        elif any(
            normalized(v.get(a)) != normalized(candidates[0].raw.get(b))
            for a, b in (("B", "B"), ("C", "C"), ("D", "E"), ("F", "G"), ("H", "I"))
        ):
            r.hold("La llave y los datos visibles de la corta no coinciden")
        else:
            r.origin = candidates[0].number  # planting source row, not inventory row
            r.destination = "harvest"
            cut_groups[r.origin].append(r)
    for sowing, group in cut_groups.items():
        source = by_key[normalized(group[0].raw["A"])][0]
        if sum((numeric(r.raw["T"]) for r in group), ZERO) > numeric(source.raw["J"]):
            for r in group:
                r.hold(
                    "Corta acumulada supera el factor de referencia; no se autoriza automáticamente"
                )
    return plan


def _code(prefix, *parts):
    return (
        prefix
        + hashlib.sha256("|".join(normalized(p) for p in parts).encode())
        .hexdigest()[:16]
        .upper()
    )


def preload(db: Session, plan: ImportPlan, *, comparison=None):
    """Populate an EMPTY module in a single caller-controlled transaction."""
    if (
        db.query(m.InventoryLot.id).first()
        or db.query(m.ExcelImportRun.id).first()
        or db.query(m.PlantingOrder.id).first()
    ):
        raise ValueError(
            "La precarga requiere datos operativos vacíos; no se mezcla con registros existentes"
        )
    run = m.ExcelImportRun(
        filename=plan.path.name,
        sha256=plan.digest,
        summary_json="{}",
        comparison_filename=Path(comparison).name if comparison else None,
        comparison_sha256=hashlib.sha256(Path(comparison).read_bytes()).hexdigest()
        if comparison
        else None,
    )
    db.add(run)
    db.flush()
    crop = db.query(m.FlowerCrop).filter_by(code="LIRIO").one_or_none()
    if crop is None:
        crop = m.FlowerCrop(code="LIRIO", name="Lirios", is_default=True)
        db.add(crop)
        db.flush()
    catalog = {
        (r.category, normalized(r.label)): r
        for r in db.query(m.CropCatalogValue).filter_by(crop_id=crop.id)
    }
    suppliers = {normalized(r.code): r for r in db.query(m.Supplier)}
    products = {}
    spaces = {normalized(r.code): r for r in db.query(m.ProductionSpace)}
    workers, crews = {}, {}
    root = db.query(m.ProductionSpace).filter_by(code="LIRIO-ROOT").first()
    if root is None:
        root = m.ProductionSpace(
            code="LIRIO-ROOT", name="Lirios", type="farm", status="active"
        )
        db.add(root)
        db.flush()

    def catalog_value(category, value):
        value = label(value)
        if not value:
            return None
        key = (category, normalized(value))
        if key not in catalog:
            row = m.CropCatalogValue(
                crop_id=crop.id,
                category=category,
                code=normalized(value),
                label=value,
                source="excel",
            )
            db.add(row)
            db.flush()
            catalog[key] = row
        return catalog[key]

    def supplier(value):
        key = normalized(value)
        if key not in suppliers:
            row = m.Supplier(code=key[:32], name=label(value), country="Por confirmar")
            db.add(row)
            db.flush()
            suppliers[key] = row
        return suppliers[key]

    def product(variety, caliber, common=None, color=None):
        key = tuple(normalized(v) for v in (variety, caliber, common, color))
        if key not in products:
            cats = [
                catalog_value(c, v)
                for c, v in zip(
                    ("variety", "caliber", "common", "color"),
                    (variety, caliber, common, color),
                )
            ]
            short_code = _code("XL-", *key)[:8]
            salt = 0
            while db.query(m.SapProduct.id).filter(m.SapProduct.code == short_code).first():
                salt += 1
                short_code = _code("XL-", *key, str(salt))[:8]
            row = m.SapProduct(
                crop_id=crop.id,
                code=short_code,
                description=f"{label(variety)} · {label(caliber)}",
                variety=label(variety),
                caliber=label(caliber),
                common_code=label(common) or None,
                color=label(color) or None,
                variety_catalog_id=cats[0].id if cats[0] else None,
                caliber_catalog_id=cats[1].id if cats[1] else None,
                common_catalog_id=cats[2].id if cats[2] else None,
                color_catalog_id=cats[3].id if cats[3] else None,
                purchase_unit="caja",
                inventory_unit="bulbo",
                unit="bulbo",
                expected_stems_per_bulb=1,
            )
            db.add(row)
            db.flush()
            products[key] = row
        return products[key]

    def bed(greenhouse, number):
        gh_code = "LIRIO-GH-" + normalized(greenhouse).replace(" ", "-")
        if gh_code not in spaces:
            row = m.ProductionSpace(
                code=gh_code,
                name=label(greenhouse),
                type="greenhouse",
                parent_id=root.id,
                status="active",
            )
            db.add(row)
            db.flush()
            spaces[gh_code] = row
        gh = spaces[gh_code]
        key = f"{gh_code}-BED-{label(number)}"
        if key not in spaces:
            row = m.ProductionSpace(
                code=key,
                name=f"Mesa {label(number)} · {label(greenhouse)}",
                type="bed",
                parent_id=gh.id,
                status="active",
            )
            db.add(row)
            db.flush()
            spaces[key] = row
        return gh, spaces[key]

    def crew(value):
        text = label(value) or "Responsable no informado"
        key = normalized(text)
        if key not in crews:
            row = m.WorkCrew(name=f"Excel · {text}"[:150], active=True)
            db.add(row)
            db.flush()
            crews[key] = row
            if value:
                catalog_value("legacy_planter", value)
                for name in dict.fromkeys(
                    label(p) for p in re.split(r"-+", text) if label(p)
                ):
                    worker_key = normalized(name)
                    if worker_key not in workers:
                        worker = m.Worker(
                            employee_code=_code("XL-", name),
                            identification=None,
                            name=name,
                            hire_date=None,
                            job_title="Excel · datos personales pendientes",
                        )
                        db.add(worker)
                        db.flush()
                        workers[worker_key] = worker
                    db.add(
                        m.WorkCrewMember(
                            crew_id=row.id, worker_id=workers[worker_key].id
                        )
                    )
        return crews[key]

    unknown = crew(None)
    for r in plan.rows["Siembra"]:
        v = r.raw
        crew(v.get("R"))
        for category, col in (("density", "P"), ("cycle_weeks", "K"), ("side", "D")):
            catalog_value(category, v.get(col))
        if label(v.get("C")) and label(v.get("E")):
            bed(v["C"], v["E"])
        product(v.get("I"), v.get("O"), v.get("F"), v.get("H"))
    lots, requisitions, cycles, batches = {}, {}, {}, {}
    outgoing = collections.defaultdict(list)
    planted = collections.defaultdict(list)
    for r in plan.rows["Salida Bulbo"]:
        if r.destination:
            outgoing[r.origin].append(r)
    for r in plan.rows["Siembra"]:
        if r.destination:
            planted[r.origin].append(r)
    for r in plan.rows["Entradas"]:
        v = r.raw
        sup, sap = supplier(v.get("I")), product(v.get("B"), v.get("C"), v.get("D"))
        if not r.destination:
            continue
        dt, qty = real_date(v["E"]), numeric(v["L"])
        boxes, each = numeric(v["J"]), numeric(v["K"])
        comment = f"[EXCEL] Reconstruido desde {plan.path.name} · Entradas fila {r.number}. Precio y pedido original no informados."
        po = m.PurchaseOrder(
            number=f"XL-OC-{r.number:05}",
            supplier_id=sup.id,
            warehouse_due_date=dt,
            status="completed",
            comments=comment,
            completed_at=stamp(dt),
            created_at=stamp(dt),
        )
        db.add(po)
        db.flush()
        pol = m.PurchaseOrderLine(
            purchase_order_id=po.id,
            sap_product_id=sap.id,
            product_code_snapshot=sap.code,
            description_snapshot=sap.description,
            variety_snapshot=sap.variety,
            caliber_snapshot=sap.caliber,
            common_code_snapshot=sap.common_code,
            quantity=boxes,
            unit_price_estimated=0,
            purchase_unit_snapshot="caja",
            inventory_unit_snapshot="bulbo",
            units_per_purchase_unit_snapshot=each,
        )
        gr = m.GoodsReceipt(
            number=f"XL-ENT-{r.number:05}",
            purchase_order_id=po.id,
            container_number=label(v.get("F")) or "No informado",
            received_at=stamp(dt),
            comments=comment,
        )
        db.add_all([pol, gr])
        db.flush()
        gl = m.GoodsReceiptLine(
            goods_receipt_id=gr.id,
            purchase_order_line_id=pol.id,
            quantity_received=qty,
            inventory_quantity_received=qty,
            purchase_quantity_received=boxes,
            boxes_received=boxes,
            units_per_box=each,
            supplier_lot_code=label(v["G"]),
            harvest_year=int(v["H"]) if numeric(v.get("H")) else None,
        )
        db.add(gl)
        db.flush()
        delivered = sum((numeric(x.raw["M"]) for x in outgoing[r.number]), ZERO)
        consumed = sum((numeric(x.raw["J"]) for x in planted[r.number]), ZERO)
        lot = m.InventoryLot(
            lot_code=f"XL-LOT-{r.number:05}",
            supplier_lot_code=label(v["G"]),
            sap_product_id=sap.id,
            goods_receipt_line_id=gl.id,
            container_number=gr.container_number,
            harvest_year=gl.harvest_year,
            variety_snapshot=sap.variety,
            caliber_snapshot=sap.caliber,
            common_code_snapshot=sap.common_code,
            boxes_received=boxes,
            units_per_box=each,
            purchase_quantity_received=boxes,
            purchase_unit_snapshot="caja",
            inventory_unit_snapshot="bulbo",
            quantity_received=qty,
            quantity_available=qty - delivered,
            quantity_issued=delivered - consumed,
            quantity_consumed=consumed,
            received_at=stamp(dt),
        )
        db.add(lot)
        db.flush()
        lots[r.number] = lot
        db.add(
            m.InventoryMovement(
                inventory_lot_id=lot.id,
                movement_type="receipt",
                quantity=qty,
                reference_type="goods_receipt",
                reference_id=gr.id,
                comments=comment,
                created_at=stamp(dt),
            )
        )
        r.links = {"inventory_lot_id": lot.id, "purchase_order_id": po.id}
        if delivered:
            order = m.PlantingOrder(
                crop_id=crop.id,
                number=f"XL-OS-{r.number:05}",
                production_space_id=root.id,
                crew_id=unknown.id,
                status="historical",
                comments="[EXCEL] Agrupación técnica por origen. El Excel no identifica una orden ni enlaza cada salida con una mesa. No implica un cierre físico.",
                created_at=stamp(dt),
            )
            db.add(order)
            db.flush()
            req = m.Requisition(
                number=f"XL-REQ-{r.number:05}",
                planting_order_id=order.id,
                status="delivered",
                comments="[EXCEL] Salidas vinculadas al mismo origen; solicitud reconstruida.",
                created_at=stamp(dt),
            )
            db.add(req)
            db.flush()
            line = m.RequisitionLine(
                requisition_id=req.id,
                inventory_lot_id=lot.id,
                sap_product_id=sap.id,
                quantity_requested=delivered,
                quantity_approved=delivered,
                quantity_delivered=delivered,
                quantity_planted=consumed,
            )
            db.add(line)
            db.flush()
            requisitions[r.number] = (order, line)
    for r in plan.rows["Salida Bulbo"]:
        if not r.destination:
            continue
        v = r.raw
        lot = lots[r.origin]
        order, line = requisitions[r.origin]
        delivery = m.RequisitionDelivery(
            number=f"XL-SAL-{r.number:05}",
            requisition_line_id=line.id,
            inventory_lot_id=lot.id,
            boxes=numeric(v["I"]),
            units_per_box=numeric(v["L"]),
            quantity=numeric(v["M"]),
            delivery_week=week(real_date(v["K"])),
            delivered_at=stamp(real_date(v["K"])),
            comments=f"[EXCEL] Salida Bulbo fila {r.number}; semana original {v.get('J')}.",
        )
        db.add(delivery)
        db.flush()
        db.add(
            m.InventoryMovement(
                inventory_lot_id=lot.id,
                movement_type="delivery",
                quantity=delivery.quantity,
                reference_type="requisition_delivery",
                reference_id=delivery.id,
                comments=delivery.comments,
                created_at=delivery.delivered_at,
            )
        )
        r.links = {"requisition_id": line.requisition_id, "delivery_id": delivery.id}
    for r in plan.rows["Siembra"]:
        if not r.destination:
            continue
        v = r.raw
        lot = lots[r.origin]
        order, line = requisitions[r.origin]
        gh, table = bed(v["C"], v["E"])
        dt = real_date(v["M"])
        if table.id not in cycles:
            cycle = m.BedCycle(
                bed_space_id=table.id,
                status="reconciliation",
                opened_at=stamp(dt),
                cleaning_notes="[EXCEL] Histórico agrupado para revisión. No acredita un único ciclo físico ni limpieza.",
            )
            db.add(cycle)
            db.flush()
            cycles[table.id] = cycle
        cycle = cycles[table.id]
        if stamp(dt) < cycle.opened_at:
            cycle.opened_at = stamp(dt)
        batch = m.ProductionBatch(
            code=f"XL-SIE-{r.number:05}",
            bed_cycle_id=cycle.id,
            crop_id=crop.id,
            planting_order_id=order.id,
            sap_product_id=lot.sap_product_id,
            inventory_lot_id=lot.id,
            production_space_id=table.id,
            greenhouse_space_id=gh.id,
            bed_space_id=table.id,
            side=label(v.get("D"))[:4] or None,
            planting_week=label(v["L"]),
            projected_harvest_week=label(v.get("N")),
            cycle_weeks=int(v["K"]),
            expected_stems_per_bulb=1,
            history_review_required=True,
            status="open",
            opened_at=stamp(dt),
        )
        db.add(batch)
        db.flush()
        sow = m.PlantingRecord(
            production_batch_id=batch.id,
            planting_order_id=order.id,
            requisition_line_id=line.id,
            inventory_lot_id=lot.id,
            greenhouse_space_id=gh.id,
            bed_space_id=table.id,
            side=batch.side,
            quantity_planted=numeric(v["J"]),
            density=numeric(v["P"]),
            area_m2=numeric(v["J"]) / numeric(v["P"]),
            cycle_weeks=int(v["K"]),
            planting_week=batch.planting_week,
            projected_harvest_week=batch.projected_harvest_week,
            sealed_at=real_date(v.get("Q")),
            crew_id=crew(v.get("R")).id,
            planted_at=stamp(dt),
            notes=f"[EXCEL] Siembra fila {r.number}. KEY original: {v.get('A')}. Rendimiento y cierre físico pendientes de confirmar.",
        )
        db.add(sow)
        db.flush()
        batches[r.number] = (batch, sow)
        db.add(
            m.InventoryMovement(
                inventory_lot_id=lot.id,
                movement_type="planting",
                quantity=sow.quantity_planted,
                reference_type="planting_record",
                reference_id=sow.id,
                comments=sow.notes,
                created_at=sow.planted_at,
            )
        )
        r.links = {
            "planting_record_id": sow.id,
            "production_batch_id": batch.id,
            "planting_order_id": order.id,
        }
    harvest_orders = {}
    for r in plan.rows["Corta"]:
        if not r.destination:
            continue
        v = r.raw
        batch, sow = batches[r.origin]
        dt = real_date(v["M"])
        if batch.id not in harvest_orders:
            ho = m.HarvestOrder(
                number=f"XL-COS-{r.origin:05}",
                production_batch_id=batch.id,
                crew_id=unknown.id,
                status="completed",
                planned_date=dt,
                completed_at=stamp(dt),
                notes="[EXCEL] Agrupación de cortas históricas; no acredita autorización original ni cierre de mesa.",
            )
            db.add(ho)
            db.flush()
            harvest_orders[batch.id] = ho
        cut = m.HarvestRecord(
            production_batch_id=batch.id,
            planting_record_id=sow.id,
            harvest_order_id=harvest_orders[batch.id].id,
            bed_space_id=sow.bed_space_id,
            crew_id=unknown.id,
            bloom_1_qty=numeric(v.get("P"), blank_zero=True),
            bloom_2_qty=numeric(v.get("Q"), blank_zero=True),
            bloom_3_5_qty=numeric(v.get("R"), blank_zero=True),
            discard_qty=numeric(v.get("N"), blank_zero=True),
            discard_reason=label(v.get("O")) or None,
            quantity_harvested=numeric(v["S"]),
            total_output=numeric(v["T"]),
            actual_harvest_week=label(v.get("L")),
            harvested_at=stamp(dt),
            notes=f"[EXCEL] Corta fila {r.number}. Clases BL conservadas; unidad pendiente de confirmar.",
        )
        db.add(cut)
        db.flush()
        r.links = {
            "harvest_record_id": cut.id,
            "production_batch_id": batch.id,
            "planting_order_id": batch.planting_order_id,
        }
    for rows in plan.rows.values():
        db.add_all(
            [
                m.ExcelImportRow(
                    import_run_id=run.id,
                    sheet=r.sheet,
                    source_row=r.number,
                    raw_json=json.dumps(
                        {k: v for k, v in r.raw.items() if k != "row"},
                        ensure_ascii=False,
                    ),
                    issues_json=json.dumps(r.issues, ensure_ascii=False),
                    links_json=json.dumps(r.links),
                    status="imported" if r.destination else "review",
                    search_text=normalized(" ".join(str(v) for v in r.raw.values())),
                )
                for r in rows
            ]
        )
    summary = plan.summary()
    summary["created"] = {
        "suppliers": len(suppliers),
        "materials": len(products),
        "workers": len(workers),
        "crews": len(crews),
        "inventory_lots": len(lots),
        "planting_segments": len(batches),
        "harvest_orders": len(harvest_orders),
    }
    run.summary_json = json.dumps(summary, ensure_ascii=False)
    if not db.query(m.ModuleSetting).filter_by(key="excel.exploration").first():
        db.add(m.ModuleSetting(key="excel.exploration", value_text="true"))
    if not db.query(m.ModuleSetting).filter_by(key="production_control.v07").first():
        db.add(
            m.ModuleSetting(key="production_control.v07", value_text="excel-preload")
        )
    db.flush()
    return run


def clear_module_data(db: Session):
    """Explicit module-only wipe, excluding access/settings and backup metadata.

    Caller must have written and verified a backup before invoking this function.
    This function does not commit, reset sequences, or touch core/other modules.
    """
    keep = {"fv_module_settings", "fv_backup_runs"}
    tables = {
        t.name: t
        for t in m.Base.metadata.tables.values()
        if t.name.startswith("fv_") and t.name not in keep
    }
    for table in reversed(m.Base.metadata.sorted_tables):
        if table.name in tables:
            db.execute(table.delete())
    db.execute(
        m.ModuleSetting.__table__.delete().where(
            m.ModuleSetting.key.in_(["excel.exploration", "production_control.v07"])
        )
    )
    db.flush()
