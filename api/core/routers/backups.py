"""Root can manage a module's data copy without changing the active industry."""
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from config import MAX_REQUEST_MB
from core.models import AppModule, Usuario
from core.module_backups import export_bytes, resolve_backup_spec, restore_bytes
from core.module_manager import ensure_superadmin
from core.routers.auth import get_current_user
from database import get_db

router = APIRouter(tags=['module-backups'])


def _spec(db, current, key):
    ensure_superadmin(current)
    module = db.query(AppModule).filter(AppModule.key == key, AppModule.status.in_(['installed', 'active', 'disabled'])).first()
    if module is None:
        raise HTTPException(404, 'El módulo no está instalado')
    return resolve_backup_spec(key)


@router.get('/modules/{module_key}/backups/export')
def export_backup(module_key: str, format: Literal['json', 'zip'] = 'zip',
                  current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    spec = _spec(db, current, module_key)
    content = export_bytes(db, spec, archive=format == 'zip')
    return Response(content, media_type='application/zip' if format == 'zip' else 'application/json',
                    headers={'Content-Disposition': f'attachment; filename="ruralia-{module_key}-respaldo.{format}"'})


@router.post('/modules/{module_key}/backups/import')
async def import_backup(module_key: str, file: UploadFile = File(...), mode: Literal['replace', 'merge'] = 'replace',
                        current: Usuario = Depends(get_current_user), db: Session = Depends(get_db)):
    spec = _spec(db, current, module_key)
    raw = await file.read(MAX_REQUEST_MB * 1024 * 1024 + 1)
    if len(raw) > MAX_REQUEST_MB * 1024 * 1024:
        raise HTTPException(413, f'El respaldo no debe superar {MAX_REQUEST_MB} MB')
    return restore_bytes(db, spec, raw, mode=mode, current=current)
