from __future__ import annotations

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from core.models import AppModule, ConfiguracionSistema, PasswordResetToken, SesionUsuario, Usuario
from core.module_manager import (
    BUILTIN_MANIFESTS,
    activate_module,
    get_active_industry_module,
    install_module,
    sync_builtin_modules,
)
from database import Base


def _add_catalog_module(db, key: str) -> AppModule:
    manifest = BUILTIN_MANIFESTS[key]
    row = AppModule(
        key=key,
        name=str(manifest["name"]),
        version=str(manifest.get("version") or "1.0.0"),
        module_type=str(manifest.get("module_type") or "industry"),
        description=str(manifest.get("description") or "") or None,
        status="imported",
        built_in=True,
        source="test",
        manifest=manifest,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_industry_modules_are_isolated_and_exclusive(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'modules.sqlite3'}", future=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Una instalación nueva crea solamente el core.
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ConfiguracionSistema.__table__,
            Usuario.__table__,
            SesionUsuario.__table__,
            PasswordResetToken.__table__,
            AppModule.__table__,
        ],
    )
    db = Session()
    try:
        initial_tables = set(inspect(engine).get_table_names())
        assert "gestionfv_module_settings" not in initial_tables
        assert "navia_recibos_cafe" not in initial_tables
        assert get_active_industry_module(db) is None

        _add_catalog_module(db, "mod_gestionfv")
        _add_catalog_module(db, "mod_caficultura")

        install_module(db, "mod_gestionfv")
        after_gf = set(inspect(engine).get_table_names())
        assert "gestionfv_module_settings" in after_gf
        assert "navia_recibos_cafe" not in after_gf

        activate_module(db, "mod_gestionfv")
        assert get_active_industry_module(db).key == "mod_gestionfv"

        # Instalar Caficultura crea solo su esquema; no cambia la industria activa.
        install_module(db, "mod_caficultura")
        after_cafe_install = set(inspect(engine).get_table_names())
        assert "navia_recibos_cafe" in after_cafe_install
        assert get_active_industry_module(db).key == "mod_gestionfv"

        # Activar una industria desactiva atómicamente la anterior.
        activate_module(db, "mod_caficultura")
        assert get_active_industry_module(db).key == "mod_caficultura"
        assert db.query(AppModule).filter(AppModule.key == "mod_gestionfv").one().status == "disabled"

        activate_module(db, "mod_gestionfv")
        assert get_active_industry_module(db).key == "mod_gestionfv"
        assert db.query(AppModule).filter(AppModule.key == "mod_caficultura").one().status == "disabled"
    finally:
        db.close()
        engine.dispose()


def test_all_builtin_module_manifests_satisfy_core_contract():
    from core.module_contract import validate_module_contract

    for key, manifest in BUILTIN_MANIFESTS.items():
        validate_module_contract(manifest)
        assert manifest["key"] == key


def test_module_contract_rejects_undeclared_permissions():
    import pytest
    from core.module_contract import validate_module_contract

    manifest = {
        "key": "mod_prueba",
        "name": "Prueba",
        "version": "1.0.0",
        "module_type": "industry",
        "permissions": ["prueba:view"],
        "role_permissions": {"gerente": ["prueba:manage"]},
        "roles": [{"key": "gerente", "label": "Gerente"}],
        "route_prefixes": ["/prueba"],
        "public_route_prefixes": [],
    }

    with pytest.raises(ValueError, match="permiso no declarado"):
        validate_module_contract(manifest)


def test_core_does_not_import_industry_implementations():
    """El App Base no debe volver a acoplarse accidentalmente a Caficultura/Flores."""
    from pathlib import Path

    core_dir = Path(__file__).resolve().parents[1] / "core"
    offenders: list[str] = []
    for path in core_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "modules.mod_" in text:
            offenders.append(str(path.relative_to(core_dir)))
    assert offenders == []



def test_builtin_modules_are_available_in_superadmin_store(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'module_store.sqlite3'}", future=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ConfiguracionSistema.__table__,
            Usuario.__table__,
            SesionUsuario.__table__,
            PasswordResetToken.__table__,
            AppModule.__table__,
        ],
    )
    db = Session()
    try:
        sync_builtin_modules(db)
        rows = {row.key: row for row in db.query(AppModule).all()}
        assert set(BUILTIN_MANIFESTS).issubset(rows)
        assert rows["mod_caficultura"].version == BUILTIN_MANIFESTS["mod_caficultura"]["version"]
        assert rows["mod_floresvolcan"].version == BUILTIN_MANIFESTS["mod_floresvolcan"]["version"]
        assert rows["mod_floresvolcan"].status == "uninstalled"
        assert get_active_industry_module(db) is None
    finally:
        db.close()
        engine.dispose()


def test_store_sync_updates_builtin_version_without_changing_status(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'module_store_update.sqlite3'}", future=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ConfiguracionSistema.__table__,
            Usuario.__table__,
            SesionUsuario.__table__,
            PasswordResetToken.__table__,
            AppModule.__table__,
        ],
    )
    db = Session()
    try:
        manifest = dict(BUILTIN_MANIFESTS["mod_caficultura"])
        old = AppModule(
            key="mod_caficultura",
            name="Caficultura",
            version="1.0.0",
            module_type="industry",
            status="disabled",
            built_in=True,
            source="upload",
            manifest={**manifest, "version": "1.0.0"},
        )
        db.add(old)
        db.commit()

        sync_builtin_modules(db)
        db.refresh(old)
        assert old.version == BUILTIN_MANIFESTS["mod_caficultura"]["version"]
        assert old.manifest["version"] == BUILTIN_MANIFESTS["mod_caficultura"]["version"]
        assert old.status == "disabled"
        assert old.source == "upload"
    finally:
        db.close()
        engine.dispose()
