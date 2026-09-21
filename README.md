# iphone_panel.py

Works out which company made the OLED panel in a connected iPhone (Samsung
Display, LG Display, BOE) without a jailbreak, without Developer Mode, and
without installing anything on the phone.

## Why

Apple dual- and triple-sources iPhone displays. Panels from different suppliers
have visibly different calibration, and people have reported green or blue tint
on some non-Samsung units. Which one you get is luck of the draw, so it is
useful to be able to check a unit in the store.

## How it works

The display module has an authentication IC sitting on an I2C bus. That IC
holds an X.509 certificate signed by Apple's "Display CA M1". The certificate's
subject common name is the display module serial plus the IC's own serial:

    CN = G9N0000000000000AA-AABBCCDDEEFF
         ^^^                ^^^^^^^^^^^^
         vendor prefix      auth IC serial

The first three characters identify the supplier. Because the certificate is
signed and bound to the physical module, this is more trustworthy than reading
`AppleCLCD2`'s `Panel_ID` string, which is published on both the internal and
external display pipes and so can be ambiguous about where it came from. The
script reads `Panel_ID` too, but only as a cross-check and fallback.

All of it is read over the usbmux diagnostics relay (`com.apple.mobile.
diagnostics_relay`), the same channel `idevicediagnostics` uses.

Note: the older method of grepping a sysdiagnose for `raw-panel` stopped
working in iOS 26. The diagnostics relay route still works, and was verified on
iOS 27.

## Known vendor prefixes

| Prefix | Supplier        |
|--------|-----------------|
| G9N    | Samsung Display |
| G9P    | Samsung Display |
| G9Q    | Samsung Display |
| GH3    | LG Display      |
| GVC    | LG Display      |

These come from community reporting, not Apple documentation. BOE prefixes are
not confirmed. An unrecognised prefix is reported as UNKNOWN rather than
guessed at; if you hit one, please open an issue with the `--json` output.

## Install

    pip install pymobiledevice3
    python3 iphone_panel.py

Works on macOS, Linux and Windows. On macOS the usbmux daemon ships with the
system. On Linux and Windows you need `usbmuxd` or iTunes/Apple Devices
installed.

The phone has to be plugged in over USB, unlocked, and paired (tap Trust This
Computer the first time).

## Usage

    python3 iphone_panel.py              # first or only attached device
    python3 iphone_panel.py --list       # list attached devices
    python3 iphone_panel.py --udid UDID  # pick a specific one
    python3 iphone_panel.py --json       # machine-readable output
    python3 iphone_panel.py --raw        # also dump the Panel_ID breakdown

## Example

    Device           : Example iPhone (iPhone19,7, iOS 27.0)
    Region           : X/A

    Panel serial     : G9N0000000000000AA
    Read from        : mogul-display certificate (Apple-signed)
    Auth IC serial   : AABBCCDDEEFF
    Auth check       : PASS   (FAIL suggests a non-genuine or mismatched display)

    Panel vendor     : Samsung Display   [prefix G9N]

    Device-tree panel IDs (raw, undocumented):
      panel-vendor-id    0xEA (234)
      panel-device-id    0x51 (81)
      panel-build-id     0xA1 (161)
      panel-program-id   0x79 (121)
      panel-variant-id   0x00 (0)

The `panel-*` device-tree values are recorded by the bootloader. Nobody has
published a mapping from `panel-vendor-id` to a supplier name, so the script
prints them raw. If enough people post these alongside a known serial prefix,
the mapping can be worked out, which would give a second independent check.

## Other generations

The auth IC node is named after an Apple codename that changes between
generations (`mogul-display` on iPhone 18 Pro Max). The script scans the device
tree for any node ending in `-display` and tries each, so it should adapt
without a code change. If it cannot find one, it falls back to `Panel_ID`.
