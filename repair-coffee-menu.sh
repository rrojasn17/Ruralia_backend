#!/usr/bin/env bash
# Run from the production backend checkout: sudo bash repair-coffee-menu.sh
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

manifest=api/modules/mod_caficultura/module.json
test -f "$manifest"
umask 077
backup=$(mktemp ./caficultura-menu-before.XXXXXXXX.json)

docker compose exec -T api python -c '
import json, sys
from database import SessionLocal
from core.models import AppModule
with SessionLocal() as db:
    row = db.query(AppModule).filter(AppModule.key == "mod_caficultura").one()
    json.dump({"key": row.key, "version": row.version, "source": row.source,
               "status": row.status, "manifest": row.manifest}, sys.stdout,
              ensure_ascii=False, indent=2)
' > "$backup"
printf 'Respaldo de configuración: %s\n' "$backup"

docker compose exec -T api python -c '
import json, sys
from database import SessionLocal
from core.models import AppModule
from core.module_contract import validate_module_contract
from core.module_manager import BUILTIN_MANIFESTS, sync_builtin_modules

desired = json.load(sys.stdin)
if desired.get("key") != "mod_caficultura":
    raise SystemExit("El archivo no corresponde a Caficultura.")
navigation = desired.get("navigation")
expected = ["Clientes", "Proveedores", "Insumos", "Informes", "Notas rápidas"]
if not isinstance(navigation, list) or [item.get("label") for item in navigation
        if item.get("group") == "Producción"] != expected:
    raise SystemExit("El repositorio no contiene el nuevo menú de Producción. Actualícelo primero.")

with SessionLocal() as db:
    row = db.query(AppModule).filter(AppModule.key == "mod_caficultura").one()
    runtime = BUILTIN_MANIFESTS.get(row.key)
    if row.status != "active":
        raise SystemExit("Caficultura no es el módulo activo; no se modificó nada.")
    if not runtime or str(row.version) != str(runtime.get("version")):
        raise SystemExit("Catálogo y runtime tienen versiones distintas; actualice el backend primero.")
    updated = dict(row.manifest or {})
    updated["navigation"] = navigation
    validate_module_contract(updated)
    row.manifest = updated
    # Same policy as a manifest imported through Administration > Modules.
    # Preserve this navigation even when the running image still has the old menu.
    row.source = "upload"
    db.commit()
    sync_builtin_modules(db)
    db.expire_all()
    row = db.query(AppModule).filter(AppModule.key == "mod_caficultura").one()
    if (row.manifest or {}).get("navigation") != navigation:
        raise SystemExit("El backend volvió a reemplazar el menú. Revise su versión antes de continuar.")
    print("Menú de Caficultura actualizado y verificado. Recargue la página.")
    print("Producción: " + " > ".join(expected))
'
