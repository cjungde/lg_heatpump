"""Constants for the LG Heat Pump custom integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "lg_heatpump"
LOGGER: Final = logging.getLogger(__package__)

CONF_UNIT_ID: Final = "unit_id"
CONF_MODEL: Final = "model"
CONF_DEBUG: Final = "debug"

DEFAULT_PORT: Final = 502
DEFAULT_UNIT_ID: Final = 1

MANUFACTURER: Final = "LG"

# A heat pump changes slowly, but the compressor and the pumps do not: poll the
# measurements often and the setpoints rarely.
SCAN_INTERVAL: Final = timedelta(seconds=10)
SETTINGS_SCAN_INTERVAL: Final = timedelta(seconds=60)

# After a write the entity re-reads until the device reports the written value,
# waiting this long before each further read. The refresh right after a write
# can come back with the old value: on 08.11.2026 a read 3 s after the previous
# identical one was answered in 1 ms (a real answer takes 36 ms or more) and the
# shift showed the old value for 60 s, until the next settings poll. A gateway
# that answers repeated requests from a cache would do that. The retries run in
# the background, so the service call returns as quickly as before.
WRITE_CONFIRM_DELAYS: Final = (3.0, 6.0)
