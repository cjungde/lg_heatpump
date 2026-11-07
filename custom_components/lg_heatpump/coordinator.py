"""DataUpdateCoordinators that poll the LG heat pump.

The measurements and the settings live in different Modbus tables and change at
very different rates, so each gets its own coordinator and its own interval.
Both drive the same ``LgHeatPump`` object, which is what the entities read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, override

from modbus_connection import ModbusError

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import utcnow

from .const import DOMAIN, EVENT_ORP, LOGGER, MANUFACTURER
from .lg_heatpump_modbus import SETTING_COMPONENTS, UpdateReport
from .lg_heatpump_modbus.logic import (
    EVENT_CANCELLED,
    EVENT_FINISHED,
    EVENT_IMMINENT,
    OilReturnPulseTracker,
)

#: The sub-system of the optional limits, polled with the settings once served.
LIMITS_COMPONENT = "limits"

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import timedelta

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .lg_heatpump_modbus import LgHeatPump
    from .optional_registers import OptionalRegisters

type LgHeatPumpConfigEntry = ConfigEntry[LgHeatPumpRuntimeData]


class LgHeatPumpCoordinator(DataUpdateCoordinator[UpdateReport]):
    """Refresh a set of the heat pump's sub-systems on a schedule.

    Each sub-system reads only its own registers, so one that stops answering
    keeps its previous values and does not take the rest of the poll with it.
    """

    config_entry: LgHeatPumpConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: LgHeatPumpConfigEntry,
        device: LgHeatPump,
        poll: Callable[[], Awaitable[UpdateReport]],
        interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=interval,
        )
        self.device = device
        self._poll = poll
        self._consecutive_failures: dict[str, int] = {}
        self.oil_return_pulse: OilReturnPulseTracker | None = None
        """Follows the ORP (oil return pulse); set by the setup if the option is on."""
        self.last_poll_time: datetime | None = None
        """When the last poll finished, whether it succeeded or not."""
        self.optional_registers: OptionalRegisters | None = None
        """Probes the optional limits after a poll; set on the settings poll only."""

    @callback
    @override
    def _async_refresh_finished(self) -> None:
        """Remember when a poll ended, for the debug timestamp sensor."""
        self.last_poll_time = utcnow()

    @property
    def device_info(self) -> dr.DeviceInfo:
        """Return the device the heat pump's entities belong to."""
        entry = self.config_entry
        return dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=self.device.model_name,
            # The unit may refuse the identity registers; fall back to the chosen model.
            model_id=self.device.info.product_code or self.device.model.key,
            name=entry.title,
        )

    async def _async_update_data(self) -> UpdateReport:
        """Poll the heat pump, or fail the update if nothing answered."""
        try:
            report = await self._poll()
        except ModbusError as error:
            # A dead link or a device that answers nothing reaches here; a
            # single refused block lands in report.failed instead.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="modbus_error",
                translation_placeholders={"error": str(error)},
            ) from error
        if not report.updated:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="no_component_answered",
            ) from ExceptionGroup(
                "no sub-system answered", list(report.failed.values())
            )
        self._log_transitions(report)
        if self.oil_return_pulse is not None:
            self._track_oil_return_pulse(report)
        if self.optional_registers is not None:
            await self.optional_registers.async_after_settings_poll(self.device, report)
        return report

    def _track_oil_return_pulse(self, report: UpdateReport) -> None:
        """Feed the tracker one observation and fire what the poll caused.

        The events go out before the entities see the new data: a warning is only
        useful while there is still time to act on it.

        A sub-system that failed this poll keeps its previous values, so its
        readings are not an observation. The tracker then gets ``None`` and skips
        the poll, which is not the same as a compressor stop.

        A withdrawn warning goes out as "after" with ``completed`` false, so an
        automation that acted on "before" always hears back.
        """
        tracker = self.oil_return_pulse
        device = self.device
        fresh = not {"sensors", "states"} & set(report.failed)
        events = tracker.update(
            utcnow(),
            device.sensors.compressor_speed if fresh else None,
            defrosting=bool(device.states.defrosting),
            dhw_heating=bool(device.states.dhw_heating),
        )
        for event in events:
            if event not in (EVENT_IMMINENT, EVENT_FINISHED, EVENT_CANCELLED):
                continue  # the start of a pulse is not offered as a trigger
            completed = event == EVENT_FINISHED
            event_type = EVENT_IMMINENT if event == EVENT_IMMINENT else EVENT_FINISHED
            record = tracker.last_pulse if completed else None
            expected = tracker.expected_at()
            self.hass.bus.async_fire(
                EVENT_ORP,
                {
                    "type": event_type,
                    "completed": completed if event_type == EVENT_FINISHED else None,
                    "device_id": self._device_id(),
                    "entry_id": self.config_entry.entry_id,
                    "runtime_minutes": (
                        record.runtime_minutes if record else tracker.runtime_minutes
                    ),
                    "expected_at": expected.isoformat() if expected else None,
                    "peak_rpm": record.peak_rpm if record else None,
                    "duration_seconds": (
                        round(record.duration_seconds, 1) if record else None
                    ),
                },
            )
            LOGGER.info(
                "%s: oil return pulse %s (running time %s min)",
                self.name,
                event,
                record.runtime_minutes if record else tracker.runtime_minutes,
            )

    def _device_id(self) -> str | None:
        """Return the device registry id the device triggers filter on."""
        entry_id = self.config_entry.entry_id
        device = dr.async_get(self.hass).async_get_device_by_identifier(
            (DOMAIN, entry_id), entry_id
        )
        return device.id if device else None

    def _log_transitions(self, report: UpdateReport) -> None:
        """Log a sub-system going quiet once, and log it coming back."""
        for name, cause in report.failed.items():
            previous = self._consecutive_failures.get(name, 0)
            self._consecutive_failures[name] = previous + 1
            if previous == 0:
                LOGGER.warning(
                    "%s: %s failed to refresh and is keeping its previous values: %s",
                    self.name,
                    name,
                    cause,
                )
        for name in report.updated:
            if self._consecutive_failures.pop(name, None) is not None:
                LOGGER.info("%s: %s is available again", self.name, name)


@dataclass
class LgHeatPumpRuntimeData:
    """Runtime data for a configured heat pump."""

    device: LgHeatPump
    readings: LgHeatPumpCoordinator
    settings: LgHeatPumpCoordinator
    optional_registers: OptionalRegisters

    def coordinator_for(self, component: str) -> LgHeatPumpCoordinator:
        """Return the coordinator that owns a sub-system's data."""
        if component in SETTING_COMPONENTS or component == LIMITS_COMPONENT:
            return self.settings
        return self.readings
