"""Isolated draft tests: no database or external AI calls."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from modules.mod_caficultura import ai_actions as actions


class FarmActivityDraftTests(unittest.TestCase):
    def prepare(self, workers):
        activity = NS(id=2, nombre='Poda', tipo='mantenimiento')
        worker = NS(id=3, nombre='Juan Pérez', jornal_diario=16000)
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [activity]
        db.query.return_value.filter.return_value.all.return_value = [worker]
        pending = NS(public_id='draft', version=1, summary='Resumen', expires_at=datetime.now(timezone.utc))
        with patch.object(actions, 'resolve_farm', return_value={'ok': True, 'row': NS(id=1, nombre='Roble', gestion_fincas_habilitada=True)}), patch.object(actions, '_create_pending_action', return_value=pending) as create:
            result = actions.prepare_farm_activity_action(db, NS(id=1), NS(id=1, rol='gerente'), NS(id=1), {'farm_name': 'Roble', 'activity_name': 'Poda', 'date': '2026-09-29', 'workers': workers})
            return result, create

    def test_worker_hours_and_catalog_wage_are_preserved(self):
        result, create = self.prepare([{'name': 'Juan', 'hours': 6, 'daily_wage': None}])
        self.assertTrue(result['requires_confirmation'])
        self.assertEqual(create.call_args.kwargs['payload']['trabajadores'], [{'trabajador_id': 3, 'horas': 6, 'jornal': 16000}])

    def test_missing_hours_requires_clarification(self):
        result, create = self.prepare([{'name': 'Juan', 'hours': None}])
        self.assertTrue(result['needs_clarification'])
        create.assert_not_called()

    def test_unknown_worker_is_not_silently_discarded(self):
        result, create = self.prepare([{'name': 'Pedro', 'hours': 8}])
        self.assertTrue(result['needs_clarification'])
        create.assert_not_called()

    def test_duplicate_worker_requires_clarification(self):
        result, create = self.prepare([{'name': 'Juan', 'hours': 8}] * 2)
        self.assertTrue(result['needs_clarification'])
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
