# atlas-agent-runtime — python-fastapi (multi-stage: dev = local 모드, runtime = prod 모드)
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy PATH=/opt/venv/bin:$PATH PYTHONPATH=/app
RUN pip install --no-cache-dir uv
WORKDIR /app

# dev: 테스트/개발 의존성 포함, 소스는 local 모드에서 바인드 마운트
FROM base AS dev
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# runtime: 운영 의존성만
FROM base AS runtime
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="atlas-agent-runtime" org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/prodai/atlas" org.opencontainers.image.licenses="Apache-2.0" \
      org.prodai.agent.unit-id="runtime" org.prodai.agent-card="/.well-known/agent-card.json"
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev
COPY . .
RUN useradd -r -u 65532 app && mkdir -p /app/data && chown -R app /app/data
USER 65532
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
