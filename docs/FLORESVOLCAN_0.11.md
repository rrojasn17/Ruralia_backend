# Flores Volcán 0.11 — espacios, registro y seguimiento

Implementación preparada el 5 de noviembre de 2026. Conserva los cambios existentes de la versión 0.10, sus camas múltiples por orden y los datos históricos. No vuelve a importar ni elimina datos del cliente.

## Uso

- **Fincas y espacios:** una finca o invernadero puede vincularse con un enlace de Google Maps. Su ficha abre el mapa en vista satélite. Las camas heredan el lugar y ya no solicitan coordenadas.
- Al configurar la capacidad de una cama, **Agregar nuevo cultivo** permite crear cultivos adicionales. Se indica el total de unidades por cama y cultivo; el sistema calcula unidades/m² a partir del largo y ancho.
- **Materiales (SAP):** permite seleccionar el cultivo, unidades, densidad y ciclo. En Lirios se completan únicamente los valores inequívocos documentados por variedad y calibre en Lirio.xlsx; los ambiguos requieren configuración. No se sobrescriben valores ya configurados.
- Los colores presentan nombres completos. WK se muestra **Blanco (WK)**, según la interpretación confirmada por el usuario; se conserva el código original para trazabilidad.
- **Trabajadores:** muestra el código individual de cuatro dígitos. Los códigos nuevos se generan al azar, sin repetir; los códigos anteriores que no cumplen el formato se conservan como referencia interna y se sustituyen al migrar. Los códigos válidos existentes se conservan.
- **Orden de siembra:** la ficha identifica el lote de procedencia y permite compartir el acceso de registro con la cuadrilla. Cosecha también permite compartir su orden.
- El enlace compartido solicita únicamente el código del trabajador activo y asignado a la cuadrilla. Abre exclusivamente esa orden; no crea una sesión del sistema ni muestra navegación administrativa.
- En **Mi trabajo**, densidad y ciclo provienen del material y no son editables. El backend valida también esa regla y registra el trabajador identificado.
- **Compras:** el tránsito se registra con fechas reales de pago y envío. Aprobar una compra no presupone que ya se pagó o despachó.
- El dashboard incorpora unidades en cultivo, bulbos en bodega, cajas equivalentes, compras en tránsito y aprobaciones pendientes según los permisos de la sesión.
- En **Ajustes → Asistente**, gerencia/superusuario configura modelo, instrucciones y clave de OpenAI. La clave se escribe, pero no se devuelve al navegador. Si falta la clave o falla el proveedor se muestra un estado calculado por reglas, identificado mediante el icono de información.

## Controles

El acceso compartido usa una cookie HttpOnly independiente, con vencimiento de doce horas, protección CSRF, límites de intentos por enlace e IP y validación de asignación vigente. La baja del trabajador, cambio de código, cierre de la orden o retiro de la cuadrilla revocan el acceso. El código no se guarda en los registros de campo ni en las propuestas de revisión de hojas.

Las siembras conservan los controles de inventario, fechas e idempotencia. Se añade la comprobación de ocupación acumulada entre órdenes distintas de una misma cama y ciclo; se congela la capacidad utilizada en el registro. No se inventan dimensiones para camas históricas.

