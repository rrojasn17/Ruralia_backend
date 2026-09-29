from datetime import date, datetime, timezone
import uuid

from modules.mod_caficultura.model_iot import IoTNode, IoTReading
from modules.mod_caficultura.models import (
    ActividadFinca,
    Cliente,
    Finca,
    InsumoFinca,
    ReciboCafe,
    RegistroFinca,
    RegistroFincaInsumo,
    RegistroFincaTrabajador,
    TrabajadorFinca,
)
from modules.mod_caficultura.reports import ReportRequest, build_report


def test_reports_use_registered_costs_sales_workers_and_humidity(db_session):
    suffix = uuid.uuid4().hex[:8]
    owner = Cliente(
        codigo=f"CLI-{suffix}",
        nombre_completo="Productor informes",
        tipo_persona="personal",
        categoria="agro",
        numero_identificacion=suffix,
        activo=True,
    )
    db_session.add(owner)
    db_session.flush()

    farm = Finca(
        codigo=f"FIN-{suffix}",
        cliente_id=owner.id,
        nombre=f"Finca informes {suffix}",
        activa=True,
        gestion_fincas_habilitada=True,
    )
    activity = ActividadFinca(nombre=f"Fertilización {suffix}", tipo="fertilizacion", activa=True)
    worker = TrabajadorFinca(nombre=f"Juan Informes {suffix}", activo=True)
    supply = InsumoFinca(nombre=f"Abono {suffix}", tipo="fertilizante", unidad="kg", stock_actual=10, activo=True)
    db_session.add_all([farm, activity, worker, supply])
    db_session.flush()

    record = RegistroFinca(
        fecha=date(2026, 9, 1),
        semana_inicio=date(2026, 8, 31),
        semana_fin=date(2026, 9, 5),
        finca_id=farm.id,
        actividad_id=activity.id,
        estado="finalizado",
    )
    db_session.add(record)
    db_session.flush()
    db_session.add(RegistroFincaTrabajador(
        registro_id=record.id,
        trabajador_id=worker.id,
        nombre_snapshot=worker.nombre,
        horas=8,
        jornal=12000,
        costo=12000,
    ))
    db_session.add(RegistroFincaInsumo(
        registro_id=record.id,
        insumo_id=supply.id,
        nombre_snapshot=supply.nombre,
        cantidad=2,
        unidad="kg",
        costo_unitario=1000,
        costo_total=2000,
    ))
    db_session.add(ReciboCafe(
        numero_recibo=f"REC-{suffix}",
        fecha=date(2026, 9, 10),
        finca_id=farm.id,
        productor_nombre=owner.nombre_completo,
        cajuelas=20,
        cuartillos=0,
        precio_fanega=100000,
        estado="recibido",
        liquidado=False,
    ))
    node = IoTNode(finca_id=farm.id, nombre=f"Sensor {suffix}", did=f"DID-{suffix}", tipo="ttn", activo=True)
    db_session.add(node)
    db_session.flush()
    db_session.add(IoTReading(
        node_id=node.id,
        recorded_at=datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
        data={"humedad_suelo": 55.5},
        source="manual",
    ))
    db_session.commit()

    profitability = build_report(db_session, ReportRequest(
        kind="profitability", farm_id=farm.id, start=date(2026, 9, 1), end=date(2026, 9, 30)
    ))
    metric_values = {item["label"]: item["value"] for item in profitability["metrics"]}
    assert metric_values["Costo registrado"] == 14000.0
    assert metric_values["Valor de ventas registradas"] == 100000.0

    workers = build_report(db_session, ReportRequest(
        kind="workers", worker=worker.nombre, start=date(2026, 9, 1), end=date(2026, 9, 30)
    ))
    assert workers["metrics"][0]["value"] == 8.0

    humidity = build_report(db_session, ReportRequest(
        kind="humidity", farm_id=farm.id, start=date(2026, 9, 1), end=date(2026, 9, 30)
    ))
    charts = [component for component in humidity["components"] if component["type"] == "chart"]
    assert charts[0]["variant"] == "line"
    assert charts[0]["items"][0]["value"] == 55.5
