# iphone_audit.py

Reads hardware component identity out of a connected iPhone: which company made
the display, whether that display passes Apple's own authenticity check, whose
silicon the modem and wireless chip actually are, real battery capacity and
cycle count, and the storage controller's details.

No jailbreak, no Developer Mode, nothing installed on the phone.

## Install

Homebrew and system Python builds block `pip install` (PEP 668), so use a
virtual environment:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python iphone_audit.py

Or with uv:

    uv venv .venv
    uv pip install --python .venv/bin/python -r requirements.txt
    .venv/bin/python iphone_audit.py

Works on macOS, Linux and Windows. macOS ships the usbmux daemon; on Linux and
Windows you need `usbmuxd` or Apple Devices/iTunes. The phone must be plugged in
over USB, unlocked, and paired (tap Trust the first time).

## Usage

    .venv/bin/python iphone_audit.py             # everything
    .venv/bin/python iphone_audit.py --list      # attached devices
    .venv/bin/python iphone_audit.py --udid UDID # pick one
    .venv/bin/python iphone_audit.py --section display   # one section, repeatable
    .venv/bin/python iphone_audit.py --json      # machine-readable
    .venv/bin/python iphone_audit.py --raw       # add the Panel_ID breakdown
    .venv/bin/python iphone_audit.py --png report.png            # PNG report card
    .venv/bin/python iphone_audit.py --png report.png --theme dark

Sections: `device`, `display`, `modem`, `wireless`, `battery`, `storage`.

`--png` needs matplotlib (`.venv/bin/pip install matplotlib`). It writes a
report card with a row of headline tiles over the detail tables, in light or
dark. `--json --png` is fine together: the "wrote ..." line goes to stderr so
stdout stays valid JSON.

## Example

    Device
    ------
      Model                  iPhone19,7  (iPhone 18 Pro Max)
      SoC                    t8160 (ChipID 0x8160)
      iOS                    27.0 (24A437)

    Display
    -------
      Panel serial           G9N0000000000000AA
      Supplier               Samsung Display   [prefix G9N]
      Read from              mogul-display certificate (Apple-signed)
      Signed by              Display CA M1
      Parts pairing          PASS
      Pixel density          460.0 ppi

    Modem
    -----
      Part                   Apple C2   [PCI 106b:1c06]
      PCIe node              baseband-pcie-leda
      Radio type             int2
      Firmware               1.01.06

    Battery
    -------
      Cycle count            2
      Design capacity        5335 mAh
      Full charge capacity   5489 mAh
      Measured health        102.9% of design

## If it fails to connect

usbmuxd refuses connections intermittently, usually when runs come back to
back. The tool retries the whole session three times before giving up, and
prints a line to stderr when it does. If all three fail:

    error: could not read the device after 3 attempts: ...

unplug and replug the phone, make sure it is unlocked, and check you tapped
Trust. The retry covers both the lockdown handshake and the per-service
connect, which are two separate points where usbmux can refuse.

## The PNG report

The page width is derived from measured text rather than fixed: every label,
value, tile and header string is measured first, and the canvas is widened until
all of them fit. Nothing is ever shortened or ellipsised to make it fit, and no
two strings can collide, because each card is at least as wide as its widest
label + value pair plus the gap.

`test_layout.py` checks that property without rendering anything:

    .venv/bin/python iphone_audit.py --json > audit.json
    .venv/bin/python test_layout.py audit.json

It reports the tightest gap in the whole page and fails if any is negative.
Running it against the bundled `sample_audit.json` (dummy serials) needs no
phone attached.

## How the display check works

The display module carries an authentication IC on an I2C bus. That IC holds an
X.509 certificate signed by Apple's own "Display CA M1", and the subject common
name is the module serial plus the IC serial:

    CN = G9N0000000000000AA-AABBCCDDEEFF
         ^^^                ^^^^^^^^^^^^
         supplier prefix    auth IC serial

The first three characters identify the supplier. The certificate is signed and
bound to the physical module, which makes it more reliable than reading
`AppleCLCD2`'s `Panel_ID` string. `Panel_ID` is published on both the internal
and the external display pipe, so on its own it does not tell you which pipe you
read. The script reads it anyway as a cross-check and a fallback.

