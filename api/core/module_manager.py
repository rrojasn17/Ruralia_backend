from __future__ import annotations

import hashlib
import json
import os
import zipfile
from io import BytesIO
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
    """Mantiene la tienda alineada con los runtimes incluidos en el despliegue.

    Los módulos integrados siempre aparecen en la tienda del SuperAdmin. Un
    manifiesto descargado, editado e importado se conserva mientras corresponda
    a la misma versión del runtime. Cuando el código desplegado sube de versión,
    la tienda vuelve al manifiesto oficial de esa nueva versión para evitar una
    combinación incompatible entre configuración y runtime.
    """
    active_claimed = db.query(AppModule).filter(
        AppModule.module_type == "industry",
        AppModule.status == "active",
    ).first() is not None

    for key, runtime_manifest in BUILTIN_MANIFESTS.items():
        row = db.query(AppModule).filter(AppModule.key == key).first()
        runtime_version = str(runtime_manifest.get("version") or "1.0.0")

        if not row:
            legacy_found = legacy_runtime_detected(key, db)
            inherited_active = legacy_found and not active_claimed
            row = AppModule(
                key=key,
                name=runtime_manifest["name"],
                version=runtime_version,
                module_type=runtime_manifest.get("module_type", "industry"),
                description=runtime_manifest.get("description"),
                status="active" if inherited_active else ("disabled" if legacy_found else "uninstalled"),
                built_in=True,
                source="legacy" if legacy_found else "builtin",
                manifest=runtime_manifest,
                activated_at=utcnow() if inherited_active else None,
            )
            db.add(row)
            if inherited_active:
                active_claimed = True
            continue

        row.built_in = True
        row.module_type = runtime_manifest.get("module_type", row.module_type or "industry")

        # Un paquete editado por el SuperAdmin puede personalizar nombres, menú,
        # roles y permisos, pero solamente para la misma versión de runtime.
        uploaded_matches_runtime = (
            row.source == "upload"
            and str(row.version or "") == runtime_version
            and isinstance(row.manifest, dict)
            and str((row.manifest or {}).get("key") or "") == key
        )
        if uploaded_matches_runtime:
            continue

        # Si el despliegue trae una versión más nueva que la importada, se publica
        # automáticamente como la versión vigente de la tienda.
        row.name = runtime_manifest["name"]
        row.version = runtime_version
        row.description = runtime_manifest.get("description")
        row.manifest = runtime_manifest
        # Conservamos el origen/archivo importado como historial. Para módulos
        # integrados, la descarga de la tienda siempre genera la versión oficial
        # del runtime actualmente desplegado.

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

    runtime_manifest = BUILTIN_MANIFESTS.get(key)
    if runtime_manifest:
        runtime_type = str(runtime_manifest.get("module_type") or "industry")
        runtime_version = str(runtime_manifest.get("version") or "1.0.0")
        if module_type != runtime_type:
            raise HTTPException(status_code=422, detail="El tipo del paquete no coincide con el módulo disponible")
        if version != runtime_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"El paquete corresponde a la versión {version}, pero esta instalación usa {runtime_version}. "
                    "Descargue la última versión desde la Tienda de módulos, edítela y vuelva a importarla."
                ),
            )

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
    previous_status = str(row.status) if row else None
    if not row:
        row = AppModule(key=key, name=name, version=version, module_type=module_type)
        db.add(row)

    row.name = name
    row.version = version
    row.module_type = module_type
    row.description = str(manifest.get("description") or "").strip() or None
    row.built_in = key in BUILTIN_MANIFESTS
    row.source = "upload"
    row.manifest = manifest
    row.package_path = str(target)
    row.checksum_sha256 = checksum

    # Al reimportar un módulo ya instalado/activo solo se actualiza su paquete y
    # configuración. No se apaga el módulo ni se obliga a repetir instalación.
    if previous_status in {"active", "installed", "disabled"}:
        row.status = previous_status
    else:
        row.status = "imported"

    db.commit()
    db.refresh(row)
    return row


def export_module_package(db: Session, key: str) -> tuple[bytes, str]:
    """Genera el ZIP editable de la versión más reciente disponible en la tienda."""
    sync_builtin_modules(db)
    row = db.query(AppModule).filter(AppModule.key == key).first()
    if not row:
        raise HTTPException(status_code=404, detail="Módulo no encontrado")

    runtime_manifest = BUILTIN_MANIFESTS.get(key)
    if runtime_manifest:
        manifest = dict(runtime_manifest)
        version = str(runtime_manifest.get("version") or row.version or "1.0.0")
    else:
        # Para módulos externos conservamos el paquete original cuando exista.
        if row.package_path:
            package_path = Path(row.package_path).expanduser()
            if package_path.is_file():
                try:
                    return package_path.read_bytes(), package_path.name
                except OSError as exc:
                    raise HTTPException(status_code=503, detail="No se pudo leer el paquete guardado") from exc
        manifest = dict(row.manifest or {})
        version = str(row.version or manifest.get("version") or "1.0.0")

    if not manifest:
        raise HTTPException(status_code=404, detail="El módulo no tiene un paquete disponible")

    readme = (
        "RuralIA - paquete de módulo editable\n\n"
        "Edite module.json para personalizar nombre, descripción, navegación, roles y permisos.\n"
        "No elimine key, version, module_type, module_api_version ni minimum_core_api_version.\n"
        "Este ZIP configura un runtime que ya forma parte de RuralIA; no contiene el código Python/Vue del módulo.\n"
        "Después de editarlo, comprima module.json nuevamente en la raíz del ZIP e impórtelo desde la Tienda de módulos.\n"
    )

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("module.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("README_MODULO.txt", readme)

    filename = f"{key}-{version}.zip"
    return output.getvalue(), filename
