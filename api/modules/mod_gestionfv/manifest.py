from __future__ import annotations

import json
from pathlib import Path

MODULE_MANIFEST = json.loads((Path(__file__).with_name("module.json")).read_text(encoding="utf-8"))
