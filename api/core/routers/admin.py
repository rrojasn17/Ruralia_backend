from __future__ import annotations

import os
import uuid
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from config import APP_BASE_URL, APP_NAME
from core.models import AppModule, ModuleUserRole, SesionUsuario, Usuario
from core.module_manager import (
    activate_module,
    collect_user_delete_blockers,
    deactivate_module,
    effective_permissions,
    role_for_module,
    module_permissions,
    export_module_package,
    ensure_superadmin,
    get_active_industry_module,
    import_module_package,
    install_module,
    module_to_dict,
    runtime_manifests,
    sync_builtin_modules,
    uninstall_module,
)
from core.routers.auth import get_current_user, require_core_permission, require_roles
from core.schemas import (
    BrandingOut,
    ConfiguracionOut,
    ConfiguracionUpdate,
    ModuleOut,
    ModuleRuntimeOut,
    UsuarioCreate,
    UsuarioOut,
    UsuarioUpdate,
    normalize_role,
)
from core.services import get_config_int, get_config_value, set_config, usuario_out
from database import get_db
from security import hashear_contrasena, validar_fortaleza_contrasena

router = APIRouter(tags=["core"])


def _upload_root() -> Path:
    return Path(os.getenv("NAVIA_UPLOAD_DIR", "uploads"))


def _config_out(db: Session, current: Usuario | None = None) -> ConfiguracionOut:
    can_manage = bool(current and (current.is_superadmin or str(current.rol).lower() == "gerente"))
    return ConfiguracionOut(
        session_days=get_config_int(db, "session_days", 30),
        app_name=get_config_value(db, "app_name", APP_NAME).strip() or APP_NAME,
        app_logo_url=get_config_value(db, "app_logo_url", "").strip() or None,
        alert_whatsapp=get_config_value(db, "alert_whatsapp", "").strip() or None,
        alert_email=get_config_value(db, "alert_email", "").strip() or None,
        can_export_backup=can_manage,
        can_import_backup=can_manage,
        can_full_reset=bool(current and current.is_superadmin),
    )


def _validate_image(content: bytes, *, max_mb: int, label: str) -> str:
    if len(content) > max_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"{label} no debe superar {max_mb} MB")
    try:
        with Image.open(BytesIO(content)) as image:
            if image.width * image.height > 25_000_000:
                raise HTTPException(status_code=422, detail=f"{label} tiene dimensiones demasiado grandes")
            image.verify()
            image_format = str(image.format or "").upper()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="El archivo no es una imagen válida") from exc
    allowed = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
    if image_format not in allowed:
        raise HTTPException(status_code=422, detail="La imagen debe ser PNG, JPG o WEBP")
    return allowed[image_format]


@router.get("/branding", response_model=BrandingOut)
def branding(db: Session = Depends(get_db)):
    return BrandingOut(
        app_name=get_config_value(db, "app_name", APP_NAME).strip() or APP_NAME,
        app_logo_url=get_config_value(db, "app_logo_url", "").strip() or None,
    )


@router.get("/configuracion", response_model=ConfiguracionOut)
def get_configuracion(current: Usuario = Depends(require_core_permission("core:config:manage")), db: Session = Depends(get_db)):
    return _config_out(db, current)


@router.patch("/configuracion", response_model=ConfiguracionOut)
def update_configuracion(payload: ConfiguracionUpdate, current: Usuario = Depends(require_core_permission("core:config:manage")), db: Session = Depends(get_db)):
    if payload.session_days is not None:
        set_config(db, "session_days", str(payload.session_days), "Duración de sesión en días")
    if payload.app_name is not None:
        set_config(db, "app_name", payload.app_name.strip(), "Nombre público de la plataforma")
    if payload.alert_whatsapp is not None:
        set_config(db, "alert_whatsapp", str(payload.alert_whatsapp or "").strip(), "WhatsApp para alertas")
    if payload.alert_email is not None:
        set_config(db, "alert_email", str(payload.alert_email or "").strip(), "Correo para alertas")
    db.commit()
    return _config_out(db, current)


