# Interfaz de Flores Volcán

Rediseño del 4 de noviembre de 2026 sobre el flujo funcional 0.7.0.

## Uso

La navegación acompaña el recorrido: **Compras → Bodega → Siembras → Cosechas**. **Mi trabajo** reúne el registro de siembra y corta para campo. En teléfono, la barra inferior mantiene los accesos principales y **Más** abre todas las secciones autorizadas.

Las listas comparten búsqueda, iconos, estados en español y selección visible. Cada pantalla destaca su siguiente acción. Los datos adicionales se agrupan en secciones plegables; las explicaciones aparecen en **i**. Los avisos de error, las diferencias pendientes y las confirmaciones de operaciones sensibles permanecen visibles cuando son necesarios.

Compras conserva mensajes y documentos como una conversación. Bodega abre el detalle al tocar un lote. Siembras separa las órdenes y las solicitudes de material. Cosechas separa cortes/revisión y limpieza de mesas. Los formularios de campo muestran primero orden, material, fecha real y conteo; la hoja en papel, notas y datos técnicos se consultan aparte.

## Implementación

- Layout `flores` y componentes compartidos en `frontend/frontend/app/modules/mod_floresvolcan/components`.
- Estilos limitados a `.fv-workspace` y `.fv-dialog`; adaptación a teléfono y escritorio, foco de teclado, enlace para saltar al contenido y movimiento reducido.
- Navegación y acciones filtradas por los permisos existentes. Los controles de inventario y conciliación siguen en la API.
- Ventanas con ancho limitado, cuerpo desplazable y acciones accesibles. Las reglas locales corrigen las dimensiones heredadas de los estilos globales.

## Verificación

La regresión reproducible y su servidor local con datos en memoria están documentados en `frontend/frontend/tests/README.md`. No requieren datos ni credenciales reales. La identidad del supervisor se sustituye exclusivamente en ese servidor de prueba; se usan los routers y las consultas reales.

La regresión completó 38 vistas (19 pantallas en dos tamaños), búsqueda, ayudas, contraste, ventanas, retorno del foco y navegación móvil sin errores de ejecución ni desbordamiento horizontal. En la compilación de producción se comprobó además el cierre visible de ventanas y la recuperación después de desconectar y reconectar el navegador.

La revisión adicional del flujo de campo verificó que una siembra rechazada por ocupación conserva la cantidad escrita y que una corta válida se guarda mediante HTTP en la base temporal. La compilación de producción se comprueba con Node 24. El chequeo global de tipos mantiene errores fuera de Flores; no se identificaron errores en el módulo ni en su layout.

Las pruebas técnicas no sustituyen una sesión de uso con los trabajadores del cliente, especialmente para confirmar el significado operativo de las categorías BL.

## Corrección de listas y acciones

Se corrigió el registro de carpetas de componentes en `nuxt.config.ts`: se registran incluso cuando aún no existen, para que Nuxt detecte su creación durante el desarrollo. En el servidor local que seguía abierto, los componentes de listas se resolvían como etiquetas desconocidas y solo quedaban visibles los botones nativos. Se verificó que ahora importa `FvListRow` y `FvLoadState` directamente.

Proveedores y trabajadores muestran su lista, cantidad de resultados, búsqueda, actualización y acciones con texto. Proveedores ofrece consultar, editar y retirar con confirmación; la API conserva su regla de bloqueo por compras pendientes y conserva el historial cuando corresponde. Trabajadores ofrece consulta, edición, desactivación y reactivación. La carga auxiliar de cuentas de campo ya no oculta una lista de trabajadores que cargó correctamente.

`tests/floresvolcan-crud.browser.mjs` verificó las operaciones reales en SQLite temporal, en escritorio (1440 px) y móvil (390 px), incluyendo el bloqueo de proveedores con compras pendientes. La compilación se realizó en una carpeta temporal separada del servidor del usuario.
