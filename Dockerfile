# hh-snpr: контейнер для VPS — конвейер тикает 24/7 (этап 3, 18.10).
# Модель: python:3.13-slim + демон в цикле; рестартует Docker
# (restart: unless-stopped) — «ноут спал» исчезает как класс.
#
# Состояние живёт ВНЕ образа (тома, см. docker-compose.yml):
#   /app/data      — сторы конвейера (seen/parked/replied) + scheduler.log
#   /app/hh-config — auth.json/cookies базы (ручка CONFIG_DIR, tool.py:345)
# Креды LLM — env SNE_AI_* (compose env_file .env; в образ не попадают).
# Конфиг промптов печётся из репо: snpr/config.toml (cli --config
# дефолтит на него); свой — примонтировать поверх или --config.
#
# Сборка/запуск: docker compose up -d --build (см. docker-compose.yml).

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    TZ=Europe/Moscow \
    CONFIG_DIR=/app/hh-config

# tzdata — чтобы шапки scheduler.log были в TZ (иначе UTC)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# База hh-applicant-tool (пиннинг 833cb658, журнал 16.10) + её зависимости
# (requests/cryptography/brotli/pillow/tomli-w) — manylinux-колёса, gcc не нужен
COPY hh-applicant-tool /app/hh-applicant-tool
RUN pip install --no-cache-dir /app/hh-applicant-tool

# snpr — не пакет, дерево модулей: python -m snpr
COPY snpr /app/snpr
COPY scripts/docker /app/scripts/docker

RUN useradd -m snpr \
    && mkdir -p /app/data /app/hh-config \
    && chown -R snpr:snpr /app
USER snpr

# Тикер: цикл демона; интервал — config [daemon].interval_seconds (60 с).
# Тик каждую минуту трогает data/last_run.txt (sne._touch_marker) —
# HEALTHCHECK гоняет scripts/docker/healthcheck.py по его свежести.
HEALTHCHECK --interval=10m --timeout=30s --retries=2 \
    CMD ["python", "-X", "utf8", "scripts/docker/healthcheck.py"]

CMD ["python", "-X", "utf8", "-m", "snpr", "daemon"]
