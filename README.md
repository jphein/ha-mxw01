# ha-mxw01 — MXW01 "cat printer" integration for Home Assistant

Print text and images on the MXW01 Bluetooth thermal printer (the cat/kitty-shaped
mini printer sold on Temu, AliExpress, Amazon, etc.) straight from Home Assistant —
including remotely through **ESPHome Bluetooth proxies**. No cloud, no vendor app.

```yaml
# an automation can now do this:
action: mxw01.print_text
data:
  text: "Leftovers — eat me first!\n{{ now().strftime('%Y-%m-%d') }}"
```

## Which printers?

Printers that advertise the BLE name **MXW01** (BLE service `0000ae30`). This is the
2025-era cat-printer board; it speaks a different protocol from the older
GB01/GB02/GT01 "catprinter" generation. Print width is 384 dots (48 mm) on 57 mm
thermal paper. For sticky labels, use **57×30 mm self-adhesive continuous thermal
rolls** — the printer has no gap sensor, so die-cut/gapped label stock won't register.

## Install

1. Copy `custom_components/mxw01/` into your Home Assistant `config/custom_components/`.
2. Add to `configuration.yaml` (or a package):

   ```yaml
   mxw01:
     address: "AA:BB:CC:DD:EE:FF"   # your printer's BLE MAC
     intensity: 93                  # default darkness, 0-255 (optional)
   ```

3. Restart Home Assistant.

Find the MAC with any BLE scanner app, `bluetoothctl scan on`, or in the HA logs of
an ESPHome Bluetooth proxy — the device advertises as `MXW01`.

## Services

### `mxw01.print_text`

| field | default | notes |
|---|---|---|
| `text` | required | multi-line supported |
| `font_size` | 56 | pixels; printable width is 384 px |
| `align` | `center` | `left` / `center` / `right` |
| `feed_lines` | 0 | extra blank lines after the label (8 ≈ 1 mm) |
| `intensity` | from config | darkness 0–255 |

### `mxw01.print_image`

| field | default | notes |
|---|---|---|
| `path` | required | image on the HA host; must be an [allowed path](https://www.home-assistant.io/integrations/homeassistant/#allowlist_external_dirs), e.g. `/config/www/…` |
| `dither` | `true` | Floyd–Steinberg for photos; `false` = hard threshold (best for text/QR) |
| `feed_lines` | 0 | as above |
| `intensity` | from config | darkness 0–255 |

Images are scaled to the 384 px print width automatically; transparency is
composited onto white.

## Bluetooth proxies

The integration connects through Home Assistant's Bluetooth stack, so it uses
whatever can reach the printer — a local adapter or any
[ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)
with `active: true`. HA picks the proxy with the best signal; nothing to configure.

## Implementation notes

- The BLE protocol port is verified byte-identical (data buffers and command
  frames) against [jeremy46231/MXW01-catprinter](https://github.com/jeremy46231/MXW01-catprinter),
  whose `PROTOCOL.md` documents the wire format. See also
  [PacoChan's protocol write-up](https://pacochan.net/software/cat-printer-ble/).
- Imaging is pure Pillow (no numpy/OpenCV): PIL's mode-`"1"` conversion provides
  Floyd–Steinberg dithering, and a 256-entry invert+bit-reverse table converts
  PIL's MSB-first/white-is-1 packing to the printer's LSB-first/black-is-1 rows.
- Connections use `habluetooth.wrappers.HaBleakClientWrapper` explicitly. Current
  HA does not reliably swap `bleak.BleakClient` for its proxy-aware wrapper by the
  time a YAML component is imported (config validation imports the module before
  the bluetooth integration patches anything), and a plain `BleakClient` crashes
  with `KeyError: 'path'` on proxy-sourced devices.
- Bulk image data is chunked to the link's negotiated MTU (row-aligned, up to
  4 rows per packet). The data characteristic is write-without-response only,
  and a long burst of small packets can congest an ESPHome proxy's BLE queue —
  dropped rows leave the printer waiting forever and nothing prints.
- The printer's status (`0xA1`) response is shorter than documented on some
  firmware, and the print-complete (`0xAA`) notification is occasionally lost over
  a proxy hop after the job already printed. Both are tolerated.

## Credits & license

MIT — see [LICENSE](LICENSE). Protocol ported from
[jeremy46231/MXW01-catprinter](https://github.com/jeremy46231/MXW01-catprinter) (MIT).
Ships [DejaVu Sans Bold](https://dejavu-fonts.github.io/) for text rendering
(free license based on Bitstream Vera).
