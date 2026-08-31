"""MXW01 cat printer over the Home Assistant Bluetooth stack (incl. ESPHome proxies)."""
from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .protocol import Mxw01ProtocolError, image_to_buffer, print_buffer
from .render import load_image, render_text

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mxw01"
CONF_ADDRESS = "address"
CONF_INTENSITY = "intensity"
DEFAULT_INTENSITY = 0x5D

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_ADDRESS): cv.string,
                vol.Optional(CONF_INTENSITY, default=DEFAULT_INTENSITY): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=255)
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PRINT_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("font_size", default=56): vol.All(vol.Coerce(int), vol.Range(min=8, max=200)),
        vol.Optional("align", default="center"): vol.In(["left", "center", "right"]),
        vol.Optional("feed_lines", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=400)),
        vol.Optional(CONF_INTENSITY): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    }
)

PRINT_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("dither", default=True): cv.boolean,
        vol.Optional("feed_lines", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=400)),
        vol.Optional(CONF_INTENSITY): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config[DOMAIN]
    address: str = conf[CONF_ADDRESS].upper()
    default_intensity: int = conf[CONF_INTENSITY]
    print_lock = asyncio.Lock()

    async def _print(img, intensity: int | None, feed_lines: int) -> None:
        if feed_lines:
            from PIL import Image

            padded = Image.new("1", (img.width, img.height + feed_lines), 1)
            padded.paste(img.convert("1"), (0, 0))
            img = padded
        buffer = await hass.async_add_executor_job(image_to_buffer, img)

        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            raise HomeAssistantError(
                f"MXW01 {address} not seen by any Bluetooth adapter or proxy — is it powered on?"
            )
        # Plain bleak.BleakClient picks a platform backend (BlueZ) at construction and
        # can't reach proxy-sourced devices; HaBleakClientWrapper defers to the HA
        # bluetooth manager at connect() time, which routes via ESPHome proxies.
        from habluetooth.wrappers import HaBleakClientWrapper

        async with print_lock:
            client = await establish_connection(
                HaBleakClientWrapper, ble_device, f"MXW01 {address}"
            )
            try:
                result = await print_buffer(
                    client, buffer, default_intensity if intensity is None else intensity
                )
            except Mxw01ProtocolError as err:
                raise HomeAssistantError(f"MXW01 print failed: {err}") from err
            finally:
                await client.disconnect()
        _LOGGER.info("MXW01 print done: %s", result)

    async def handle_print_text(call: ServiceCall) -> None:
        img = await hass.async_add_executor_job(
            render_text, call.data["text"], call.data["font_size"], 12, call.data["align"]
        )
        await _print(img, call.data.get(CONF_INTENSITY), call.data["feed_lines"])

    async def handle_print_image(call: ServiceCall) -> None:
        path: str = call.data["path"]
        if not await hass.async_add_executor_job(hass.config.is_allowed_path, path):
            raise HomeAssistantError(
                f"Path not allowed: {path} (add it to allowlist_external_dirs or use /config/www)"
            )
        img = await hass.async_add_executor_job(load_image, path)
        if not call.data["dither"]:
            img = img.point(lambda p: 255 if p > 127 else 0).convert("1")
        await _print(img, call.data.get(CONF_INTENSITY), call.data["feed_lines"])

    hass.services.async_register(DOMAIN, "print_text", handle_print_text, schema=PRINT_TEXT_SCHEMA)
    hass.services.async_register(DOMAIN, "print_image", handle_print_image, schema=PRINT_IMAGE_SCHEMA)
    return True
