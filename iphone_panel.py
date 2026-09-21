#!/usr/bin/env python3
"""
iphone_panel.py - identify which OLED panel vendor is inside a connected iPhone.

Apple buys iPhone displays from more than one supplier (Samsung Display, LG
Display, BOE), and which one you get is luck of the draw. The display module
carries an authentication IC on an I2C bus. That IC holds an Apple-signed X.509
certificate whose subject common name is the display module serial plus the
IC's own serial:

    CN = G9N0000000000000AA-AABBCCDDEEFF
         ^^^                ^^^^^^^^^^^^
         vendor prefix      auth IC serial (matches the node's `idsn`)

The first three characters of that serial identify the supplier.

Everything is read over the usbmux diagnostics relay, so no jailbreak, no
developer mode, and nothing is installed on the phone. The phone only has to be
plugged in, unlocked, and paired ("Trust This Computer").

Usage:
    pip install pymobiledevice3
    python3 iphone_panel.py              # first/only attached device
    python3 iphone_panel.py --list       # show attached devices
    python3 iphone_panel.py --udid UDID  # pick one
    python3 iphone_panel.py --json       # machine-readable
    python3 iphone_panel.py --raw        # also dump the Panel_ID breakdown
"""

import argparse
import asyncio
import inspect
import json
import re
import sys

# Serial prefix -> supplier. Sourced from community reporting on the iPhone
# 15/16/17/18 "display lottery". Apple does not document these, so treat an
# unknown prefix as unknown rather than guessing.
VENDOR_PREFIXES = {
    "G9N": "Samsung Display",
    "G9P": "Samsung Display",
    "G9Q": "Samsung Display",
    "GH3": "LG Display",
    "GVC": "LG Display",
}

# Device-tree nodes holding the display authentication IC. The name is an Apple
# internal codename that changes between generations (iPhone 18 Pro Max uses
# "mogul-display"), so the script also discovers candidates from the tree.
KNOWN_AUTH_NODES = ["mogul-display", "display-auth", "panel-auth"]

PANEL_SERIAL_RE = re.compile(rb"[A-Z][A-Z0-9]{17}-[0-9A-F]{12}")
CN_OID = b"\x06\x03\x55\x04\x03"  # OBJECT IDENTIFIER 2.5.4.3 (commonName)


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


async def call(fn, *args, **kwargs):
    """pymobiledevice3 went async in v5; this supports both old and new."""
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def connect(udid):
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
    except ImportError:
        die("pymobiledevice3 is not installed. Run: pip install pymobiledevice3")
    try:
        return await call(create_using_usbmux, serial=udid)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface whatever usbmux says
        die(f"could not connect to the device: {exc}\n"
            "Check that it is plugged in, unlocked, and that you tapped Trust.")


async def show_devices():
    from pymobiledevice3.usbmux import list_devices
    devices = await call(list_devices)
    if not devices:
        die("no devices attached")
    for dev in devices:
        lockdown = await connect(dev.serial)
        name = await call(lockdown.get_value, key="DeviceName")
        ptype = await call(lockdown.get_value, key="ProductType")
        version = await call(lockdown.get_value, key="ProductVersion")
        print(f"{dev.serial}  {name}  ({ptype}, iOS {version})")


def der_common_names(der):
    """Pull every commonName value out of a DER blob without a crypto library."""
    names = []
    pos = 0
    while True:
        pos = der.find(CN_OID, pos)
        if pos < 0:
            return names
        pos += len(CN_OID)
        if pos + 2 > len(der):
            return names
        tag, length = der[pos], der[pos + 1]
        # UTF8String (0x0c), PrintableString (0x13), IA5String (0x16)
        if tag in (0x0C, 0x13, 0x16) and length < 0x80:
            try:
                names.append(der[pos + 2:pos + 2 + length].decode("ascii"))
            except UnicodeDecodeError:
                pass
        pos += 2


def walk_names(node):
    """Yield every node name in an ioregistry plane dump."""
    if isinstance(node, dict):
        name = node.get("name") or node.get("className")
        if isinstance(name, str):
            yield name
        for child in node.get("children") or []:
            yield from walk_names(child)


async def read_entry(diag, **kwargs):
    try:
        entry = await call(diag.ioregistry, **kwargs)
    except Exception:  # noqa: BLE001 - a missing node is not fatal
        return None
    return entry if isinstance(entry, dict) else None


async def find_auth_nodes(diag):
    candidates = list(KNOWN_AUTH_NODES)
    tree = await read_entry(diag, plane="IODeviceTree")
    if tree:
        for name in walk_names(tree):
            if name.endswith("-display") and name not in candidates:
                candidates.append(name)
    return candidates


def decode_int(value):
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "little")
    return value


def classify(serial):
    if not serial:
        return None, None
    prefix = serial[:3]
    return prefix, VENDOR_PREFIXES.get(prefix)


