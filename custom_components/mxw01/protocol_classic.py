"""Classic cat-printer protocol (GB01/GB02/GT01/RT034h family).

**A different protocol that shares the MXW01's service UUIDs**, which is the
trap this module exists to name: both printer generations expose `ae30` with
`ae01`/`ae02`/`ae03`, so a device can enumerate identically and still ignore
every MXW01 command. Told apart by framing, not by GATT:

    MXW01   0x22 0x21 <cmd> 0x00 <len16> <data> <crc8> 0xFF   bulk on ae03
    classic 0x51 0x78 <cmd> 0x00 <len16> <data> <crc8> 0xFF   EVERYTHING on ae01

Probe with `GetDeviceInfo` (0xA8): the classic family answers with an ASCII
firmware string; an MXW01 stays silent. (Verified 2026-09-09 on RT034h-AFD9,
firmware 3.0.45 — silent to A1/AB/B1, immediate reply to A3/A8.)

Protocol reverse-engineered by rbaron/catprinter (MIT); this is a no-numpy
reimplementation of its command set for Home Assistant.
"""
from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

SERVICE_UUIDS = (
    "0000ae30-0000-1000-8000-00805f9b34fb",
    "0000af30-0000-1000-8000-00805f9b34fb",
)
# ⚠️ ae01 carries BOTH control and bitmap data on this family. There is no
# separate bulk characteristic — writing rows to ae03 (the MXW01 habit) sends
# them into a void the printer never reads.
TX_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
RX_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"

WIDTH = 384  # dots; 48 mm of the 57 mm roll, same head width as the MXW01

# The printer says "I have finished and I am ready" with this exact frame.
READY_NOTIFICATION = bytes.fromhex("5178ae0101000000ff")

PACING_S = 0.02
DONE_TIMEOUT_S = 40.0

_CRC8 = []
for _i in range(256):
    _v = _i
    for _ in range(8):
        _v = ((_v << 1) ^ 0x07) & 0xFF if _v & 0x80 else (_v << 1) & 0xFF
    _CRC8.append(_v)


def _crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c


def _cmd(command: int, data: bytes) -> bytes:
    return (
        bytes([0x51, 0x78, command & 0xFF, 0x00, len(data) & 0xFF, (len(data) >> 8) & 0xFF])
        + data
        + bytes([_crc8(data), 0xFF])
    )


CMD_GET_DEV_STATE = _cmd(0xA3, b"\x00")
CMD_GET_DEV_INFO = _cmd(0xA8, b"\x00")
CMD_SET_QUALITY_200DPI = _cmd(0xA4, bytes([0x32]))
CMD_APPLY_ENERGY = _cmd(0xBE, bytes([0x01]))
CMD_DRAWING_MODE_IMAGE = _cmd(0xBE, bytes([0x00]))
CMD_SET_PAPER = _cmd(0xA1, bytes([0x30, 0x00]))
CMD_LATTICE_START = _cmd(0xA6, bytes([0xAA, 0x55, 0x17, 0x38, 0x44, 0x5F, 0x5F, 0x5F, 0x44, 0x38, 0x2C]))
CMD_LATTICE_END = _cmd(0xA6, bytes([0xAA, 0x55, 0x17, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x17]))


def _cmd_set_energy(energy: int) -> bytes:
    energy = max(0, min(0xFFFF, energy))
    return _cmd(0xAF, bytes([(energy >> 8) & 0xFF, energy & 0xFF]))


def _cmd_feed_paper(lines: int) -> bytes:
    return _cmd(0xBD, bytes([lines & 0xFF]))


def _rle(row: list[int]) -> list[int] | None:
    """Run-length encode a row; None when it would be larger than raw bytes."""
    out: list[int] = []
    count = 0
    last = -1
    for val in row:
        if val == last:
            count += 1
        else:
            while count > 0x7F:
                out.append(0x7F | (last << 7))
                count -= 0x7F
            if count > 0:
                out.append((last << 7) | count)
            count = 1
        last = val
    while count > 0x7F:
        out.append(0x7F | (last << 7))
        count -= 0x7F
    if count > 0:
        out.append((last << 7) | count)
    return None if len(out) > WIDTH // 8 else out


def _byte_encode(row: list[int]) -> list[int]:
    """8 pixels per byte, **LSB = leftmost** (same bit order as the MXW01)."""
    out = []
    for start in range(0, len(row), 8):
        byte = 0
        for bit in range(8):
            if row[start + bit]:
                byte |= 1 << bit
        out.append(byte)
    return out


def _cmd_row(row: list[int]) -> bytes:
    packed = _rle(row)
    if packed is not None:
        return _cmd(0xBF, bytes(packed))  # compressed
    return _cmd(0xA2, bytes(_byte_encode(row)))  # raw bitmap


