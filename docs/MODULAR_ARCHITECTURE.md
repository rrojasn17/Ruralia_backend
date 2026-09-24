# Arquitectura modular multipropósito

## Objetivo

La aplicación se divide en un **núcleo estable** y **runtimes de industria**. El núcleo puede desplegarse sin dominio productivo activo y conserva autenticación, usuarios, permisos, configuración y el motor de módulos. Caficultura pasa a ser `mod_caficultura`; `mod_gestionfv` valida la expansión a una segunda industria sin duplicar identidad ni autenticación.

## 1. Núcleo (`api/core`)

Responsabilidades exclusivas del core:

- autenticación, sesiones y recuperación de contraseña;
- identidad global `Usuario`;
- permisos explícitos y RBAC core;
- configuración/branding de instancia;
- catálogo `core_app_modules`;
- importación e instalación de paquetes;
- exclusividad de módulos `industry`;
- lifecycle dispatcher y guards de módulo;
- CMS de usuarios y módulos.

Tablas core: `navia_usuarios`, `navia_sesiones_usuario`, `navia_password_reset_tokens`, `navia_configuracion_sistema` y `core_app_modules`.

Una base nueva no crea tablas de Caficultura ni GestiónFV hasta instalar el módulo correspondiente.

## 2. Descubrimiento de runtimes (`api/modules/registry.py`)

El registro contiene únicamente módulos cuyo código fue revisado e incluido en el despliegue, pero **no mantiene una lista manual de industrias**. Descubre por convención las carpetas `api/modules/mod_*` que incluyan:

- `manifest.py` / `module.json`;
- `lifecycle.py`;
- `runtime.py`, con routers y worker opcional.

El core no contiene imports concretos de Caficultura o GestiónFV. Agregar un runtime revisado consiste en desplegar su carpeta `mod_xxx`; el registry lo detecta durante el arranque. Importar un ZIP desde el CMS registra el paquete y su manifiesto, pero nunca ejecuta código arbitrario desde ese ZIP.

## 3. Contrato de `module.json`

Campos principales:

```json
{
  "key": "mod_gestionfv",
  "name": "GestiónFV",
  "version": "0.1.0",
  "module_type": "industry",
  "permissions": [],
  "roles": [],
  "role_permissions": {},
  "navigation": [],
  "route_prefixes": [],
  "public_route_prefixes": []
}
```

`module.json` es la fuente de verdad del backend. `manifest.py` únicamente carga este archivo.

## 4. Ciclo de vida

Estados:

`imported -> installed -> active -> disabled -> uninstalled`

Hooks disponibles:

- `install(db)`: crea/migra solo tablas del módulo;
- `upgrade_schema(db)`: actualización de schema en despliegue;
- `activate(db)`: prepara el módulo para uso y lo habilita;
- `deactivate(db)`: detiene comportamiento específico sin borrar datos;
- `uninstall(db)`: por política actual es no destructivo;
- `user_delete_blockers(db, user_id)`: evita borrar identidades referenciadas por datos de módulo.

La activación de una industria desactiva la industria anterior **en la misma transacción**. Primero se hace `flush` del estado `disabled` y después se marca el nuevo módulo `active`, respetando el índice único parcial de base de datos.

## 5. Seguridad del marketplace

Solo `Usuario.is_superadmin = true` puede:

- importar;
- instalar;
- activar;
- desactivar;
- desinstalar módulos.

Los roles de industria nunca reciben permiso global `*`. Root/SuperAdmin es el único sujeto que obtiene `*`; cada rol de módulo recibe permisos explícitos de su namespace/dominio.

## 6. Caficultura

`api/modules/mod_caficultura` es propietario de:

- clientes y fincas productivas;
- recibos y consecutivos;
- lotes/OT, aprobaciones y ventas;
- gestión de finca, trabajadores, actividades e insumos;
- trazabilidad y portal de recibos;
- IA agrícola, conocimiento y automatizaciones;
- IoT;
- notificaciones y worker de alertas.

El lifecycle de Caficultura calcula sus tablas por módulo y ejecuta `create_all(..., tables=...)`; nunca crea tablas de GestiónFV.

Los antiguos módulos `models/*`, `routers/*`, `schemas/*` y `services/*` que todavía son usados externamente quedan como **fachadas temporales de compatibilidad**. El runtime de Caficultura ya importa su implementación directamente desde `modules.mod_caficultura`.

## 7. GestiónFV

`mod_gestionfv` se incluye en esta fase como runtime mínimo de validación. Aporta:

- roles `gerente_fv`, `produccion_fv`, `calidad_fv`, `exportacion_fv`;
- permisos namespaced `gestionfv:*`;
- navegación propia;
- API `/gestionfv`;
- tabla propia `gestionfv_module_settings`;
- lifecycle independiente.

La lógica de producción, poscosecha, calidad y exportación se agregará en la siguiente fase sin modificar autenticación ni tabla de usuarios.

## 8. Frontend

El shell `layouts/admin.vue` contiene únicamente menú core. La navegación de industria se construye con `active_module.manifest.navigation` recibido de `/modules/runtime`.

`useModules` usa `route_prefixes` de manifiestos conocidos para impedir acceso a rutas de módulos inactivos. Las rutas públicas usan `public_route_prefixes` y consultan `/modules/public-runtime` antes de saltarse autenticación.

Los componentes y páginas de Caficultura se ubican en `app/modules/mod_caficultura`; GestiónFV usa `app/modules/mod_gestionfv`. `app/modules/runtime.ts` usa `import.meta.glob` para autodetectar `mod_*/manifest.ts` y `mod_*/pages/dashboard.vue`, por lo que el shell no necesita enumerar industrias. Los wrappers de `app/pages` conservan las URLs actuales de Nuxt y son una capa de routing, no implementación de negocio.

## 9. Migración de instalaciones existentes

Si `core_app_modules` todavía no existe y se detectan tablas históricas de Caficultura (`navia_recibos_cafe`, `navia_ordenes_trabajo`, `navia_clientes` o `navia_fincas`), el bootstrap registra `mod_caficultura` como activo con `source=legacy` y conserva todos los datos.

Una instalación nueva no registra ningún módulo industrial automáticamente. Root debe importar el paquete del módulo, instalarlo y activarlo desde CMS.

## 10. Workers y schema upgrade

`schema_upgrade.py` solo conoce core y despacha `upgrade_schema` al módulo industrial activo.

`module_worker.py` consulta la industria activa y ejecuta su `worker_entrypoint` si existe. Caficultura registra su worker de notificaciones; GestiónFV no tiene worker en esta fase.
