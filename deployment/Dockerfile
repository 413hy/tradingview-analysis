FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium
COPY shared /app/shared
COPY signal-system /app/signal-system
COPY trading-system /app/trading-system
COPY migrations /app/migrations
COPY alembic.ini /app/
ENV PYTHONPATH=/app/shared/src:/app/signal-system/src:/app/trading-system/src
ENV PYTHONUNBUFFERED=1
