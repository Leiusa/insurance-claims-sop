FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first: this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY sop ./sop
COPY fixtures ./fixtures
COPY scenarios ./scenarios

RUN useradd --create-home --uid 1000 app
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000
EXPOSE 8000

# The API key is supplied at runtime (docker run -e LLM_API_KEY=...), never baked into the image.
CMD ["sh", "-c", "uvicorn sop.web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
