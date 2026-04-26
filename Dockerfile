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
        # ffmpeg is what discord.py shells out to for transcoding voice
        # clips into Opus frames.
        ffmpeg \
        # libopus + libsodium ship the codecs PyNaCl/discord.py link against
        # at runtime when sending audio packets.
        libopus0 \
        libsodium23 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
RUN pip install -r requirements.txt

COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY voices/ voices/

RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "alembic upgrade head && python -m src.main"]
