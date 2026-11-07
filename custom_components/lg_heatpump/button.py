"""Button platform for the LG Heat Pump integration."""

from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LgHeatPumpConfigEntry, LgHeatPumpRuntimeData
from .optional_registers import async_request_recheck

PARALLEL_UPDATES = 1

REFRESH_DESCRIPTION = ButtonEntityDescription(
    key="refresh",
    translation_key="refresh",
    entity_category=EntityCategory.DIAGNOSTIC,
)

# Off by default: a unit serving none of the optional limits pays one request
# timeout per limit for every recheck, on a link other devices may share.
RECHECK_DESCRIPTION = ButtonEntityDescription(
    key="recheck_optional_registers",
    translation_key="recheck_optional_registers",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LgHeatPumpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the LG heat pump button platform."""
    async_add_entities(
        [
            LgHeatPumpRefreshButton(entry.runtime_data, entry.entry_id),
            LgHeatPumpRecheckButton(entry),
        ]
    )


class LgHeatPumpRefreshButton(ButtonEntity):
    """Polls the measurements and the settings right away."""

    _attr_has_entity_name = True

    def __init__(self, runtime_data: LgHeatPumpRuntimeData, entry_id: str) -> None:
        """Initialize the button."""
        self.entity_description = REFRESH_DESCRIPTION
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry_id}_{REFRESH_DESCRIPTION.key}"
        self._attr_device_info = runtime_data.readings.device_info

    @override
    async def async_press(self) -> None:
        """Poll both tables now, skipping the coordinators' request debounce."""
        # One after the other, to keep the load on the Modbus link low.
        await self._runtime_data.readings.async_refresh()
        await self._runtime_data.settings.async_refresh()


class LgHeatPumpRecheckButton(ButtonEntity):
    """Asks the unit again which optional limits it serves.

    For a unit whose main board was replaced: its identity may stay the same
    while the served registers change, so the stored answers would stand.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: LgHeatPumpConfigEntry) -> None:
        """Initialize the button."""
        self.entity_description = RECHECK_DESCRIPTION
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{RECHECK_DESCRIPTION.key}"
        self._attr_device_info = entry.runtime_data.readings.device_info

    @override
    async def async_press(self) -> None:
        """Forget the stored answers and reload; probing starts after setup."""
        await async_request_recheck(self.hass, self._entry)
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
