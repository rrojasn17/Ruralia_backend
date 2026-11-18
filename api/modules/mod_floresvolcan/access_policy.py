"""Role ceilings for Flores; explicit grants cannot widen this hierarchy."""

import json
from pathlib import Path

ROLE_PERMISSIONS = json.loads(Path(__file__).with_name("module.json").read_text())[
    "role_permissions"
]


def role_permissions(role):
    role = str(role or "").strip().lower()
    role = {"administrador": "admin"}.get(role, role)
    return set(ROLE_PERMISSIONS.get(role, []))