El asistente recibe resúmenes autorizados, sin claves ni códigos de trabajadores. El trabajador recibe solo información de sus órdenes. La generación usa OpenAI Responses con salida estructurada, `store=false`, límite de 300 caracteres, caché de cinco minutos por usuario/permisos/configuración y ejecución en segundo plano. La interfaz no espera la respuesta de OpenAI para cargar. Referencia: [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

## Validación

- Suite final del módulo, roles, conciliación, Excel, paginación, espacios y mapas: **94 aprobadas, 3 omitidas** (requieren PostgreSQL para concurrencia).
- Suite de arquitectura de módulos: **6 aprobadas** (100 pruebas aprobadas en total).
- Compilación Nuxt de producción aislada: aprobada. El chequeo TypeScript no informa errores en Flores; persisten 27 errores fuera de este módulo.
- Navegador de escritorio y móvil (1440 y 390 px): altas de finca/cama/cultivo, mapa satélite, listas, código automático, dashboard, configuración y registro mediante enlace aprobados.
- SAP en móvil: nombres de colores y configuración técnica visibles, sin desbordamiento horizontal.
- Restricciones de los cuatro roles y revisión de hojas: aprobadas en escritorio y móvil.
- Integración OpenAI probada con respuesta simulada. No se ha verificado una llamada al proveedor en el sistema activo.

## Activación pendiente de autorización

Se creó un respaldo privado de la base en `.local-backups/floresvolcan_v11_20261105/database.pgdump`, y se construyó la imagen `ruralia-api:1.4.0`. La imagen anterior queda etiquetada como `ruralia-api:fv-before-v11`.

**La aplicación activa aún no recibió esta actualización.** La revisión automática rechazó detener los servicios y ejecutar la migración por requerir autorización explícita para interrumpir la API y modificar la base activa. No se ejecutó la migración ni se reiniciaron los servicios.

Tras autorización: detener brevemente API/worker, ejecutar `docker compose run --rm schema-init`, iniciar API/worker con la imagen nueva y comprobar salud, versión 0.11.0, conteos históricos, unicidad de códigos y configuración del asistente. No recrear PostgreSQL ni sus volúmenes. La migración es aditiva, pero normaliza códigos y completa configuraciones; un retorno de imagen no revierte esos cambios de datos.

## Corrección activa de códigos — 5 de noviembre de 2026

A petición expresa del usuario se aplicó **únicamente** la normalización de trabajadores en la base activa, sin detener servicios ni activar el resto de 0.11. Resultado: 38 trabajadores, 38 códigos de cuatro dígitos, cero duplicados y 38 referencias anteriores conservadas. Un trigger de compatibilidad normaliza nuevas escrituras de importadores antiguos; una restricción CHECK y la unicidad existente garantizan el formato y la no repetición. La migración reproducible está en `api/modules/mod_floresvolcan/migrations/worker_codes_4_digits.sql` y el ciclo de actualización la instala en PostgreSQL.

Antes de aplicar se probó toda la transacción con ROLLBACK, incluyendo inserción con código antiguo y rechazo de duplicados. Respaldo previo: `.local-backups/floresvolcan_v11_20261105/before_worker_codes.pgdump`. Las pruebas de inserción se revirtieron; no quedan trabajadores de prueba. El resto de la activación descrita anteriormente sigue pendiente.

## Corrección del enlace de cuadrilla y aviso de disponibilidad

El frontend anterior admitía crear un URL terminado en `/registro/planting/` cuando la API activa no devolvía `qr_token`. Ahora el QR y compartir exigen un token y una orden aprobada/en proceso. Un endpoint POST protegido habilita el token de órdenes antiguas, preservándolo en solicitudes posteriores. Si la API aún no fue actualizada, se muestra el problema y se deshabilita compartir; no se fabrica un enlace incompleto.

La consulta activa encontró una orden aprobada sin token y 301 ciclos históricos en conciliación. No se cerraron ni liberaron ciclos históricos automáticamente. Las opciones de cama en tareas incluyen su disponibilidad y el motivo del bloqueo; Mi trabajo y el acceso compartido lo muestran antes del envío y deshabilitan el registro directo si la cama está bloqueada. El encargado puede ir a Cosecha → Limpieza de mesas desde Mi trabajo. Se mantiene la validación al guardar frente a cambios simultáneos.

Validación adicional: pruebas nuevas de token estable/estado y consulta de disponibilidad sin alterar el historial; compilación Nuxt aislada aprobada. El URL con `localhost` sirve únicamente en la computadora donde corre la aplicación: al compartir con teléfonos se necesita la dirección de red o dominio accesible de esa aplicación. No se ha supuesto una IP ni modificado la exposición de red.

Para activar estas correcciones de servidor se requiere actualizar la API y ejecutar las migraciones de 0.11, reconstruyendo antes la imagen para incluir los ajustes más recientes. Sigue pendiente la autorización de la breve interrupción solicitada en el cierre anterior; solo se aplicó en vivo la corrección específica de códigos de trabajador.

### Liberación solicitada y aplicada

El usuario confirmó: «limpialas, dejalas listas para seguir usando el app, y guarda historicos». Se aplicó exclusivamente `migrations/release_imported_beds_20261105.sql`, con respaldo previo `before_bed_release.pgdump` y prueba transaccional con ROLLBACK. Quedaron 301 ciclos limpios y 375 lotes de órdenes históricas archivados (`status=historical`); se conservaron exactamente 375 registros de siembra y 1.020 de cosecha. Se guardó el estado previo de ciclos/lotes en `maintenance.release_imported_beds.20261105`. No se modificaron cantidades ni se fabricaron conciliaciones. Ciclos con órdenes operativas compartidas quedan excluidos.

### Eliminación posterior del historial de camas

El usuario cambió la instrucción y solicitó borrar ese historial importado, dejando las camas vacías. Se aplicó `migrations/remove_imported_bed_history_20261105.sql` después de un respaldo privado (`before_remove_bed_history.pgdump`) y prueba con ROLLBACK. Se eliminaron 375 siembras, 1.020 cosechas, 375 lotes productivos, 301 ciclos de cama y 131 órdenes de cosecha asociadas exclusivamente al histórico. Las cuatro tablas de ocupación/registro quedaron sin filas. No se alteraron las camas, trabajadores, proveedores, materiales, inventario, movimientos, requisiciones ni las dos órdenes nuevas; se compararon sus conteos antes y después. Las requisiciones importadas se conservan como trazabilidad del inventario; no ocupan camas. Las hojas originales del Excel también se conservan. No se ejecuta este mantenimiento desde el arranque de la aplicación.

## Activación completada

Tras la autorización explícita «dale», se creó `before_activation.pgdump`, se reconstruyó la imagen, se detuvieron brevemente API/worker, se ejecutó `schema-init` y se iniciaron ambos servicios. La API y el worker quedaron saludables; `/ready` confirmó la base disponible y el módulo 0.11.0 activo.

La orden operativa devuelve el mismo token desde su detalle y desde el endpoint de compartir. Se verificó por HTTP real el login de trabajador asignado (200), la sesión limitada a esa orden (200) y su cierre posterior, sin crear registros de campo en la prueba. Los códigos siguen siendo de cuatro dígitos. Al activar ya existía una nueva siembra operativa: se conservó junto a su ciclo, y no se reintrodujeron los históricos eliminados (cero cosechas históricas).

### Corrección CORS del registro compartido

La API respondía 400 al preflight OPTIONS de siembra porque faltaba `X-FV-CSRF` en `allow_headers` de `main.py`. Los fixtures de UI usaban comodín y no detectaban esa diferencia. Se agregó únicamente esta cabecera a la lista permitida y una prueba que reutiliza la configuración CORS real para siembra, cosecha y cierre de sesión. La prueba pasó; se reconstruyó y reinició solo la API, sin migración ni modificación de datos. Un navegador real desde `localhost:3027` pudo enviar el POST a `localhost:8027` con esa cabecera y leer el 401 JSON esperado sin sesión; no se registró una siembra de prueba.

### Cantidad obligatoria en el registro compartido

El formulario público permitía enviar `quantity_planted=null` porque el indicador visual de requerido no imponía validación y faltaba el control previo en `save`. Se exige ahora cantidad entera positiva, material, cama y fecha válidos, sin superar el saldo de la orden. El campo tiene también `required` nativo. Tras guardar se vacía la cantidad y se exige nuevamente antes de otro envío; se conserva la idempotencia para reintentos. Se agregaron controles equivalentes básicos de cosecha y traducción de los errores de validación del servidor a texto de usuario. Mi trabajo rechaza también cantidades no numéricas antes de enviarlas.

Prueba de navegador sobre el frontend activo en móvil y escritorio, con API de campo completamente simulada: cantidad vacía no genera POST, 20 llega como número, un segundo envío vacío se bloquea y un 422 de Decimal se muestra como mensaje legible. No se escribieron datos del cliente. No requiere migración ni reinicio de la API.

### Ficha de siembra: resumen, bitácora y ubicación

La ficha `/floresvolcan/produccion/:id` muestra primero lo planificado, sembrado, faltante del plan y material entregado sin sembrar (descuenta devoluciones/pérdidas). Cada lote desglosa sus cantidades; un plan no configurado se muestra como no definido. El QR y las acciones de cuadrilla quedan arriba a la derecha en escritorio y debajo del resumen en móvil.

La bitácora muestra cantidades, trabajador identificado, usuario que cargó cuando corresponde, observaciones y origen del lote. La hora de registro se obtiene del movimiento de consumo creado al guardar, separada de la fecha real del trabajo, en zona Costa Rica. Si falta esa evidencia no se inventa una hora. La ubicación se reconstruye desde la cama registrada y sus ancestros; finca o invernadero ausentes se indican como no registrados. Las camas solamente habilitadas se distinguen de las que ya tienen siembra.

El backend usa consultas agrupadas y pagina la bitácora de 40 en 40, manteniendo los totales de toda la orden. La conciliación de cada lote se carga únicamente al abrirla. Nuevas pruebas cubren autor, hora, origen, jerarquía completa, devoluciones/pérdidas y paginación. Se verificaron también los casos existentes de acceso compartido y camas. Compilación aislada aprobada; TypeScript mantiene los 27 errores anteriores fuera de Flores. La API fue reconstruida y actualizada sin migraciones ni cambios en los datos; la ficha operativa real devolvió sus tres registros con autor y hora.

### Identidad de nuevos espacios de cultivo

Fincas e invernaderos nuevos requieren un código de 1 a 4 letras o números, normalizado a mayúsculas. La cama admite un nombre alfanumérico de 1 a 4 caracteres (incluido un solo dígito); el servidor genera su código como `CODIGO_PADRE-NOMBRE`, por ejemplo `F01-12B`. El código de cama se muestra de solo lectura tanto al crear como al editar y se deriva del padre seleccionado. Se valida el formato y la unicidad también en la API, ignorando un código manual enviado para una cama. Si el padre importado tiene un código largo, se solicita corregirlo antes de crear nuevas camas. No se renombraron masivamente los espacios existentes; se permite conservar una identidad importada al editar otros atributos.

Pruebas: código largo o con guiones rechazado, normalización a mayúsculas, generación desde finca e invernadero, nombres de un carácter, nombres inválidos y duplicados. Los casos de mapas se adaptaron al formato nuevo conservando sus verificaciones. El cambio no requiere migraciones ni modificaciones de los datos existentes.

Se actualizó la API activa. Compilación Nuxt aprobada, 22 pruebas de backend relevantes aprobadas y flujo de navegador en 1440/390 px aprobado, incluyendo creación de la cama con código derivado. El chequeo TypeScript conserva únicamente los 27 errores anteriores fuera de Flores.

### Ampliación a seis caracteres

Se amplió a seis caracteres el código de finca/invernadero, en formulario y API, manteniendo cuatro para el nombre de cama. La prueba valida un invernadero INV017 y su cama INV017-12B. API reconstruida y actualizada. El recorte masivo solicitado está pendiente de decisión del usuario: 21 códigos actuales comparten los primeros seis caracteres `LIRIO-`, por lo que el recorte literal viola la unicidad. Se preguntó si se deben generar códigos únicos conservando el distintivo o mantener los existentes por ahora; no se modificaron esos registros ni los prefijos de sus camas.

### Acrónimos aplicados desde nombres originales

El usuario resolvió las colisiones solicitando acrónimos derivados de los nombres originales. Se aplicó `migrations/space_acronyms_20261105.py --apply` tras comprobar el plan sin escribir y crear `before_space_acronyms.pgdump`. Se asignaron 22 códigos únicos de exactamente seis caracteres: por ejemplo Lirios→LIRIOS, BG2→INVBG2, 7V→INV07V, CATALINAS→CATALI, VICTORIA A→VICTOA y VICTORIA B→VICTOB. Se actualizaron los prefijos de las 949 camas, conservando sus sufijos, nombres e IDs. Verificación: cero códigos repetidos, cero prefijos incoherentes, tres siembras operativas conservadas y cero cosechas. El detalle previo de cada código quedó registrado en `maintenance.space_acronyms.20261105`. No se reinició la API ni se modificaron cantidades o relaciones.

### Códigos como acceso a ficha y materiales de ocho caracteres

El código de cada fila en Espacios y Proveedores abre su perfil; en Trabajadores y Materiales abre su ficha de consulta existente. Se conserva la navegación accesible mediante enlace o botón, separada de las acciones laterales.

Se recortaron los 253 códigos de materiales a sus primeros ocho caracteres, después de verificar que no hay colisiones (también sin distinguir mayúsculas), crear `before_material_codes8.pgdump` y registrar el mapa anterior/nuevo en `maintenance.material_codes_8.20261105`. Se conservaron los IDs y las relaciones; no se reescribieron snapshots históricos de documentos. Verificación final: 253 materiales, máximo ocho caracteres, cero duplicados.

El formulario y `SapProductIn` limitan los códigos a ocho; una restricción en la base activa también impide exceder ese máximo. La generación de códigos desde Excel se adaptó a ocho caracteres y resuelve colisiones antes de guardar. Las 30 pruebas de acceso compartido y precarga Excel pasaron. TypeScript continúa con los 27 errores anteriores fuera de Flores. API reconstruida y actualizada.

### Fecha prevista y calendario automático de cosecha

Se restituyó la fecha prevista en cada fila de Siembra, manteniendo fuera a los integrantes de cuadrilla. Cosecha presenta código enlazado a la ficha de la orden y al lote concreto, variedad, ubicación del invernadero (o finca) y cama, semanas transcurridas/ciclo junto al estado. El detalle operativo permanece desplegable por separado.

La madurez estimada cambia automáticamente en cada consulta: más de 21 días pendientes, en cultivo; desde 21 días antes, próximo a cosechar; fecha prevista alcanzada o vencida, lista para cosechar. La fecha se calcula desde cada siembra y su ciclo congelado, con el ciclo del lote como respaldo; no se recalcula desde un SAP editado posteriormente. Sin ciclo válido no se inventa una fecha. El lote toma el segmento con vencimiento más próximo; el aviso no implica que todos sus tallos estén listos ni sustituye el registro selectivo.

Estados operativos como programada/en cosecha/cerrada conservan prioridad; el aviso de madurez se muestra adicionalmente cuando corresponde. No se crean cosechas, cambios de inventario ni cierres automáticos. La página visible consulta cada minuto y al recuperar foco, con actualización silenciosa de la lista. No hace falta una tarea cron para estos estados calculados.

Validación: 31 pruebas aprobadas; límites de 22/21/0 días, vencidos, ciclo ausente y cambio posterior del catálogo. Navegador de escritorio/móvil aprobado: fecha, variedad, ubicación, semanas, estado y navegación a ficha. Compilación aprobada; 27 errores TypeScript anteriores fuera de Flores, ninguno nuevo del módulo. API actualizada sin migrar ni modificar datos.

### Operaciones terminadas y cierre progresivo de cosecha (2026-11-05)

- Compras completadas, lotes sin unidades disponibles y salidas entregadas se consultan en grupos cerrados por defecto. Cada grupo solicita páginas de 40 filas al desplegarse; no elimina registros.
- La cabecera de una siembra permite registrar manualmente y resolver material entregado sin sembrar: devolución de sobrantes o baja por daño, con cantidad y motivo. Conserva los controles transaccionales e idempotencia de inventario.
- Las órdenes de cosecha mantienen cuadrilla, aprobación, enlace restringido para trabajadores y cortes parciales. Con una corta válida el lote permanece «En cosecha» aunque se complete la orden; solo el cierre técnico termina el ciclo.
- El administrador concilia cada segmento y documenta las mermas. Si existe diferencia física respecto al potencial de la siembra, se conserva la solicitud en «En revisión de gerencia», sin cerrar ni liberar la cama. Gerencia puede aprobar o devolver con comentario. Superusuario conserva acceso completo.
- La decisión conserva solicitante, fecha, balance, explicaciones y resoluciones previas. Un cambio de balance invalida la aprobación y exige devolver para corregir. Las revisiones pendientes aparecen en el dashboard de gerencia y en el filtro «En revisión» de cosecha.
- La comparación usa el potencial configurado del cultivo y la salida física (venta más descarte); no presupone un tallo por bulbo para todas las variedades.

Validación: pruebas aisladas de conciliación y acceso de campo; aprobación/devolución por rol y rechazo de balances modificados; compilación Nuxt y navegador en 1440/390 px para grupos diferidos y ficha de siembra. TypeScript conserva 27 errores previos fuera de Flores Volcán, sin errores nuevos del módulo.

### Preparación con material y gerente asignado (2026-11-05)

Las nuevas preparaciones del formulario incluyen camas, material disponible (variedad, color y común), lotes concretos, cantidades, cuadrilla y gerente aprobador. El selector de material agrupa existencias y el de inventario filtra por esa identidad, usando los valores del lote. Las cantidades pueden ocupar parte de la capacidad de las camas.

Crear la preparación guarda una instantánea del material y genera la solicitud de salida pendiente, sin mover inventario. El administrador prepara; únicamente el gerente asignado o el superusuario aprueba. Una sola aprobación reserva las cantidades y aprueba tanto siembra como salida. La entrega física sigue siendo una operación explícita de bodega.

Antes de aprobar se bloquean y revisan existencias, identidad del material e integridad de las líneas. Un conflicto deja ambas operaciones pendientes; no hay reserva parcial persistida. La ficha conserva solicitante, gerente elegido, aprobador real, fecha/hora y solicitud enlazada. El dashboard no duplica esa aprobación y la atribuye al gerente asignado. Los registros anteriores mantienen su trazabilidad; no se infieren selecciones históricas.

El plan usa `ModuleSetting` con clave `planting_plan.<id>` y las tablas existentes de solicitudes, líneas y movimientos; no requiere migración de esquema. Las integraciones administrativas antiguas del superusuario conservan compatibilidad, mientras el formulario exige el plan completo.

Validación: 63 pruebas correctas y 3 casos PostgreSQL omitidos en SQLite; 8 pruebas específicas cubren gerente asignado, disponibilidad cambiante, material alterado, capacidad, duplicados, filtros y reserva única. Nuxt compila; la revisión TypeScript conserva los 27 errores previos fuera de Flores.

Navegador: flujo completo correcto a 1440 y 390 px, desde búsqueda de finca y selección de material hasta aprobación por el gerente elegido y comprobación de la salida reservada. La fecha prevista vacía se envía como `null`. Seleccionar una preparación que ya tiene salida abre su solicitud existente.

### Acceso de cuadrilla antes de entregar material (2026-11-05)

El enlace de trabajador incluye los materiales reservados con entrega pendiente, además de los ya entregados. Antes, el filtro por saldo entregado ocultaba toda la orden y el conteo cuando solo había reserva. Ahora muestra lote, cama, cantidad por entregar y el formulario de conteo deshabilitado con un aviso específico. «Actualizar orden» consulta la entrega sin solicitar otra vez el PIN, conservando la selección de cama y material. Solo se habilita el registro sobre material realmente entregado; la reserva no se convierte en entrega automática. La ficha de siembra facilita el acceso a registrar la entrega de bulbos.

Validación: 24 pruebas de acceso/preparación correctas, incluyendo reserva → entrega parcial → registro con código, protección de inventario y atribución; navegador 390/1440 px con escrituras simuladas para visibilidad, bloqueo y habilitación al actualizar. La consulta real confirmó una preparación con 250 reservados y 0 entregados, sin modificar esos valores.

### CRUD de cuadrillas permanentes y temporales (2026-11-05)

Trabajadores incorpora la pestaña Cuadrillas con búsqueda paginada, alta, ficha desde el código, edición y retiro. Administradores y gerentes gestionan la configuración; únicamente el superusuario puede borrar (retiro del uso activo, conservando referencias históricas).

- Permanente: reutilizable en siembra y cosecha.
- Temporal: fecha y actividad obligatorias. Sus integrantes tienen vigencia únicamente ese día. La asignación de órdenes verifica fecha y actividad; los selectores excluyen temporales vencidas o incompatibles. Las cuadrillas generadas desde la selección de trabajadores de una preparación nueva son temporales para esa siembra y fecha.
- Las órdenes abiertas bloquean cambios de integrantes y desactivación. Una cuadrilla con historial no cambia tipo, fecha ni actividad; las temporales con historial tampoco cambian integrantes. En permanentes con actividades finalizadas se conservan los intervalos de membresía y una bitácora de modificaciones.

La configuración adicional usa `crew_config.<id>` en ModuleSetting y la vigencia ya existente en WorkCrewMember; no requiere migración ni altera cuadrillas históricas. Las anteriores sin configuración explícita se consideran permanentes.

Validación: 34 pruebas de integración de cuadrillas, preparación, camas y acceso de campo correctas; 7 pruebas específicas finales correctas de CRUD, permisos, fecha/actividad y conservación/revocación de membresías. Navegador 1440/390 px: crear temporal, consultar ficha, editar y retirar. Nuxt compila; TypeScript conserva 27 errores previos fuera de Flores, sin errores nuevos del módulo.

### Aprobación unificada con salida automática (2026-11-05, flujo vigente)

Este cambio sustituye el paso independiente de solicitud/entrega de las preparaciones nuevas. Crear la preparación define y vincula los materiales; aprobarla valida existencias e identidad, reserva y registra la salida completa en una única transacción. Quedan aprobador, fecha, detalle de entrega y movimiento ISSUE_TO_FIELD. El trabajador puede registrar inmediatamente. Un reintento no duplica movimientos.

Se retiró el formulario de solicitud manual de la vista de producción y sus accesos desde la ficha de siembra. Salidas de bulbo conserva la consulta de los documentos generados y su historial. Las rutas administrativas antiguas permanecen para registros legados; las nuevas preparaciones ya no dependen de ellas.

`python -m modules.mod_floresvolcan.auto_issue_backfill` presenta una vista previa; `--apply` completa únicamente salidas pendientes de preparaciones previamente aprobadas, con auditoría del superusuario y sin duplicarlas. En la instalación local se aplicó a la orden 581/solicitud 580: 250 bulbos habilitados, 0 pendientes de entrega y una cama disponible. No se modificaron órdenes históricas sin preparación vinculada.

Validación: 25 pruebas previas correctas de preparación/acceso/inventario; 10 pruebas finales de preparación, incluyendo regularización idempotente y registro por PIN inmediatamente tras aprobar; navegador 1440/390 px con salida automática correcta. Compilación Nuxt correcta y sin errores TypeScript nuevos del módulo.

### Pasaporte público y QR de trazabilidad (2026-11-05)

Cada preparación nueva genera un token de lectura independiente del token de registro de cuadrilla. La ficha muestra ese QR desde la preparación y conserva el enlace al aprobar, sembrar, cortar y cerrar el ciclo. Las órdenes anteriores habilitan su pasaporte al abrir la ficha con permisos de gestión. El QR público no permite iniciar sesión con PIN ni escribir datos.

La página `/floresvolcan/trazabilidad/:token` abre sin autenticación y presenta un diseño de pasaporte del cultivo: proveedor y país del proveedor, lote de origen, variedad/color/común, fecha de compra e ingreso, preparación/aprobación/siembra/cosecha, finca/invernadero/cama y cantidades de la orden. Los tallos cosechados corresponden a salida física no anulada. No publica inventario disponible, precios, credenciales, usuarios ni observaciones internas.

La ficha añade «Nota para el pasaporte público»: texto curado por administrador/gerencia, visible para quien tenga el enlace. Las notas internas no se copian automáticamente. El acceso por código para cuadrilla permanece como acción separada y conserva su alcance restringido. Los pasaportes usan `trace_order.<id>` y `trace_token.<token>` en ModuleSetting, sin migración de esquema. Se incorporó la ruta a los manifiestos públicos y al módulo activo.

Validación: 29 pruebas correctas de trazabilidad, preparación y acceso por código. Navegador a 1440/390 px verifica origen, nota pública, ausencia de petición de autenticación y separación del QR de registro. Compilación Nuxt correcta; TypeScript sin errores nuevos del módulo (27 previos fuera de Flores).

### Pasaporte con línea de tiempo interactiva (2026-11-05)

La presentación pública se reorganiza en origen, preparación, aprobación, siembra y cosecha. La etapa actual abre por defecto; cada etapa tiene su propio panel accesible con teclado. Se puede abrir/cerrar todo, filtrar etapas con registros y usar las cifras del resumen para navegar a siembra, planificación o cosecha. El origen permite seleccionar entre varios lotes. El resumen permanece visible al desplazarse en escritorio y se adapta como tarjeta compacta en móvil.

El indicador cuenta etapas con registro; no estima progreso biológico ni inventa fechas faltantes. Una aprobación conocida sin fecha se muestra como aprobada con fecha no registrada. Los ciclos cerrados sin cortes se identifican como tales. Se respetan las preferencias de movimiento reducido. La URL pública y el acceso del QR permanecen sin cambios.

Verificación visual y funcional en 1440/390 px: paneles, filtro, navegación por cifras, teclado, ausencia de autenticación y QR estable.
