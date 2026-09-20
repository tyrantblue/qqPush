# qqpush 服务镜像：运行时零依赖，镜像里只多一个 Python 运行时
FROM python:3.12-slim

# uv 只用于构建期安装依赖
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    QQPUSH_HOST=0.0.0.0 \
    QQPUSH_PORT=8088 \
    QQPUSH_GROUPS_FILE=/app/data/groups.json \
    QQPUSH_FAILED_LOG=/app/data/failed.ndjson \
    QQPUSH_HEALTH_PORT=8089

WORKDIR /app

# 依赖安装（本项目运行时零依赖，仅安装自身；有 uv.lock 时用 --frozen 保证可复现）
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project
RUN uv sync --frozen --no-dev || uv sync --no-dev

COPY data/groups.example.json ./data/groups.json

# 非 root 运行
RUN useradd --create-home --uid 10001 qqpush \
    && mkdir -p /app/data \
    && chown -R qqpush:qqpush /app
USER qqpush

EXPOSE 8088 8089

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('QQPUSH_HEALTH_PORT') or os.environ.get('QQPUSH_PORT','8088'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status==200 else 1)"

CMD ["qqpush"]
