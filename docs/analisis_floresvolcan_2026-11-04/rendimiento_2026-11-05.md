# Fluidez de Flores · 0.9.3

Las vistas de Compras, Entradas, Salidas de bulbo, Siembra, Cosecha,
Materiales (SAP), Mesas y espacios y Limpieza consultan páginas de 40 registros.
Los filtros y búsquedas se aplican al conjunto completo antes de paginar.
Los saldos de Entradas y los contadores de Cosecha siguen siendo globales.

Los selectores de órdenes, lotes y espacios buscan en el servidor y descargan
como máximo 30 opciones. Solo consultan al abrirse o al escribir. Conservan la
etiqueta seleccionada aunque la opción esté fuera de los resultados actuales.
Las listas locales grandes de catálogos y trabajadores usan SelectMenu con
virtualización. Los equipos y catálogos de formularios se cargan al abrir el
formulario, en vez de descargarse con cada visita a una lista.

Las búsquedas esperan 250 ms para agrupar pulsaciones. Las consultas anteriores
se cancelan y las respuestas tardías se descartan. No se comparte una caché de
saldos, inventario o permisos entre usuarios. La API sigue validando cada escritura.

## Contrato de lectura

Los siguientes GET admiten `page` (desde 1) y `page_size` (1–100, predeterminado 40):

- `/floresvolcan/purchase-orders`: `q`, `status` (uno o varios separados por coma), `selected_id`.
- `/floresvolcan/inventory`: `q`, `available_only`, `selected_id`.
- `/floresvolcan/planting-orders`: `q`, `status`, `selected_id`.
- `/floresvolcan/requisitions`: `q`.
- `/floresvolcan/production-spaces`: `q`, `type`, `status`, `selected_id`.
- `/floresvolcan/sap-products`: `q`, `active`.
- `/floresvolcan/harvest-calendar`: `q`, `phase`, `selected_id`.
- `/floresvolcan/bed-cycles`: `active_only`.

Con `page`, la respuesta es `{items, total, page, page_size}`. Inventario y
calendario agregan `summary` con totales globales. `phase` acepta `active`,
`ready`, `in_harvest`, `closed` o `selectable`; esta última excluye lotes cerrados
y pendientes de revisión histórica de los selectores de cosecha.

Sin `page` se mantiene la respuesta en forma de arreglo para consumidores
anteriores. Se eliminaron los límites silenciosos de 200 materiales y 500 lotes
del calendario. Las páginas usan orden estable con ID como desempate.
Los permisos de cada endpoint se mantienen; los parámetros de selección no
amplían el acceso de trabajadores o administradores al catálogo SAP.

En Cosecha, las fechas y estados se calculan sobre metadatos del conjunto de
lotes; las conciliaciones detalladas se calculan solo para la página visible.
Se eliminó la búsqueda repetida de lotes dentro del bucle de fechas. Limpieza
agrupa la consulta de mesas, lotes, conciliaciones y ocupación por ciclo, en
vez de ejecutar consultas individuales por cada mesa y lote.

## Medición local de solo lectura

Misma base local, 5 de noviembre de 2026. Tamaños JSON sin compresión, incluyendo
los detalles y totales necesarios para la vista. No son tiempos de navegación
ni una simulación de red móvil.

| Lista | Registros totales | Antes por carga | Página de 40 |
| --- | ---: | ---: | ---: |
| Entradas | 654 | 555.170 bytes | 34.149 bytes |
| Compras | 654 | 459.242 bytes | 28.206 bytes |
| Siembra | 576 | 236.054 bytes | 16.453 bytes |
| Salidas de bulbo | 576 | 493.483 bytes | 35.952 bytes |
| Mesas y espacios | 970 | 211.012 bytes | 9.473 bytes |
| Cosecha | 375 | 405.514 bytes | 33.816 bytes |

Además, Siembra y Salidas dejaron de descargar seis colecciones completas en
paralelo: ahora cada vista solicita su lista y los formularios solicitan las
referencias al utilizarlas. La reducción de Entradas fue de aproximadamente
94 % y la de Mesas y espacios, de 95,5 %.

## Verificación

- 73 pruebas de backend aprobadas; 3 casos de concurrencia PostgreSQL omitidos
  por requerir la base temporal específica. Las pruebas ejecutadas usaron SQLite
  temporal y no alteraron registros reales.
- Pruebas nuevas cubren páginas sin duplicados, límites de tamaño, búsqueda fuera
  de la primera página, totales globales, permisos, ocupación y selección por ID.
- Un caso con 510 lotes confirma que solo se concilian los 40 visibles y que se
  encuentra un lote posterior al antiguo límite de 500.
- Compilación de producción de Nuxt en directorios de `/tmp`, sin modificar la
  compilación del servidor de desarrollo. El chequeo global de tipos conserva
  27 errores previos fuera de Flores; ningún error en el módulo modificado.
- Chrome verifica el flujo en 1440 y 390 px y las restricciones de cuatro roles.
- La prueba de volumen aprobó las siete vistas, la búsqueda global, la agrupación
  de pulsaciones, la carga diferida y la conservación de selecciones en 1440 y
  390 px, sin errores de JavaScript ni desbordamiento horizontal.
- La prueba de volumen usa exclusivamente el servidor de cuentas ficticias con
  `FV_TEST_PERFORMANCE=1`; agrega registros únicamente en su SQLite temporal.

La mejora limita transferencia y trabajo de renderizado. La latencia final aún
depende de red, dispositivo y disponibilidad del servidor; no se ha realizado
una prueba de carga con múltiples usuarios concurrentes.
