"""La ficha identifica el proveedor de la compra original y distingue sus fechas."""
from datetime import datetime, timezone
from test_floresvolcan_reconciliation_v07 import fv, value
from modules.mod_floresvolcan.models import PurchaseOrder


def test_inventory_origin_available_and_archived(fv):
    po = fv.db.query(PurchaseOrder).filter_by(number='TEST-PO').one()
    po.created_at = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    fv.lot.received_at = datetime(2026, 2, 3, 12, tzinfo=timezone.utc)
    fv.db.commit()
    for archived in (False, True):
        if archived:
            fv.lot.quantity_available = 0
            fv.db.commit()
        result = value(fv.client.get('/floresvolcan/inventory', params={
            'page':1, 'available_only':not archived, 'empty_only':archived,
        }), 200)
        row = result['items'][0]
        assert row['supplier_name'] == 'Proveedor de prueba'
        assert row['purchase_order_id'] == po.id
        assert row['purchase_order_number'] == 'TEST-PO'
        assert row['purchase_order_created_at'].startswith('2026-01-02')
        assert row['received_at'].startswith('2026-02-03')
