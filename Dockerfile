# Stable Python minor + Debian release; update deliberately with the runtime matrix.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY dprovenancekit ./dprovenancekit

RUN python -m pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home dpk

USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["dpk"]
CMD [
  "ui",
  "--source", "primary=Traces=/traces/traces.sqlite",
  "--host", "0.0.0.0",
  "--port", "8080",
  "--allow-host", "localhost",
  "--allow-host", "127.0.0.1",
  "--allow-host", "::1"
]
