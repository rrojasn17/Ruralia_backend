"""Completa datos inequívocos del instrumento original, sin recalcular historia."""

import json
from functools import lru_cache
import unicodedata
from pathlib import Path
from decimal import Decimal
from .models import SapProduct, CropCatalogValue, ModuleSetting

COLOR_NAMES = {
    "OR": "Naranja",
    "PK": "Rosado",
    "RE": "Rojo",
    "WH": "Blanco",
    "YL": "Amarillo",
    "WK": "Blanco (WK)",
}


def key(value):
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", str(value or "")).upper()
        if not unicodedata.combining(c)
    ).strip()


@lru_cache(maxsize=1)
def material_profiles():
    return json.loads(
        Path(__file__).with_name("material_workbook_defaults.json").read_text()
    )["profiles"]


def complete_material(product):
    profile = next(
        (
            p
            for p in material_profiles()
            if (key(p["variety"]), key(p["caliber"]))
            == (key(product.variety), key(product.caliber))
        ),
        None,
    )
    if profile:
        if (
            product.default_cycle_weeks is None
            and len(profile["cycles"]) == 1
            and 1 <= profile["cycles"][0] <= 52
        ):
            product.default_cycle_weeks = int(profile["cycles"][0])
        if product.default_density is None and len(profile["densities"]) == 1:
            product.default_density = Decimal(str(profile["densities"][0]))


def upgrade_material_defaults(db):
    marker = db.query(ModuleSetting).filter_by(key="material_defaults.v11").first()
    if marker:
        return
    source = material_profiles()
    profiles = {(key(p["variety"]), key(p["caliber"])): p for p in source}
    for color in db.query(CropCatalogValue).filter(
        CropCatalogValue.category == "color"
    ):
        code = key(color.code)
        if code in COLOR_NAMES and key(color.label) == code:
            color.label = COLOR_NAMES[code]
    for product in db.query(SapProduct):
        profile = profiles.get((key(product.variety), key(product.caliber)))
        if not profile:
            continue
        if product.default_cycle_weeks is None and len(profile["cycles"]) == 1:
            cycle = profile["cycles"][0]
            if int(cycle) == cycle and 1 <= cycle <= 52:
                product.default_cycle_weeks = int(cycle)
        if product.default_density is None and len(profile["densities"]) == 1:
            product.default_density = Decimal(str(profile["densities"][0]))
        # Solo completar texto cuando una única clasificación está documentada.
        if not product.color and len(profile["colors"]) == 1:
            product.color = profile["colors"][0]
    db.add(
        ModuleSetting(
            key="material_defaults.v11",
            value_text="Lirio.xlsx · Siembra; datos no ambiguos",
        )
    )
    db.commit()
