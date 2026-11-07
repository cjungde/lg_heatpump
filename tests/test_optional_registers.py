"""Tests for the optional limits: probed once, remembered, polled only where served."""

from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
)
from modbus_connection.mock import MockModbusUnit
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lg_heatpump.const import DOMAIN
from custom_components.lg_heatpump.diagnostics import async_get_config_entry_diagnostics
from custom_components.lg_heatpump.optional_registers import (
    RECHECK_RECORD,
    async_request_recheck,
    fingerprint,
    pending,
    restore,
)
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

SENSOR = "sensor.lg_therma_v_optional_registers"
MODULATION = "number.lg_therma_v_maximum_compressor_modulation"
POWER = "number.lg_therma_v_power_limit"
ENABLE = "switch.lg_therma_v_power_limit_enable"
RECHECK = "button.lg_therma_v_recheck_optional_registers"
LIMIT_READS = {("holding", 23), ("holding", 24), ("coil", 6)}
SILENT = ModbusTimeoutError("no answer")


def _silence(unit: MockModbusUnit, *which: str) -> None:
    """Make the named limits time out, as a unit lacking them does."""
    places = {
        "compressor_modulation_limit": ("holding", 23),
        "power_limit": ("holding", 24),
        "power_limit_enable": ("coil", 6),
    }
    for name in which:
        space, address = places[name]
        unit.fail_read(address, SILENT, register_type=space)


def _limit_reads(unit: MockModbusUnit) -> list[tuple[str, int]]:
    return [
        (e.register_type, e.address)
        for e in unit.read_events
        if (e.register_type, e.address) in LIMIT_READS
    ]


