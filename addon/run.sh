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

bashio::log.info "Starting FranklinWH Modbus Bridge (ingress on :8099)"
if bashio::services.available "mqtt"; then
  bashio::log.info "MQTT service available — the bridge will auto-configure from the Supervisor"
else
  bashio::log.warning "No MQTT service registered; configure a broker in Settings or install Mosquitto"
fi

exec uvicorn franklinwh_bridge.main:app \
  --host 0.0.0.0 \
  --port 8099 \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips '*'
