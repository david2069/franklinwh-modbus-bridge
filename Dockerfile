# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git \
    && rm -rf /var/lib/apt/lists/*

# Install franklinwh-modbus from GitHub (includes pysunspec2, pymodbus, pyserial).
# Pinned to an immutable git ref for reproducible builds. To upgrade, bump
# FWM_REF to a new tag or commit SHA (prefer a release tag once cut) — because
# the ref is part of this RUN command, changing it busts the layer cache, so a
# plain `docker compose build` picks it up WITHOUT needing --no-cache.
ARG FWM_REF=eb698258361ad2fbd30bf3d48767975f73f48d9d
RUN pip install --no-cache-dir pyserial \
    "franklinwh-modbus @ git+https://github.com/david2069/franklinwh-modbus.git@${FWM_REF}"

# Install the bridge package
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8099/api/status || exit 1

CMD ["uvicorn", "franklinwh_bridge.main:app", "--host", "0.0.0.0", "--port", "8099", "--workers", "1"]