async def collect(diag):
    out = {
        "panel_serial": None,
        "panel_serial_source": None,
        "auth_ic_serial": None,
        "auth_node": None,
        "auth_passed": None,
        "panel_id_string": None,
        "panel_id_fields": [],
        "device_tree_ids": {},
    }

    # 1. Preferred source: the Apple-signed certificate on the display auth IC.
    #    It is bound to the physical module, so there is no ambiguity about
    #    which display pipe the data came from.
    for name in await find_auth_nodes(diag):
        entry = await read_entry(diag, name=name)
        if not entry or "certificate" not in entry:
            continue
        der = bytes(entry["certificate"])
        serial = next((cn for cn in der_common_names(der)
                       if PANEL_SERIAL_RE.fullmatch(cn.encode())), None)
        if serial is None:
            match = PANEL_SERIAL_RE.search(der)
            serial = match.group().decode() if match else None
        if serial:
            module, _, ic = serial.partition("-")
            out.update(panel_serial=module,
                       panel_serial_source=f"{name} certificate (Apple-signed)",
                       auth_ic_serial=ic or None,
                       auth_node=name)
            if "auth-passed" in entry:
                out["auth_passed"] = bool(decode_int(entry["auth-passed"]))
            if "idsn" in entry:
                out["auth_ic_serial"] = bytes(entry["idsn"]).hex().upper()
            break

    # 2. Cross-check / fallback: the framebuffer's Panel_ID string.
    clcd = (await read_entry(diag, ioclass="AppleCLCD2")
            or await read_entry(diag, name="AppleCLCD2"))
    if clcd and isinstance(clcd.get("Panel_ID"), str) and clcd["Panel_ID"]:
        out["panel_id_string"] = clcd["Panel_ID"]
        out["panel_id_fields"] = clcd["Panel_ID"].split("+")
        if not out["panel_serial"]:
            first = out["panel_id_fields"][0]
            if re.fullmatch(r"[A-Z][A-Z0-9]{17}", first):
                out.update(panel_serial=first,
                           panel_serial_source="AppleCLCD2 Panel_ID")

    # 3. Extra context: numeric panel IDs the bootloader recorded.
    disp0 = await read_entry(diag, name="disp0")
    if disp0:
        for key in ("panel-vendor-id", "panel-device-id", "panel-build-id",
                    "panel-program-id", "panel-variant-id"):
            if key in disp0:
                out["device_tree_ids"][key] = decode_int(disp0[key])
    return out


def report(info, data, args):
    prefix, vendor = classify(data["panel_serial"])
    if args.json:
        print(json.dumps({**info, **data, "vendor_prefix": prefix, "vendor": vendor},
                         indent=2, default=str))
        return 0

    print(f"Device           : {info['device_name']} ({info['product_type']}, iOS {info['ios_version']})")
    print(f"Region           : {info['region']}")
    print()
    if not data["panel_serial"]:
        print("Panel serial     : not found")
        print()
        print("No readable display auth node on this device. Re-run with --json")
        print("and report the output so the node name can be added.")
        return 2

    print(f"Panel serial     : {data['panel_serial']}")
    print(f"Read from        : {data['panel_serial_source']}")
    if data["auth_ic_serial"]:
        print(f"Auth IC serial   : {data['auth_ic_serial']}")
    if data["auth_passed"] is not None:
        print(f"Auth check       : {'PASS' if data['auth_passed'] else 'FAIL'}"
              "   (FAIL suggests a non-genuine or mismatched display)")
    print()
    if vendor:
        print(f"Panel vendor     : {vendor}   [prefix {prefix}]")
    else:
        print(f"Panel vendor     : UNKNOWN   [prefix {prefix}]")
        print("                   Prefix not in the table yet. Known prefixes:")
        for pfx, name in sorted(VENDOR_PREFIXES.items()):
            print(f"                     {pfx} = {name}")

    if data["device_tree_ids"]:
        print()
        print("Device-tree panel IDs (raw, undocumented):")
        for key, value in data["device_tree_ids"].items():
            print(f"  {key:<18} 0x{value:02X} ({value})")

    if args.raw and data["panel_id_string"]:
        print()
        print("AppleCLCD2 Panel_ID fields:")
        for i, field in enumerate(data["panel_id_fields"]):
            print(f"  [{i}] {field}")
    return 0


async def run(args):
    if args.list:
        await show_devices()
        return 0

    from pymobiledevice3.services.diagnostics import DiagnosticsService

    lockdown = await connect(args.udid)
    info = {}
    for label, key in (("device_name", "DeviceName"), ("product_type", "ProductType"),
                       ("ios_version", "ProductVersion"), ("serial_number", "SerialNumber"),
                       ("region", "RegionInfo")):
        info[label] = await call(lockdown.get_value, key=key)

    service = DiagnosticsService(lockdown)
    if hasattr(service, "__aenter__"):
        async with service as diag:
            data = await collect(diag)
    else:
        with service as diag:
            data = await collect(diag)

    return report(info, data, args)


def main():
    ap = argparse.ArgumentParser(
        description="Identify the OLED panel supplier in a connected iPhone.")
    ap.add_argument("--udid", help="target a specific device")
    ap.add_argument("--list", action="store_true", help="list attached devices and exit")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--raw", action="store_true", help="also print the Panel_ID breakdown")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
