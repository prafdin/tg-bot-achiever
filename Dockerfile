FROM python:3.13-slim

WORKDIR /srv/achiever
COPY pyproject.toml .
COPY app app
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 achiever \
    && install -d -o achiever /data
ENV PYTHONUNBUFFERED=1 DATABASE_PATH=/data/achiever.db
USER achiever
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
