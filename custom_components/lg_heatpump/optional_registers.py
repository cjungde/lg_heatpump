"""Which of the optional limits a unit serves, found out once and remembered.

Holding 23 (compressor modulation limit), holding 24 (power limit) and coil 6
(power limit enable) are served by some units and not others. An address a unit
lacks is not refused but left unanswered, so asking costs a full request timeout
on a connection that may be shared with other devices. The library therefore
reads none of them until told which ones the unit serves
(``LgHeatPump.set_served_limits``); this module decides that and keeps the answer.

The rules:

* **One probe per settings poll**, and only after that poll answered in full.
  The answered poll is the witness that a silence means "not served" rather than
  "gateway down". One probe per poll keeps a unit serving none of the three from
  stalling the link for three timeouts in a row.
* **No probe during setup**, so a silent register does not hold up the start.
* **A failed link is no answer**: the probe is repeated on a later poll.
* **The answer is stored per config entry** and trusted on later starts only if
  the questions are the same and the unit still reports the same identity
  (input 9997/9998). A unit that does not report its identity keeps only its
  positives: a stored "not served" would then rest on hardware that cannot be
  confirmed to be the same, and re-asking costs one timeout once.
* A newly found register reloads the entry, so its entities appear.
* **A recheck can be requested** (button, or the options of a release that has
  them): it stores the request, so it survives a restart, and reloads the entry.
  Meant for a unit whose main board was replaced: the identity may stay the
  same while the served registers change, so a stored "not served" would
  otherwise never be asked again.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LOGGER
from .lg_heatpump_modbus import LIMIT_DATAPOINTS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import LgHeatPumpConfigEntry
    from .lg_heatpump_modbus import LgHeatPump, UpdateReport

STORAGE_VERSION = 1

#: The record that stands for "forget the answers and ask again".
RECHECK_RECORD: dict[str, bool] = {"recheck": True}

#: Sub-systems that must have answered before a silence counts as an answer.
WITNESS_COMPONENTS: frozenset[str] = frozenset({"controls", "switches"})

#: Where each limit lives, for the diagnostic sensor and the fingerprint.
LIMIT_ADDRESSES: dict[str, tuple[str, int]] = {
    "compressor_modulation_limit": ("holding", 23),
    "power_limit": ("holding", 24),
    "power_limit_enable": ("coil", 6),
}


def fingerprint() -> str:
    """Return a hash over the questions asked; a stored answer to others is void."""
    payload = [[name, *LIMIT_ADDRESSES[name]] for name in LIMIT_DATAPOINTS]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def identity_of(device: LgHeatPump) -> dict[str, int]:
    """Return the unit's own account of what it is, as far as it gives one."""
    identity: dict[str, int] = {}
    if (group := device.info.product_group) is not None:
        identity["product_group"] = int(group)
    if (info := device.info.product_info) is not None:
        identity["product_info"] = int(info)
    return identity


def restore(
    record: Any, current_fingerprint: str, identity: dict[str, int]
) -> tuple[dict[str, bool], str]:
    """Return what a stored record lets us believe now, and why.

    ``miss`` (nothing stored), ``recheck_requested``, ``invalid``,
    ``definitions_changed`` and ``identity_changed`` believe nothing;
    ``positive_only`` believes only the served ones; ``hit`` believes all of it.
    """
    if not isinstance(record, dict) or not record or record == RECHECK_RECORD:
        return {}, "recheck_requested" if record == RECHECK_RECORD else "miss"
    if record.get("fingerprint") != current_fingerprint:
        return {}, "definitions_changed"
    stored_identity = record.get("identity")
    support = record.get("support")
    if not isinstance(stored_identity, dict) or not isinstance(support, dict):
        return {}, "invalid"
    clean = {
        name: value
        for name, value in support.items()
        if name in LIMIT_DATAPOINTS and type(value) is bool
    }
    if identity and stored_identity and identity != stored_identity:
        return {}, "identity_changed"
    if not identity or not stored_identity:
        return {name: True for name, value in clean.items() if value}, "positive_only"
    return clean, "hit"


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.optional_registers")


async def async_request_recheck(
    hass: HomeAssistant, entry: LgHeatPumpConfigEntry
) -> None:
    """Forget the stored answers so that the next start asks all of them again.

    The caller reloads the entry (or leaves it to the next start). A running
    probe is stopped first, so it cannot store its answer over the request.
    """
    if entry.state is ConfigEntryState.LOADED:
        entry.runtime_data.optional_registers.armed = False
    await _store(hass, entry.entry_id).async_save(dict(RECHECK_RECORD))
    LOGGER.info("%s: the optional registers will be probed again", entry.title)


def pending(support: dict[str, bool]) -> tuple[str, ...]:
    """Return the limits whose presence is still unknown, in probing order."""
    return tuple(name for name in LIMIT_DATAPOINTS if name not in support)


class OptionalRegisters:
    """Probe the optional limits of one unit and remember the answers."""

    def __init__(self, hass: HomeAssistant, entry: LgHeatPumpConfigEntry) -> None:
        """Prepare the store; nothing is read until :meth:`async_restore`."""
        self.hass = hass
        self.entry = entry
        self.support: dict[str, bool] = {}
        self.source = "miss"
        """How :attr:`support` came about: a ``restore`` reason or ``probed``."""
        self.armed = False
        """Whether probing may start; set once setup has finished."""
        self._store = _store(hass, entry.entry_id)

    async def async_restore(self, device: LgHeatPump) -> None:
        """Load the stored answers and declare the served limits to the device."""
        try:
            record = await self._store.async_load()
        except Exception as error:  # noqa: BLE001 - a lost cache only costs probes
            LOGGER.warning(
                "%s: cannot read the stored optional registers (%s); probing again",
                self.entry.title,
                error,
            )
            record = None
        self.support, self.source = restore(record, fingerprint(), identity_of(device))
        LOGGER.debug(
            "%s: optional registers %s (%s)",
            self.entry.title,
            self.support,
            self.source,
        )
        device.set_served_limits(name for name, ok in self.support.items() if ok)

    async def async_after_settings_poll(
        self, device: LgHeatPump, report: UpdateReport
    ) -> None:
        """Probe one unknown limit, if this poll proved the link alive."""
        if not self.armed or not (todo := pending(self.support)):
            return
        if WITNESS_COMPONENTS - set(report.updated):
            return
        name = todo[0]
        served = await device.async_probe_limit(name)
        if not self.armed:
            return  # a recheck was requested while this probe waited
        if served is None:
            LOGGER.debug("%s: probe of %s hit a link failure", self.entry.title, name)
            return
        space, address = LIMIT_ADDRESSES[name]
        LOGGER.info(
            "%s: the unit %s %s (%s %s)",
            self.entry.title,
            "serves" if served else "does not serve",
            name,
            space,
            address,
        )
        self.support[name] = served
        self.source = "probed"
        await self._async_save(device)
        if served:
            # The entity for it exists only after a reload.
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    async def _async_save(self, device: LgHeatPump) -> None:
        try:
            await self._store.async_save(
                {
                    "fingerprint": fingerprint(),
                    "identity": identity_of(device),
                    "support": dict(self.support),
                }
            )
        except Exception as error:  # noqa: BLE001 - a lost cache only costs probes
            LOGGER.warning(
                "%s: cannot store the optional registers (%s)", self.entry.title, error
            )
