from __future__ import annotations

import json
from pathlib import Path

# ``module.json`` es la única fuente de verdad del contrato del módulo.
MODULE_MANIFEST = json.loads((Path(__file__).with_name("module.json")).read_text(encoding="utf-8"))
