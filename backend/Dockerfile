FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN pip install --no-cache-dir ".[mem0]" \
  && useradd --create-home ice \
  && mkdir -p /app/data/sessions /app/data/backups \
  && chown -R ice:ice /app /entrypoint.sh \
  && chmod +x /entrypoint.sh \
  && apt-get update \
  && apt-get install -y --no-install-recommends gosu \
  && rm -rf /var/lib/apt/lists/*
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
