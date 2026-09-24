FROM ghcr.io/astral-sh/uv:0.11.25-python3.12-trixie-slim@sha256:1b9ee30381d729b07044688631ba4473c72db5c048a5f2a504c40186e65efb86 AS build

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM ghcr.io/astral-sh/uv:0.11.25-python3.12-trixie-slim@sha256:1b9ee30381d729b07044688631ba4473c72db5c048a5f2a504c40186e65efb86 AS runtime

RUN groupadd --system --gid 10001 invoiceops \
    && useradd --system --uid 10001 --gid invoiceops --no-log-init --create-home invoiceops
WORKDIR /app
COPY --from=build --chown=invoiceops:invoiceops /app/.venv /app/.venv
COPY --chown=invoiceops:invoiceops deploy/seed.py /app/deploy/seed.py
COPY --chown=invoiceops:invoiceops alembic.ini /app/alembic.ini
COPY --chown=invoiceops:invoiceops migrations /app/migrations
COPY --chown=invoiceops:invoiceops eval/reports /app/eval/reports
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER invoiceops
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "invoiceops_agent.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