def build_job(img, intensity: int) -> bytes:
    """PIL image (width 384) -> the whole command stream, ready to stream out.

    `intensity` is the shared 0-255 knob used across this integration; the
    classic family's energy register is 16-bit, so it is scaled by 257 (255 ->
    0xFFFF) rather than truncated, which would have capped the printer at 0.4%
    of its range and looked like faulty hardware.
    """
    if img.width != WIDTH:
        raise ValueError(f"image width must be {WIDTH}, got {img.width}")
    if img.mode != "1":
        img = img.convert("L").convert("1")  # Floyd-Steinberg
    px = img.load()
    rows = [[1 if px[x, y] == 0 else 0 for x in range(WIDTH)] for y in range(img.height)]

    out = bytearray()
    out += CMD_GET_DEV_STATE
    out += CMD_SET_QUALITY_200DPI
    out += _cmd_set_energy(max(0, min(255, intensity)) * 257)
    out += CMD_APPLY_ENERGY
    out += CMD_DRAWING_MODE_IMAGE
    out += CMD_LATTICE_START
    for row in rows:
        out += _cmd_row(row)
    out += _cmd_feed_paper(25)
    out += CMD_SET_PAPER * 3
    out += CMD_LATTICE_END
    out += CMD_GET_DEV_STATE
    return bytes(out)


async def send_job(client, payload: bytes) -> dict:
    """Stream a built job to the printer and wait for its ready frame."""
    service = None
    for s in client.services:
        if s.uuid.lower() in SERVICE_UUIDS:
            service = s
            break
    if service is None:
        raise ClassicProtocolError("cat-printer service not found on device")
    tx = service.get_characteristic(TX_UUID)
    rx = service.get_characteristic(RX_UUID)
    if not tx or not rx:
        raise ClassicProtocolError("ae01/ae02 characteristics missing")

    done = asyncio.Event()
    seen: list[bytes] = []

    def on_notify(_sender, data: bytearray) -> None:
        b = bytes(data)
        seen.append(b)
        if b == READY_NOTIFICATION:
            done.set()

    # ⚠️ BlueZ reports a fixed MTU of 23 until negotiation is forced, which
    # would chunk the job into 20-byte writes and crawl. Ask for the real one
    # when the backend exposes it (absent on the HA proxy wrapper — fall back).
    acquire = getattr(getattr(client, "_backend", None), "_acquire_mtu", None)
    if acquire is not None and getattr(client, "mtu_size", 23) <= 23:
        try:
            await acquire()
        except Exception:  # noqa: BLE001 - best effort; pacing still works at 20 B
            pass
    chunk = max(20, (getattr(client, "mtu_size", 23) or 23) - 3)

    await client.start_notify(rx, on_notify)
    try:
        _LOGGER.info("classic: streaming %d bytes in %d-byte chunks", len(payload), chunk)
        for i in range(0, len(payload), chunk):
            await client.write_gatt_char(tx, payload[i : i + chunk], response=False)
            await asyncio.sleep(PACING_S)
        try:
            await asyncio.wait_for(done.wait(), DONE_TIMEOUT_S)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "classic: no ready frame within %.0fs (%d notifications seen) — "
                "the job was sent; the printer may still be feeding",
                DONE_TIMEOUT_S, len(seen),
            )
        return {"bytes": len(payload), "notifications": len(seen)}
    finally:
        try:
            await client.stop_notify(rx)
        except Exception:  # noqa: BLE001 - disconnecting anyway
            pass


async def get_status(client) -> dict:
    """Ask the printer who it is. Returns firmware string when it answers."""
    service = None
    for s in client.services:
        if s.uuid.lower() in SERVICE_UUIDS:
            service = s
            break
    if service is None:
        raise ClassicProtocolError("cat-printer service not found on device")
    tx, rx = service.get_characteristic(TX_UUID), service.get_characteristic(RX_UUID)

    replies: dict[int, bytes] = {}
    got = asyncio.Event()

    def on_notify(_sender, data: bytearray) -> None:
        b = bytes(data)
        if len(b) >= 6 and b[0] == 0x51 and b[1] == 0x78:
            replies[b[2]] = bytes(b[6 : 6 + int.from_bytes(b[4:6], "little")])
            got.set()

    await client.start_notify(rx, on_notify)
    try:
        info: dict = {}
        for cmd in (CMD_GET_DEV_INFO, CMD_GET_DEV_STATE):
            got.clear()
            await client.write_gatt_char(tx, cmd, response=False)
            try:
                await asyncio.wait_for(got.wait(), 4.0)
            except asyncio.TimeoutError:
                continue
        if 0xA8 in replies:
            text = "".join(chr(c) for c in replies[0xA8] if 32 <= c < 127)
            info["firmware"] = text.strip()
        if 0xA3 in replies:
            info["state"] = replies[0xA3].hex(" ")
        return info
    finally:
        try:
            await client.stop_notify(rx)
        except Exception:  # noqa: BLE001
            pass


class ClassicProtocolError(Exception):
    """The device did not behave like a classic cat printer."""
