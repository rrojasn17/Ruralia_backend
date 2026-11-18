"""Identificadores breves de espacios; las camas derivan del padre."""
import re
from fastapi import HTTPException
from .models import ProductionSpace


def validate_space_identity(db, payload, existing=None):
    # Se puede mantener un espacio importado sin renombrarlo al editar otros datos.
    if existing and (payload.code, payload.name, payload.type, payload.parent_id) == (existing.code, existing.name, existing.type, existing.parent_id):
        return
    payload.code = payload.code.strip().upper()
    payload.name = payload.name.strip()
    if payload.type in {'farm', 'greenhouse'}:
        if not re.fullmatch(r'[A-Z0-9]{1,6}', payload.code):
            raise HTTPException(422, 'El código de finca o invernadero debe tener de 1 a 6 letras o números')
    elif payload.type == 'bed':
        payload.name = payload.name.upper()
        if not re.fullmatch(r'[A-Z0-9]{1,4}', payload.name):
            raise HTTPException(422, 'El nombre de la cama debe tener de 1 a 4 letras o números, por ejemplo 1A')
        parent = db.get(ProductionSpace, payload.parent_id)
        if not parent or not re.fullmatch(r'[A-Za-z0-9]{1,6}', parent.code):
            raise HTTPException(422, 'Primero configure el código de la finca o invernadero con un máximo de 6 letras o números')
        payload.code = f'{parent.code.upper()}-{payload.name}'
    elif not payload.code:
        raise HTTPException(422, 'El código es obligatorio')
