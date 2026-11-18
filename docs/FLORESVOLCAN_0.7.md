# Flores Volcán 0.7.0

Esta versión agrega conciliación por siembra y liberación física de mesas. Mantiene los lotes de origen separados aunque compartan variedad, cama y semana. No importa ni corrige automáticamente los Excel históricos.

## Uso diario

1. Compras, recepción, reserva y entregas parciales conservan su flujo.
2. Campo registra fecha real, material, cama, lado y conteo entero sembrado. El trabajador toma densidad y ciclo del producto; un encargado puede ajustarlos.
3. Cada registro crea un segmento con ID propio. El origen mostrado incluye proveedor, lote de inventario, lote del proveedor y contenedor.
4. Aprobar la OT de cosecha bloquea nuevas siembras en esa ocupación de mesa. Las cortas seleccionan el segmento, conservan su fecha real y consumen su saldo acumulado.
5. En **Cosechas → Revisar siembras y cierre**, el encargado revisa cortas, asigna históricos pendientes y documenta la merma de potencial de cada siembra. Primero debe completar las OT de cosecha.
6. Cerrar un lote conserva la venta, el descarte, la merma justificada y cualquier excedente autorizado como cantidades distintas. La cama continúa ocupada.
7. En **Cosechas → Cierre y limpieza de mesas**, se confirma que todos los lotes están conciliados y la mesa está vacía y limpia. Se guarda responsable, fecha y comentario. Entonces puede comenzar un nuevo ciclo.
8. En la requisición se devuelven sobrantes, registran bajas de material entregado sin sembrar o liberan reservas sin entregar. La orden solo puede completarse con todos los lotes y materiales resueltos.

No se presupone una equivalencia nueva para las referencias DN. El factor tallos/unidad sigue siendo configurable en SAP y queda fijado en el lote al sembrar. Debe validarse con el cliente. El conteo BL continúa representando tallos por clase, conforme al contrato previo; una merma de potencial se expresa como tallos equivalentes y no se presenta como descarte físico.

## Contrato de API

La especificación estructural sigue siendo la generada por FastAPI. Las pruebas de `api/tests/test_floresvolcan_reconciliation_v07.py` ejercitan las rutas HTTP contra el contrato de comportamiento descrito aquí.

| Operación | Ruta | Comportamiento |
|---|---|---|
| Siembra | `POST /floresvolcan/field/planting` | Exige token estable, fecha real y línea de requisición explícita. Sin sustitución automática de referencias inválidas. Valida origen, orden, cama, lado, semana, ciclo y factor del lote explícito. |
| Corta | `POST /floresvolcan/field/harvest` | Exige token, fecha real, OT aprobada y segmento exacto cuando hay varios. Rechaza fechas anteriores a siembra/madurez sin liberación técnica y salidas que superen el cupo. |
| Resumen | `GET /floresvolcan/production-batches/{id}/summary` | Incluye segmentos, saldos, potencial, venta, descarte, merma, vínculos pendientes y cortas originales/anuladas. |
| Cierre | `POST /floresvolcan/production-batches/{id}/technical-close` | `comments` y `segments[]`: cada línea identifica `planting_record_id`, `loss_quantity`, `loss_reason`. La merma debe cubrir exactamente el potencial pendiente y tener causa. Cero pendiente admite omitir la línea. |
| Ciclos | `GET /floresvolcan/bed-cycles` | Devuelve ocupación, lotes por cerrar y revisión pendiente. |
| Limpieza | `POST /floresvolcan/bed-cycles/{id}/clean` | `confirmed_empty=true` y `comments`. Requiere todos los segmentos conciliados y lotes cerrados. |
| Material | `POST /floresvolcan/requisition-lines/{id}/resolve-material` | `client_token`, `kind=return/field_loss/release`, `quantity`, `comments`. Genera movimiento y evidencia; rechaza exceder el saldo correspondiente. |
| Completar orden | `POST /floresvolcan/planting-orders/{id}/complete` | Verifica lotes cerrados y ausencia de reservas/material entregado pendientes. |
| Vincular histórico | `POST /floresvolcan/harvest-records/{id}/assign-segment` | `planting_record_id`, `comments`. Solo para vínculos ausentes o inválidos. Conserva la explicación del responsable. |
| Anular corta | `POST /floresvolcan/harvest-records/{id}/void` | `comments`. Conserva valores originales, responsable, fecha y causa; deja de consumir saldo. No cambia movimientos de un lote ya conciliado. |
| Excedente histórico | `POST /floresvolcan/production-batches/{id}/approve-historical-overage` | `quantity`, `comments`. La cantidad debe coincidir con el exceso histórico pendiente. La aprobación queda como evidencia independiente. |
| Cancelar OT | `POST /floresvolcan/harvest-orders/{id}/cancel` | `comments`. Solo órdenes abiertas sin cortas vigentes; si tienen cortas deben completarse. |

