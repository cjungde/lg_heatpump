"""Tests for the flush pulse device triggers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from modbus_connection import IllegalDataAddressError
from modbus_connection.mock import MockModbusUnit
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.lg_heatpump.const import (
    CONF_FLUSH_PULSE,
    DOMAIN,
    EVENT_FLUSH_PULSE,
)
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

COMPRESSOR_FREQUENCY = 24  # input register, in Hz; rpm = Hz x 60
LOW_LOAD_HZ = 25  # 1500 rpm, the top of the low-load band
POLL = timedelta(seconds=10)
RAMP_HZ = [26, 35, 45, 55, 60]  # as recorded: 1560, 2100, 2700, 3300, 3600 rpm
CLOCK = "custom_components.lg_heatpump.coordinator.utcnow"


class _Clock:
    """A clock the test advances one poll at a time."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, *, enabled: bool) -> None:
    entry.add_to_hass(hass)
    if enabled:
        hass.config_entries.async_update_entry(entry, options={CONF_FLUSH_PULSE: True})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    return device.id


async def _poll(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    clock: _Clock,
    unit: MockModbusUnit,
    hz: int,
) -> None:
    unit.input[COMPRESSOR_FREQUENCY] = hz
    clock.now += POLL
    await entry.runtime_data.readings.async_refresh()
    await hass.async_block_till_done()


async def _run_a_pulse(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    clock: _Clock,
    unit: MockModbusUnit,
) -> None:
    """Sixty minutes in the low-load band, then the recorded ramp and back."""
    for _ in range(360):
        await _poll(hass, entry, clock, unit, LOW_LOAD_HZ)
    for hz in [*RAMP_HZ, 60, 60, 25]:
        await _poll(hass, entry, clock, unit, hz)


async def test_triggers_are_offered_only_when_the_option_is_on(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patched_connection: None,
) -> None:
    """The tracker is off by default, so there is nothing to trigger on."""
    await _setup(hass, mock_config_entry, enabled=False)
    device_id = _device_id(hass, mock_config_entry)

    offered = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    assert [t for t in offered if t["domain"] == DOMAIN] == []

    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_FLUSH_PULSE: True}
    )
    offered = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    assert sorted(t["type"] for t in offered if t["domain"] == DOMAIN) == [
        "flush_pulse_finished",
        "flush_pulse_imminent",
    ]


async def test_a_pulse_fires_a_warning_and_an_all_clear(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_unit: MockModbusUnit,
    patched_connection: None,
) -> None:
    """Both events fire once, in order, for one recorded-shape pulse."""
    clock = _Clock()
    events = async_capture_events(hass, EVENT_FLUSH_PULSE)
    with patch(CLOCK, clock):
        await _setup(hass, mock_config_entry, enabled=True)
        await _run_a_pulse(hass, mock_config_entry, clock, mock_unit)

    assert [e.data["type"] for e in events] == [
        "flush_pulse_imminent",
        "flush_pulse_finished",
    ]
    warning, finished = (e.data for e in events)
    device_id = _device_id(hass, mock_config_entry)
    assert warning["device_id"] == finished["device_id"] == device_id
    assert warning["entry_id"] == mock_config_entry.entry_id
    assert warning["expected_at"] is not None
    assert warning["completed"] is None
    assert finished["completed"] is True
    assert finished["peak_rpm"] == 3600
    assert 59 <= finished["runtime_minutes"] <= 61
    assert 20 <= finished["duration_seconds"] <= 70


async def test_nothing_fires_while_the_option_is_off(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_unit: MockModbusUnit,
    patched_connection: None,
) -> None:
    """With the tracker off the integration never looks at the compressor."""
    clock = _Clock()
    events = async_capture_events(hass, EVENT_FLUSH_PULSE)
    with patch(CLOCK, clock):
        await _setup(hass, mock_config_entry, enabled=False)
        await _run_a_pulse(hass, mock_config_entry, clock, mock_unit)

    assert events == []
    assert mock_config_entry.runtime_data.readings.flush_pulse is None


async def test_a_high_load_phase_fires_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_unit: MockModbusUnit,
    patched_connection: None,
) -> None:
    """Running at 3300 rpm after a load change is not a flush pulse."""
    clock = _Clock()
    events = async_capture_events(hass, EVENT_FLUSH_PULSE)
    with patch(CLOCK, clock):
        await _setup(hass, mock_config_entry, enabled=True)
        for hz in [30] * 60 + [40, 50, 55] + [55] * 30 + [30]:
            await _poll(hass, mock_config_entry, clock, mock_unit, hz)

    assert events == []


async def test_a_device_trigger_runs_an_automation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patched_connection: None,
) -> None:
    """The trigger fires for its own device and type, and for nothing else."""
    await _setup(hass, mock_config_entry, enabled=True)
    device_id = _device_id(hass, mock_config_entry)
    calls = async_mock_service(hass, "test", "automation")

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": device_id,
                    "type": "flush_pulse_imminent",
                },
                "action": {"service": "test.automation"},
            }
        },
    )

    hass.bus.async_fire(
        EVENT_FLUSH_PULSE, {"device_id": device_id, "type": "flush_pulse_finished"}
    )
    hass.bus.async_fire(
        EVENT_FLUSH_PULSE, {"device_id": "another", "type": "flush_pulse_imminent"}
    )
    await hass.async_block_till_done()
    assert calls == []

    hass.bus.async_fire(
        EVENT_FLUSH_PULSE, {"device_id": device_id, "type": "flush_pulse_imminent"}
    )
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_a_withdrawn_warning_still_fires_after(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_unit: MockModbusUnit,
    patched_connection: None,
) -> None:
    """The compressor stops after the warning: "after" fires, marked not completed.

    Without it an automation that raised the flow temperature on the warning
    would never be told to put it back.
    """
    clock = _Clock()
    events = async_capture_events(hass, EVENT_FLUSH_PULSE)
    with patch(CLOCK, clock):
        await _setup(hass, mock_config_entry, enabled=True)
        for _ in range(352):  # 58.7 minutes in the band, past the warning
            await _poll(hass, mock_config_entry, clock, mock_unit, LOW_LOAD_HZ)
        await _poll(hass, mock_config_entry, clock, mock_unit, 0)

    assert [(e.data["type"], e.data["completed"]) for e in events] == [
        ("flush_pulse_imminent", None),
        ("flush_pulse_finished", False),
    ]
    assert events[1].data["peak_rpm"] is None


async def test_a_failed_read_is_not_an_observation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_unit: MockModbusUnit,
    patched_connection: None,
) -> None:
    """A sub-system that failed keeps its old values; they must not be fed as new."""
    clock = _Clock()
    with patch(CLOCK, clock):
        await _setup(hass, mock_config_entry, enabled=True)
        tracker = mock_config_entry.runtime_data.readings.flush_pulse
        with patch.object(tracker, "update", wraps=tracker.update) as spy:
            mock_unit.fail_read(0, IllegalDataAddressError(), register_type="input")
            await _poll(hass, mock_config_entry, clock, mock_unit, LOW_LOAD_HZ)
            assert spy.call_args.args[1] is None

            mock_unit.fail_read(0, None, register_type="input")
            await _poll(hass, mock_config_entry, clock, mock_unit, LOW_LOAD_HZ)
            assert spy.call_args.args[1] == 1500
