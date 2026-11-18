# Respaldos independientes por módulo

El núcleo conserva la identidad y el acceso de la instalación: cuentas, contraseñas, sesiones, permisos globales, marca, catálogo de módulos y módulo activo. Un respaldo de industria no exporta esas tablas ni puede reemplazarlas.

Cada módulo declara un proveedor en su `runtime.py`:

```python
BACKUP_ENTRYPOINT = 'modules.mod_ejemplo.backup_provider:backup_spec'
```

La función retorna `core.module_backups.ModuleBackupSpec` con:

- `key` y `version`: identidad del módulo y versión de sus datos.
- `models`: modelos propios, ordenados según sus dependencias de restauración.
- `clear_models`: tablas transitorias propias que se limpian al reemplazar datos, sin exportar sesiones o tokens temporales.
- `roots`: directorios físicos autorizados. Los módulos nuevos deben usar carpetas con su clave, por ejemplo `uploads/mod_ejemplo` y `private_uploads/mod_ejemplo`.
- `file_paths(payload)`: rutas de adjuntos referenciados por sus registros, relativas a las raíces declaradas. No se recorren indiscriminadamente carpetas compartidas.
- `remap_json(model, values, id_maps)`: adapta referencias internas guardadas en JSON y rutas dependientes de la instalación.
- `upgrade_legacy(payload, warnings)`: adaptador opcional para formatos antiguos del mismo módulo.
- `after_restore(db)`: ajustes de negocio propios, dentro de la transacción. No debe cambiar configuración global, activar módulos ni borrar cuentas.

El registro descubre el proveedor junto con los demás puntos de entrada. Agregar un módulo requiere desplegar su runtime revisado; no requiere editar Café, Flores ni una lista de industrias dentro del motor de respaldos. Los ZIP no ejecutan código ni instalan módulos.

## Formato

El esquema `ruralia_module_backup_v1` contiene `module`, `module_version`, `app_version`, fecha, `tables`, `identities`, `memberships` y, para ZIP, el manifiesto SHA-256 de `files`. `tables` admite únicamente las tablas del proveedor. Se rechaza otro módulo, tablas o columnas desconocidas, identidades inválidas y adjuntos que no estén referenciados por los registros del módulo. Los formatos incompletos solo se aceptan cuando un adaptador de legado conocido lo permite.

`identities` contiene únicamente ID de origen, nombre y correo de los autores referenciados. La restauración los resuelve por correo, sin modificar ninguna cuenta existente. Un autor inexistente se registra como identidad histórica **inactiva**, con contraseña aleatoria local, rol global sin acceso administrativo y sin permisos. No se copian contraseñas ni privilegios del origen. El resultado informa `identidades_nuevas`; la activación y el acceso siguen bajo control del administrador del núcleo.

`memberships` incluye solo los roles de este módulo. Restaurarlos requiere Root y nunca cambia asignaciones de otra industria. Los respaldos antiguos que no incluían esta sección conservan las asignaciones locales.

## API

Root puede operar una industria instalada, aunque otra esté activa:

- `GET /modules/{module_key}/backups/export?format=zip|json`
- `POST /modules/{module_key}/backups/import?mode=replace|merge`, con `file` multipart.

Las rutas requieren sesión autenticada de Root. Un módulo no instalado o sin proveedor retorna 404. Un formato, módulo o alcance incorrecto retorna 422; un conflicto de integridad retorna 409. La importación valida el formato antes de borrar registros y conserva la transacción y los archivos anteriores si falla.

`replace` reemplaza únicamente las tablas y adjuntos referenciados del módulo seleccionado. `merge` combina sus identidades de negocio y remapea sus referencias. Los archivos de otras industrias, los archivos globales y los archivos sin pertenencia demostrable se conservan. Los JSON no reemplazan archivos. La restauración no instala, activa ni cambia de industria.

Caficultura conserva sus rutas `/respaldos/exportar` y `/respaldos/importar` como adaptadores al mismo motor. Flores usa el mismo formato en sus respaldos manuales y programados y ofrece restauración a Root desde sus Ajustes. Los Excel de Flores se conservan como archivos auxiliares de consulta dentro del ZIP.

## Compatibilidad

Los respaldos anteriores de Café `navia_backup_v1` y `navia_backup_v2` siguen siendo legibles. Sus usuarios se tratan únicamente como referencias históricas; se ignoran hashes, permisos, marca y demás configuración global. También se omiten los archivos del ZIP anterior que no pertenecen a registros de Café. La respuesta informa las omisiones.

Los ZIP anteriores de Flores que solo contenían `floresvolcan_data.json` y Excel no declaraban identidades ni adjuntos y no eran un formato de restauración. Se conservan para consulta; para una copia restaurable se debe generar un respaldo con la versión actualizada. No se atribuyen usuarios del origen por coincidencia de IDs entre instalaciones.

Los archivos o credenciales del núcleo requieren el procedimiento de copia de la plataforma. Un respaldo de módulo no sustituye una copia integral de PostgreSQL y la configuración del despliegue.

## Verificación

`api/tests/test_module_backups.py` comprueba aislamiento en ambas direcciones, preservación de cuentas y marca, rechazo de alcance incorrecto, referencias históricas sin acceso y un tercer proveedor ficticio que se integra mediante el registro existente. Las pruebas de snapshots e IoT mantienen las garantías de integridad, reversión, adjuntos, fórmulas y compatibilidad.