Las OT con `allow_overage=true` ahora requieren `overage_quantity>0` y motivo. La excepción tiene un cupo cuantitativo acumulado por lote, conservando los saldos de los segmentos. Las OT antiguas sin cantidad no conceden un cupo ilimitado.

Los tokens de siembra, corta y resolución de material son obligatorios en las rutas de escritura. Un reintento idéntico devuelve el registro original; el mismo token con contenido diferente responde 409. Las anulaciones, cierres y limpiezas repetidas se rechazan. Los permisos de trabajador no incluyen cierre, limpieza, correcciones ni aprobación de excedentes.

Las fechas se interpretan en `America/Costa_Rica`, se almacenan en UTC y se agrupan por semana ISO local. La pantalla permite indicar el día real y el borrador OCR puede proponerlo. La imagen sigue requiriendo revisión humana. Una imagen idéntica presentada otra vez a la misma orden recupera la captura pendiente o rechaza una ya contabilizada; una fotografía distinta del mismo papel requiere revisión del operador.

## Contabilidad y exportación

El saldo del segmento se calcula con cantidades de su ID, nunca buscando una llave de texto. El potencial es cantidad sembrada × factor del lote. Los rendimientos porcentuales se calculan contra ese potencial; la razón tallos/bulbo se entrega por separado. Los excedentes no compensan faltantes de otros segmentos.

El material cumple la separación entre disponible, reservado, entregado sin sembrar, consumido y dado de baja. Una devolución pasa de entregado a disponible; una baja pasa de entregado a bajas; liberar reserva pasa de reservado a disponible. Cada operación conserva su movimiento y motivo.

Excel agrega IDs y usa `SIEMBRA-{id}` como referencia. **Conciliacion** tiene una fila por segmento y es la base sumable de comparación. **Corta** conserva el detalle por visita con «SEMBRADO DEL SEGMENTO (NO SUMAR)». Las cortas anuladas salen en una hoja separada con sus cantidades originales y causas, y no incrementan los totales vigentes.

## Actualización del esquema

La instalación/actualización del módulo usa migraciones aditivas: nuevas columnas y tablas `fv_bed_cycles`, `fv_segment_reconciliations`, `fv_material_resolutions` y `fv_historical_overage_approvals`, además de índices de tokens. No borra movimientos ni modifica los Excel del cliente.

La migración inicial recupera `total_output` cuando una versión antigua lo dejó en cero pero conservó venta/descarte. No inventa asignaciones a siembras. Esas cortas deben vincularse mediante revisión antes de seguir cosechando o cerrar. Las mesas existentes se agrupan conservadoramente en una ocupación pendiente: los cierres históricos sin conciliación deben revisarse antes de liberar la mesa. La revalidación de un cierre antiguo preserva su resumen original en `previous_summary_json`.

Si existen tokens históricos duplicados, la actualización informa el conflicto y requiere revisar esos registros antes de crear la unicidad. El ciclo físico se protege con un índice único para una sola ocupación no limpiada por cama. Los bloqueos transaccionales de PostgreSQL serializan la cama antes de modificar lotes, cortas o cierres; las reservas y entregas bloquean sus saldos.

## Verificación

Comandos desde RuralIA, con las dependencias del backend instaladas en un entorno de pruebas:

```bash
python -m pytest -q backend/backend/api/tests/test_floresvolcan_reconciliation_v07.py \
  backend/backend/api/tests/test_floresvolcan_field_v06.py \
  backend/backend/api/tests/test_module_architecture.py
```

SQLite verifica el comportamiento funcional. Los casos concurrentes requieren `FV_TEST_DATABASE_URL` apuntando exclusivamente a `127.0.0.1:55439/fv_test`. Cada prueba PostgreSQL crea y elimina su propio esquema. Se verifican cosechas concurrentes sobre un segmento, siembras desde dos lotes/órdenes sobre la misma capacidad y reintentos simultáneos con un solo movimiento.

El frontend requiere Node compatible con Nuxt; se utilizó Node 24. El build incluye los componentes de conciliación y limpieza. La revisión global de tipos puede seguir reportando fallas anteriores en otros módulos; no se modificó Caficultura para resolverlas. No se invocaron servicios de IA, Drive ni correo durante las pruebas.

### Resultado de esta implementación (4 de noviembre de 2026)

- 39 pruebas aprobadas: rutas HTTP, permisos, instalación/arquitectura y PostgreSQL con tres casos de concurrencia real.
- Build de Nuxt aprobado con Node 24.21.0.
- Ruff aprobado en el servicio nuevo y su suite; sintaxis Python y `git diff --check` aprobados.
- Typecheck global: 25 errores anteriores fuera de Flores Volcán; ninguno reportado en `mod_floresvolcan`.
- PostgreSQL temporal eliminado al terminar. No se aplicaron migraciones a una base operativa ni se desplegó el código.
