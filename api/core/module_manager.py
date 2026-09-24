from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.models import AppModule, Usuario
from core.module_contract import MODULE_KEY_RE, validate_module_contract
from database import get_db
from modules.registry import builtin_manifests, get_runtime_spec, legacy_runtime_detected, runtime_manifests

BUILTIN_MANIFESTS: dict[str, dict[str, Any]] = builtin_manifests()



def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def module_to_dict(row: AppModule) -> dict[str, Any]:
    return {
        "key": row.key,
        "name": row.name,
        "version": row.version,
        "module_type": row.module_type,
        "description": row.description,
        "status": row.status,
        "built_in": bool(row.built_in),
        "source": row.source,
        "manifest": row.manifest or {},
        "installed_at": row.installed_at,
        "activated_at": row.activated_at,
    }


def sync_builtin_modules(db: Session) -> None:
    """Sincroniza runtimes conocidos sin instalar módulos en una base nueva.

    Una instancia recién creada conserva únicamente el core. El registro de
    Caficultura aparece al importar su paquete desde CMS. Para una base heredada,
    la presencia del esquema anterior se interpreta como una instalación activa.
    """
    for key, manifest in BUILTIN_MANIFESTS.items():
        row = db.query(AppModule).filter(AppModule.key == key).first()
        if not row and legacy_runtime_detected(key, db):
            row = AppModule(
                key=key,
                name=manifest["name"],
                version=manifest["version"],
                module_type=manifest.get("module_type", "industry"),
                description=manifest.get("description"),
                status="active",
                built_in=True,
                source="legacy",
                manifest=manifest,
                activated_at=utcnow(),
            )
            db.add(row)
            continue

        if not row:
            continue

        # Mantiene metadatos del runtime desplegado sin alterar el estado elegido
        # por el administrador ni sustituir el manifiesto importado.
        row.built_in = key in BUILTIN_MANIFESTS
        if row.source in {"builtin", "legacy"}:
            row.name = manifest["name"]
            row.version = manifest["version"]
            row.module_type = manifest.get("module_type", row.module_type or "industry")
            row.description = manifest.get("description")
            row.manifest = manifest

    db.commit()


def get_active_industry_module(db: Session) -> AppModule | None:
    return (
        db.query(AppModule)
        .filter(AppModule.module_type == "industry", AppModule.status == "active")
        .order_by(AppModule.activated_at.desc().nullslast(), AppModule.id.asc())
        .first()
    )


def effective_permissions(db: Session, user: Usuario) -> list[str]:
    if bool(getattr(user, "is_superadmin", False)):
        return ["*"]

    permissions = set(str(p) for p in (getattr(user, "permisos", None) or []) if str(p).strip())
    active = get_active_industry_module(db)
    if active:
        manifest = active.manifest or {}
        role_map = manifest.get("role_permissions") or {}
        for permission in role_map.get((user.rol or "operario").lower(), []):
            permissions.add(str(permission))
    return sorted(permissions)


def require_active_module(module_key: str):
    def dep(db: Session = Depends(get_db)) -> None:
        row = db.query(AppModule).filter(AppModule.key == module_key, AppModule.status == "active").first()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"El módulo {module_key} no está activo en esta instancia",
            )
    return dep


def ensure_superadmin(user: Usuario) -> None:
    if not bool(getattr(user, "is_superadmin", False)):
        raise HTTPException(status_code=403, detail="Solo Root / SuperAdmin puede gestionar módulos")


