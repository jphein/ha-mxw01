"""Awake/asleep presence for the MXW01, from BLE advertisements (any adapter or proxy)."""
from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if discovery_info is None:
        return
    async_add_entities([Mxw01Presence(hass)])


class Mxw01Presence(BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False
    _attr_name = "Kitty printer"

    def __init__(self, hass: HomeAssistant) -> None:
        self._address = hass.data[DOMAIN]["address"]
        self._attr_unique_id = f"mxw01_{self._address}_presence"
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        # The MXW01 stops advertising while asleep (and while something is
        # connected to it) — "seen recently" is our awake signal.
        self._attr_is_on = bluetooth.async_address_present(
            self.hass, self._address, connectable=False
        )
        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                self._seen,
                bluetooth.BluetoothCallbackMatcher(address=self._address, connectable=False),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self.async_on_remove(
            bluetooth.async_track_unavailable(
                self.hass, self._gone, self._address, connectable=False
            )
        )

    @callback
    def _seen(self, service_info, change) -> None:
        if not self._attr_is_on:
            self._attr_is_on = True
            self.async_write_ha_state()

    @callback
    def _gone(self, service_info) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
