# Flores Volcán 0.8: exploración de Excel y asistente

## Alcance del reinicio

Se reemplazan únicamente los registros del módulo `fv_*`. Se conservan las cuentas y sesiones del núcleo, los otros módulos, los ajustes privados de respaldo y los metadatos de copias anteriores. Los archivos adjuntos antiguos se conservan para que el respaldo siga siendo recuperable. Las cuentas de trabajadores no se enlazan automáticamente a nombres del Excel.

Antes de reemplazar se genera un respaldo PostgreSQL verificable y una copia privada de las tablas de Flores. El reemplazo operativo es transaccional. No se reinician secuencias de IDs.

## Criterio de importación

Se usa `Lirio (2).xlsx` como copia de exploración y se conserva el SHA-256 de ambos archivos. `Lirio.xlsx` se registra como comparación, sin sumar sus movimientos. Se leen valores guardados: no se ejecutan fórmulas, macros, instrucciones del documento ni vínculos externos.

Todas las filas de Entradas, Salida Bulbo, Siembra y Corta se conservan en `fv_excel_import_rows`, con hoja, número de fila, celdas originales, motivo de revisión y enlaces operativos. No se convierten textos dudosos a cero ni se corrigen fechas silenciosamente. El histórico se consulta por páginas desde **Histórico Excel**.

Las filas pasan a las pantallas operativas solo si tienen identidad única, cantidades coherentes, fechas admisibles y respaldo suficiente de material. No se prorratean cortas entre varias siembras candidatas. Las cortas duplicadas o que exceden el factor de referencia quedan pendientes. Para el enlace de origen se exige lote + variedad + calibre + proveedor, sin elegir la primera recepción cuando hay varios contenedores candidatos.

Cada entrada operativa genera una compra y recepción **reconstruidas**, con prefijo XL. Los precios originales no están disponibles y la interfaz indica que faltan. Las solicitudes se reconstruyen por origen; no se afirma que sean órdenes originales del cliente ni que una salida específica corresponda a una mesa concreta.

Los nombres separados por guiones se conservan como cuadrillas del Excel; se crean referencias de personas sin inventar identificaciones, contratos, salarios ni fechas de ingreso. Las grafías distintas no se unifican automáticamente, excepto espacios, mayúsculas y acentos para comparación. Los nuevos registros manuales siguen exigiendo identificación y fecha de ingreso.

Los lotes productivos cargados quedan bloqueados para nuevas cortas y cierre hasta revisar el histórico y confirmar unidades/factores. Las mesas se agrupan para revisión: no se inventa una limpieza ni un único ciclo físico. El factor 1 tallo/bulbo es una referencia de exploración, pendiente de confirmar, especialmente para DN. Los saldos calculados no certifican existencias físicas.

## Asistente

El botón **Asistente** está en la cabecera de Flores. Ofrece:

- Explicación según la pantalla y respuestas de una guía local por temas.
- Recorrido con pasos, navegación, avance, retroceso y cierre voluntario.
- Ayuda para proveedores, trabajadores, compras, bodega, siembra, campo, corta, conciliación e histórico.

La guía respeta los permisos. No usa servicios externos de IA ni modifica registros. Un texto no reconocido invita a elegir un tema; no inventa respuestas sobre cifras o reglas del cliente.

## Operación y recuperación

Vista previa, sin cambios:

```sh
python -m modules.mod_floresvolcan.preload_excel --source '/ruta/Lirio (2).xlsx'
```

Reemplazo explícito, con escritores detenidos y un respaldo PostgreSQL ya verificado:

```sh
python -m modules.mod_floresvolcan.preload_excel \
  --source '/ruta/Lirio (2).xlsx' --comparison '/ruta/Lirio.xlsx' \
  --apply-replace --backup-dir '/ruta/privada/persistente' \
  --report '/ruta/privada/persistente/resultado.json'
```

La copia JSON contiene ajustes privados y debe mantenerse fuera del repositorio y de rutas públicas. Para restaurar el estado completo se usa el archivo PostgreSQL de antes del reinicio, con la aplicación detenida; una restauración completa afecta al estado de toda esa base y debe hacerse como una operación explícita de recuperación. La precarga nunca ejecuta una restauración automáticamente.

## Verificación

`test_floresvolcan_excel_preload.py` cubre conservación de filas, stock, origen ambiguo, llave de siembra repetida, texto en conteos, aritmética inconsistente, excedentes, fechas futuras, duplicados, doble copia, reversión y preservación de cuentas/ajustes. Las pruebas existentes siguen cubriendo los controles de producción.

`floresvolcan-assistant.browser.mjs` consulta los datos reales de prueba, comprueba paginación, originales pendientes, ayuda, recorrido por pantallas y cierre bloqueado del histórico, en escritorio y móvil. No modifica registros.

## Resultado aplicado en la instancia local

La precarga se aplicó el 4 de noviembre de 2026, con API y worker detenidos durante el reemplazo. La comprobación posterior al arranque encontró módulo 0.8.0, 2 cuentas conservadas, 5 proveedores, 38 referencias de personas, 253 materiales, 653 lotes de inventario, 1.126 entregas, 375 segmentos de siembra y 1.020 cortas.

Se conservaron las 24.182 filas originales; 21.008 permanecen por revisar. No se detectaron cantidades negativas ni incumplimientos de la ecuación de inventario en los lotes cargados. Se verificó que el reinicio no vuelve a introducir los catálogos de una copia anterior.

Los respaldos privados y el reporte de la operación están en `.local-backups/floresvolcan_excel_20261104/` dentro del backend. La copia JSON adicional reside en `data/ai_uploads/floresvolcan_preload_backups/`, con acceso privado del usuario de la API. No se incluyen en Git.