def require_module_permission(permission: str):
    """Autoriza un permiso efectivo aportado por el módulo activo.

    Se evalúan permisos explícitos del usuario y ``role_permissions`` del
    manifiesto activo. Root/SuperAdmin siempre conserva acceso total.
    """
    from core.routers.auth import get_current_user

    def dep(
        current: Usuario = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Usuario:
        permissions = set(effective_permissions(db, current))
        if "*" in permissions or permission in permissions:
            return current
        raise HTTPException(status_code=403, detail=f"Permiso requerido: {permission}")

    return dep


def _call_lifecycle(key: str, hook: str, db: Session) -> None:
    spec = get_runtime_spec(key)
    if not spec:
        return
    import importlib
    module = importlib.import_module(spec.lifecycle_module)
    callback = getattr(module, hook, None)
    if callback:
        callback(db)


def collect_user_delete_blockers(db: Session, user_id: int) -> list[str]:
    blockers: list[str] = []
    for key in BUILTIN_MANIFESTS:
        spec = get_runtime_spec(key)
        if not spec:
            continue
        import importlib
        callback = getattr(importlib.import_module(spec.lifecycle_module), "user_delete_blockers", None)
        if callback:
            blockers.extend(callback(db, user_id))
    return blockers


def install_module(db: Session, key: str) -> AppModule:
    row = db.query(AppModule).filter(AppModule.key == key).first()
    if not row:
        raise HTTPException(status_code=404, detail="Módulo no encontrado")
    if key not in BUILTIN_MANIFESTS:
        # El paquete queda instalado a nivel de catálogo. Su runtime deberá formar
        # parte del despliegue antes de poder activarse.
        row.status = "installed"
        db.commit()
        db.refresh(row)
        return row
    _call_lifecycle(key, "install", db)
    row.status = "installed"
    row.deactivated_at = None
    db.commit()
    db.refresh(row)
    return row


def activate_module(db: Session, key: str) -> AppModule:
    row = db.query(AppModule).filter(AppModule.key == key).first()
    if not row or row.status == "uninstalled":
        raise HTTPException(status_code=404, detail="Módulo no instalado")
    if row.status == "imported":
        raise HTTPException(status_code=409, detail="Instale el módulo antes de activarlo")

    if key not in BUILTIN_MANIFESTS:
        raise HTTPException(
            status_code=409,
            detail=(
                "El paquete fue importado, pero esta versión de la aplicación no contiene "
                "su runtime. Despliegue el runtime del módulo antes de activarlo."
            ),
        )

    # Instala/migra primero el esquema del módulo. El cambio de módulo activo se
    # hace después como transacción corta y protegida por bloqueo + índice único.
    _call_lifecycle(key, "activate", db)

    try:
        row = db.query(AppModule).filter(AppModule.key == key).with_for_update().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Módulo no instalado")

        if row.module_type == "industry":
            others = (
                db.query(AppModule)
                .filter(AppModule.module_type == "industry", AppModule.status == "active")
                .with_for_update()
                .all()
            )
            changed = False
            for other in others:
                if other.id != row.id:
                    _call_lifecycle(other.key, "deactivate", db)
                    other.status = "disabled"
                    other.deactivated_at = utcnow()
                    changed = True
            # El índice parcial que garantiza una sola industria activa puede
            # evaluar los UPDATE en cualquier orden. Persistimos primero las
            # desactivaciones dentro de la misma transacción y luego el nuevo activo.
            if changed:
                db.flush()

        row.status = "active"
        row.activated_at = utcnow()
        row.deactivated_at = None
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Ya existe otro módulo de industria activo en esta instancia",
        ) from exc


def deactivate_module(db: Session, key: str) -> AppModule:
    row = db.query(AppModule).filter(AppModule.key == key).first()
    if not row:
        raise HTTPException(status_code=404, detail="Módulo no encontrado")
    if row.status == "active":
        _call_lifecycle(key, "deactivate", db)
        row.status = "disabled"
        row.deactivated_at = utcnow()
        db.commit()
        db.refresh(row)
    return row


def uninstall_module(db: Session, key: str) -> AppModule:
    row = db.query(AppModule).filter(AppModule.key == key).first()
    if not row:
        raise HTTPException(status_code=404, detail="Módulo no encontrado")
    if row.status == "active":
        raise HTTPException(status_code=409, detail="Desactive el módulo antes de desinstalarlo")
    _call_lifecycle(key, "uninstall", db)
    # Desinstalación no destructiva: conserva tablas/datos para permitir reinstalación segura.
    row.status = "uninstalled"
    row.deactivated_at = utcnow()
    db.commit()
    db.refresh(row)
    return row


