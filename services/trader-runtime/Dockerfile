FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

WORKDIR /app
RUN groupadd --system ai-trader && useradd --system --gid ai-trader --home /nonexistent ai-trader
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --no-managed-python \
    && mkdir -p /data && chown ai-trader:ai-trader /data
ENV PATH="/app/.venv/bin:$PATH"

ENV PYTHONUNBUFFERED=1
ENV AI_TRADER_DB_PATH=/data/ai-trader.sqlite3
EXPOSE 8000

USER ai-trader
CMD ["uvicorn", "trader_runtime.main:app", "--host", "0.0.0.0", "--port", "8000"]
