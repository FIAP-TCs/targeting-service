# --- ETAPA 1: Builder (Construção e instalação de dependências) ---
# Utilizamos uma imagem leve do Alpine com Python 3.11 para otimizar o tamanho da build
FROM python:3.11-alpine AS builder

WORKDIR /app

# Criamos um ambiente virtual (/venv) isolado para manter as dependências organizadas
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Copiamos o ficheiro de requisitos e instalamos as dependências do projeto sem cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- ETAPA 2: Runner (Imagem final de execução limpa e segura) ---
FROM python:3.11-alpine AS runner

# Boas práticas para Kubernetes: criamos um grupo e um utilizador sem privilégios (non-root)
RUN addgroup -S appgroup
RUN adduser -S -G appgroup -H -s /sbin/nologin appuser

WORKDIR /app

# Copiamos o ambiente virtual compilado e o código da aplicação da etapa anterior
COPY --from=builder --chown=appuser:appgroup /venv /venv
COPY --chown=appuser:appgroup app.py .

# Otimizações para o comportamento do Python no contentor
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/venv/bin:$PATH"

# --- CONFIGURAÇÃO DE OBSERVABILIDADE (Fase 4 - OpenTelemetry) ---
# Define o nome do serviço, protocolos OTLP e ativa a correlação de logs com traces
ENV OTEL_SERVICE_NAME=targeting-service \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=none \
    OTEL_PYTHON_LOG_CORRELATION=true \
    OTEL_PYTHON_FLASK_EXCLUDED_URLS=health \
    OTEL_SEMCONV_STABILITY_OPT_IN=http

# Executamos o contentor com o utilizador seguro criado anteriormente
USER appuser

# Porta exposta pelo microsserviço
EXPOSE 8003

# Comando de arranque: o opentelemetry-instrument injeta o rastreamento automaticamente no Gunicorn/Flask
CMD ["opentelemetry-instrument", "gunicorn", "--bind", "0.0.0.0:8003", "app:app"]
