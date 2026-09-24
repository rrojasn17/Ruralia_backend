from __future__ import annotations

"""Descubrimiento de runtimes modulares incluidos en el despliegue.

El catálogo persistente vive en ``core_app_modules``. Este registro descubre por
convención únicamente código que ya forma parte del artefacto desplegado:

    modules/mod_xxx/manifest.py   -> MODULE_MANIFEST
    modules/mod_xxx/lifecycle.py
    modules/mod_xxx/runtime.py    -> ROUTER_ENTRYPOINTS / WORKER_ENTRYPOINT

Importar un ZIP desde CMS registra el manifiesto, pero nunca ejecuta código del
ZIP. Para poder activarse, el runtime revisado debe existir en este despliegue.
"""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Iterable

from core.module_contract import validate_module_contract


@dataclass(frozen=True)
class ModuleRuntimeSpec:
    key: str
    manifest: dict[str, Any]
    lifecycle_module: str
    router_entrypoints: tuple[str, ...] = ()
    worker_entrypoint: str | None = None
    legacy_detector_entrypoint: str | None = None


def _discover_runtime_modules() -> dict[str, ModuleRuntimeSpec]:
    modules_dir = Path(__file__).resolve().parent
    discovered: dict[str, ModuleRuntimeSpec] = {}

    for folder in sorted(modules_dir.iterdir(), key=lambda item: item.name):
        if not folder.is_dir() or not folder.name.startswith("mod_"):
            continue
        if not (folder / "runtime.py").is_file() or not (folder / "manifest.py").is_file():
            continue

        package = f"modules.{folder.name}"
        manifest_module = import_module(f"{package}.manifest")
        runtime_module = import_module(f"{package}.runtime")
        manifest = dict(getattr(manifest_module, "MODULE_MANIFEST"))
        key = str(manifest.get("key") or "").strip()
        validate_module_contract(manifest)

        if key != folder.name:
            raise RuntimeError(
                f"Runtime modular inválido: {folder.name}/module.json debe declarar key={folder.name!r}"
            )
        if key in discovered:
            raise RuntimeError(f"Runtime modular duplicado: {key}")

        routers = tuple(str(value) for value in getattr(runtime_module, "ROUTER_ENTRYPOINTS", ()) if value)
        worker = getattr(runtime_module, "WORKER_ENTRYPOINT", None)
        legacy_detector = getattr(runtime_module, "LEGACY_DETECTOR_ENTRYPOINT", None)
        discovered[key] = ModuleRuntimeSpec(
            key=key,
            manifest=manifest,
            lifecycle_module=f"{package}.lifecycle",
            router_entrypoints=routers,
            worker_entrypoint=str(worker) if worker else None,
            legacy_detector_entrypoint=str(legacy_detector) if legacy_detector else None,
        )

    return discovered


RUNTIME_MODULES: dict[str, ModuleRuntimeSpec] = _discover_runtime_modules()


def builtin_manifests() -> dict[str, dict[str, Any]]:
    return {key: spec.manifest for key, spec in RUNTIME_MODULES.items()}


def get_runtime_spec(key: str) -> ModuleRuntimeSpec | None:
    return RUNTIME_MODULES.get(key)


def runtime_manifests() -> list[dict[str, Any]]:
    return [spec.manifest for spec in RUNTIME_MODULES.values()]


def resolve_entrypoint(entrypoint: str) -> Any:
    module_name, attr = entrypoint.split(":", 1)
    return getattr(import_module(module_name), attr)


def iter_runtime_routers() -> Iterable[tuple[str, Any]]:
    for key, spec in RUNTIME_MODULES.items():
        for entrypoint in spec.router_entrypoints:
            yield key, resolve_entrypoint(entrypoint)


def resolve_worker(key: str):
    spec = get_runtime_spec(key)
    if not spec or not spec.worker_entrypoint:
        return None
    return resolve_entrypoint(spec.worker_entrypoint)


def legacy_runtime_detected(key: str, db) -> bool:
    spec = get_runtime_spec(key)
    if not spec or not spec.legacy_detector_entrypoint:
        return False
    detector = resolve_entrypoint(spec.legacy_detector_entrypoint)
    return bool(detector(db))
