FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
RUN pip install -r requirements.txt

COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./

RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "alembic upgrade head && python -m src.main"]
