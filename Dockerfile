# Stage 1 — build Gammu 1.43.2 from source, then python-gammu against it
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        cmake \
        make \
        curl \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build Gammu 1.43.2 into /usr/local so pkg-config finds Gammu.pc without PATH tricks.
# python-gammu 3.2.6 requires >= 1.43.0; Debian only ships 1.42.0.
WORKDIR /gammu-src
RUN curl -fsSL https://github.com/gammu/gammu/archive/refs/tags/1.43.2.tar.gz \
    | tar -xz --strip-components=1 \
 && cmake -B build \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON \
        -DWITH_Libusb=OFF \
        -DWITH_MySQL=OFF \
        -DWITH_Postgres=OFF \
        -DWITH_CURL=OFF \
        -DWITH_Bluetooth=OFF \
 && cmake --build build -j"$(nproc)" \
 && cmake --install build

WORKDIR /build
COPY requirements.txt .
ENV CFLAGS="-Wno-return-mismatch"
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Stage 2 — runtime (no compiler, no headers)
FROM python:3.12-slim

# Copy the Gammu shared library from the build stage
COPY --from=builder /usr/local/lib/libGammu* /usr/local/lib/
COPY --from=builder /usr/local/lib/libgsmsd* /usr/local/lib/
RUN ldconfig

WORKDIR /app

COPY --from=builder /install /usr/local
COPY modem_daemon.py ./

# dialout gives access to serial/USB modem devices (GID 20 = dialout on Debian)
RUN groupadd -g 20 dialout_host 2>/dev/null || true \
    && useradd -r -u 1000 -g 20 modem

USER modem

ENV HOST=0.0.0.0 \
    PORT=8080 \
    POLL_INTERVAL=30 \
    STATUS_INTERVAL=60 \
    DB_PATH=/data/sms.db

EXPOSE 8080

VOLUME ["/data", "/etc/gammurc"]

ENTRYPOINT ["python", "modem_daemon.py"]
