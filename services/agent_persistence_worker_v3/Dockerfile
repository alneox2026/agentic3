FROM python:3.12-slim

WORKDIR /app

COPY services/agent_persistence_worker_v3/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn services.agent_persistence_worker_v3.app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

