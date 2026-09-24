#!/usr/bin/env sh
set -eu

# Permite reutilizar la misma imagen para la API, el worker y comandos de mantenimiento.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

UPLOAD_DIR="${NAVIA_UPLOAD_DIR:-/app/uploads}"
if [ ! -d "$UPLOAD_DIR" ]; then
  echo "ERROR: el directorio de uploads no existe: $UPLOAD_DIR" >&2
  exit 1
fi
if [ ! -r "$UPLOAD_DIR" ] || [ ! -w "$UPLOAD_DIR" ]; then
  echo "ERROR: el usuario de la API no puede leer/escribir en $UPLOAD_DIR" >&2
  echo "Ejecute: docker compose run --rm uploads-init" >&2
  exit 1
fi

exec gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers "${WEB_CONCURRENCY:-1}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
  --keep-alive "${GUNICORN_KEEP_ALIVE:-5}" \
  --max-requests "${GUNICORN_MAX_REQUESTS:-1000}" \
  --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-100}" \
  --access-logfile - \
  --error-logfile -