async def _poll_settings(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.settings.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _enable(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entry: MockConfigEntry,
    *entity_ids: str,
) -> None:
    for entity_id in entity_ids:
        entity_registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


# --- the rules, without Home Assistant ----------------------------------------


IDENTITY = {"product_group": 128, "product_info": 8208}
ALL_FALSE = dict.fromkeys(
    ("compressor_modulation_limit", "power_limit", "power_limit_enable"), False
)


def _record(
    identity: dict[str, int] = IDENTITY,
    support: dict[str, bool] | None = None,
    fp: str | None = None,
) -> dict[str, object]:
    return {
        "fingerprint": fp or fingerprint(),
        "identity": identity,
        "support": ALL_FALSE if support is None else support,
    }


@pytest.mark.parametrize(
    ("record", "identity", "expected", "reason"),
    [
        (None, IDENTITY, {}, "miss"),
        ({}, IDENTITY, {}, "miss"),
        (dict(RECHECK_RECORD), IDENTITY, {}, "recheck_requested"),
        (_record(fp="other"), IDENTITY, {}, "definitions_changed"),
        ({"fingerprint": fingerprint(), "identity": 1}, IDENTITY, {}, "invalid"),
        (_record(), {"product_group": 131}, {}, "identity_changed"),
        (_record(), IDENTITY, ALL_FALSE, "hit"),
        (
            _record(support={"power_limit": True, "power_limit_enable": False}),
            {},
            {"power_limit": True},
            "positive_only",
        ),
        (
            _record(
                support={"power_limit": True, "flow": True, "power_limit_enable": 1}
            ),
            IDENTITY,
            {"power_limit": True},
            "hit",
        ),
    ],
)
def test_restore_rules(
    record: object, identity: dict[str, int], expected: dict[str, bool], reason: str
) -> None:
    """Stored answers are believed only for the same questions and the same unit."""
    assert restore(record, fingerprint(), identity) == (expected, reason)


def test_pending_keeps_the_probing_order() -> None:
    """Unknown limits are asked in a fixed order."""
    assert pending({"power_limit": False}) == (
        "compressor_modulation_limit",
        "power_limit_enable",
    )


# --- probing on a live entry ----------------------------------------------------


@pytest.mark.usefixtures("patched_connection")
async def test_a_unit_serving_none_is_probed_once_per_settings_poll(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    hass_storage: dict,
) -> None:
    """Three silent registers cost three polls with one timeout each, then never."""
    _silence(
        mock_unit, "compressor_modulation_limit", "power_limit", "power_limit_enable"
    )
    await _setup(hass, mock_config_entry)

    assert _limit_reads(mock_unit) == []  # nothing during setup
    state = hass.states.get(SENSOR)
    assert state.state == "0"
    assert state.attributes["pending"] == [
        "compressor_modulation_limit",
        "power_limit",
        "power_limit_enable",
    ]

    for expected in ([("holding", 23)], [("holding", 24)], [("coil", 6)]):
        mock_unit.read_events.clear()
        await _poll_settings(hass, mock_config_entry)
        assert _limit_reads(mock_unit) == expected

    mock_unit.read_events.clear()
    await _poll_settings(hass, mock_config_entry)
    assert _limit_reads(mock_unit) == []

    state = hass.states.get(SENSOR)
    assert state.state == "0"
    assert state.attributes["source"] == "probed"
    assert state.attributes["pending"] == []
    assert state.attributes["power_limit"] is False
    stored = hass_storage[f"{DOMAIN}.{mock_config_entry.entry_id}.optional_registers"]
    assert stored["data"]["support"] == ALL_FALSE
    events = mock_config_entry.runtime_data.journal.events
    assert [e["source"] for e in events if e["event"] == "optional_registers"] == [
        "miss"
    ]
    assert [(e["register"], e["served"]) for e in events if e["event"] == "probe"] == [
        ("compressor_modulation_limit", False),
        ("power_limit", False),
        ("power_limit_enable", False),
    ]
    for entity_id in (MODULATION, POWER, ENABLE):
        assert entity_registry.async_get(entity_id) is None
    assert mock_config_entry.state is ConfigEntryState.LOADED


@pytest.mark.usefixtures("patched_connection")
async def test_a_found_register_gets_a_disabled_entity(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A served register reloads the entry; its entity is off until turned on."""
    mock_unit.holding[23] = 80
    _silence(mock_unit, "power_limit", "power_limit_enable")
    await _setup(hass, mock_config_entry)

    await _poll_settings(hass, mock_config_entry)  # finds holding 23, reloads

    entry = entity_registry.async_get(MODULATION)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entity_registry.async_get(POWER) is None
    assert hass.states.get(SENSOR).attributes["source"] == "hit"

    await _enable(hass, entity_registry, mock_config_entry, MODULATION)
    assert hass.states.get(MODULATION).state == "80"
    mock_unit.read_events.clear()
    await _poll_settings(hass, mock_config_entry)
    # the served one is polled with the settings; the silent ones are not asked
    # again until they are pending, and they are not (power_limit is next)
    assert ("holding", 23) in _limit_reads(mock_unit)


@pytest.mark.usefixtures("patched_connection")
async def test_the_modulation_limit_takes_off_or_40_to_100(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    hass_storage: dict,
) -> None:
    """0 and 40-100 % reach the register; 1-39 % is refused before the bus."""
    mock_unit.holding[23] = 80
    await _setup_with_stored(
        hass,
        mock_unit,
        mock_config_entry,
        hass_storage,
        compressor_modulation_limit=True,
    )
    await _enable(hass, entity_registry, mock_config_entry, MODULATION)

    for value in (0, 40):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: MODULATION, ATTR_VALUE: value},
            blocking=True,
        )
        assert mock_unit.holding[23] == value

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: MODULATION, ATTR_VALUE: 39},
            blocking=True,
        )
    assert mock_unit.holding[23] == 40


@pytest.mark.usefixtures("patched_connection")
async def test_the_power_limit_is_written_in_tenths_and_the_switch_on_coil_6(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    hass_storage: dict,
) -> None:
    """6.7 kW is 67 in the register, not 6."""
    mock_unit.holding[24] = 125
    mock_unit.coils[6] = True
    await _setup_with_stored(
        hass,
        mock_unit,
        mock_config_entry,
        hass_storage,
        power_limit=True,
        power_limit_enable=True,
    )
    await _enable(hass, entity_registry, mock_config_entry, POWER, ENABLE)
    assert hass.states.get(POWER).state == "12.5"
    assert hass.states.get(ENABLE).state == "on"

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: POWER, ATTR_VALUE: 6.7},
        blocking=True,
    )
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENABLE}, blocking=True
    )

    assert mock_unit.holding[24] == 67
    assert mock_unit.coils[6] is False


async def _setup_with_stored(
    hass: HomeAssistant,
    unit: MockModbusUnit,
    entry: MockConfigEntry,
    hass_storage: dict,
    **served: bool,
) -> None:
    """Set up with answers stored for this unit's identity."""
    support = {**ALL_FALSE, **served}
    hass_storage[f"{DOMAIN}.{entry.entry_id}.optional_registers"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.optional_registers",
        "data": _record(identity=_identity(unit), support=support),
    }
    await _setup(hass, entry)


def _identity(unit: MockModbusUnit) -> dict[str, int]:
    return {
        "product_group": int(unit.input.get(9997, 0)),
        "product_info": int(unit.input.get(9998, 0)),
    }


@pytest.mark.usefixtures("patched_connection")
async def test_stored_answers_for_the_same_unit_need_no_probe(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A restart believes the stored answers and asks nothing."""
    await _setup_with_stored(hass, mock_unit, mock_config_entry, hass_storage)
    mock_unit.read_events.clear()

    await _poll_settings(hass, mock_config_entry)

    assert _limit_reads(mock_unit) == []
    assert hass.states.get(SENSOR).attributes["source"] == "hit"


@pytest.mark.usefixtures("patched_connection")
async def test_another_unit_is_asked_again(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Answers stored for a different identity are discarded."""
    hass_storage[f"{DOMAIN}.{mock_config_entry.entry_id}.optional_registers"] = {
        "version": 1,
        "key": "x",
        "data": _record(identity={"product_group": 1, "product_info": 2}),
    }
    _silence(mock_unit, "compressor_modulation_limit")
    await _setup(hass, mock_config_entry)

    assert hass.states.get(SENSOR).attributes["source"] == "identity_changed"
    mock_unit.read_events.clear()
    await _poll_settings(hass, mock_config_entry)
    assert _limit_reads(mock_unit) == [("holding", 23)]


@pytest.mark.usefixtures("patched_connection")
async def test_a_unit_without_identity_keeps_only_positives(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Without 9997/9998 a stored "not served" is asked again, a "served" kept."""
    mock_unit.fail_read(9997, IllegalDataAddressError(), register_type="input")
    hass_storage[f"{DOMAIN}.{mock_config_entry.entry_id}.optional_registers"] = {
        "version": 1,
        "key": "x",
        "data": _record(identity={}, support={**ALL_FALSE, "power_limit": True}),
    }
    await _setup(hass, mock_config_entry)

    attributes = hass.states.get(SENSOR).attributes
    assert attributes["source"] == "positive_only"
    assert attributes["power_limit"] is True
    assert attributes["pending"] == [
        "compressor_modulation_limit",
        "power_limit_enable",
    ]
    assert mock_config_entry.runtime_data.device.served_limits == ("power_limit",)


@pytest.mark.usefixtures("patched_connection")
async def test_a_link_failure_is_no_answer(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A probe that hits a dead link is repeated, not remembered as absent."""
    mock_unit.fail_read(23, ModbusConnectionError("gone"))
    await _setup(hass, mock_config_entry)

    await _poll_settings(hass, mock_config_entry)
    assert hass.states.get(SENSOR).attributes["compressor_modulation_limit"] is None
    probes = [
        e
        for e in mock_config_entry.runtime_data.journal.events
        if e["event"] == "probe"
    ]
    assert [(e["register"], e["served"]) for e in probes] == [
        ("compressor_modulation_limit", None)
    ]

    mock_unit.fail_read(23, None)
    _silence(mock_unit, "compressor_modulation_limit")
    await _poll_settings(hass, mock_config_entry)
    assert hass.states.get(SENSOR).attributes["compressor_modulation_limit"] is False


@pytest.mark.usefixtures("patched_connection")
async def test_no_probe_after_an_incomplete_settings_poll(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Without a full poll as witness, a silence proves nothing."""
    _silence(mock_unit, "compressor_modulation_limit")
    await _setup(hass, mock_config_entry)
    mock_unit.fail_read(0, IllegalDataAddressError())  # controls block

    mock_unit.read_events.clear()
    await _poll_settings(hass, mock_config_entry)

    assert _limit_reads(mock_unit) == []
    assert hass.states.get(SENSOR).attributes["compressor_modulation_limit"] is None


@pytest.mark.usefixtures("patched_connection")
async def test_diagnostics_show_the_verdicts(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The download says what was decided and why."""
    await _setup_with_stored(
        hass, mock_unit, mock_config_entry, hass_storage, power_limit=True
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["optional_registers"] == {
        "support": {**ALL_FALSE, "power_limit": True},
        "source": "hit",
        "served": ["power_limit"],
    }


# --- asking again on request --------------------------------------------------


def _stored(hass_storage: dict, entry: MockConfigEntry) -> dict:
    return hass_storage[f"{DOMAIN}.{entry.entry_id}.optional_registers"]["data"]


@pytest.mark.usefixtures("patched_connection")
async def test_the_recheck_button_asks_again_after_a_board_swap(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    hass_storage: dict,
) -> None:
    """Same identity, new board: only a requested recheck finds the new register."""
    await _setup_with_stored(hass, mock_unit, mock_config_entry, hass_storage)
    button = entity_registry.async_get(RECHECK)
    assert button is not None
    assert button.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    await _enable(hass, entity_registry, mock_config_entry, RECHECK)

    # the new board serves holding 23, the identity stays the same
    mock_unit.holding[23] = 80
    _silence(mock_unit, "power_limit", "power_limit_enable")
    await _poll_settings(hass, mock_config_entry)
    assert entity_registry.async_get(MODULATION) is None  # the stored answer stands

    mock_unit.read_events.clear()
    before = mock_config_entry.runtime_data.journal
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: RECHECK}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _stored(hass_storage, mock_config_entry) == RECHECK_RECORD
    # noted in the run that was asked; the reload starts a new journal
    assert [e["event"] for e in before.events].count("recheck_requested") == 1
    restored = [
        e
        for e in mock_config_entry.runtime_data.journal.events
        if e["event"] == "optional_registers"
    ]
    assert [e["source"] for e in restored] == ["recheck_requested"]
    state = hass.states.get(SENSOR)
    assert state.attributes["source"] == "recheck_requested"
    assert len(state.attributes["pending"]) == 3
    assert _limit_reads(mock_unit) == []  # nothing during the reload's setup

    await _poll_settings(hass, mock_config_entry)  # finds holding 23, reloads

    assert entity_registry.async_get(MODULATION) is not None
    assert _stored(hass_storage, mock_config_entry)["support"] == {
        "compressor_modulation_limit": True
    }


@pytest.mark.usefixtures("patched_connection")
async def test_a_requested_recheck_survives_a_restart(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The request is stored before the reload, so a restart in between keeps it."""
    await _setup_with_stored(hass, mock_unit, mock_config_entry, hass_storage)
    await async_request_recheck(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(SENSOR).attributes["source"] == "recheck_requested"


@pytest.mark.usefixtures("patched_connection")
async def test_a_probe_running_into_a_recheck_does_not_store_its_answer(
    hass: HomeAssistant,
    mock_unit: MockModbusUnit,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A request made while a probe waits for its timeout wins over that probe."""
    await _setup(hass, mock_config_entry)
    device = mock_config_entry.runtime_data.device

    async def probe_meets_a_recheck(name: str) -> bool:
        await async_request_recheck(hass, mock_config_entry)
        return False

    device.async_probe_limit = probe_meets_a_recheck
    await _poll_settings(hass, mock_config_entry)

    assert _stored(hass_storage, mock_config_entry) == RECHECK_RECORD
    assert mock_config_entry.runtime_data.optional_registers.support == {}
