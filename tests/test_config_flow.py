"""Tests for the LG Heat Pump config flow."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

from modbus_connection import ModbusTimeoutError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lg_heatpump.const import (
    CONF_DEBUG,
    CONF_MODEL,
    CONF_ORP,
    CONF_ORP_LEAD,
    CONF_RECHECK_OPTIONAL,
    CONF_SCAN_INTERVAL,
    CONF_UNIT_ID,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from . import MOCK_HOST, MOCK_PORT, MOCK_UNIT_ID, MOCK_USER_INPUT

if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from modbus_connection.mock import MockModbusConnection, MockModbusUnit

    from homeassistant.core import HomeAssistant

FLOW_CONNECTION = "homeassistant.components.modbus.connection.ModbusConnection"


async def test_user_flow(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    mock_setup_entry: AsyncMock,
) -> None:
    """A reachable heat pump creates an entry titled with its model."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(FLOW_CONNECTION, return_value=mock_connection):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], MOCK_USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "LG Therma V"
    assert result["data"] == {**MOCK_USER_INPUT, CONF_DEBUG: False}
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_flow_cannot_connect(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    mock_unit: MockModbusUnit,
    mock_setup_entry: AsyncMock,
) -> None:
    """A heat pump that does not answer surfaces cannot_connect, then recovers."""
    mock_unit.fail_requests(ModbusTimeoutError())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(FLOW_CONNECTION, return_value=mock_connection):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], MOCK_USER_INPUT
        )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}

        mock_unit.fail_requests(None)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], MOCK_USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The same host, port and unit cannot be added twice."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(FLOW_CONNECTION, return_value=mock_connection):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], MOCK_USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_flow(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    init_integration: MockConfigEntry,
) -> None:
    """Reconfiguring updates the connection details in place."""
    result = await init_integration.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(FLOW_CONNECTION, return_value=mock_connection):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.1.60",
                CONF_PORT: MOCK_PORT,
                CONF_UNIT_ID: MOCK_UNIT_ID,
                CONF_MODEL: "therma_v_single_circuit",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.data[CONF_HOST] == "192.168.1.60"
    assert init_integration.data[CONF_MODEL] == "therma_v_single_circuit"


async def test_reconfigure_flow_cannot_connect(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    mock_unit: MockModbusUnit,
    init_integration: MockConfigEntry,
) -> None:
    """Reconfiguring onto an unreachable heat pump keeps the old settings."""
    mock_unit.fail_requests(ModbusTimeoutError())

    result = await init_integration.start_reconfigure_flow(hass)
    with patch(FLOW_CONNECTION, return_value=mock_connection):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.1.60",
                CONF_PORT: MOCK_PORT,
                CONF_UNIT_ID: MOCK_UNIT_ID,
                CONF_MODEL: "therma_v",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert init_integration.data[CONF_HOST] == MOCK_HOST


async def test_options_flow_sets_the_scan_interval(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """The options flow stores the chosen interval and reloads the entry."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 10}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {
        CONF_SCAN_INTERVAL: 10,
        CONF_ORP: False,
        CONF_ORP_LEAD: 180,
    }
    assert init_integration.runtime_data.readings.update_interval == timedelta(
        seconds=10
    )


async def test_options_flow_rejects_an_interval_out_of_range(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """An interval below the minimum is refused by the form."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 1}
        )


STORE = f"{DOMAIN}.{{}}.optional_registers"


@pytest.mark.parametrize(
    "interval", [30, 20], ids=["nothing_else_changed", "interval_changed_too"]
)
async def test_options_flow_requests_a_recheck_without_storing_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict,
    interval: int,
) -> None:
    """The box asks the optional limits again, reloads once and is not kept."""
    mock_config_entry = init_integration
    # store every option the form has, so that "nothing else changed" holds
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 30}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    stored_options = {**mock_config_entry.options, CONF_SCAN_INTERVAL: interval}

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    reload = hass.config_entries.async_reload
    with patch.object(hass.config_entries, "async_reload", wraps=reload) as reloaded:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL: interval, CONF_RECHECK_OPTIONAL: True},
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == stored_options
    assert reloaded.call_count == 1
    assert hass_storage[STORE.format(mock_config_entry.entry_id)]["data"] == {
        "recheck": True
    }
    optional = mock_config_entry.runtime_data.optional_registers
    assert optional.source == "recheck_requested"


async def test_options_flow_leaves_the_answers_alone_without_the_box(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Changing only the interval does not touch the stored answers."""
    key = STORE.format(init_integration.entry_id)
    before = hass_storage.get(key)
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 15}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass_storage.get(key) == before
    assert (
        init_integration.runtime_data.optional_registers.source != "recheck_requested"
    )
