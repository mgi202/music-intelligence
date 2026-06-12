FROM python:3.11-slim

WORKDIR /srv/mis

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/

ENV PYTHONUNBUFFERED=1 \
    SQLITE_PATH=/data/library.db

VOLUME /data

# Default command = API server. The worker service overrides this in compose.
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8080"]
