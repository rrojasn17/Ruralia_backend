FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN python -m pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/home/navia/.local/bin:${PATH}

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && groupadd --gid 10001 navia \
    && useradd --uid 10001 --gid navia --shell /usr/sbin/nologin --create-home navia \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels

COPY . .
RUN chmod +x docker-entrypoint.sh \
    && mkdir -p /app/uploads \
    && chown -R navia:navia /app

USER navia

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
