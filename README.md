# NAVIA Backend · producción y notificaciones inteligentes

API FastAPI + PostgreSQL para gestión cafetalera, recibos, órdenes de trabajo, finca, trazabilidad, ventas, sincronización offline y alertas por correo.

## Arranque

```bash
cp .env.example .env
nano .env
mkdir -p data/postgres data/uploads
sudo docker compose run --rm uploads-init
sudo docker compose up -d --build
```

El API queda enlazado por defecto a `127.0.0.1:8002`; debe publicarse mediante Caddy o un proxy HTTPS. PostgreSQL no expone su puerto al host. El servicio `schema-init` verifica las tablas antes de iniciar la API y el servicio `notification-worker` ejecuta el motor de alertas en un proceso independiente.

## Arquitectura de alertas

1. **Detectores determinísticos** consultan hechos operativos: lotes en alerta, aprobaciones vencidas, lotes sin seguimiento, inventario bajo, solicitudes de venta pendientes y recibos sin lote.
2. **Agente OpenAI** recibe únicamente un JSON mínimo del evento, decide prioridad y redacta el aviso con salida estructurada.
3. **Cola transaccional** deduplica eventos, respeta preferencias y horarios silenciosos, registra auditoría y reintenta fallos SMTP.
4. Las alertas críticas marcadas `force_send` no pueden ser descartadas por la IA. La IA no modifica registros productivos.

## Endpoints de notificaciones

- `GET/PATCH /notifications/preferences/me`
- `GET/POST/PATCH/DELETE /notifications/schedules`
- `GET /notifications/status`
- `GET /notifications/events`
- `GET /notifications/runs`
- `POST /notifications/run`
- `POST /notifications/test-email`

Los endpoints de auditoría y ejecución manual requieren gerencia o superadmin.

## Comprobaciones

```bash
curl http://127.0.0.1:8002/health
curl http://127.0.0.1:8002/ready
curl http://127.0.0.1:8002/auth/status
sudo docker compose ps
sudo docker compose logs -f api notification-worker
```

## Producción

Use `APP_ENV=production`, HTTPS, dominios explícitos, `EXPOSE_AUTH_TOKEN=false`, `SEED_DEMO_DATA=false`, SMTP autenticado y una clave OpenAI guardada únicamente en `.env`. La guía completa está en `PRODUCCION.md`.
