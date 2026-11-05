"""Device triggers for the oil-return flush pulse.

Two of them, because they are the two moments an automation can do something
about:

* **before a flush pulse** -- the unit is about to end its heating run. Three
  minutes of warning by default, configurable in the integration's options.
  Long enough to write to the machine, which the 50-second ramp is not.
* **after a flush pulse** -- the run has ended and the clock has restarted.

What to do at those moments, for instance raising the flow temperature, is left
to the user's automation. The integration only says when. They are offered only
when the flush pulse option is on, because the tracker behind them is switched
off by default.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import CONF_FLUSH_PULSE, DOMAIN, EVENT_FLUSH_PULSE
from .lg_heatpump_modbus.logic import EVENT_FINISHED, EVENT_IMMINENT

TRIGGER_TYPES = {EVENT_IMMINENT, EVENT_FINISHED}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """Offer both triggers for a device whose entry has the tracker switched on."""
    registry_entry = dr.async_get(hass).async_get(device_id)
    if registry_entry is None:
        return []
    enabled = any(
        (entry := hass.config_entries.async_get_entry(entry_id)) is not None
        and entry.domain == DOMAIN
        and entry.options.get(CONF_FLUSH_PULSE, False)
        for entry_id in registry_entry.config_entries
    )
    if not enabled:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in sorted(TRIGGER_TYPES)
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for the one event and filter it down to this device and type."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_FLUSH_PULSE,
            event_trigger.CONF_EVENT_DATA: {
                CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                CONF_TYPE: config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
