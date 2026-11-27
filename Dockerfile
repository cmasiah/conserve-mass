FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    TZ=UTC \
    CONFIG_DIR=/config \
    PYTHONPATH=/app

RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu tini sqlite3 tzdata curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set by the GitHub build: which channel this image belongs to and its commit,
# so the in-app update check compares against the right branch.
ARG RELEASE_CHANNEL=stable
ARG GIT_SHA=unknown
ENV APP_CHANNEL=$RELEASE_CHANNEL \
    APP_GIT_SHA=$GIT_SHA

VOLUME ["/config"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
