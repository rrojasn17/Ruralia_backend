# Panel IoT y variables calculadas

En la ficha de una finca, abra **Telemetría IoT → Configurar → Diseño del panel**.

- Cambie color, representación (línea, área, barras, último valor, indicador o reloj), decimales y orden. Los indicadores permiten configurar mínimo y máximo.
- El botón de ojo oculta o muestra cada variable, conservando su definición. **Guardar panel** persiste el diseño por usuario y finca, incluso si no queda ninguna variable visible.
- Las variables físicas que no formen parte del diseño aparecen en **Variable oculta o nueva → Mostrar variable**. No se añaden automáticamente a un panel guardado.
- **Restaurar** recupera las variables físicas y conserva las calculadas. Guarde para persistir ese cambio.

## Fórmulas

1. Pulse **Nueva variable calculada** y escriba nombre y unidad.
2. Seleccione la variable de sensor correspondiente a `x1`. Añada `x2`, `x3`, etc. cuando necesite combinar mediciones de la misma finca.
3. Ingrese la fórmula. Ejemplo: si `x1` es temperatura en °C, `x1 * 9 / 5 + 32` produce °F.
4. Configure la tolerancia temporal, pulse **Añadir al panel** y después **Guardar panel**. El lápiz permite editar una fórmula existente.

Operadores: `+`, `-`, `*`, `/`, `**` (potencia), paréntesis. Funciones: `sqrt`, `exp`, `log` (natural), `abs`, `min` y `max`. Use punto decimal. Se admiten hasta 16 entradas y 500 caracteres. Todas las entradas seleccionadas deben utilizarse en la fórmula.

Los puntos se calculan en el servidor sobre las lecturas del período seleccionado. Se toma la hora de la primera entrada y la lectura anterior más reciente de las demás, siempre dentro de la tolerancia elegida (15 minutos por defecto; cero exige el mismo instante). No se usan lecturas futuras ni se reemplazan faltantes por cero. Se omiten divisiones por cero, resultados no finitos y entradas demasiado antiguas; el panel indica el número de puntos omitidos. Un rango truncado también limita los cálculos.

Las fórmulas y preferencias se guardan en el JSON del diseño existente; no modifican mediciones originales ni requieren migración de esquema. El CSV actual exporta las mediciones originales. Las variables calculadas son propias del panel de cada usuario.

Para evapotranspiración, introduzca la ecuación del método que corresponda y seleccione las mediciones y unidades requeridas. No se incluye una ecuación agronómica predeterminada ni agregaciones diarias implícitas.

## GPT Café

Las consultas ambientales del asistente general usan `farm_sensor_analysis` para resolver la finca y consultar el inventario y todas las variables medidas. La respuesta distingue sensores registrados de ausencia de lecturas recientes. Se conservan las métricas de rachas de humedad utilizadas por las automatizaciones.

## Activación

Actualice backend y frontend juntos mediante el despliegue habitual. En el Compose del backend, reconstruya la imagen y recree API y worker; reiniciar contenedores antiguos no incorpora código nuevo. No hay cambios en claves API ni en los datos de sensores.
