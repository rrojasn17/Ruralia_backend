# Flujo familiar con el Excel · 0.9.1

El registro diario se presenta en este orden en el menú, el dashboard, Lirios, el histórico y el recorrido del asistente:

| Etapa | Pantalla | Indicador principal | Trabajo enlazado |
| --- | --- | --- | --- |
| Entradas | `/floresvolcan/inventario` | Bulbos recibidos | Compra → recepción, proveedor y lote |
| Salidas de bulbo | `/floresvolcan/salidas-bulbo` | Bulbos entregados a campo | Solicitud → reserva → entrega física para una orden |
| Siembra | `/floresvolcan/produccion` | Bulbos realmente sembrados | Orden autorizada → entrega de origen → mesa, fecha y trabajador |
| Cosecha | `/floresvolcan/cosechas` | Tallos cosechados, venta y descarte | Orden de cosecha → segmento de siembra → corta, revisión y cierre |

Los indicadores son acumulados operativos. El saldo disponible, las reservas y el material pendiente de sembrar siguen visibles en Entradas, en cada salida y en Más cifras de la operación. No se confunden las entradas con el saldo actual ni la entrega histórica con el remanente en campo. El histórico original conserva sus nombres físicos de hoja y sus filas; sus etiquetas visibles siguen el vocabulario del usuario.

## Uso entre roles

El administrador prepara las órdenes y el movimiento de material. Los trabajadores registran Siembra y Cosecha en Mi trabajo, limitados a sus órdenes y cuadrillas. Las hojas fotografiadas conservan el paso de revisión y aprobación del administrador. Los gerentes y el superusuario mantienen sus permisos de supervisión; las aprobaciones de compra y la exclusividad del borrado no cambian.

La salida requiere una orden de siembra aprobada: es la planificación del destino antes del movimiento físico. Desde Salidas de bulbo se puede Preparar siembra; después de aprobar la orden, Solicitar salida abre el formulario con esa orden precargada. Desde una salida, Ver siembra regresa a su orden vinculada. Aprobar una solicitud solo reserva; Registrar salida acredita la entrega real y sigue admitiendo entregas parciales.

Las explicaciones y los responsables están en los controles de información `(i)`. El menú Operaciones sigue Compras → Entradas → Salidas de bulbo → Siembra → Cosecha. En móvil, los primeros accesos autorizados ocupan la barra inferior y los demás están en Más; el trabajador conserva únicamente su espacio de campo. Compras, proveedores, materiales y demás herramientas permanecen disponibles como apoyo al registro diario.

## Compatibilidad y verificación

Las rutas existentes de inventario, producción, solicitudes y cosechas se conservan. La ruta nueva de Salidas de bulbo abre directamente las solicitudes y entregas, reutilizando sus controles transaccionales. El detalle de una solicitud activa Salidas de bulbo en el menú aunque su URL histórica esté dentro de producción. No se duplica el inventario ni se transforma una solicitud en una entrega por cambiarle el nombre.

La lista de solicitudes precarga lotes y productos en tres consultas, en vez de repetir consultas por cada línea. Los nuevos indicadores agregados no añaden recorridos lote por lote.

Se verifican acumulados frente a saldos, clasificación de cosecha y trazabilidad de la lista; navegación, menús móviles, controles `(i)`, vínculos entre salida y siembra, orden precargada y recorrido guiado mediante Chrome contra datos ficticios en escritorio y móvil. No se vuelve a importar ni se modifica el Excel original.

## Agrupación del menú · 0.9.2

- **Operaciones:** Compras, Entradas, Salidas de bulbo, Siembra y Cosecha, en ese orden.
- **Diario:** Lirios, Revisar hojas y Mi trabajo.
- **Empresa:** Materiales (SAP), Proveedores, Mesas y espacios, Trabajadores y Ajustes.

Inicio permanece arriba sin encabezado de grupo. Histórico Excel se conserva en el menú de la cuenta y en el aviso de datos importados. Cada usuario ve sus opciones autorizadas.