Alongside the certificate the IC exposes an `auth-passed` flag that iOS sets
after challenge-response. That is the mechanism behind the "Unknown Part"
warnings, and reading it directly is a quick way to check a used phone before
you buy it.

The older trick of grepping a sysdiagnose for `raw-panel` stopped working in
iOS 26. The diagnostics relay route still works and was verified on iOS 27.

### Known display prefixes

| Prefix | Supplier        |
|--------|-----------------|
| G9N    | Samsung Display |
| G9P    | Samsung Display |
| G9Q    | Samsung Display |
| GH3    | LG Display      |
| GVC    | LG Display      |

Community-sourced, not from Apple. BOE prefixes are unconfirmed. An
unrecognised prefix is reported as UNKNOWN rather than guessed at; if you hit
one, open an issue with the `--json` output.

## Modem and wireless

This is the interesting one on the iPhone 18 Pro Max, because Apple shipped two
different modems in the same phone: US units got Qualcomm's Snapdragon X80,
every other region got the Apple C2. Nothing in Settings tells you which one you
have.

Every PCIe device is listed with its PCI-SIG vendor ID, read straight off the
bus, so it reflects the part actually fitted:

    Part                   Apple C2   [PCI 106b:1c06]
    PCIe node              baseband-pcie-leda

    centauri-alpha         Apple   [PCI 106b:1902]
    centauri-beta          Apple   [PCI 106b:1903]
    centauri-control       Apple   [PCI 106b:1901]

`0x106B` is Apple. A Qualcomm modem enumerates as `0x17CB` and a Broadcom
wireless part as `0x14E4`. On this generation neither appears anywhere, so the
modem and the Wi-Fi/Bluetooth combo are both first-party silicon.

Two layers go into the "Part" line, and they are not equally solid. The maker
comes from the PCI vendor ID, which is hardware talking. The marketing name
(C1, C1X, C2, Snapdragon X80) comes from a lookup table built from teardowns and
Apple's own statements, because the phone never names its modem part. If a model
is missing from that table the tool prints the maker alone rather than guessing.

## Reference tables vs measured values

Three things in the output are looked up rather than read off the phone, and
each is marked as such:

- the **marketing model name** (iPhone 18 Pro Max), from Xcode's CoreSimulator
  device profiles
- the **SoC name** (Apple A20 Pro). `t8160` is Apple's internal silicon part
  number and `ChipID 0x8160` is the same value; the marketing name is not
  stored anywhere on the device
- the **modem part name** (C2, Snapdragon X80), as described above

Everything else, including the GPU generation (`AGXAcceleratorG19P`), the PCI
vendor IDs and every serial, is read from the device.

`Region` is the sales-region code from the model number suffix, so `X/A` is
Australia / New Zealand on a unit whose retail code ends `X/A`. It is fixed in
hardware and never changes.

## Battery

The battery pack carries a serial in the same 18-character format as the display
module, so the same prefix trick should identify the cell supplier. Nobody has
published that mapping, so `BATTERY_VENDOR_PREFIXES` in the script is empty and
the prefix is printed unresolved. Post yours in an issue and it can be filled
in.

Capacity figures come from the gas gauge, so `Measured health` is
full-charge over design capacity rather than the rounded number Settings shows.
A new pack often reads slightly over 100%.

## Storage

The controller reports its model, serial and firmware. The flash die vendor
(Kioxia, SK Hynix, Samsung) is not exposed, because Apple's own controller
fronts the NAND.

The `NANDInfo` blob in the `com.apple.disk_usage` lockdown domain is decoded as
a flat sequence of `u32 tag, u32 count, count * 8 bytes`. On a 256 GB phone that
is 3379 records covering all 65536 bytes exactly. Almost every tag is an
undocumented counter, so the script only surfaces the few that hold a
recognisable version or build string. An 8-byte counter lands in the printable
ASCII range often enough that mining for strings without that filter produces
garbage that changes between runs.

## Other generations

The display auth node is named after an Apple codename that changes each
generation (`mogul-display` on iPhone 18 Pro Max). The script scans the device
tree for any node ending in `-display` and tries each, so it should adapt
without a code change. If none is readable it falls back to `Panel_ID`.
