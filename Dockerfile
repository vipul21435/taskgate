# TaskGate CLI image. Bases are pinned by digest; the app runs as a non-root user.
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
LABEL project=taskgate \
      org.opencontainers.image.title="taskgate" \
      org.opencontainers.image.description="Review gates for pull requests that add or change benchmark tasks" \
      org.opencontainers.image.licenses="MIT"

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
# Dependencies first so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
 && useradd --create-home --uid 10001 taskgate
COPY examples ./examples

USER taskgate
ENTRYPOINT ["taskgate"]
CMD ["--help"]
