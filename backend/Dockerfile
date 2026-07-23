FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY entrypoint.sh /entrypoint.sh
RUN apt-get update \
  && apt-get install -y --no-install-recommends gosu \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --no-cache-dir ".[mem0]" \
  && useradd --create-home --uid 10001 ice \
  && mkdir -p /app/data/sessions /app/data/backups \
  && chown -R ice:ice /app \
  && sed -i 's/\r$//' /entrypoint.sh \
  && chmod 755 /entrypoint.sh
# Start as root so entrypoint can chown the mounted volume, then gosu -> ice
USER root
EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
