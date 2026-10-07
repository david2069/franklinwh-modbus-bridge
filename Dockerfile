# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git \
    && rm -rf /var/lib/apt/lists/*

# Pinned known-good versions for every (transitive) dependency — see
# constraints.txt. Applied via `-c` to all pip installs so the whole tree is
# reproducible, not just franklinwh-modbus. Copied first so its layer caches
# independently of the source.
COPY constraints.txt .

# Install franklinwh-modbus from GitHub (includes pysunspec2, pymodbus, pyserial).
# Pinned to an immutable git ref for reproducible builds. To upgrade, bump
# FWM_REF to a new tag or commit SHA (prefer a release tag once cut) — because
# the ref is part of this RUN command, changing it busts the layer cache, so a
# plain `docker compose build` picks it up WITHOUT needing --no-cache.
ARG FWM_REF=v0.9.5
RUN pip install --no-cache-dir -c constraints.txt pyserial \
    "franklinwh-modbus @ git+https://github.com/david2069/franklinwh-modbus.git@${FWM_REF}"

# Install the bridge package
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir -c constraints.txt .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8099/api/status || exit 1

CMD ["uvicorn", "franklinwh_bridge.main:app", "--host", "0.0.0.0", "--port", "8099", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
