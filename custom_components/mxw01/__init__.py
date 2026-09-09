"""Cat printers over the Home Assistant Bluetooth stack (incl. ESPHome proxies).

Two printer generations share the `ae30` service UUID and nothing else:

  * `mxw01`   — the 2025 board (`0x22 0x21` framing, bulk data on ae03)
  * `classic` — GB01/GB02/GT01/RT034h (`0x51 0x78` framing, everything on ae01)

Both are driven here behind one set of services; every service takes an
optional `printer:` slug and falls back to the default printer, so a
single-printer config written before multi-printer support keeps working
untouched.
"""
from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import protocol_classic as classic
from .protocol import Mxw01ProtocolError, get_status, image_to_buffer, print_buffer
from .render import load_image, render_text

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mxw01"
CONF_ADDRESS = "address"
CONF_INTENSITY = "intensity"
CONF_PRINTERS = "printers"
CONF_PROTOCOL = "protocol"
CONF_NAME = "name"
CONF_PRINTER = "printer"
DEFAULT_INTENSITY = 0x5D
DEFAULT_SLUG = "kitty"
SIGNAL_UPDATE = "mxw01_update"
EVENT_PRINT_COMPLETED = "mxw01_print_completed"

PROTOCOLS = ("mxw01", "classic")

PRINTER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_PROTOCOL, default="mxw01"): vol.In(PROTOCOLS),
        vol.Optional(CONF_INTENSITY, default=DEFAULT_INTENSITY): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                # Legacy single-printer form — still the whole config for one printer.
                vol.Optional(CONF_ADDRESS): cv.string,
                vol.Optional(CONF_INTENSITY, default=DEFAULT_INTENSITY): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=255)
                ),
                vol.Optional(CONF_PRINTERS, default={}): {cv.slug: PRINTER_SCHEMA},
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

_COMMON = {
    vol.Optional(CONF_PRINTER): cv.slug,
    vol.Optional("feed_lines", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=400)),
    vol.Optional(CONF_INTENSITY): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
}

PRINT_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("font_size", default=56): vol.All(vol.Coerce(int), vol.Range(min=8, max=200)),
        vol.Optional("align", default="center"): vol.In(["left", "center", "right"]),
        **_COMMON,
    }
)

PRINT_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("dither", default=True): cv.boolean,
        **_COMMON,
    }
)

GET_STATUS_SCHEMA = vol.Schema({vol.Optional(CONF_PRINTER): cv.slug})