@router.post("/configuracion/branding/logo", response_model=ConfiguracionOut)
async def upload_brand_logo(file: UploadFile = File(...), current: Usuario = Depends(require_core_permission("core:config:manage")), db: Session = Depends(get_db)):
    content = await file.read()
    ext = _validate_image(content, max_mb=5, label="El logotipo")
    folder = _upload_root().resolve() / "branding"
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"logo_{uuid.uuid4().hex}{ext}"
    target = (folder / filename).resolve()
    if folder.resolve() not in target.parents:
        raise HTTPException(status_code=500, detail="Ruta de logotipo inválida")
    target.write_bytes(content)
    previous = get_config_value(db, "app_logo_url", "")
    set_config(db, "app_logo_url", f"{APP_BASE_URL}/uploads/branding/{filename}", "Logotipo institucional")
    db.commit()
    if "/uploads/branding/" in previous:
        old = (folder / Path(previous.split("?", 1)[0]).name).resolve()
        if old != target and old.parent == folder.resolve():
            old.unlink(missing_ok=True)
    return _config_out(db, current)


@router.delete("/configuracion/branding/logo", response_model=ConfiguracionOut)
def delete_brand_logo(current: Usuario = Depends(require_core_permission("core:config:manage")), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    previous = get_config_value(db, "app_logo_url", "")
    set_config(db, "app_logo_url", "", "Logotipo institucional")
    db.commit()
    if "/uploads/branding/" in previous:
        folder = _upload_root().resolve() / "branding"
        old = (folder / Path(previous.split("?", 1)[0]).name).resolve()
        if old.parent == folder.resolve():
            old.unlink(missing_ok=True)
    return _config_out(db, current)


@router.get("/usuarios", response_model=list[UsuarioOut])
def list_users(current: Usuario = Depends(require_core_permission("core:users:manage")), db: Session = Depends(get_db)):
    return [usuario_out(row, effective_permissions(db, row)) for row in db.query(Usuario).order_by(Usuario.nombre.asc()).all()]


@router.get("/usuarios/select", response_model=list[UsuarioOut])
def select_users(current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    del current
    rows = db.query(Usuario).filter(Usuario.activo).order_by(Usuario.nombre.asc()).all()
    return [usuario_out(row, effective_permissions(db, row)) for row in rows]


@router.post("/usuarios", response_model=UsuarioOut, status_code=201)
def create_user(payload: UsuarioCreate, current: Usuario = Depends(require_core_permission("core:users:manage")), db: Session = Depends(get_db)):
    correo = payload.correo.strip().lower()
    if db.query(Usuario).filter(Usuario.correo == correo).first():
        raise HTTPException(status_code=409, detail="Ese correo ya está en uso")
    requested_superadmin = bool(payload.is_superadmin)
    if requested_superadmin and not bool(current.is_superadmin):
        raise HTTPException(status_code=403, detail="Solo un superadmin puede crear otro superadmin")
    if "*" in payload.permisos and not bool(current.is_superadmin):
        raise HTTPException(status_code=403, detail="Solo un superadmin puede asignar permiso global")
    try:
        validar_fortaleza_contrasena(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = Usuario(
        nombre=payload.nombre.strip(), correo=correo, hash_contrasena=hashear_contrasena(payload.password),
        rol="gerente" if requested_superadmin else normalize_role(payload.rol), activo=True if requested_superadmin else payload.activo,
        is_superadmin=requested_superadmin, permisos=["*"] if requested_superadmin else payload.permisos,
    )
    db.add(row); db.commit(); db.refresh(row)
    return usuario_out(row, effective_permissions(db, row))


@router.patch("/usuarios/{usuario_id}", response_model=UsuarioOut)
def update_user(usuario_id: int, payload: UsuarioUpdate, current: Usuario = Depends(require_core_permission("core:users:manage")), db: Session = Depends(get_db)):
    row = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    data = payload.model_dump(exclude_unset=True)
    current_root = bool(current.is_superadmin)
    target_root = bool(row.is_superadmin)
    if target_root and not current_root:
        raise HTTPException(status_code=403, detail="Un gerente normal no puede modificar un superadmin")
    if "is_superadmin" in data and not current_root:
        raise HTTPException(status_code=403, detail="Solo un superadmin puede cambiar la bandera de superadmin")
    if row.id == current.id and (data.get("activo") is False or data.get("is_superadmin") is False):
        raise HTTPException(status_code=400, detail="No puede desactivar ni quitarse privilegios Root a sí mismo")
    if "correo" in data and data["correo"]:
        correo = str(data["correo"]).strip().lower()
        if db.query(Usuario).filter(Usuario.correo == correo, Usuario.id != usuario_id).first():
            raise HTTPException(status_code=409, detail="Ese correo ya está en uso")
        row.correo = correo
    if data.get("nombre"):
        row.nombre = data["nombre"].strip()
    if "is_superadmin" in data:
        if target_root and not data["is_superadmin"] and db.query(Usuario).filter(Usuario.is_superadmin, Usuario.activo).count() <= 1:
            raise HTTPException(status_code=400, detail="Debe existir al menos un superadmin activo")
        row.is_superadmin = bool(data["is_superadmin"])
    if data.get("rol"):
        row.rol = "gerente" if row.is_superadmin else normalize_role(data["rol"])
    if "activo" in data:
        if row.is_superadmin and data["activo"] is False:
            raise HTTPException(status_code=400, detail="No se puede desactivar un superadmin")
        row.activo = bool(data["activo"])
    if "firma_url" in data:
        row.firma_url = data["firma_url"] or None
    if "permisos" in data and data["permisos"] is not None:
        if "*" in data["permisos"] and not current_root:
            raise HTTPException(status_code=403, detail="Solo un superadmin puede asignar permiso global")
        row.permisos = ["*"] if row.is_superadmin else list(dict.fromkeys(data["permisos"]))
    if data.get("password"):
        try:
            validar_fortaleza_contrasena(data["password"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row.hash_contrasena = hashear_contrasena(data["password"])
    if row.is_superadmin:
        row.rol = "gerente"; row.activo = True; row.permisos = ["*"]
    db.commit(); db.refresh(row)
    return usuario_out(row, effective_permissions(db, row))


@router.post("/usuarios/{usuario_id}/firma", response_model=UsuarioOut)
async def upload_signature(usuario_id: int, file: UploadFile = File(...), current: Usuario = Depends(require_core_permission("core:users:manage")), db: Session = Depends(get_db)):
    row = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if row.is_superadmin and not current.is_superadmin:
        raise HTTPException(status_code=403, detail="Solo un superadmin puede cambiar la firma de otro superadmin")
    content = await file.read(); ext = _validate_image(content, max_mb=3, label="La firma")
    folder = _upload_root().resolve() / "firmas"; folder.mkdir(parents=True, exist_ok=True)
    filename = f"usuario_{usuario_id}_{uuid.uuid4().hex}{ext}"; (folder / filename).write_bytes(content)
    row.firma_url = f"{APP_BASE_URL}/uploads/firmas/{filename}"
    db.commit(); db.refresh(row)
    return usuario_out(row, effective_permissions(db, row))


@router.delete("/usuarios/{usuario_id}", status_code=204)
def delete_user(usuario_id: int, current: Usuario = Depends(require_core_permission("core:users:manage")), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    row = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if row.id == current.id:
        raise HTTPException(status_code=400, detail="No puede eliminar su propio usuario")
    if row.is_superadmin:
        raise HTTPException(status_code=400, detail="No se puede eliminar un superadmin")
    blockers = collect_user_delete_blockers(db, usuario_id)
    if blockers:
        raise HTTPException(status_code=409, detail="El usuario posee información asociada: " + "; ".join(blockers))
    db.query(SesionUsuario).filter(SesionUsuario.usuario_id == usuario_id).delete(synchronize_session=False)
    db.delete(row); db.commit()
    return Response(status_code=204)


@router.get("/modules/public-runtime")
def public_module_runtime(db: Session = Depends(get_db)):
    """Estado mínimo del módulo activo para resolver rutas públicas del frontend."""
    sync_builtin_modules(db)
    active = get_active_industry_module(db)
    if not active:
        return {"active_module": None}
    manifest = active.manifest or {}
    return {
        "active_module": {
            "key": active.key,
            "name": active.name,
            "public_route_prefixes": manifest.get("public_route_prefixes") or [],
        }
    }


@router.get("/modules", response_model=list[ModuleOut])
def list_modules(current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    sync_builtin_modules(db)
    return [module_to_dict(row) for row in db.query(AppModule).order_by(AppModule.name.asc()).all()]


@router.get("/modules/runtime", response_model=ModuleRuntimeOut)
def module_runtime(current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    sync_builtin_modules(db)
    active = get_active_industry_module(db)
    available = db.query(AppModule).filter(AppModule.status != "uninstalled").order_by(AppModule.name.asc()).all()
    return ModuleRuntimeOut(
        active_module=module_to_dict(active) if active else None,
        effective_permissions=effective_permissions(db, current),
        active_module_role=role_for_module(db, current, active.key) if active else None,
        available_modules=[module_to_dict(row) for row in available],
        known_manifests=runtime_manifests(),
    )


@router.post("/modules/import", response_model=ModuleOut, status_code=201)
async def import_module(file: UploadFile = File(...), current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    return module_to_dict(import_module_package(db, file.filename or "module.zip", await file.read()))


@router.get("/modules/{key}/download")
def download_module(key: str, current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    content, filename = export_module_package(db, key)
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/modules/{key}/install", response_model=ModuleOut)
def install(key: str, current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    return module_to_dict(install_module(db, key))


@router.post("/modules/{key}/activate", response_model=ModuleOut)
def activate(key: str, current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    return module_to_dict(activate_module(db, key))


@router.post("/modules/{key}/deactivate", response_model=ModuleOut)
def deactivate(key: str, current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    return module_to_dict(deactivate_module(db, key))


@router.delete("/modules/{key}", response_model=ModuleOut)
def uninstall(key: str, current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_superadmin(current)
    return module_to_dict(uninstall_module(db, key))


# ---------- Usuarios y roles específicos por industria ----------
from pydantic import BaseModel, EmailStr, Field


class ModuleUserCreate(BaseModel):
    nombre: str = Field(min_length=2, max_length=180)
    correo: EmailStr
    password: str | None = Field(default=None, min_length=10, max_length=128)
    role: str = 'trabajador'
    activo: bool = True


class ModuleUserPatch(BaseModel):
    role: str | None = None
    activo: bool | None = None


def _module_for_users(db: Session, module_key: str, current: Usuario) -> AppModule:
    row = db.query(AppModule).filter(AppModule.key == module_key, AppModule.status == 'active').first()
    if not row:
        raise HTTPException(404, 'Módulo inactivo o inexistente')
    current_role = role_for_module(db, current, module_key)
    if not current.is_superadmin and current_role not in {'gerente', 'admin', 'superadmin'}:
        raise HTTPException(403, 'No administra usuarios de este módulo')
    return row


def _role_valid(module: AppModule, role: str, current: Usuario, db: Session) -> str:
    role = 'superadmin' if str(role).strip().lower() == 'superadmin' else normalize_role(role)
    allowed = {str(r['key']) for r in (module.manifest or {}).get('roles', [])}
    allowed.add('superadmin')
    if role not in allowed:
        raise HTTPException(422, 'Rol no disponible para este módulo')
    if role == 'superadmin' and not (current.is_superadmin or role_for_module(db, current, module.key) == 'superadmin'):
        raise HTTPException(403, 'Solo un superadmin puede asignar otro superadmin del módulo')
    return role


@router.get('/modules/{module_key}/users')
def module_users(module_key: str, current: Usuario = Depends(get_current_user), db: Session = Depends(get_db),
                 page: int = 1, page_size: int = 40, q: str = ''):
    module = _module_for_users(db, module_key, current)
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, 'Paginación inválida')
    query = db.query(Usuario, ModuleUserRole).join(ModuleUserRole, Usuario.id == ModuleUserRole.usuario_id).filter(
        ModuleUserRole.module_key == module_key
    )
    if q.strip():
        term = '%' + q.strip().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        query = query.filter(Usuario.nombre.ilike(term, escape='!') | Usuario.correo.ilike(term, escape='!'))
    total = query.count()
    rows = query.order_by(Usuario.nombre, Usuario.id).offset((page - 1) * page_size).limit(page_size).all()
    role_map = (module.manifest or {}).get('role_permissions') or {}
    all_module_permissions = [p for p in (module.manifest or {}).get('permissions', []) if p != 'floresvolcan:records:delete']
    return {'items': [{'id': u.id, 'nombre': u.nombre, 'correo': u.correo, 'rol': m.role,
                       'activo': u.activo, 'is_superadmin': u.is_superadmin,
                       'permisos': (['*'] if u.is_superadmin else all_module_permissions if m.role == 'superadmin' else role_map.get(m.role, []))}
                      for u, m in rows], 'total': total, 'page': page, 'page_size': page_size}


@router.post('/modules/{module_key}/users', status_code=201)
def create_module_user(module_key: str, payload: ModuleUserCreate,
                       current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    module = _module_for_users(db, module_key, current)
    role = _role_valid(module, payload.role, current, db)
    email = str(payload.correo).strip().lower()
    user = db.query(Usuario).filter(Usuario.correo == email).first()
    if user:
        if db.query(ModuleUserRole.id).filter(ModuleUserRole.usuario_id == user.id, ModuleUserRole.module_key == module_key).first():
            raise HTTPException(409, 'Este usuario ya pertenece al módulo')
        # No reescribir credenciales, estado ni permisos de una identidad existente.
    else:
        if not payload.password:
            raise HTTPException(422, 'Contraseña requerida para un usuario nuevo')
        try:
            validar_fortaleza_contrasena(payload.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        user = Usuario(nombre=payload.nombre.strip(), correo=email,
                       hash_contrasena=hashear_contrasena(payload.password),
                       rol='operario', activo=payload.activo, is_superadmin=False, permisos=[])
        db.add(user); db.flush()
    db.add(ModuleUserRole(usuario_id=user.id, module_key=module_key, role=role))
    db.commit()
    return {'id': user.id, 'nombre': user.nombre, 'correo': user.correo, 'rol': role, 'activo': user.activo}


@router.patch('/modules/{module_key}/users/{user_id}')
def edit_module_user(module_key: str, user_id: int, payload: ModuleUserPatch,
                     current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    module = _module_for_users(db, module_key, current)
    membership = db.query(ModuleUserRole).filter(ModuleUserRole.module_key == module_key,
                                                   ModuleUserRole.usuario_id == user_id).first()
    if not membership:
        raise HTTPException(404, 'Usuario no asignado a este módulo')
    if membership.role == 'superadmin' and not (current.is_superadmin or role_for_module(db, current, module_key) == 'superadmin'):
        raise HTTPException(403, 'No puede modificar un superadmin')
    if payload.role is not None:
        membership.role = _role_valid(module, payload.role, current, db)
    if payload.activo is not None:
        # Desactivar identidad tiene efecto global: solo Root puede hacerlo.
        if not current.is_superadmin:
            raise HTTPException(403, 'El estado global solo lo administra Root')
        user = db.get(Usuario, user_id)
        if user and not user.is_superadmin:
            user.activo = payload.activo
    db.commit()
    return {'id': user_id, 'rol': membership.role, 'activo': db.get(Usuario, user_id).activo}


@router.delete('/modules/{module_key}/users/{user_id}', status_code=204)
def unlink_module_user(module_key: str, user_id: int, current: Usuario = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    _module_for_users(db, module_key, current)
    membership = db.query(ModuleUserRole).filter(ModuleUserRole.usuario_id == user_id,
                                                   ModuleUserRole.module_key == module_key).first()
    if not membership:
        raise HTTPException(404, 'Usuario no asignado')
    if membership.role == 'superadmin' and not current.is_superadmin:
        raise HTTPException(403, 'Solo Root puede retirar un superadmin')
    if user_id == current.id:
        raise HTTPException(409, 'No puede retirarse a sí mismo del módulo')
    db.delete(membership); db.commit()
    return Response(status_code=204)
