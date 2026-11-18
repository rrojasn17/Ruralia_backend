# Sensores TTN sin lecturas

Estar en la misma aplicación TTN permite compartir el webhook, pero cada uplink se identifica por `end_device_ids.dev_eui`. RuralIA exige que ese DevEUI esté registrado en la integración correspondiente y que `uplink_message.decoded_payload` contenga valores numéricos. Las variables se descubren por las claves del payload, sin depender de un modelo específico de sensor.

“0 lecturas almacenadas” significa que no hay lecturas persistidas. No permite concluir que el webhook nunca recibió mensajes: RuralIA reconoce con HTTP 200 algunos uplinks ignorados para evitar reintentos innecesarios. La pantalla muestra ese hecho y sugiere comprobar el DevEUI, la aplicación, el payload y la entrega del webhook.

Para diagnosticar un sensor:

1. Comparar el DevEUI del nodo en RuralIA con `end_device_ids.dev_eui` de un uplink real en TTN.
2. Confirmar el Application ID y la integración elegida. Verificar la URL y el encabezado secreto del webhook en TTN sin compartir sus secretos.
3. Revisar los eventos de entrega del webhook: que TTN reciba un uplink por radio no garantiza que lo haya entregado a RuralIA.
4. Comprobar que el `decoded_payload` entregado contiene números o cadenas numéricas compatibles.
5. Revisar en los logs de la API `TTN uplink ignored`. Incluyen motivo, DevEUI, aplicación e ID de integración; no incluyen secretos ni el payload completo.

Motivos registrados: `sensor_not_registered_for_integration`, `no_numeric_decoded_payload`, `sensor_inactive` y `legacy_sensor_not_registered`. Un endpoint heredado no acepta un nodo que ya tiene una integración asignada.

## Revisión del 17 de noviembre de 2026

Consulta de solo lectura sobre la base **local**, sin conectarse a producción:

| DevEUI | Activo | Finca | Integración | Lecturas |
| --- | --- | --- | --- | --- |
| `2CF7F1C07380029C` | Sí | 3 | 3 · `zona-santos-8-en-1` | 22 |
| `2CF7F1C0738002C3` | Sí | 3 | 3 · `zona-santos-8-en-1` | 0 |

La integración está activa. Ambos DevEUI están normalizados correctamente. La `v` inicial del nombre del segundo sensor no forma parte del DevEUI y no participa en la búsqueda. En esta copia no hay diferencias de asociación que expliquen el problema. También hay un Dragino registrado sin lecturas, lo que por sí solo no identifica una causa común.

Los últimos mensajes almacenados de `…0029C` usan un decodificador plano: `Potassium`, `Nitrogen`, `Phosphorus`, etc., incluso en mensajes parciales. No hay un mensaje almacenado de `…002C3`; no se puede afirmar que la compatibilidad con listas sea la causa de su ausencia. Hace falta el registro de una entrega real para cerrar ese diagnóstico.

### Correcciones y garantías verificadas

- La identidad se resuelve por el **DevEUI completo y la integración**. Compartir aplicación, valores, contador y sesión entre dos nodos no elimina lecturas de uno de ellos. La restricción de unicidad se aplica a `(node_id, source_event_id)`.
- Se mantiene la deduplicación por sesión y contador. Las claves largas se resumen con SHA-256 en lugar de truncar el contador. Sin esos campos, se utiliza la fecha del mensaje o un identificador de correlación propio del uplink; una conexión compartida no identifica una lectura.
- Las escrituras TTN bloquean la fila del nodo en PostgreSQL mientras descubren variables y guardan la lectura. Esto evita perder configuración cuando llegan fragmentos simultáneos. El límite de peticiones es por integración y DevEUI, para que un nodo no consuma la cuota de los demás. `IOT_INGEST_RATE_LIMIT_PER_MINUTE` conserva su valor configurado, ahora aplicado a cada sensor.
- Se leen objetos anidados y listas numéricas con límites de profundidad y cantidad. Las claves literales con puntos siguen funcionando en mensajes posteriores al descubrimiento.
- El formato oficial SenseCAP S210X `{valid, err, payload, messages}` se adapta por `measurementId`, evitando que el orden de las mediciones cambie su identidad. Las variables se publican como `sensecap_4097`, etc. y se pueden etiquetar en la configuración. Se conserva el JSON original. Los campos `valid`, `err`, identificadores y mensajes de configuración no se presentan como mediciones. Un fallo declarado por ese decodificador produce `decoder_error`.
- Los decodificadores planos de SenseCAP y Dragino siguen admitidos. No se recortan valores por los mínimos y máximos de presentación.
- Una forma JSON incorrecta devuelve 422. Un error de integridad de base de datos solo se reconoce como duplicado si se encuentra la lectura de ese mismo nodo y evento; otros errores siguen siendo errores del servidor.

