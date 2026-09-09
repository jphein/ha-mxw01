"""Battery sensor per printer — updated on every print and mxw01.get_status.

Only the MXW01 generation reports a battery level (`0xAB`); the classic
family has no equivalent command, so those printers get no battery entity
rather than a permanently-unknown one.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, SIGNAL_UPDATE


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if discovery_info is None:
        return
    printers = hass.data[DOMAIN]["printers"]
    async_add_entities(
        Mxw01Battery(slug, p) for slug, p in printers.items() if p["protocol"] == "mxw01"
    )


class Mxw01Battery(SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_should_poll = False

    def __init__(self, slug: str, printer: dict) -> None:
        # ⚠️ name and unique_id MUST be set here, not in async_added_to_hass:
        # the entity registry reads them while ADDING the entity, so deferring
        # them makes the platform silently add nothing (cost: one restart).
        self._slug = slug
        self._attr_name = f"{printer['name']} battery"
        self._attr_unique_id = f"mxw01_{printer['address']}_battery"

    @property
    def _printer(self) -> dict:
        return self.hass.data[DOMAIN]["printers"][self._slug]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._refresh))

    @property
    def native_value(self):
        return self._printer["battery"]

    @property
    def extra_state_attributes(self):
        p = self._printer
        return {"last_print": p["last_print"], "printer": self._slug, "protocol": p["protocol"]}

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
