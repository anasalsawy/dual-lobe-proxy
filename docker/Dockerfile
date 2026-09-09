FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml alembic.ini requirements.lock ./
COPY alembic ./alembic
COPY src ./src

RUN pip install --no-cache-dir --require-hashes -r requirements.lock
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/app/src

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8801
ENTRYPOINT ["/entrypoint.sh"]
