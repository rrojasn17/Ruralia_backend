# Referencias técnicas del catálogo de lirios — 5 de noviembre de 2026

Se completaron exclusivamente campos NULL del cultivo LIRIO. Antes había 157 materiales incompletos. Se llenaron 131 densidades y 78 ciclos (209 campos, 156 materiales afectados); 146 materiales quedaron completos y 11 siguen requiriendo densidad del proveedor. No se modificaron valores existentes, capacidades de camas, cantidades ni snapshots de siembra.

## Fuentes primarias consultadas

- [iBulb: Lilium Forcing Guide, distribuida por Onings](https://onings.com/wp-content/uploads/Lilium-Forcing-Guide-English.pdf): tabla 5, página impresa 24; tabla 9, página impresa 30. Guía de producción comercial de flor de corte; sus rangos varían por grupo, calibre y estación. Se verificó la alineación de columnas del PDF, evitando trasladar valores de macetas a camas.
- [Jan de Wit: Eremo](https://www.jandewitenzonen.com/en/assortment/lily/1223/Eremo/) y [Idaho](https://www.jandewitenzonen.com/en/assortment/lily/1905/Idaho/): fichas varietales de proveedor con tiempos de forzado.
- [Flamingo Holland: Roselily Angela de corte](https://www.flamingoholland.com/wholesale-flower-bulbs/roselilies/roselily-angela) y [Roselily Viola de corte](https://www.flamingoholland.com/wholesale-flower-bulbs/roselilies/roselily-viola): rangos varietales, no instrucciones de maceta.
- [Zabo Plant: Starlily Alvaro](https://www.zaboplant.nl/products/44313-Alvaro): identifica esta variedad como OT doble y publica su ciclo de corte. Se usó esa clasificación en la referencia técnica, sin recodificar el catálogo original RL.

## Política aplicada

Las cifras agregadas son **referencias estimadas de planificación**, no ensayos confirmados para la finca del cliente. El valor de densidad elegido es el mínimo del rango publicado por grupo/calibre, por m² **neto de cama**. Para Oriental se usó la referencia conservadora de hoja ancha; debe revisarse según cultivar. Solo se aceptan calibres contenidos completamente en un intervalo documentado: por ejemplo 12,5/14 en 12/14. No se extrapola fuera de tabla, ni se interpreta DN como calibre normal.

Para los ciclos con ficha varietal se tomó el punto medio del rango de días y se convirtió a semanas, redondeando hacia arriba. Cuando no se encontró ficha específica se utilizó el punto medio de la envolvente estacional del grupo de la tabla 9, convertido del mismo modo. Esta última regla es una decisión explícita de planificación del software, **no un valor varietal publicado**. No se trasladan las estaciones neerlandesas al calendario de Costa Rica. La duración excluye la refrigeración previa y debe calibrarse con datos locales de temperatura, luz, almacenamiento, variedad y cosecha.

En Materiales, al abrir la ficha, «Referencia técnica consultada» contiene un icono de información y enlaces de densidad/ciclo. El registro conserva el rango publicado, método, familia y condición de estimado. Una edición manual posterior no borra esa consulta histórica: no certifica que los valores editados sigan coincidiendo con la referencia inicial.

## Once materiales pendientes

- Roselily Angela y Viola, calibre 15/17: no se aplicó una densidad de maceta ni se interpoló entre columnas.
- Hinault 11/12: calibre no cubierto para LA en la tabla utilizada.
- Zambessi/Zambesi DN, Eremo DN y Litouwen DN: ocho códigos con doble nariz o calibre OT 20/22 fuera de la tabla aplicable.

Para completarlos hace falta la ficha del lote/proveedor o un criterio agronómico local documentado. El detalle de los once códigos y motivos se guarda en `maintenance.technical_material_fill.20261105`.

## Auditoría y verificación

- Respaldo previo privado: `.local-backups/floresvolcan_v11_20261105/before_technical_material_fill.pgdump`.
- Mantenimiento reproducible, no ejecutado al arrancar: `api/modules/mod_floresvolcan/migrations/technical_material_fill_20261105.py` (sin `--apply` solo presenta el plan).
- `technical_material.<id>` conserva la referencia y `maintenance.technical_material_fill.20261105` los valores anteriores y cambios.
- Se invalidó la caché de mensajes del asistente para que evalúe el catálogo actualizado.
- Verificación posterior: 11 incompletos y cero valores previos modificados.
- 18 pruebas aprobadas, incluidas no extrapolación, preservación de valores, referencias específicas y acceso de campo. TypeScript conserva los 27 errores anteriores fuera de Flores, sin errores nuevos del módulo.
