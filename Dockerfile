# DProvenanceKit hosted backend — pure standard library, so no pip install step.
FROM python:3.12-slim
WORKDIR /app

# The free library (src/) and the service (server/).
COPY src/ ./src/
COPY server/ ./server/

ENV PYTHONUNBUFFERED=1 \
    DPROV_STORAGE=sqlite \
    DPROV_DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8787

EXPOSE 8787
VOLUME ["/data"]

# First run seeds a demo project + API key (printed once in the logs).
CMD ["python", "server/run.py"]
