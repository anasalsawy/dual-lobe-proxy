FROM python:3.11-slim

WORKDIR /app

COPY persona/pyproject.toml ./
COPY persona/src ./src

RUN pip install --no-cache-dir .

# Non-root runtime; the container itself is the persona's sandbox.
RUN useradd -m persona && mkdir -p /var/lib/dual-lobe-persona \
    && chown -R persona:persona /var/lib/dual-lobe-persona /app
USER persona

ENV DUAL_LOBE_PERSONA_SANDBOX_ROOT=/var/lib/dual-lobe-persona/workspaces

EXPOSE 8802

CMD ["python", "-m", "dual_lobe_persona"]