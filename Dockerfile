# Stage 1 — build python-gammu against libgammu headers
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libgammu-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Stage 2 — runtime only (no compiler, no headers)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgammu8 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local
COPY modem_daemon.py ./

# dialout gives access to serial/USB modem devices
RUN groupadd -g 20 dialout_host 2>/dev/null || true \
    && useradd -r -u 1000 -g dialout_host modem

USER modem

ENV HOST=0.0.0.0 \
    PORT=8080 \
    POLL_INTERVAL=30 \
    STATUS_INTERVAL=60 \
    DB_PATH=/data/sms.db

EXPOSE 8080

VOLUME ["/data", "/etc/gammurc"]

ENTRYPOINT ["python", "modem_daemon.py"]
