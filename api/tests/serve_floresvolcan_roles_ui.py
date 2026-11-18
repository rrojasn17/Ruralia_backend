"""Isolated UI fixture: fictional accounts, temporary SQLite, local port 8059.

The X-FV-Test-Role header selects a fictional identity ONLY in this test app.
Never include this server or its identity override in the application runtime.
"""

import os, sys, json, uuid, io, sqlite3, tempfile
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["APP_ENV"] = "local"
os.environ["OPENAI_API_KEY"] = ""
os.environ["FV_PAPER_STORAGE_DIR"] = "/tmp/fv-v09-paper"
os.environ["NAVIA_UPLOAD_DIR"] = "/tmp/fv-v09-uploads"
api = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(api), str(api / "tests")]
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from core.models import AppModule, Usuario
from core.routers import admin, auth
from core.routers.auth import get_current_user
from modules.mod_floresvolcan.manifest import MODULE_MANIFEST
from modules.mod_floresvolcan.models import PaperCapture
from modules.mod_floresvolcan.paper_forms import normalize_scan
from test_floresvolcan_reconciliation_v07 import fv
from PIL import Image
import uvicorn

records = fv.__wrapped__()
state = next(records)
state.db.add(
    AppModule(
        key="mod_floresvolcan",
        name="Flores",
        version=MODULE_MANIFEST["version"],
        module_type="industry",
        status="active",
        manifest=MODULE_MANIFEST,
        built_in=True,
    )
)
state.db.commit()
app = state.client.app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3049"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
planted = state.plant(1000)
assert planted.status_code == 201

users = {}
for role in ["superadmin", "gerente", "admin"]:
    u = Usuario(
        nombre=role,
        correo=f"{role}@example.com",
        hash_contrasena="test",
        rol="gerente" if role == "superadmin" else role,
        is_superadmin=role == "superadmin",
    )
    state.db.add(u)
    state.db.flush()
    users[role] = u
state.user.is_superadmin = False
state.user.rol = "trabajador"
users["trabajador"] = state.user
image = io.BytesIO()
Image.new("RGB", (400, 600), "#f2f9f2").save(image, format="PNG")
content, digest = normalize_scan(image.getvalue())
folder = Path("/tmp/fv-v09-paper")
folder.mkdir(exist_ok=True)
(folder / "review.jpg").write_bytes(content)
submission = {
    "client_token": "paper-browser-test",
    "paper_capture_id": 1,
    "planting_order_id": state.order.id,
    "requisition_line_id": state.line.id,
    "bed_space_id": state.bed.id,
    "quantity_planted": 10,
    "density": state.product.default_density,
    "cycle_weeks": state.product.default_cycle_weeks,
    "planted_at": "2026-01-05T12:00:00-06:00",
    "notes": "Browser handwriting approval",
}
state.db.add(
    PaperCapture(
        capture_key=uuid.uuid4().hex,
        operation="planting",
        planting_order_id=state.order.id,
        storage_key="review.jpg",
        sha256=digest,
        mime_type="image/jpeg",
        status="submitted",
        created_by_id=state.user.id,
        suggested_json=json.dumps({"submission": submission}),
    )
)
state.db.commit()
# Optional volume fixture; all records stay in this temporary test database.
if os.getenv("FV_TEST_PERFORMANCE") == "1":
    from datetime import date, datetime, timezone
    from modules.mod_floresvolcan import models as m
    for i in range(125):
        bed = m.ProductionSpace(code=f"PERF-{i:03d}", name=f"Mesa remota {i:03d}", type="bed", status="active", capacity=100)
        product = m.SapProduct(code=f"PERF-{i:03d}", description=f"Material remoto {i:03d}", variety=f"Variedad remota {i:03d}")
        state.db.add_all([bed, product]); state.db.flush()
        state.db.add(m.InventoryLot(lot_code=f"PERF-{i:03d}", sap_product_id=product.id,
            goods_receipt_line_id=state.lot.goods_receipt_line_id, quantity_received=10, quantity_available=10,
            received_at=datetime(2020,1,1,tzinfo=timezone.utc)))
        order=m.PlantingOrder(number=f"PERF-{i:03d}",production_space_id=bed.id,crew_id=state.order.crew_id,status="approved")
        state.db.add(order); state.db.flush()
        state.db.add(m.Requisition(number=f"PERF-{i:03d}",planting_order_id=order.id,status="pending"))
        state.db.add(m.PurchaseOrder(number=f"PERF-{i:03d}",supplier_id=state.db.query(m.Supplier).first().id,
            warehouse_due_date=date(2026,12,1),status="in_process"))
        state.db.add(m.ProductionBatch(code=f"PERF-{i:03d}",planting_order_id=order.id,sap_product_id=product.id,
            production_space_id=bed.id,bed_space_id=bed.id,status="open"))
    state.db.commit()

if os.getenv("FV_TEST_V11") == "1":
    from modules.mod_floresvolcan.models import Worker, PlantingOrder
    state.db.query(Worker).filter(Worker.id==1).one().employee_code="2468"
    state.db.query(PlantingOrder).filter(PlantingOrder.id==state.order.id).one().qr_token="v11-browser-link-"+"a"*32
    state.db.commit()

# Separate connections for concurrent browser requests; StaticPool is for serial unit tests.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import get_db

handle, dbfile = tempfile.mkstemp(prefix="fv-roles-ui-", suffix=".sqlite")
os.close(handle)
raw = state.engine.raw_connection()
with sqlite3.connect(dbfile) as target:
    raw.driver_connection.backup(target)
raw.close()
engine = create_engine("sqlite:///" + dbfile, connect_args={"check_same_thread": False})
factory = sessionmaker(bind=engine, expire_on_commit=False)


def session_override():
    with factory() as db:
        yield db


app = state.client.app
app.dependency_overrides[get_db] = session_override
app.include_router(auth.router)
app.include_router(admin.router)


@app.get("/__fv_roles_test__")
def isolated_marker():
    return {"isolated_fv_roles": True, "performance_fixture": os.getenv("FV_TEST_PERFORMANCE") == "1", "v11_fixture": os.getenv("FV_TEST_V11") == "1"}


def identity(request: Request):
    return users.get(request.headers.get("x-fv-test-role"), users["superadmin"])


app.dependency_overrides[get_current_user] = identity

try:
    uvicorn.run(app, host="127.0.0.1", port=8059)
finally:
    engine.dispose()
    Path(dbfile).unlink(missing_ok=True)
    try:
        next(records)
    except StopIteration:
        pass
