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
