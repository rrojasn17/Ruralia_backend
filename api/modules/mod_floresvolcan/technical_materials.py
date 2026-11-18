"""Referencias de planificación para Lilium de corte; no equivalen a validación local."""

import json
import math
import re
from decimal import Decimal
from .material_defaults import key
from .models import ModuleSetting

GUIDE = "https://onings.com/wp-content/uploads/Lilium-Forcing-Guide-English.pdf"
# Tabla 5, página impresa 24. Valores mínimos del rango: criterio conservador.
DENSITIES = {
    "LA": {14: (45, 55), 16: (40, 50), 18: (35, 45), 20: (30, 40)},
    "OT": {14: (55, 65), 16: (45, 55), 18: (40, 50), 20: (35, 45)},
    "OR": {16: (40, 50), 18: (35, 45), 20: (30, 40), 22: (25, 35)},
}
# Tabla 9, página impresa 30; envolvente estacional en días.
CYCLES = {"LA": (55, 110), "OT": (60, 125), "OR": (75, 135)}
CULTIVARS = {
    "EREMO": (95, 95, "https://www.jandewitenzonen.com/en/assortment/lily/1223/Eremo/"),
    "IDAHO": (90, 90, "https://www.jandewitenzonen.com/en/assortment/lily/1905/Idaho/"),
    "ROSELILY ANGELA": (
        105,
        115,
        "https://www.flamingoholland.com/wholesale-flower-bulbs/roselilies/roselily-angela",
    ),
    "ROSELILY VIOLA": (
        110,
        120,
        "https://www.flamingoholland.com/wholesale-flower-bulbs/roselilies/roselily-viola",
    ),
    "STARLILY ALVARO": (91, 98, "https://www.zaboplant.nl/products/44313-Alvaro"),
}


def reference_for(product):
    variety = key(product.variety)
    family = key(product.common_code)
    if variety in {"ROSELILY ANGELA", "ROSELILY VIOLA"}:
        family = "OR"
    if variety == "STARLILY ALVARO":
        family = "OT"
    updates = {}
    evidence = {}
    pending = []
    if product.default_density is None:
        size = key(product.caliber).replace(",", ".")
        match = re.fullmatch(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)", size)
        if match and family in DENSITIES:
            low, high = map(float, match.groups())
            # Solo intervalos contenidos en un calibre documentado, sin extrapolar.
            bucket = next(
                (
                    upper
                    for upper in sorted(DENSITIES[family])
                    if upper - 2 <= low < high <= upper
                ),
                None,
            )
            if bucket:
                bounds = DENSITIES[family][bucket]
                updates["default_density"] = Decimal(bounds[0])
                evidence["default_density"] = {
                    "source": GUIDE + "#page=24",
                    "range": bounds,
                    "unit": "bulbos/m² netos de cama",
                    "method": f"Mínimo del rango de referencia {family}, calibre {bucket - 2}/{bucket}; no es una medición local",
                    "estimated": True,
                }
        if "default_density" not in updates:
            pending.append(
                "Densidad: calibre/grupo no cubierto o bulbo DN; requiere ficha del proveedor"
            )
    if product.default_cycle_weeks is None:
        source = CULTIVARS.get(variety)
        if source:
            lo, hi, url = source
            updates["default_cycle_weeks"] = math.ceil((lo + hi) / 14)
            evidence["default_cycle_weeks"] = {
                "source": url,
                "range_days": [lo, hi],
                "method": "Punto medio de días publicados, dividido entre siete y redondeado hacia arriba",
                "estimated": True,
            }
        elif family in CYCLES:
            lo, hi = CYCLES[family]
            updates["default_cycle_weeks"] = math.ceil((lo + hi) / 14)
            evidence["default_cycle_weeks"] = {
                "source": GUIDE + "#page=30",
                "range_days": [lo, hi],
                "method": f"Estimación de grupo {family}, punto medio de la envolvente estacional, no ficha específica de variedad",
                "estimated": True,
            }
        else:
            pending.append("Ciclo: grupo no documentado")
    return {
        "updates": updates,
        "evidence": evidence,
        "pending": pending,
        "family": family,
        "summary": "Referencia técnica de planificación para flor de corte, pendiente de validación local. Densidad por m² neto, no por superficie total. Ciclo desde siembra, sin incluir refrigeración previa. Luz, temperatura, suelo, variedad y estado del bulbo modifican el resultado.",
    }


def references(db, ids):
    prefix = "technical_material."
    return {
        int(row.key.removeprefix(prefix)): json.loads(row.value_text)
        for row in db.query(ModuleSetting).filter(
            ModuleSetting.key.in_([prefix + str(i) for i in ids])
        )
    }
