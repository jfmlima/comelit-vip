"""Constants for the Comelit ViP integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "comelit_vip"
MANUFACTURER: Final = "Comelit"

CONF_TOKEN: Final = "token"
CONF_WEB_PORT: Final = "web_port"
CONF_USER_SLOT: Final = "user_slot"
CONF_RTSP_PORT: Final = "rtsp_port"
CONF_RTSP_HOST: Final = "rtsp_host"
CONF_EXPOSE_STREAM: Final = "expose_stream"
CONF_RECORD_ON_RING: Final = "record_on_ring"
CONF_SNAPSHOT_ON_RING: Final = "snapshot_on_ring"
CONF_RECORD_SECONDS: Final = "record_seconds"
CONF_RECORD_PATH: Final = "record_path"

DEFAULT_PORT: Final = 64100
DEFAULT_WEB_PORT: Final = 8080
DEFAULT_WEB_PASSWORD: Final = "comelit"
DEFAULT_RTSP_PORT: Final = 8554
DEFAULT_RECORD_SECONDS: Final = 15
# Relative to hass.config.media_dirs["local"]; that is /media only in Docker.
DEFAULT_RECORD_DIRNAME: Final = "comelit_vip"

EVENT_RING: Final = "ring"
EVENT_INTERNAL_CALL: Final = "internal_call"
EVENT_CALL_ENDED: Final = "call_ended"
EVENT_TYPES: Final = [EVENT_RING, EVENT_INTERNAL_CALL, EVENT_CALL_ENDED]

BUS_EVENT: Final = f"{DOMAIN}_event"

RECONNECT_BACKOFF: Final = (5, 15, 30, 60, 120, 300, 600)
# Consecutive UAUT refusals before reauth. The panel's response code for a
# slot held by another client is unknown; it may be the same as for a bad token.
AUTH_FAILURES_BEFORE_REAUTH: Final = 3
# Connection lifetime after which the backoff resets.
STABLE_CONNECTION: Final = 120.0
WATCHDOG_INTERVAL: Final = 60.0
# An idle panel sends nothing between calls; the watchdog probes it after this silence.
KEEPALIVE_INTERVAL: Final = 240.0

# Registration lease, measured on a 6741W (firmware 2.1.3): the diagnostics
# page counts down from 3600 s, and at zero calls stop arriving on a
# connection that stays open.
REGISTRATION_TTL: Final = 3600.0
REGISTRATION_REFRESH: Final = REGISTRATION_TTL / 4
# Whether an in-band renewal extends the panel's lease is not confirmed, so a
# fresh connection is taken before the TTL runs out.
CONNECTION_MAX_AGE: Final = REGISTRATION_TTL - 600.0
