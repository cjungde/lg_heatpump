"""Number platform for the LG Heat Pump integration.

The weather-compensation setpoint shifts are plain numbers: they are neither a
temperature the heat pump aims for nor a mode, so they belong here rather than
on the climate entities. So are the optional limits, where the unit serves them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import LIMITS_COMPONENT, LgHeatPumpConfigEntry
from .entity import LgHeatPumpEntity, LgHeatPumpEntityDescription, build_entities
from .lg_heatpump_modbus import LgValueValidationError
from .lg_heatpump_modbus.subsystems.controls import SHIFT_MAX, SHIFT_MIN

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LgHeatPumpNumberDescription(NumberEntityDescription, LgHeatPumpEntityDescription):
    """Describe an LG heat pump number."""

    to_device: Callable[[float], Any] = int
    """Turn the entity value into what the library writes."""


def _shift(circuit: int) -> LgHeatPumpNumberDescription:
    """Describe the auto-mode setpoint shift of one circuit."""
    attribute = f"shift_in_auto_mode_circuit_{circuit}"
    return LgHeatPumpNumberDescription(
        key=attribute,
        translation_key="shift_in_auto_mode",
        translation_placeholders={"circuit": str(circuit)},
        component="controls",
        attribute=attribute,
        native_min_value=SHIFT_MIN,
        native_max_value=SHIFT_MAX,
        native_step=1,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    )


NUMBER_DESCRIPTIONS: tuple[LgHeatPumpNumberDescription, ...] = (
    _shift(1),
    _shift(2),
    # Optional limits: an entity exists only where the unit was found to serve
    # the register, and it is disabled until the user turns it on, because it
    # limits what the heat pump may draw.
    LgHeatPumpNumberDescription(
        key="compressor_modulation_limit",
        translation_key="compressor_modulation_limit",
        component=LIMITS_COMPONENT,
        attribute="compressor_modulation_limit",
        # 0 switches the limit off, so both ends stay reachable; 1-39 % is not a
        # valid setting and is refused by the library.
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    LgHeatPumpNumberDescription(
        key="power_limit",
        translation_key="power_limit",
        component=LIMITS_COMPONENT,
        attribute="power_limit",
        native_min_value=0.1,
        native_max_value=25.0,
        native_step=0.1,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=NumberDeviceClass.POWER,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        to_device=lambda value: round(value, 1),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LgHeatPumpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the LG heat pump number platform."""
    async_add_entities(
        build_entities(entry.runtime_data, LgHeatPumpNumber, NUMBER_DESCRIPTIONS)
    )


class LgHeatPumpNumber(LgHeatPumpEntity, NumberEntity):
    """A single writable numeric register on the heat pump."""

    entity_description: LgHeatPumpNumberDescription

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current value."""
        return self._value

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write a new value and refresh so the state follows the device."""
        try:
            await self._async_write_and_refresh(
                self.entity_description.to_device(value)
            )
        except LgValueValidationError as error:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_value",
                translation_placeholders={"error": str(error)},
            ) from error
