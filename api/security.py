from __future__ import annotations

import hashlib
import secrets

from passlib.context import CryptContext

_contexto_pwd = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")
_DUMMY_PASSWORD_HASH = _contexto_pwd.hash("NAVIA-dummy-password-never-used-2026!")


def generar_token(bytes_length: int = 48) -> str:
    return secrets.token_urlsafe(bytes_length)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hashear_contrasena(contrasena: str) -> str:
    return _contexto_pwd.hash(contrasena)


def verificar_contrasena(contrasena: str, hash_contrasena: str | None) -> bool:
    candidate_hash = hash_contrasena or _DUMMY_PASSWORD_HASH
    try:
        return _contexto_pwd.verify(contrasena, candidate_hash)
    except (TypeError, ValueError):
        return False


def verificar_contrasena_dummy(contrasena: str) -> None:
    """Consume un costo bcrypt similar cuando el usuario no existe."""
    verificar_contrasena(contrasena, _DUMMY_PASSWORD_HASH)


def validar_fortaleza_contrasena(contrasena: str) -> None:
    errors: list[str] = []
    if len(contrasena) < 10:
        errors.append("al menos 10 caracteres")
    if not any(ch.islower() for ch in contrasena):
        errors.append("una letra minúscula")
    if not any(ch.isupper() for ch in contrasena):
        errors.append("una letra mayúscula")
    if not any(ch.isdigit() for ch in contrasena):
        errors.append("un número")
    if not any(not ch.isalnum() for ch in contrasena):
        errors.append("un símbolo")
    if errors:
        raise ValueError("La contraseña debe incluir " + ", ".join(errors) + ".")
