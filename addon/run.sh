#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
#
# Add-on entrypoint. Reads the user's add-on options and exports them as the
# env vars the bridge already understands (settings.py precedence:
# env > config file > options.json > defaults), then execs uvicorn.
#
# MQTT is deliberately NOT set here — config/supervisor.py asks the Supervisor
# for the broker at startup, so there is nothing for the user to type.
set -e

export APP_ENV="ha_addon"

# Timezone. Schedule triggers and TOU windows are evaluated in LOCAL time, so a
# wrong TZ silently runs every automation at the wrong hour.
#
# This bashio attempt is BEST-EFFORT ONLY and must not be trusted as the fix.
# On real add-on installs this call has been observed returning "Unable to
# access the API, forbidden" while the identical token works from Python, and
# the `> /dev/null` guard below cannot tell that from "no timezone set" — it
# just falls through, leaving the container on UTC. The authoritative path is
# config/supervisor.py:apply_timezone(), which runs at startup BEFORE the
# clock guard records the expected zone. Leave this in (when it works it
# agrees, and Python then no-ops), but do not "fix" the timezone here.
if bashio::info.timezone > /dev/null 2>&1; then
  TZ="$(bashio::info.timezone)"
  if [ -n "${TZ}" ] && [ "${TZ}" != "null" ]; then
    export TZ
  fi
fi

# Gateway connection. An empty host is allowed: the UI can add gateways later,
# so the add-on should start and be reachable rather than refusing to boot.
if bashio::config.has_value 'gateway_host'; then
  export MODBUS_HOST="$(bashio::config 'gateway_host')"
fi
export MODBUS_PORT="$(bashio::config 'gateway_port')"
export MODBUS_UNIT_ID="$(bashio::config 'gateway_unit_id')"
export MODBUS_POLL_INTERVAL="$(bashio::config 'poll_interval')"

# settings.py reads logging.level from options.json directly; exporting
# LOG_LEVEL as well keeps uvicorn and the app in step.
export LOG_LEVEL="$(bashio::config 'log_level')"

BRIDGE_VERSION="$(python3 -c 'import franklinwh_bridge as b; print(b.__version__)' 2>/dev/null || echo unknown)"
bashio::log.info "Starting FranklinWH Modbus Bridge v${BRIDGE_VERSION} (ingress on :8099)"
if bashio::services.available "mqtt"; then
  bashio::log.info "MQTT service available — the bridge will auto-configure from the Supervisor"
else
  bashio::log.warning "No MQTT broker yet — install the Mosquitto broker app and the bridge connects to it automatically (or set a broker in Settings → MQTT)"
fi

exec uvicorn franklinwh_bridge.main:app \
  --host 0.0.0.0 \
  --port 8099 \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips '*'
