"""Battery sensor for the MXW01 — updated on every print and mxw01.get_status."""
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
    async_add_entities([Mxw01Battery(hass)])


class Mxw01Battery(SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_should_poll = False
    _attr_name = "Kitty printer battery"

    def __init__(self, hass: HomeAssistant) -> None:
        self._attr_unique_id = f"mxw01_{hass.data[DOMAIN]['address']}_battery"

    @property
    def native_value(self):
        return self.hass.data[DOMAIN]["battery"]

    @property
    def extra_state_attributes(self):
        return {"last_print": self.hass.data[DOMAIN]["last_print"]}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