Referencias del contrato: [formato de mensajes de The Things Stack](https://www.thethingsindustries.com/docs/integrations/data-formats/) y [decodificador oficial SenseCAP S210X, ejemplos y estructura de messages](https://github.com/Seeed-Solution/SenseCAP-Decoder/blob/main/S210X/TTN/SenseCAP_S210X_TTN_Decoder.js).

### Diagnóstico desde la aplicación

`GET /iot/nodes/{id}/status` incorpora `last_ingest`: fecha de recepción por RuralIA, resultado `success` / `duplicate` / `ignored`, motivo, claves superiores del decodificador y contador. «Verificar sensor» muestra el resultado en la interfaz. No incluye secretos ni valores del payload.

Este diagnóstico se registra para mensajes autenticados que coinciden con un nodo de la integración. Si un DevEUI no está registrado para esa integración, sigue apareciendo en los logs como `sensor_not_registered_for_integration`. Una aplicación distinta devuelve 403 y deja un aviso. Un secreto inválido devuelve 403 sin modificar el diagnóstico. La ausencia de `last_ingest` significa que no existe un diagnóstico desde esta actualización; no prueba que nunca se hayan recibido mensajes antes de ella.

Motivos adicionales: `not_uplink`, `decoder_error` y `no_mapped_numeric_variables`. Este último indica que el mensaje tiene números, pero la configuración no los pudo asignar, por ejemplo porque todas las variables disponibles ya están ocupadas por claves anteriores. No se eliminan configuraciones existentes automáticamente.

### Instalación y validación

Esta actualización agrega la columna JSON nullable `navia_iot_nodes.ingest_diagnostics`. Antes de iniciar la nueva API sobre una base existente, ejecutar el procedimiento habitual de actualización del esquema (`python -m schema_upgrade`, servicio `schema-init` en Compose) con **la nueva imagen**. La migración es aditiva y repetible; no modifica lecturas históricas. Actualizar también el frontend para mostrar el diagnóstico.

La revisión se validó con 75 pruebas de backend: aislamiento de sensores, reintentos, cambios de sesión, claves largas, formatos planos y oficiales, datos anidados, límites, autenticación HTTP y diagnósticos. Tres pruebas usan PostgreSQL temporal: reintentos simultáneos, fragmentos simultáneos y migración repetida. Las pruebas de gráficos y widgets del frontend también pasaron. No se han validado entregas de TTN a producción ni se ha desplegado esta actualización.

Ruff pasó sobre el router, modelo, esquema y pruebas IoT modificados; el componente Vue compila. ESLint detecta los mismos 41 errores anteriores del componente (mismas reglas y cantidades que en HEAD), sin errores adicionales por este cambio.

Para repetir las pruebas normales desde `api`:

```sh
python -m pytest tests/test_iot_ingest_diagnostics.py tests/test_iot_backup.py tests/test_iot_formulas.py tests/test_iot_branding_rag.py -q
```

Las tres pruebas PostgreSQL se omiten salvo que `IOT_TEST_POSTGRES_URL` apunte a una base **desechable** llamada `iot_test`. Crean y eliminan un esquema aislado por prueba. No usar credenciales ni bases de producción.
