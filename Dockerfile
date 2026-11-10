FROM python:3.11-alpine AS builder

WORKDIR /app

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-alpine AS runner

RUN addgroup -S appgroup
RUN adduser -S -G appgroup -H -s /sbin/nologin appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appgroup /venv /venv
COPY --chown=appuser:appgroup app.py .

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/venv/bin:$PATH"

# OpenTelemetry: endpoint e nome do serviço vêm do Kubernetes (OTEL_EXPORTER_OTLP_ENDPOINT,
# OTEL_SERVICE_NAME). Logs saem por stdout em JSON e são coletados pelo Alloy, por isso
# o exporter de logs do SDK fica desligado (evita log duplicado).
ENV OTEL_SERVICE_NAME=targeting-service \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=none \
    OTEL_PYTHON_LOG_CORRELATION=true \
    OTEL_PYTHON_FLASK_EXCLUDED_URLS=health \
    OTEL_SEMCONV_STABILITY_OPT_IN=http

USER appuser

EXPOSE 8003

CMD ["opentelemetry-instrument", "gunicorn", "--bind", "0.0.0.0:8003", "app:app"]
