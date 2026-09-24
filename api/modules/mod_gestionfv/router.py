from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.models import Usuario
from core.module_manager import require_module_permission
from core.routers.auth import get_current_user
from database import get_db
from modules.mod_gestionfv.models import GestionFVSetting

router = APIRouter(prefix="/gestionfv", tags=["gestionfv"])


@router.get("/status")
def status(
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    version = db.query(GestionFVSetting).filter(GestionFVSetting.clave == "module_version").first()
    return {
        "ok": True,
        "module": "mod_gestionfv",
        "version": version.valor if version else "0.1.0",
        "user": current.nombre,
        "phase": "architecture-ready",
    }


@router.get("/workspace")
def workspace(
    current: Usuario = Depends(require_module_permission("gestionfv:dashboard:view")),
):
    return {
        "ok": True,
        "module": "mod_gestionfv",
        "message": "Runtime GestiónFV activo y RBAC modular operativo.",
        "user_id": current.id,
        "next_capabilities": ["produccion", "poscosecha", "calidad", "exportacion"],
    }
