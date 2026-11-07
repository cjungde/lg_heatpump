"""The LG Heat Pump custom integration.

Reads and controls LG heat pumps (Therma V and siblings) over Modbus TCP,
backed by the vendored ``lg_heatpump_modbus`` device library.

For more details, see https://github.com/lg-heatpump/lg_heatpump
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.modbus import async_get_unit
from homeassistant.const import Platform

from .connection import create_modbus_params
from .const import (
    CONF_DEBUG,
    CONF_MODEL,
    CONF_ORP,
    CONF_ORP_LEAD,
    CONF_SCAN_INTERVAL,
    CONF_UNIT_ID,
    DEFAULT_ORP_LEAD,
    LOGGER,
    REQUEST_TIMEOUT,
    SCAN_INTERVAL,
    SETTINGS_SCAN_INTERVAL,
)
from .coordinator import LgHeatPumpCoordinator, LgHeatPumpRuntimeData
from .journal import RecordingUnit, forget_journal, journal_for
from .lg_heatpump_modbus import LgHeatPump
from .lg_heatpump_modbus.logic import OilReturnPulseConfig, OilReturnPulseTracker
from .optional_registers import OptionalRegisters

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import LgHeatPumpConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LgHeatPumpConfigEntry,
) -> bool:
    """Set up an LG heat pump from a config entry."""
    unit = async_get_unit(
        hass,
        entry,
        create_modbus_params(entry.data),
        entry.data[CONF_UNIT_ID],
    )
    # ``require_timeout`` needs modbus-connection 4.12.3 (Home Assistant 2026.10);
    # the 2026.9 line pins an older release without it. Skipping the call there
    # leaves the library default in place, which is what every release before
    # this change used.
    if (require_timeout := getattr(unit, "require_timeout", None)) is not None:
        require_timeout(REQUEST_TIMEOUT)
    else:
        LOGGER.debug(
            "modbus-connection has no require_timeout; keeping the default timeout"
        )
    journal = journal_for(hass, entry)
    journal.event("setup", model=entry.data[CONF_MODEL])
    device = LgHeatPump(
        RecordingUnit(unit, journal),
        model=entry.data[CONF_MODEL],
        debug=entry.data.get(CONF_DEBUG, False),
    )

    interval = timedelta(
        seconds=entry.options.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL.total_seconds())
    )
    readings = LgHeatPumpCoordinator(
        hass, entry, device, device.async_update_readings, interval
    )
    if entry.options.get(CONF_ORP, False):
        readings.oil_return_pulse = OilReturnPulseTracker(
            config=OilReturnPulseConfig(
                lead_seconds=entry.options.get(CONF_ORP_LEAD, DEFAULT_ORP_LEAD)
            )
        )
    settings = LgHeatPumpCoordinator(
        hass, entry, device, device.async_update_settings, SETTINGS_SCAN_INTERVAL
    )

    readings.journal, readings.journal_table = journal, "readings"
    settings.journal, settings.journal_table = journal, "settings"

    await readings.async_config_entry_first_refresh()
    # The first readings poll read the unit's identity, which decides whether the
    # stored answers about the optional limits still apply. Restored before the
    # first settings poll, so served limits are read with it.
    optional = OptionalRegisters(hass, entry)
    await optional.async_restore(device)
    settings.optional_registers = optional
    await settings.async_config_entry_first_refresh()

    entry.runtime_data = LgHeatPumpRuntimeData(
        device, readings, settings, optional, journal=journal
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Probing starts with the next settings poll, never during setup.
    optional.armed = True
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: LgHeatPumpConfigEntry,
) -> bool:
    """Unload a config entry; the shared connection closes with its last holder."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant,
    entry: LgHeatPumpConfigEntry,
) -> None:
    """Drop what the entry kept in memory."""
    forget_journal(hass, entry.entry_id)
