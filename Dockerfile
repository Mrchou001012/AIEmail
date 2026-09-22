FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY pyproject.toml /app/
RUN mkdir -p /app/app && touch /app/app/__init__.py \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[dev]"
COPY app /app/app
COPY . /app
RUN mkdir -p /app/runtime/demo_outbox /app/runtime/inbound_archive /app/runtime/mail_archive

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
