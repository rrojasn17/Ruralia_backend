"""Controles aditivos del módulo Flores Volcán 0.6.0; no toca Café.

Se prueban modelos, permisos declarados y papelería sin importar el router
cuando el entorno de pruebas no dispone de todas las dependencias del backend.
"""
from __future__ import annotations

import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import inspect

from core import models as core_models  # registra FK navia_usuarios antes del fixture SQLAlchemy
from modules.mod_floresvolcan import lifecycle, models
from modules.mod_floresvolcan.paper_forms import build_field_pdf, normalize_scan, suggest_photo_data
from modules.mod_floresvolcan.schemas import HarvestRecordIn, SapProductIn


def test_floresvolcan_manifest_restricts_worker():
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'modules/mod_floresvolcan/module.json').read_text())
    assert manifest['key'] == 'mod_floresvolcan'
    worker = set(manifest['role_permissions']['trabajador'])
    assert {'floresvolcan:field:use', 'floresvolcan:planting:register', 'floresvolcan:harvest:register'} <= worker
    assert not any(p.startswith('floresvolcan:inventory:') for p in worker)
    assert 'floresvolcan:workers:view' not in worker
    assert 'floresvolcan:dashboard:view' not in worker


def test_floresvolcan_install_is_idempotent_and_creates_only_fv_tables(db_session):
    lifecycle.install(db_session)
    inspector = inspect(db_session.get_bind())
    names = set(inspector.get_table_names())
    assert {'fv_harvest_orders', 'fv_paper_captures', 'fv_planting_records', 'fv_harvest_records'} <= names
    before = db_session.query(models.CropCatalogValue).count()
    assert before > 100  # valores históricos derivados de los Excel
    lifecycle.install(db_session)
    assert db_session.query(models.CropCatalogValue).count() == before
    assert {x['name'] for x in inspector.get_columns('fv_sap_products')} >= {'expected_stems_per_bulb'}
    assert {x['name'] for x in inspector.get_columns('fv_workers')} >= {'job_title'}
    assert {x['name'] for x in inspector.get_columns('fv_harvest_records')} >= {'harvest_order_id'}


@pytest.mark.parametrize('operation,labels', [
    ('planting', ('Cantidad de bulbos sembrados', 'Invernadero / cama / mesa')),
    ('harvest', ('Flores 1 BL', 'Flores 2 BL', 'Cantidad descartada')),
])
def test_paper_form_is_one_legible_letter_page(operation, labels):
    pdf = build_field_pdf(operation=operation, number='OT-PRUEBA-01', context={
        'variety': 'Lirio Beau Soleil', 'lot': 'LOTE 125', 'location': 'Invernadero 2 / Cama 18'})
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert abs(float(page.mediabox.width) - 612) < 1
    assert abs(float(page.mediabox.height) - 792) < 1
    content = page.extract_text()
    assert 'Flores Volcán' in content and 'OT-PRUEBA-01' in content
    for label in labels: assert label in content


def test_scan_validation_and_manual_review(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    img = Image.new('RGB', (1200, 800), '#fffdf7')
    buf = io.BytesIO(); img.save(buf, format='PNG')
    normalized, digest = normalize_scan(buf.getvalue())
    assert normalized.startswith(b'\xff\xd8')
    assert len(digest) == 64
    result = suggest_photo_data(normalized, 'harvest')
    assert result['status'] == 'manual'
    assert result['requires_confirmation'] is True
    assert not result['fields']
    with pytest.raises(HTTPException): normalize_scan(b'not an image')
    with pytest.raises(HTTPException): normalize_scan(b'0' * (8 * 1024 * 1024 + 1))


def test_harvest_and_yield_validation():
    form = HarvestRecordIn(production_batch_id=1, bloom_1_qty=3, bloom_2_qty=5, discard_qty=1)
    assert form.bloom_1_qty + form.bloom_2_qty + form.discard_qty == Decimal('9')
    with pytest.raises(ValueError):
        HarvestRecordIn(production_batch_id=1, discard_qty=-1)
    with pytest.raises(ValueError):
        SapProductIn(code='SKU', description='Bulbos de lirio', expected_stems_per_bulb=0)
