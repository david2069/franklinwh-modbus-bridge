# Home Assistant add-on image.
#
# Mirrors the root Dockerfile's install (same constraints.txt, same pinned
# franklinwh-modbus ref) but builds FROM the Supervisor-provided base image so
# the add-on gets the right architecture, s6 init and HA's CA bundle.
#
# Build context is addon/ — the Supervisor builds an add-on with its own
# directory as context and COPY cannot reach "..". So the bridge itself and
# constraints.txt are fetched from GitHub at BRIDGE_REF rather than copied.
# BRIDGE_REF should match `version` in config.yaml once releases are tagged.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.19
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=ha_addon

WORKDIR /app

# build-base/linux-headers/git are needed to compile a few wheels on Alpine and
# to pip-install from git; dropped again in the same layer so they don't ship.
ARG FWM_REF=v0.9.5
ARG BRIDGE_REF=main
ARG REPO=https://github.com/david2069/franklinwh-modbus-bridge
RUN apk add --no-cache --virtual .build-deps build-base linux-headers git \
    && apk add --no-cache curl \
    && pip install --no-cache-dir \
       -c "https://raw.githubusercontent.com/david2069/franklinwh-modbus-bridge/${BRIDGE_REF}/constraints.txt" \
       pyserial \
       "franklinwh-modbus @ git+https://github.com/david2069/franklinwh-modbus.git@${FWM_REF}" \
       "franklinwh-modbus-bridge @ git+${REPO}.git@${BRIDGE_REF}" \
    && apk del .build-deps

COPY run.sh /run.sh
RUN chmod a+x /run.sh

# /data is the add-on's persistent volume, provided by the Supervisor.
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8099/api/status || exit 1

CMD ["/run.sh"]
