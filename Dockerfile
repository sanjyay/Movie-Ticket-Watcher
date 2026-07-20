FROM python:3.12.10-slim-bookworm

ARG APP_VERSION=0.2.0
ARG PLAYWRIGHT_VERSION=1.61.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    APP_VERSION=${APP_VERSION} \
    CONTAINER_DEPLOYMENT=true

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/* /root/.cache

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app
COPY --chown=app:app . /app
RUN chmod +x /app/scripts/*.sh /app/scripts/*.py

USER app:app
EXPOSE 8787
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["web"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "/app/scripts/healthcheck.py", "--web"]
