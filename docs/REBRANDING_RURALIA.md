# Marca ruralIA

Cambio aplicado el 5 de noviembre de 2026 en backend y frontend.

La marca pública se escribe **ruralIA**. Se actualizaron textos de interfaz, pantalla de carga, modo sin conexión, correos, mensajes de IA, exportaciones, QR nuevos, documentación, estilos, nombre del paquete frontend y servicios de ejecución. Las guías anteriores ahora se llaman `README_RURALIA_*`. El backend publica `ruralIA API` y la imagen local es `ruralia-api`.

La configuración pública guardada con la marca anterior, o con otra capitalización de ruralIA, se normaliza al iniciar. Las marcas de empresas que tienen otro nombre se conservan. El frontend también reconoce textos históricos de la marca anterior cuando aplica la personalización.

## Compatibilidad conservada

Los nombres físicos de tablas, claves foráneas, índices, esquemas de respaldos existentes, cookie de sesión, variables de integración existentes y red interna conservan sus identificadores históricos. Son contratos de persistencia, no textos públicos. No se renombraron cuentas, correos de acceso ni contraseñas. Los respaldos históricos siguen siendo importables.

La cola sin conexión utiliza el prefijo nuevo `ruralia_offline_queue_v2_user_`. Copia la cola anterior exclusivamente del usuario autenticado, conserva identificadores de idempotencia, evita duplicados y retiene el original si no puede leerlo o escribirlo. La limpieza de caché reconoce ambos prefijos de marca; también se conserva la preferencia anterior de la barra lateral.

## Verificación

- Compilación Nuxt de producción.
- 60 pruebas backend aprobadas: marca personalizable, respaldo/restauración, arquitectura modular, permisos y conciliación de Flores. Tres casos de concurrencia PostgreSQL omitidos en esta batería.
- Seis escenarios de inicio de sesión en Chrome sobre la vista previa de producción, con API simuladas y sin credenciales reales; se comprueba que la pantalla utiliza ruralIA.
- Migración de colas: aislamiento por usuario, conservación de registros e idempotencia, mezcla sin duplicados y conservación de originales ilegibles (`tests/offline-rebrand.mjs` en frontend).
- API y trabajador de fondo saludables tras la actualización local. No se recreó la base ni se volvió a importar Excel.
