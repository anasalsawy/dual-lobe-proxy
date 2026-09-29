FROM python:3.13-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "dual_lobe_crewai.webapp"]