RENDER_PREVIEW_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("font_size", default=56): vol.All(vol.Coerce(int), vol.Range(min=8, max=200)),
        vol.Optional("align", default="center"): vol.In(["left", "center", "right"]),
        vol.Optional(CONF_PRINTER): cv.slug,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config[DOMAIN]
    printers: dict[str, dict] = {}

    if conf.get(CONF_ADDRESS):
        printers[DEFAULT_SLUG] = {
            "slug": DEFAULT_SLUG,
            "name": "Kitty printer",
            "address": conf[CONF_ADDRESS].upper(),
            "protocol": "mxw01",
            "intensity": conf[CONF_INTENSITY],
        }
    for slug, pconf in (conf.get(CONF_PRINTERS) or {}).items():
        printers[slug] = {
            "slug": slug,
            "name": pconf.get(CONF_NAME) or slug.replace("_", " ").title(),
            "address": pconf[CONF_ADDRESS].upper(),
            "protocol": pconf[CONF_PROTOCOL],
            "intensity": pconf[CONF_INTENSITY],
        }
    if not printers:
        _LOGGER.error("mxw01: no printers configured (need `address:` or `printers:`)")
        return False

    default_slug = DEFAULT_SLUG if DEFAULT_SLUG in printers else next(iter(printers))
    for p in printers.values():
        p.update(battery=None, last_print=None, lock=asyncio.Lock())

    hass.data[DOMAIN] = {
        "printers": printers,
        "default": default_slug,
        # Legacy mirrors, so anything reading the old keys keeps working.
        "address": printers[default_slug]["address"],
        "battery": None,
        "last_print": None,
    }

    def _pick(call: ServiceCall) -> dict:
        slug = call.data.get(CONF_PRINTER) or default_slug
        printer = printers.get(slug)
        if printer is None:
            raise HomeAssistantError(
                f"Unknown printer '{slug}'. Configured: {', '.join(sorted(printers))}"
            )
        return printer

    def _absorb(printer: dict, info: dict) -> None:
        if info.get("battery") is not None:
            printer["battery"] = info["battery"]
            if printer["slug"] == default_slug:
                hass.data[DOMAIN]["battery"] = info["battery"]
        async_dispatcher_send(hass, SIGNAL_UPDATE)

    async def _connect(printer: dict):
        ble_device = bluetooth.async_ble_device_from_address(
            hass, printer["address"], connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(
                f"{printer['name']} ({printer['address']}) not seen by any Bluetooth "
                "adapter or proxy — is it powered on?"
            )
        # Plain bleak.BleakClient picks a platform backend (BlueZ) at construction and
        # can't reach proxy-sourced devices; HaBleakClientWrapper defers to the HA
        # bluetooth manager at connect() time, which routes via ESPHome proxies.
        from habluetooth.wrappers import HaBleakClientWrapper

        return await establish_connection(
            HaBleakClientWrapper, ble_device, f"{printer['name']} {printer['address']}"
        )

    async def _print(printer: dict, img, intensity: int | None, feed_lines: int) -> None:
        if feed_lines:
            from PIL import Image

            padded = Image.new("1", (img.width, img.height + feed_lines), 1)
            padded.paste(img.convert("1"), (0, 0))
            img = padded

        level = printer["intensity"] if intensity is None else intensity
        is_classic = printer["protocol"] == "classic"
        payload = await hass.async_add_executor_job(
            (lambda i: classic.build_job(i, level)) if is_classic else image_to_buffer, img
        )

        async with printer["lock"]:
            client = await _connect(printer)
            try:
                if is_classic:
                    result = await classic.send_job(client, payload)
                    result["lines"] = img.height
                else:
                    result = await print_buffer(client, payload, level)
            except (Mxw01ProtocolError, classic.ClassicProtocolError) as err:
                raise HomeAssistantError(f"{printer['name']} print failed: {err}") from err
            finally:
                await client.disconnect()

        _LOGGER.info("%s print done: %s", printer["name"], result)
        printer["last_print"] = {"when": dt_util.now().isoformat(), "lines": result.get("lines")}
        if printer["slug"] == default_slug:
            hass.data[DOMAIN]["last_print"] = printer["last_print"]
        _absorb(printer, result)
        hass.bus.async_fire(
            EVENT_PRINT_COMPLETED,
            {"printer": printer["slug"], "address": printer["address"], **result},
        )

    async def handle_print_text(call: ServiceCall) -> None:
        img = await hass.async_add_executor_job(
            render_text, call.data["text"], call.data["font_size"], 12, call.data["align"]
        )
        await _print(_pick(call), img, call.data.get(CONF_INTENSITY), call.data["feed_lines"])

    async def handle_print_image(call: ServiceCall) -> None:
        path: str = call.data["path"]
        if not await hass.async_add_executor_job(hass.config.is_allowed_path, path):
            raise HomeAssistantError(
                f"Path not allowed: {path} (add it to allowlist_external_dirs or use /config/www)"
            )
        img = await hass.async_add_executor_job(load_image, path)
        if not call.data["dither"]:
            img = img.point(lambda p: 255 if p > 127 else 0).convert("1")
        await _print(_pick(call), img, call.data.get(CONF_INTENSITY), call.data["feed_lines"])

    async def handle_get_status(call: ServiceCall) -> ServiceResponse:
        printer = _pick(call)
        async with printer["lock"]:
            client = await _connect(printer)
            try:
                if printer["protocol"] == "classic":
                    info = await classic.get_status(client)
                else:
                    info = await get_status(client)
            except (Mxw01ProtocolError, classic.ClassicProtocolError) as err:
                raise HomeAssistantError(f"{printer['name']} status failed: {err}") from err
            finally:
                await client.disconnect()
        _absorb(printer, info)
        return {"printer": printer["slug"], **info}

    async def handle_render_preview(call: ServiceCall) -> ServiceResponse:
        """Render what print_text WOULD print, to /config/www, for preview-before-print."""
        printer = _pick(call)
        img = await hass.async_add_executor_job(
            render_text, call.data["text"], call.data["font_size"], 12, call.data["align"]
        )

        def _save() -> tuple[str, int]:
            import os

            out_dir = hass.config.path("www", "mxw01")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"preview-{printer['slug']}.png")
            # 384 px wide is tiny on a phone; scale 2x, keep it crisp (no resampling blur).
            img.convert("L").resize((img.width * 2, img.height * 2), 0).save(path)
            return path, img.height

        path, lines = await hass.async_add_executor_job(_save)
        return {
            "printer": printer["slug"],
            "path": path,
            "url": f"/local/mxw01/preview-{printer['slug']}.png?v={int(dt_util.utcnow().timestamp())}",
            "lines": lines,
            "mm": round(lines / 8, 1),
        }

    hass.services.async_register(
        DOMAIN,
        "render_preview",
        handle_render_preview,
        schema=RENDER_PREVIEW_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "print_text", handle_print_text, schema=PRINT_TEXT_SCHEMA)
    hass.services.async_register(DOMAIN, "print_image", handle_print_image, schema=PRINT_IMAGE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        "get_status",
        handle_get_status,
        schema=GET_STATUS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.async_create_task(discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config))
    hass.async_create_task(discovery.async_load_platform(hass, "binary_sensor", DOMAIN, {}, config))
    _LOGGER.info(
        "mxw01: %d printer(s) configured: %s",
        len(printers),
        ", ".join(f"{s} ({p['protocol']})" for s, p in printers.items()),
    )
    return True