def import_module_package(db: Session, filename: str, content: bytes) -> AppModule:
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=422, detail="El módulo debe importarse como archivo ZIP")
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El paquete de módulo supera 50 MB")

    try:
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = archive.namelist()
            if "module.json" not in names:
                raise HTTPException(status_code=422, detail="El ZIP debe contener module.json en su raíz")
            manifest = json.loads(archive.read("module.json").decode("utf-8"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Paquete de módulo inválido: {exc}") from exc

    key = str(manifest.get("key") or "").strip().lower()
    name = str(manifest.get("name") or "").strip()
    version = str(manifest.get("version") or "1.0.0").strip()
    module_type = str(manifest.get("module_type") or "industry").strip().lower()
    validate_module_contract(manifest, http_error=True)

    if not MODULE_KEY_RE.fullmatch(key):
        raise HTTPException(status_code=422, detail="module.json contiene un key inválido")
    if not name:
        raise HTTPException(status_code=422, detail="module.json requiere name")
    if module_type not in {"industry", "feature"}:
        raise HTTPException(status_code=422, detail="module_type debe ser industry o feature")

    # Los contenedores de producción ejecutan /app como read-only. Los paquetes
    # importados son datos persistentes, no código de aplicación, por lo que deben
    # almacenarse en un volumen escribible. MODULE_PACKAGE_DIR permite override;
    # cuando no se configura, reutilizamos AI_PRIVATE_UPLOAD_DIR (montado por
    # docker-compose en /app/private_uploads). En desarrollo local conservamos un
    # fallback relativo para no exigir rutas Docker.
    configured_package_dir = os.getenv("MODULE_PACKAGE_DIR", "").strip()
    if configured_package_dir:
        packages_dir = Path(configured_package_dir).expanduser().resolve()
    else:
        private_upload_dir = os.getenv("AI_PRIVATE_UPLOAD_DIR", "").strip()
        docker_private_uploads = Path("/app/private_uploads")
        if private_upload_dir:
            packages_dir = Path(private_upload_dir).expanduser().resolve() / "module_packages"
        elif docker_private_uploads.exists():
            packages_dir = docker_private_uploads / "module_packages"
        else:
            packages_dir = Path("module_packages").resolve()

    try:
        packages_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "No se puede escribir en el directorio de paquetes de módulos "
                f"({packages_dir}). Configure MODULE_PACKAGE_DIR hacia un volumen escribible."
            ),
        ) from exc

    checksum = hashlib.sha256(content).hexdigest()
    target = packages_dir / f"{key}-{version}-{checksum[:12]}.zip"
    try:
        target.write_bytes(content)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "No se puede guardar el paquete del módulo en "
                f"{target}. Configure MODULE_PACKAGE_DIR hacia un volumen escribible."
            ),
        ) from exc

    row = db.query(AppModule).filter(AppModule.key == key).first()
    if not row:
        row = AppModule(key=key, name=name, version=version, module_type=module_type)
        db.add(row)
    row.name = name
    row.version = version
    row.module_type = module_type
    row.description = str(manifest.get("description") or "").strip() or None
    row.status = "imported"
    row.built_in = key in BUILTIN_MANIFESTS
    row.source = "upload"
    if key in BUILTIN_MANIFESTS:
        runtime_manifest = BUILTIN_MANIFESTS[key]
        if module_type != runtime_manifest.get("module_type", "industry"):
            raise HTTPException(status_code=422, detail="El tipo del paquete no coincide con el runtime desplegado")
        manifest = runtime_manifest
        row.name = runtime_manifest["name"]
        row.version = runtime_manifest["version"]
        row.module_type = runtime_manifest.get("module_type", "industry")
        row.description = runtime_manifest.get("description")
    row.manifest = manifest
    row.package_path = str(target)
    row.checksum_sha256 = checksum
    db.commit()
    db.refresh(row)
    return row
