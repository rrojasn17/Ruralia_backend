from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

# Contrato estable entre App Base y módulos de industria.
# Se incrementa solo cuando una versión futura del core rompa compatibilidad.
MODULE_API_VERSION = 1

MODULE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")
PERMISSION_RE = re.compile(r"^[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)+$")


def module_contract_version(manifest: dict[str, Any]) -> int:
    raw = manifest.get("module_api_version", 1)
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("module_api_version debe ser un entero") from exc
    if version < 1:
        raise ValueError("module_api_version debe ser mayor o igual a 1")
    return version


def _require_text(manifest: dict[str, Any], key: str, *, max_len: int) -> str:
    value = str(manifest.get(key) or "").strip()
    if not value:
        raise ValueError(f"module.json requiere {key}")
    if len(value) > max_len:
        raise ValueError(f"{key} supera {max_len} caracteres")
    return value


def _string_list(manifest: dict[str, Any], key: str) -> list[str]:
    raw = manifest.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{key} debe ser una lista")
    values: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if not value:
            raise ValueError(f"{key} contiene un valor vacío")
        values.append(value)
    if len(values) != len(set(values)):
        raise ValueError(f"{key} contiene valores duplicados")
    return values


def _validate_routes(manifest: dict[str, Any]) -> None:
    for key in ("route_prefixes", "public_route_prefixes"):
        for prefix in _string_list(manifest, key):
            if not prefix.startswith("/") or "//" in prefix:
                raise ValueError(f"{key} contiene una ruta inválida: {prefix}")
            if len(prefix) > 180:
                raise ValueError(f"{key} contiene una ruta demasiado larga")


def _validate_permissions(manifest: dict[str, Any]) -> None:
    permissions = _string_list(manifest, "permissions")
    declared = set(permissions)
    for permission in permissions:
        if len(permission) > 120 or not PERMISSION_RE.fullmatch(permission):
            raise ValueError(f"Permiso inválido en module.json: {permission}")

    role_permissions = manifest.get("role_permissions") or {}
    if not isinstance(role_permissions, dict):
        raise ValueError("role_permissions debe ser un objeto")
    for role, values in role_permissions.items():
        role_key = str(role or "").strip()
        if not role_key:
            raise ValueError("role_permissions contiene un rol vacío")
        if not isinstance(values, list):
            raise ValueError(f"role_permissions[{role_key}] debe ser una lista")
        for permission in values:
            permission_key = str(permission or "").strip()
            if permission_key not in declared:
                raise ValueError(
                    f"El rol {role_key} referencia un permiso no declarado: {permission_key}"
                )

    navigation = manifest.get("navigation") or []
    if not isinstance(navigation, list):
        raise ValueError("navigation debe ser una lista")
    for index, item in enumerate(navigation, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"navigation[{index}] debe ser un objeto")
        label = str(item.get("label") or "").strip()
        to = str(item.get("to") or "").strip()
        if not label or not to.startswith("/"):
            raise ValueError(f"navigation[{index}] requiere label y ruta absoluta")
        permission = str(item.get("permission") or "").strip()
        if permission and permission not in declared:
            raise ValueError(
                f"navigation[{index}] referencia un permiso no declarado: {permission}"
            )


def _validate_roles(manifest: dict[str, Any]) -> None:
    roles = manifest.get("roles") or []
    if not isinstance(roles, list):
        raise ValueError("roles debe ser una lista")
    seen: set[str] = set()
    for index, item in enumerate(roles, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"roles[{index}] debe ser un objeto")
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or "").strip()
        if not key or not label:
            raise ValueError(f"roles[{index}] requiere key y label")
        if key in seen:
            raise ValueError(f"Rol duplicado en module.json: {key}")
        seen.add(key)


def validate_module_contract(manifest: dict[str, Any], *, http_error: bool = False) -> None:
    """Valida el contrato que todo módulo de RuralIA debe cumplir.

    El objetivo es que una nueva agroindustria pueda añadirse sin tocar lógica
    interna del core: el core solo confía en un manifiesto validado, lifecycle y
    runtime. Los errores de contrato se detectan al arrancar/importar, no durante
    una operación de negocio.
    """
    try:
        if not isinstance(manifest, dict):
            raise ValueError("module.json debe ser un objeto JSON")

        key = _require_text(manifest, "key", max_len=80)
        if not MODULE_KEY_RE.fullmatch(key):
            raise ValueError("key debe usar minúsculas, números y guion bajo")
        _require_text(manifest, "name", max_len=180)
        _require_text(manifest, "version", max_len=40)

        module_type = str(manifest.get("module_type") or "industry").strip().lower()
        if module_type not in {"industry", "feature"}:
            raise ValueError("module_type debe ser industry o feature")

        version = module_contract_version(manifest)
        if version > MODULE_API_VERSION:
            raise ValueError(
                f"El módulo requiere contrato {version}, pero esta App Base soporta {MODULE_API_VERSION}"
            )
        minimum = int(manifest.get("minimum_core_api_version", 1))
        if minimum < 1:
            raise ValueError("minimum_core_api_version debe ser mayor o igual a 1")
        if minimum > MODULE_API_VERSION:
            raise ValueError(
                f"El módulo requiere App Base API >= {minimum}; instalada: {MODULE_API_VERSION}"
            )

        _validate_roles(manifest)
        _validate_permissions(manifest)
        _validate_routes(manifest)
    except (TypeError, ValueError) as exc:
        if http_error:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
