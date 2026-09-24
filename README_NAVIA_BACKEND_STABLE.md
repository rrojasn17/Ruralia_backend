# NAVIA Backend estable

Backend FastAPI + PostgreSQL para digitalizar recibos de café, órdenes de trabajo, trazabilidad y sincronización offline.

## Arranque

```bash
cp .env.example .env
sudo docker compose up --build
```

API: `http://localhost:8002`
Docs: `http://localhost:8002/docs`

Usuario inicial:

- correo: `admin@navia.com`
- contraseña: `AdminNavia2026!`

Cambie esas credenciales en `.env` antes de producción.

## Variables relevantes

- `APP_PORT`: puerto público del API.
- `CORS_ORIGINS`: dominios del frontend permitidos.
- `DEFAULT_SESSION_DAYS`: duración inicial de la sesión, por defecto 30 días.
- `COOKIE_SECURE`: usar `true` en HTTPS productivo.
- `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME`: usuario gerente inicial.

## Roles

- `gerente`: acceso completo, usuarios, recibos, OT, configuración.
- `operario`: ve sus OT asignadas, registra seguimientos y puede recibir café.
- `administrativo`: recibe café y consulta información operativa; compras de insumos queda previsto para siguiente fase.

## Endpoints principales

- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/logout`
- `GET/POST/PATCH /usuarios`
- `GET/POST/PATCH /recibos`
- `GET/POST/PATCH /ot`
- `POST /ot/{id}/seguimientos`
- `POST /sync/push`
- `GET /dashboard`
- `GET/PATCH /configuracion`
