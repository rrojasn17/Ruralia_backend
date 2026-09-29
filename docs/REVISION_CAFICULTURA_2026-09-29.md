# Revisión de Caficultura — 29 de septiembre de 2026

Se conservaron los cambios útiles que ya estaban presentes en ambos repositorios y se completaron los puntos que habían quedado a medias. Esta revisión se hizo sobre las fuentes entregadas; no se desplegó ni se modificó la base de producción.

## Cambios confirmados y completados

- Interfaz: superficies más neutras y ordenadas, separación visual consistente y controles compactos. Checkbox/radio quedan fuera de la altura genérica de inputs y se muestran cuadrados.
- Sidebar: botón para colapsar/expandir el menú lateral en escritorio, con persistencia local de la preferencia.
- IA Consulting: historial en panel compacto y eliminación lógica de conversaciones antiguas. El borrado no elimina registros operativos generados por la conversación.
- Permisos: administrador autorizado en operaciones de gerencia del módulo; superusuario sin restricciones propias del rol operario.
- Lotes: administrador/superusuario puede ser responsable. Se rechazan recibos repetidos o anulados y sobreasignaciones. En PostgreSQL se bloquean filas de recibos al asignar cantidades para evitar consumo concurrente.
- Recibos: se conserva la liquidación con fecha, referencia, comprobante, nota e importe. El estado visible es Cancelado; el campo técnico `liquidado` se mantiene por compatibilidad.
- Finca: indicador de ventas cobradas basado en importes reales de recibos cancelados, más pendiente de pago y cantidad de recibos cancelados.
- Gestión de Finca: se añadió reparación de secuencias PostgreSQL después de importar respaldos y al activar Caficultura. Esto evita que una importación con IDs existentes deje la secuencia atrasada y provoque colisiones al crear el siguiente registro.
- IA: se reparó el retorno de preparación de recibos que había quedado fuera de su función. Inventario aporta faltantes y última compra por insumo, y el agente debe diferenciar precios históricos de cotizaciones actuales.
- Alertas: canal interno disponible sin destinatarios externos; correo/Telegram siguen siendo opcionales.
- Informes: nueva ruta `/informes` con permiso `informes:view`, informes predefinidos y consulta natural. Se incluyen rentabilidad por período, trabajadores/horas y comportamiento de humedad con gráficos y actividades del mismo período.
- Rentabilidad: los costos se calculan a partir de mano de obra e insumos consumidos en actividades. Las facturas de compra se muestran como referencia y no se vuelven a sumar, evitando doble conteo del inventario.
- Informes naturales: el modelo de IA, cuando está configurado, solo interpreta tipo de informe y filtros. Los cálculos y consultas permanecen controlados por el backend; no se permite SQL generado por IA.

## Entorno y despliegue

- No existe un entorno virtual Python requerido por el proyecto. `.venv/`, `venv/`, `env/`, `ENV/`, `__pycache__/` y `.pytest_cache/` son artefactos locales y están excluidos por `.gitignore`.
- El frontend conserva el despliegue existente por PM2 mediante `ecosystem.config.cjs` y `.output/server/index.mjs`.
- El backend conserva Docker Compose, incluyendo `schema-init`, API y `notification-worker`.
- `.nuxt/` y `.output/` son resultados de compilación y no deben versionarse. La copia recibida todavía tenía `.output` históricamente rastreado por Git; debe retirarse del índice una sola vez y regenerarse con el build.

## Verificación de esta recuperación

- Compilación sintáctica Python (`compileall`) correcta para el backend modificado.
- Contrato de `module.json` válido y runtime de Informes registrado.
- Rutas frontend verificadas: nombres/rutas únicos y archivo de `/informes` presente.
- Los bloques TypeScript de los archivos Vue modificados transpilados sin errores de sintaxis con TypeScript disponible en el entorno.
- 13 pruebas de sesión frontend aprobadas.
- Lógica de informes validada con base SQLite temporal: costo de mano de obra + insumos, valor de recibos, horas de trabajadores y gráfico de humedad.
- `git diff --check` correcto para fuentes backend y frontend, excluyendo artefactos generados históricos de `.output`.
- No se encontraron secretos nuevos hardcodeados en los archivos modificados.

## Limitaciones de la validación en este entorno

- El contenedor de revisión no trae `passlib`/`bcrypt`, por lo que no fue posible volver a ejecutar aquí la suite completa de pytest que importa autenticación. Las dependencias sí están declaradas en `requirements.txt` y se instalan dentro de la imagen Docker del proyecto.
- No se pudo ejecutar un build Nuxt completo porque la copia entregada no incluye `node_modules` y el entorno de revisión no tiene acceso de red para descargar `pnpm`. Se validaron sintaxis, rutas y las pruebas de sesión disponibles.
- No se levantaron servicios productivos, no se conectó PostgreSQL productivo y no se enviaron notificaciones externas.

## Reproducir en el entorno normal del proyecto

Backend, manteniendo el modelo Docker existente:

```sh
docker compose up -d --build
docker compose exec api pytest tests -q
```

Frontend, usando el gestor fijado en `package.json`:

```sh
corepack enable
pnpm install --frozen-lockfile
pnpm build
node --test tests/auth.test.mjs
pm2 restart ecosystem.config.cjs --update-env
```
