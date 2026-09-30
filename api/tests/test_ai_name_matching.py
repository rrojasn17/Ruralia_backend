"""Name matching regression tests, independent of database and provider."""
import ast
from pathlib import Path
from difflib import SequenceMatcher
from types import SimpleNamespace as Row
import unicodedata
import unittest

source = Path(__file__).resolve().parents[1] / 'modules/mod_caficultura/ai_metrics.py'
tree = ast.parse(source.read_text())
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {'_norm', '_resolve_named'}]
namespace = {'Any': object, 'SequenceMatcher': SequenceMatcher, 'unicodedata': unicodedata}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
resolve = namespace['_resolve_named']


class NameMatchingTests(unittest.TestCase):
    def test_typo_with_unrelated_metadata(self):
        farm = Row(id=1, nombre='Chelsea', codigo='FIN-00004', propietario='Productor de café')
        self.assertIs(resolve([farm], 'chgelsea', ('nombre', 'codigo', 'propietario'), 'finca')['row'], farm)

    def test_prefix_and_accents(self):
        farm = Row(id=1, nombre='Finca Chelseá')
        self.assertTrue(resolve([farm], 'chelsea', ('nombre',), 'finca')['ok'])

    def test_ambiguous_names(self):
        farms = [Row(id=1, nombre='Chelsea Norte'), Row(id=2, nombre='Chelsea Sur')]
        self.assertTrue(resolve(farms, 'Chelsea', ('nombre',), 'finca')['needs_clarification'])

    def test_weak_match_is_not_selected(self):
        self.assertFalse(resolve([Row(id=1, nombre='Mariana')], 'Marisol', ('nombre',), 'persona')['ok'])

    def test_exact_name_has_priority(self):
        farms = [Row(id=1, nombre='Chelsea'), Row(id=2, nombre='Chelsia')]
        self.assertEqual(resolve(farms, 'Chelsea', ('nombre',), 'finca')['row'].id, 1)


if __name__ == '__main__':
    unittest.main()
