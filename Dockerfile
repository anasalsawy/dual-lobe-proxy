FROM python:3.11-slim

WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy package files
COPY pyproject.toml README.md ./
COPY src ./src

# Install the package
RUN pip install --no-cache-dir -e "."

# Railway sets PORT env var; default to 8000 for local
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn dual_lobe.api.app:app --host 0.0.0.0 --port ${PORT}"]
