"""MXW01 cat-printer BLE protocol.

Ported from jeremy46231/MXW01-catprinter (MIT). Transport-agnostic pieces:
command framing, CRC8, and 1-bpp row packing from a PIL image.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

MAIN_SERVICE_UUID = "0000ae30-0000-1000-8000-00805f9b34fb"
MAIN_SERVICE_UUID_ALT = "0000af30-0000-1000-8000-00805f9b34fb"
CONTROL_WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
DATA_WRITE_UUID = "0000ae03-0000-1000-8000-00805f9b34fb"

PRINTER_WIDTH_PIXELS = 384
PRINTER_WIDTH_BYTES = PRINTER_WIDTH_PIXELS // 8  # 48
# Printer misbehaves on very short jobs; pad to at least 90 lines.
MIN_DATA_BYTES = 90 * PRINTER_WIDTH_BYTES

CMD_GET_STATUS = 0xA1
CMD_PRINT_INTENSITY = 0xA2
CMD_PRINT = 0xA9
CMD_PRINT_COMPLETE = 0xAA
CMD_PRINT_DATA_FLUSH = 0xAD

MODE_MONOCHROME = 0x00

PACING_DELAY_S = 0.015
NOTIFICATION_TIMEOUT_S = 10.0
PRINT_COMPLETE_BASE_TIMEOUT_S = 15.0
PRINT_COMPLETE_LINES_PER_SEC = 15.0

# fmt: off
_CRC8_TABLE = [
    0x00, 0x07, 0x0E, 0x09, 0x1C, 0x1B, 0x12, 0x15, 0x38, 0x3F, 0x36, 0x31, 0x24, 0x23, 0x2A, 0x2D,
    0x70, 0x77, 0x7E, 0x79, 0x6C, 0x6B, 0x62, 0x65, 0x48, 0x4F, 0x46, 0x41, 0x54, 0x53, 0x5A, 0x5D,
    0xE0, 0xE7, 0xEE, 0xE9, 0xFC, 0xFB, 0xF2, 0xF5, 0xD8, 0xDF, 0xD6, 0xD1, 0xC4, 0xC3, 0xCA, 0xCD,
    0x90, 0x97, 0x9E, 0x99, 0x8C, 0x8B, 0x82, 0x85, 0xA8, 0xAF, 0xA6, 0xA1, 0xB4, 0xB3, 0xBA, 0xBD,
    0xC7, 0xC0, 0xC9, 0xCE, 0xDB, 0xDC, 0xD5, 0xD2, 0xFF, 0xF8, 0xF1, 0xF6, 0xE3, 0xE4, 0xED, 0xEA,
    0xB7, 0xB0, 0xB9, 0xBE, 0xAB, 0xAC, 0xA5, 0xA2, 0x8F, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9D, 0x9A,
    0x27, 0x20, 0x29, 0x2E, 0x3B, 0x3C, 0x35, 0x32, 0x1F, 0x18, 0x11, 0x16, 0x03, 0x04, 0x0D, 0x0A,
    0x57, 0x50, 0x59, 0x5E, 0x4B, 0x4C, 0x45, 0x42, 0x6F, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7D, 0x7A,
    0x89, 0x8E, 0x87, 0x80, 0x95, 0x92, 0x9B, 0x9C, 0xB1, 0xB6, 0xBF, 0xB8, 0xAD, 0xAA, 0xA3, 0xA4,
    0xF9, 0xFE, 0xF7, 0xF0, 0xE5, 0xE2, 0xEB, 0xEC, 0xC1, 0xC6, 0xCF, 0xC8, 0xDD, 0xDA, 0xD3, 0xD4,
    0x69, 0x6E, 0x67, 0x60, 0x75, 0x72, 0x7B, 0x7C, 0x51, 0x56, 0x5F, 0x58, 0x4D, 0x4A, 0x43, 0x44,
    0x19, 0x1E, 0x17, 0x10, 0x05, 0x02, 0x0B, 0x0C, 0x21, 0x26, 0x2F, 0x28, 0x3D, 0x3A, 0x33, 0x34,
    0x4E, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5C, 0x5B, 0x76, 0x71, 0x78, 0x7F, 0x6A, 0x6D, 0x64, 0x63,
    0x3E, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2C, 0x2B, 0x06, 0x01, 0x08, 0x0F, 0x1A, 0x1D, 0x14, 0x13,
    0xAE, 0xA9, 0xA0, 0xA7, 0xB2, 0xB5, 0xBC, 0xBB, 0x96, 0x91, 0x98, 0x9F, 0x8A, 0x8D, 0x84, 0x83,
    0xDE, 0xD9, 0xD0, 0xD7, 0xC2, 0xC5, 0xCC, 0xCB, 0xE6, 0xE1, 0xE8, 0xEF, 0xFA, 0xFD, 0xF4, 0xF3,
]
# fmt: on

# byte -> bit-reversed byte (printer wants LSB = leftmost pixel)
_BIT_REVERSE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


def _command(command_id: int, payload: bytes) -> bytearray:
    cmd = bytearray(
        [0x22, 0x21, command_id & 0xFF, 0x00, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]
    )
    cmd.extend(payload)
    cmd.append(_crc8(payload))
    cmd.append(0xFF)
    return cmd


def image_to_buffer(img) -> bytearray:
    """Pack a PIL image (any mode, width 384) into the printer's 1-bpp format.

    PIL mode "1" packs MSB-first with bit 1 = white; the printer wants
    LSB-first with bit 1 = black, hence invert + bit-reverse per byte.
    """
    if img.width != PRINTER_WIDTH_PIXELS:
        raise ValueError(f"image width must be {PRINTER_WIDTH_PIXELS}, got {img.width}")
    if img.mode != "1":
        img = img.convert("L").convert("1")  # Floyd-Steinberg dithering
    raw = img.tobytes()
    buf = bytearray(len(raw))
    for i, b in enumerate(raw):
        buf[i] = _BIT_REVERSE[b ^ 0xFF]
    if len(buf) < MIN_DATA_BYTES:
        buf.extend(bytearray(MIN_DATA_BYTES - len(buf)))
    return buf


class Mxw01ProtocolError(Exception):
    """Printer rejected the job or a required response never arrived."""


def parse_status(payload: bytes) -> dict:
    """Parse an A1 status payload. Handles the documented 13+-byte form and the
    short ~10-byte form some firmware sends (battery still sits at offset 9)."""
    info: dict = {}
    if len(payload) >= 13:
        if payload[12] != 0:
            info["error"] = payload[13] if len(payload) >= 14 else 0
        else:
            info["state"] = payload[6]
            info["battery"] = payload[9]
    elif len(payload) >= 10 and payload[9] <= 100:
        info["battery"] = payload[9]
    return info


def _find_chars(client: BleakClient):
    service = None
    for s in client.services:
        if s.uuid.lower() in (MAIN_SERVICE_UUID, MAIN_SERVICE_UUID_ALT):
            service = s
            break
    if service is None:
        raise Mxw01ProtocolError("MXW01 main service not found on device")
    chars = (
        service.get_characteristic(CONTROL_WRITE_UUID),
        service.get_characteristic(NOTIFY_UUID),
        service.get_characteristic(DATA_WRITE_UUID),
    )
    if not all(chars):
        raise Mxw01ProtocolError("MXW01 characteristics missing")
    return chars


async def get_status(client: BleakClient) -> dict:
    """Query A1 status on an already-connected client."""
    control_char, notify_char, _ = _find_chars(client)
    received: dict[int, bytes] = {}
    condition = asyncio.Condition()
    loop = asyncio.get_running_loop()

    def on_notify(_sender, data: bytearray) -> None:
        if len(data) < 6 or data[0] != 0x22 or data[1] != 0x21:
            return
        payload_len = int.from_bytes(data[4:6], "little")
        if len(data) < 6 + payload_len:
            return
        payload = bytes(data[6 : 6 + payload_len])
        cmd_id = data[2]

        async def _store() -> None:
            async with condition:
                received[cmd_id] = payload
                condition.notify_all()

        loop.create_task(_store())

    await client.start_notify(notify_char, on_notify)
    try:
        await client.write_gatt_char(control_char, _command(CMD_GET_STATUS, bytes([0x00])), response=False)
        async with condition:
            try:
                await asyncio.wait_for(
                    condition.wait_for(lambda: CMD_GET_STATUS in received),
                    timeout=NOTIFICATION_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                raise Mxw01ProtocolError("printer did not answer status request (A1)")
            return parse_status(received[CMD_GET_STATUS])
    finally:
        try:
            await client.stop_notify(notify_char)
        except Exception:  # noqa: BLE001 - disconnecting anyway
            pass


async def print_buffer(client: BleakClient, buffer: bytes, intensity: int) -> dict:
    """Drive one print job over an already-connected BleakClient.

    Returns a dict with status info (battery level when the printer reports it).
    """
    control_char, notify_char, data_char = _find_chars(client)

    received: dict[int, bytes] = {}
    condition = asyncio.Condition()
    loop = asyncio.get_running_loop()

    def on_notify(_sender, data: bytearray) -> None:
        if len(data) < 6 or data[0] != 0x22 or data[1] != 0x21:
            return
        cmd_id = data[2]
        payload_len = int.from_bytes(data[4:6], "little")
        if len(data) < 6 + payload_len:
            _LOGGER.warning("MXW01 notification 0x%02X shorter than declared payload", cmd_id)
            return
        payload = bytes(data[6 : 6 + payload_len])

        async def _store() -> None:
            async with condition:
                received[cmd_id] = payload
                condition.notify_all()

        loop.create_task(_store())

    async def wait_for(cmd_id: int, timeout: float) -> Optional[bytes]:
        async with condition:
            try:
                await asyncio.wait_for(
                    condition.wait_for(lambda: cmd_id in received), timeout=timeout
                )
            except asyncio.TimeoutError:
                return None
            return received.pop(cmd_id)

    result: dict = {}
    await client.start_notify(notify_char, on_notify)
    try:
        await client.write_gatt_char(control_char, _command(CMD_PRINT_INTENSITY, bytes([max(0, min(255, intensity))])), response=False)
        await asyncio.sleep(0.1)

        await client.write_gatt_char(control_char, _command(CMD_GET_STATUS, bytes([0x00])), response=False)
        status = await wait_for(CMD_GET_STATUS, NOTIFICATION_TIMEOUT_S)
        if status is None:
            raise Mxw01ProtocolError("printer did not answer status request (A1)")
        info = parse_status(status)
        if "error" in info:
            raise Mxw01ProtocolError(
                f"printer reports error 0x{info['error']:02X} (no paper / lid open / overheat?)"
            )
        result.update(info)

        line_count = len(buffer) // PRINTER_WIDTH_BYTES
        req = bytearray(line_count.to_bytes(2, "little")) + bytes([0x30, MODE_MONOCHROME])
        await client.write_gatt_char(control_char, _command(CMD_PRINT, bytes(req)), response=False)
        ack = await wait_for(CMD_PRINT, NOTIFICATION_TIMEOUT_S)
        if ack is None or len(ack) < 1 or ack[0] != 0:
            raise Mxw01ProtocolError(f"printer rejected print request (A9): {ack.hex() if ack else 'timeout'}")

        # AE03 is write-without-response only, so writes larger than the link's
        # MTU payload (mtu-3) are silently truncated/dropped — fatal on ESPHome
        # proxy links that negotiate a small MTU. Chunk to what the link allows;
        # the data channel is a byte stream, so chunks need not be row-aligned.
        payload = max(20, (getattr(client, "mtu_size", 23) or 23) - 3)
        if payload >= 2 * PRINTER_WIDTH_BYTES:
            chunk_size = min(payload // PRINTER_WIDTH_BYTES, 4) * PRINTER_WIDTH_BYTES
        else:
            chunk_size = min(payload, PRINTER_WIDTH_BYTES)
        _LOGGER.info("MXW01: mtu=%s → %d-byte chunks", getattr(client, "mtu_size", None), chunk_size)
        for i in range(0, len(buffer), chunk_size):
            await client.write_gatt_char(data_char, buffer[i : i + chunk_size], response=False)
            await asyncio.sleep(PACING_DELAY_S)

        await client.write_gatt_char(control_char, _command(CMD_PRINT_DATA_FLUSH, bytes([0x00])), response=False)

        timeout = PRINT_COMPLETE_BASE_TIMEOUT_S + line_count / PRINT_COMPLETE_LINES_PER_SEC
        done = await wait_for(CMD_PRINT_COMPLETE, timeout)
        if done is None:
            _LOGGER.warning("MXW01: no print-complete (AA) notification within %.0fs", timeout)
        result["lines"] = line_count
        return result
    finally:
        try:
            await client.stop_notify(notify_char)
        except Exception:  # noqa: BLE001 - disconnecting anyway
            pass
