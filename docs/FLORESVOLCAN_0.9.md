# Flores Volcán 0.9 — jerarquía, hojas de campo y rendimiento

Aplicado en el entorno local el 5 de noviembre de 2026. Conserva las cuentas y los datos de Excel de 0.8.

## Jerarquía

| Capacidad | Superusuario | Gerente | Administrador | Trabajador |
| --- | --- | --- | --- | --- |
| Borrar o anular registros | Sí | No | No | No |
| Instalar, activar o desinstalar módulos | Sí | No | No | No |
| Crear y administrar usuarios | Sí | Sí, sin modificar superusuarios | No | No |
| Configuración general y del módulo | Sí | Sí | No | No |
| Catálogo Materiales (SAP) | Sí | Sí | No | No |
| Compras | Sí | Sí | Sí | No |
| Aprobar compras | Sí | Sí | No | No |
| Lirios y operación | Sí | Sí | Sí | Solo tareas asignadas |
| Revisar y aprobar hojas fotografiadas | Sí | Sí | Sí | No |
| Registrar siembra y cosecha directamente | Sí | Sí | Sí | En sus órdenes autorizadas |

El superusuario se identifica por `is_superadmin`, separado del rol. El módulo ofrece exactamente gerente, administrador (`admin`) y trabajador. Los perfiles antiguos distintos no heredan acceso: deben reclasificarse expresamente desde Usuarios. Las cuentas existentes no se cambian automáticamente.

El manifiesto declara techos de permisos (`enforce_role_permissions`), métodos y acciones exclusivos del superusuario. El núcleo los interpreta sin importar implementaciones de industria. Los permisos explícitos antiguos, incluido `*`, no amplían esos techos. Usuarios y configuración general siguen la jerarquía global. Las restricciones se verifican en el servidor y la navegación las refleja.

El trabajador usa la interfaz de campo con diseño móvil, también al abrirla en una pantalla grande. No se confía en el ancho de pantalla como control de seguridad: la API limita las tareas por cuenta, trabajador activo y cuadrilla vigente. El panel gerencial está reservado a supervisión; filtra alertas por permiso y devuelve la identidad de sesión. Los trabajadores ven únicamente sus órdenes y el estado de sus hojas.

Desactivar trabajadores y proveedores usa PATCH de estado y conserva el historial. DELETE y la anulación de cortas son exclusivos del superusuario. La desactivación de proveedor comprueba órdenes pendientes.

## Papel → imagen → revisión

1. En Mi trabajo, seleccionar una orden autorizada y su material o segmento de siembra.
2. Descargar una hoja PDF carta con orden, variedad, lote, mesa y trabajador precargados. Los conteos reales quedan en blanco para escribir con lapicero.
3. Fotografiar o subir la hoja. Se valida y normaliza la imagen y se conserva como evidencia privada.
4. La IA propone cantidades y fecha. Si no puede leerla, sigue disponible la transcripción manual. Una sugerencia no modifica saldos.
5. El trabajador completa los datos y pulsa **Enviar a revisión**. Puede retomar hojas aún no enviadas desde Mi trabajo. Una hoja enviada queda bloqueada para evitar cambios mientras se revisa.
6. En **Revisar hojas**, un administrador o gerente compara la foto con la orden, material, lote, mesa y cifras. Puede corregir fecha, cantidades, lado y observaciones. Puede rechazar indicando motivo. Otro usuario debe aprobar las hojas propias; el superusuario conserva acceso total.
7. **Aprobar y registrar** ejecuta los mismos controles transaccionales del registro directo: material entregado, saldo de inventario, ocupación de mesa, fecha real, segmento de siembra, ciclo y saldo técnico de cosecha. Un fallo conserva la hoja pendiente y no consume stock.
8. La imagen, propuesta de IA, datos enviados, correcciones aprobadas, trabajador original y revisor permanecen trazables. Repetir una aprobación no duplica el movimiento.

Los registros directos sin hoja siguen disponibles para trabajadores autorizados. Las hojas requieren aprobación aunque quien las subió complete manualmente los números.

### Contrato añadido

- `GET /floresvolcan/field/paper/mine`: últimas 30 hojas del usuario.
- `POST /floresvolcan/field/paper/captures/{id}/submit`: validar y enviar datos; no contabiliza. Reenvío idéntico idempotente; cambios durante revisión devuelven 409.
- `GET /floresvolcan/paper-reviews`: primeras 100 hojas pendientes, antiguas primero; al revisarlas aparecen las siguientes.
- `GET /floresvolcan/paper-reviews/{id}`: contexto para comparar la evidencia.
- `POST /floresvolcan/paper-reviews/{id}/approve`: revisar y contabilizar de forma atómica. Solo permite corregir mediciones, fechas y observaciones; no sustituir IDs de orden, lote, material, mesa o segmento.
- `POST /floresvolcan/paper-reviews/{id}/reject`: conservar evidencia y motivo sin contabilizar.
- `PATCH /floresvolcan/workers/{id}/status` y `/suppliers/{id}/status`: desactivar/reactivar sin borrar.

El servidor conserva las rutas previas de impresión, carga de foto e imagen protegida. No se exponen originales como archivos públicos. Los permisos de revisión son `floresvolcan:paper:review`.

## Rendimiento medido con los datos locales

Misma base: 375 lotes productivos, 375 siembras, 1.020 cortas y 653 lotes de inventario. Medición de funciones de consulta dentro del contenedor; no incluye red, descarga de JavaScript o renderizado del navegador.

| Consulta | Antes | Después | SQL antes → después |
| --- | --- | --- | --- |
| Panel gerencial | 1.372 ms | 172 ms | 2.276 → 31 |
| Lotes productivos | 1.755 ms | 54 ms | 3.376 → 9 |
| Calendario de cosecha | 2.892 ms | 83 ms | 5.400 → 12 |

Se precargan registros por colección en vez de consultar por cada lote. Se conserva el cálculo por segmento, los descartes, las conciliaciones, las cortas anuladas y los excedentes autorizados. No hay caché compartida de saldos ni se reduce el histórico para aparentar rapidez.

## Verificación y límites

La batería cubre techos de permisos con concesiones heredadas, aprobación de compras, exclusividad del borrado y de instalar módulos, bloqueo de trabajadores, fotografías de siembra y cosecha, atribución al trabajador, reintentos, revisión fallida sin movimientos y equivalencia de cálculos individuales y en bloque. Resultado: 57 pruebas aprobadas y 3 casos de concurrencia PostgreSQL omitidos en esta batería. La prueba de navegador aprobó las 8 combinaciones de cuatro identidades ficticias y dos anchos de pantalla (1440 y 390 px), incluyendo una aprobación real sobre la SQLite temporal. La compilación Nuxt y el lint de los archivos de control y pruebas añadidos pasaron.

La lectura de IA está configurada en el entorno local. No se ha evaluado su precisión con una nueva hoja manuscrita real del cliente; las pruebas no envían documentos a un servicio externo. La aprobación humana sigue siendo obligatoria. El chequeo general de TypeScript conserva errores previos fuera de Flores; la compilación del frontend se verifica por separado.

Copia privada previa a activar: `.local-backups/floresvolcan_roles_20261105/before-v09.pgdump` (ignorada por Git). No se ejecuta nuevamente la precarga ni se reinicia la base.
