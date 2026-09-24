# Puesta en producción de NAVIA

## 1. Respaldo obligatorio

```bash
sudo docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > navia_predeploy.sql
sudo tar -czf navia_uploads_predeploy.tar.gz data/uploads
```

No use `docker compose down -v` durante la actualización.

## 2. Variables y secretos

```bash
cp .env.example .env
nano .env
```

Reemplace todos los valores `CAMBIE_`. Verifique especialmente:

- `APP_ENV=production`
- `APP_BASE_URL=https://DOMINIO/backend`
- `FRONTEND_URL=https://DOMINIO`
- `CORS_ORIGINS=https://DOMINIO`
- `TRUSTED_HOSTS=DOMINIO,localhost,127.0.0.1`
- `COOKIE_SECURE=true`, `COOKIE_HTTPONLY=true`, `COOKIE_SAMESITE=lax`
- `EXPOSE_AUTH_TOKEN=false`
- `SEED_DEMO_DATA=false`
- `AUTO_CREATE_SCHEMA=false`; el servicio `schema-init` realiza la inicialización idempotente
- claves independientes para PostgreSQL, superadmin, SMTP, pgAdmin y OpenAI

Proteja el archivo:

```bash
chmod 600 .env
```

## 3. Preparar archivos persistentes

```bash
mkdir -p data/postgres data/uploads
sudo docker compose run --rm uploads-init
stat -c '%u:%g %a %n' data/uploads
```

El propietario esperado es `10001:10001`.

## 4. Construir, verificar esquema e iniciar

```bash
sudo docker compose build --pull
sudo docker compose run --rm schema-init
sudo docker compose up -d
sudo docker compose ps
```

`api` no inicia hasta que PostgreSQL, `schema-init` y `uploads-init` terminen correctamente. El worker no inicia hasta que `/ready` responda.

## 5. Corregir e instalar el frontend

Copie los cuatro archivos del parche en sus rutas originales:

- `composables/useAuth.ts`
- `plugins/api.ts`
- `middleware/auth.global.ts`
- `pages/login.vue`

Luego:

```bash
cd app
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm build
pm2 startOrReload ecosystem.config.cjs
pm2 save
```

Configure `NUXT_PUBLIC_API_BASE=/backend`. La sesión queda exclusivamente en la cookie `HttpOnly` emitida por FastAPI; el frontend ya no crea una segunda cookie con el token.

## 6. SMTP y OpenAI

Configure SMTP y pruebe primero el canal sin IA:

```bash
curl -X POST https://DOMINIO/backend/notifications/test-email \
  -H 'Cookie: navia_token=SESION_DE_GERENCIA'
```

Después configure:

```env
NOTIFICATION_WORKER_ENABLED=true
AI_NOTIFICATIONS_ENABLED=true
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
```

La clave nunca debe enviarse al frontend ni guardarse en PostgreSQL. El agente recibe solo hechos mínimos del evento. Si OpenAI falla, el motor usa una redacción determinística y conserva las alertas críticas.

## 7. Flujo de prueba de login

1. Abra una ventana privada.
2. Inicie sesión una sola vez.
3. Debe entrar inmediatamente a `/dashboard`, sin refrescar.
4. En DevTools confirme una cookie `navia_token` con `HttpOnly`, `Secure` y `SameSite=Lax`.
5. Confirme que no existe `navia_front_token`.
6. Recargue `/dashboard` y valide que SSR conserva la sesión.
7. Cierre sesión y confirme que una ruta protegida vuelve a `/login?next=...`.

Prueba API:

```bash
curl -i -c /tmp/navia-cookie.txt \
  -X POST https://DOMINIO/backend/auth/login \
  -H 'Content-Type: application/json' \
  --data '{"correo":"CORREO","password":"CLAVE"}'

curl -i -b /tmp/navia-cookie.txt https://DOMINIO/backend/auth/me
```

Ambas respuestas deben ser `200`.

## 8. Validación del motor de alertas

```bash
sudo docker compose logs --tail=200 api notification-worker
curl https://DOMINIO/backend/notifications/status -H 'Cookie: navia_token=SESION_DE_GERENCIA'
curl -X POST https://DOMINIO/backend/notifications/run -H 'Cookie: navia_token=SESION_DE_GERENCIA'
```

Revise:

- una sola alerta por entidad dentro del período de enfriamiento;
- destinatarios activos con correo válido;
- preferencias de severidad y horas silenciosas;
- eventos, entregas, intentos, errores y ejecuciones auditables;
- reintentos SMTP exponenciales;
- el worker saludable y con heartbeat reciente.

## 9. Proxy HTTPS

`Caddyfile.example` usa `handle_path /backend/*`, por lo que elimina `/backend` antes de enviar a FastAPI:

- navegador: `/backend/auth/login`
- FastAPI: `/auth/login`

El proxy debe ser el único punto público. Mantenga API y PostgreSQL enlazados a red interna o `127.0.0.1`.

## 10. Recuperar el superadmin

Para sincronizar una nueva contraseña existente, active temporalmente:

```env
ADMIN_EMAIL=correo-real@dominio.com
ADMIN_PASSWORD=NUEVA_CLAVE_FUERTE
ADMIN_PASSWORD_SYNC_ON_STARTUP=true
```

Reinicie API, confirme el ingreso y vuelva inmediatamente a `ADMIN_PASSWORD_SYNC_ON_STARTUP=false`.

## 11. Operación continua

Programe respaldos externos de PostgreSQL y `data/uploads`, rotación y retención, supervisión de `/ready`, revisión de `notification-worker`, renovación de secretos y actualización controlada de dependencias. Antes de cada actualización ejecute `pnpm lint`, `pnpm typecheck`, pruebas backend y una restauración de respaldo en un ambiente de ensayo.
