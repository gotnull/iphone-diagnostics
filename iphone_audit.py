#!/usr/bin/env python3
"""
iphone_audit.py - read hardware component identity from a connected iPhone.

Apple multi-sources the parts inside an iPhone and does not tell you which one
you got. Some of that is visible over the usbmux diagnostics relay: the display
module's supplier, whether the display passes Apple's own authenticity check,
which silicon the modem and wireless chip actually are, real battery capacity
and cycle count, and the storage controller's identity.

No jailbreak, no Developer Mode, nothing installed on the phone. It only has to
be plugged in, unlocked, and paired ("Trust This Computer").

    pip install pymobiledevice3
    python3 iphone_audit.py

Sections can be run individually, see --help.
"""

import argparse
import asyncio
import inspect
import json
import re
import struct
import sys

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# Display module serial prefix -> supplier. From community reporting on the
# iPhone 15/16/17/18 "display lottery". Apple does not document these.
DISPLAY_VENDOR_PREFIXES = {
    "G9N": "Samsung Display",
    "G9P": "Samsung Display",
    "G9Q": "Samsung Display",
    "GH3": "LG Display",
    "GVC": "LG Display",
}

# Battery packs carry a serial in the same 18-character format as the display,
# so the same prefix trick should identify the cell supplier. Nobody has
# published the mapping yet. Add entries here as they are confirmed.
BATTERY_VENDOR_PREFIXES = {}

# PCI-SIG vendor IDs, for working out whose silicon is actually on the bus.
PCI_VENDORS = {
    0x106B: "Apple",
    0x17CB: "Qualcomm",
    0x14E4: "Broadcom",
    0x8086: "Intel",
    0x168C: "Qualcomm Atheros",
    0x11AB: "Marvell",
    0x1217: "O2 Micro",
}

# Device-tree nodes holding a component authentication IC. The name is an Apple
# codename that changes between generations, so these are a starting point and
# the tree is also scanned for anything matching.
KNOWN_AUTH_NODES = ["mogul-display", "display-auth", "panel-auth"]

PANEL_SERIAL_RE = re.compile(rb"[A-Z][A-Z0-9]{17}-[0-9A-F]{12}")
COMPONENT_SERIAL_RE = re.compile(r"[A-Z][A-Z0-9]{17}")
CN_OID = b"\x06\x03\x55\x04\x03"  # OBJECT IDENTIFIER 2.5.4.3 (commonName)

SECTIONS = ["device", "display", "modem", "wireless", "battery", "storage"]


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

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


async def get_value(lockdown, key=None, domain=None):
    try:
        kwargs = {}
        if key is not None:
            kwargs["key"] = key
        if domain is not None:
            kwargs["domain"] = domain
        return await call(lockdown.get_value, **kwargs)
    except Exception:  # noqa: BLE001 - a missing key is not fatal
        return None


async def read_entry(diag, **kwargs):
    try:
        entry = await call(diag.ioregistry, **kwargs)
    except Exception:  # noqa: BLE001 - a missing node is not fatal
        return None
    return entry if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------

def as_int(value):
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "little")
    return value


def as_text(value):
    if isinstance(value, (bytes, bytearray)):
        return value.rstrip(b"\x00").decode("ascii", "replace")
    return value


def der_common_names(der):
    """Pull every commonName value out of a DER blob without a crypto library."""
    names, pos = [], 0
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


def walk_tree(node, parent=None):
    """Yield (name, parent_name, node) for every entry in a plane dump."""
    if isinstance(node, dict):
        name = node.get("name") or node.get("className")
        name = name if isinstance(name, str) else None
        yield name, parent, node
        for child in node.get("children") or []:
            yield from walk_tree(child, name)


def decode_nand_info(blob):
    """
    The NANDInfo blob in the com.apple.disk_usage lockdown domain is a flat
    sequence of records: u32 tag, u32 count, then count * 8 bytes of payload.
    Tag meanings are undocumented apart from the ones that hold ASCII.
    """
    records, pos = [], 0
    while pos + 8 <= len(blob):
        tag, count = struct.unpack_from("<II", blob, pos)
        pos += 8
        size = count * 8
        if pos + size > len(blob):
            break
        records.append((tag, count, blob[pos:pos + size]))
        pos += size
    strings = {}
    for tag, _count, data in records:
        for match in re.finditer(rb"[ -~]{4,}", data):
            text = match.group().decode()
            # Most records hold counters, and an 8-byte counter regularly lands
            # entirely in the printable range by chance. Rather than guess, only
            # surface runs that match a shape we can actually name: a dotted
            # version, or an Apple build number such as 24A437.
            if not (re.fullmatch(r"\d+(\.\d+)+", text)
                    or re.fullmatch(r"\d{2}[A-Z]\d{3,}[a-z]?", text)):
                continue
            end = match.end()
            if end < len(data) and data[end] != 0:
                continue
            strings.setdefault(tag, []).append(text)
    return {
        "records": len(records),
        "bytes_consumed": pos,
        "bytes_total": len(blob),
        "strings": {f"0x{tag:04x}": vals for tag, vals in strings.items()},
    }


def classify(serial, table):
    if not serial:
        return None, None
    prefix = serial[:3]
    return prefix, table.get(prefix)


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

async def collect_device(lockdown):
    keys = {
        "device_name": "DeviceName",
        "product_type": "ProductType",
        "ios_version": "ProductVersion",
        "build": "BuildVersion",
        "serial_number": "SerialNumber",
        "region": "RegionInfo",
        "hardware_model": "HardwareModel",
        "soc": "HardwarePlatform",
        "chip_id": "ChipID",
        "board_id": "BoardId",
        "bootloader": "FirmwareVersion",
        "production_soc": "ProductionSOC",
    }
    out = {}
    for label, key in keys.items():
        out[label] = await get_value(lockdown, key=key)
    return out


async def collect_display(diag):
    out = {
        "panel_serial": None, "source": None, "auth_ic_serial": None,
        "auth_node": None, "auth_passed": None, "auth_ca": None,
        "panel_id_string": None, "panel_id_fields": [], "device_tree_ids": {},
    }

    candidates = list(KNOWN_AUTH_NODES)
    tree = await read_entry(diag, plane="IODeviceTree")
    if tree:
        for name, _parent, _node in walk_tree(tree):
            if name and name.endswith("-display") and name not in candidates:
                candidates.append(name)

    for name in candidates:
        entry = await read_entry(diag, name=name)
        if not entry or "certificate" not in entry:
            continue
        der = bytes(entry["certificate"])
        cns = der_common_names(der)
        serial = next((c for c in cns if PANEL_SERIAL_RE.fullmatch(c.encode())), None)
        if serial is None:
            match = PANEL_SERIAL_RE.search(der)
            serial = match.group().decode() if match else None
        if not serial:
            continue
        module, _, ic = serial.partition("-")
        out.update(panel_serial=module, auth_node=name, auth_ic_serial=ic or None,
                   source=f"{name} certificate (Apple-signed)")
        out["auth_ca"] = next((c for c in cns if "CA" in c and "Root" not in c), None)
        if "auth-passed" in entry:
            out["auth_passed"] = bool(as_int(entry["auth-passed"]))
        if "idsn" in entry:
            out["auth_ic_serial"] = bytes(entry["idsn"]).hex().upper()
        break

    clcd = (await read_entry(diag, ioclass="AppleCLCD2")
            or await read_entry(diag, name="AppleCLCD2"))
    if clcd and isinstance(clcd.get("Panel_ID"), str) and clcd["Panel_ID"]:
        out["panel_id_string"] = clcd["Panel_ID"]
        out["panel_id_fields"] = clcd["Panel_ID"].split("+")
        if not out["panel_serial"]:
            first = out["panel_id_fields"][0]
            if COMPONENT_SERIAL_RE.fullmatch(first):
                out.update(panel_serial=first, source="AppleCLCD2 Panel_ID")

    disp0 = await read_entry(diag, name="disp0")
    if disp0:
        for key in ("panel-vendor-id", "panel-device-id", "panel-build-id",
                    "panel-program-id", "panel-variant-id"):
            if key in disp0:
                out["device_tree_ids"][key] = as_int(disp0[key])
        if "dot-pitch-float" in disp0:
            out["ppi"] = round(struct.unpack("<f", disp0["dot-pitch-float"])[0], 1)
    return out


async def collect_pci(diag):
    """Every PCIe device, with the vendor ID that says whose silicon it is."""
    devices = []
    tree = await read_entry(diag, plane="IODeviceTree")
    if not tree:
        return devices
    names = [name for name, parent, _n in walk_tree(tree)
             if name and parent and parent.startswith("pci-bridge")]
    for name in sorted(set(names)):
        entry = await read_entry(diag, name=name)
        if not entry or "vendor-id" not in entry:
            continue
        vid = as_int(entry["vendor-id"])
        devices.append({
            "node": name,
            "vendor_id": vid,
            "vendor": PCI_VENDORS.get(vid),
            "device_id": as_int(entry.get("device-id")),
            "class_code": as_int(entry.get("class-code")),
            "revision": as_int(entry.get("revision-id")),
        })
    return devices


async def collect_modem(lockdown, diag, pci_devices):
    out = {
        "chip_id": await get_value(lockdown, key="BasebandChipID"),
        "cert_id": await get_value(lockdown, key="BasebandCertId"),
        "firmware": await get_value(lockdown, key="BasebandVersion"),
        "status": await get_value(lockdown, key="BasebandStatus"),
        "serial": None, "pci": None, "radio_type": None, "compatible": None,
    }
    serial = await get_value(lockdown, key="BasebandSerialNumber")
    if isinstance(serial, (bytes, bytearray)):
        serial = serial.hex().upper()
    out["serial"] = serial

    bb = await read_entry(diag, name="baseband")
    if bb:
        out["radio_type"] = as_text(bb.get("BasebandRadioType"))
        out["compatible"] = as_text(bb.get("compatible"))

    out["pci"] = next((d for d in pci_devices if "baseband" in d["node"]), None)
    return out


async def collect_wireless(pci_devices):
    """Anything on PCIe that is not the modem: Wi-Fi, Bluetooth, UWB."""
    return [d for d in pci_devices if "baseband" not in d["node"]]


async def collect_battery(diag):
    out = {}
    batt = await read_entry(diag, name="AppleSmartBattery")
    if not batt:
        return out
    data = batt.get("BatteryData") or {}
    design = data.get("DesignCapacity")
    full = data.get("FullChargeCapacity")
    out.update({
        "serial": as_text(batt.get("Serial")),
        "manufacturer_data": as_text(batt.get("ManufacturerData")),
        "cycle_count": batt.get("CycleCount"),
        "design_capacity_mah": design,
        "full_charge_capacity_mah": full,
        "nominal_capacity_mah": data.get("NominalChargeCapacity"),
        "current_charge_pct": batt.get("CurrentCapacity"),
        "reported_health_pct": batt.get("MaxCapacity"),
        "is_charging": batt.get("IsCharging"),
        "voltage_mv": batt.get("AppleRawBatteryVoltage"),
    })
    if design and full:
        out["measured_health_pct"] = round(full / design * 100, 1)
    return out


async def collect_storage(lockdown, diag):
    out = {}
    ctrl = await read_entry(diag, name="AppleANS3CGv2Controller")
    if ctrl:
        chars = ctrl.get("Controller Characteristics") or {}
        out.update({
            "model": ctrl.get("Model Number"),
            "serial": ctrl.get("Serial Number"),
            "firmware": ctrl.get("Firmware Revision"),
            "vendor": ctrl.get("Vendor Name"),
            "interconnect": ctrl.get("Physical Interconnect"),
            "encryption": chars.get("Encryption Type"),
            "status": ctrl.get("AppleNANDStatus"),
        })
    usage = await get_value(lockdown, domain="com.apple.disk_usage")
    if isinstance(usage, dict):
        out["bytes_available"] = usage.get("AmountDataAvailable")
        blob = usage.get("NANDInfo")
        if isinstance(blob, (bytes, bytearray)):
            out["nand_info"] = decode_nand_info(bytes(blob))
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def hdr(title):
    print()
    print(title)
    print("-" * len(title))


def row(label, value, note=""):
    if value is None or value == "":
        return
    print(f"  {label:<22} {value}{note}")


def report_device(d):
    hdr("Device")
    row("Name", d.get("device_name"))
    row("Model", d.get("product_type"))
    row("Hardware model", d.get("hardware_model"))
    row("SoC", f"{d.get('soc')} (ChipID 0x{d['chip_id']:04X})"
        if d.get("chip_id") else d.get("soc"))
    row("Board ID", d.get("board_id"))
    row("iOS", f"{d.get('ios_version')} ({d.get('build')})")
    row("Region", d.get("region"))
    row("Bootloader", d.get("bootloader"))
    row("Production SoC", d.get("production_soc"))


def report_display(d):
    hdr("Display")
    if not d.get("panel_serial"):
        print("  no readable display auth node or Panel_ID")
        return
    prefix, vendor = classify(d["panel_serial"], DISPLAY_VENDOR_PREFIXES)
    row("Panel serial", d["panel_serial"])
    row("Supplier", f"{vendor}   [prefix {prefix}]" if vendor
        else f"UNKNOWN   [prefix {prefix}] - not in the table yet")
    row("Read from", d.get("source"))
    row("Signed by", d.get("auth_ca"))
    row("Auth IC serial", d.get("auth_ic_serial"))
    if d.get("auth_passed") is not None:
        row("Parts pairing", "PASS" if d["auth_passed"] else "FAIL",
            "" if d["auth_passed"] else "   (non-genuine or mismatched display)")
    row("Pixel density", f"{d['ppi']} ppi" if d.get("ppi") else None)
    if d.get("device_tree_ids"):
        ids = "  ".join(f"{k.split('-', 1)[1]}=0x{v:02X}"
                        for k, v in d["device_tree_ids"].items())
        row("Device-tree IDs", ids)


def report_modem(m):
    hdr("Modem")
    pci = m.get("pci")
    if pci:
        vendor = pci["vendor"] or f"unknown vendor 0x{pci['vendor_id']:04X}"
        row("Silicon", f"{vendor}   [PCI {pci['vendor_id']:04x}:{pci['device_id']:04x}]")
        row("PCIe node", pci["node"])
    row("Baseband chip ID", f"{m['chip_id']} (0x{m['chip_id']:04X})"
        if isinstance(m.get("chip_id"), int) else m.get("chip_id"))
    row("Radio type", m.get("radio_type"))
    row("Compatible", m.get("compatible"))
    row("Firmware", m.get("firmware"))
    row("Cert ID", m.get("cert_id"))
    row("Status", m.get("status"))
    if pci and pci["vendor_id"] == 0x106B:
        print("  note: PCI vendor 0x106B is Apple. A Qualcomm modem would")
        print("        enumerate as 0x17CB.")


def report_wireless(devices):
    hdr("Wireless and other PCIe")
    if not devices:
        print("  none found")
        return
    for d in devices:
        vendor = d["vendor"] or f"unknown 0x{d['vendor_id']:04X}"
        row(d["node"], f"{vendor}   [PCI {d['vendor_id']:04x}:{d['device_id']:04x}]")


def report_battery(b):
    hdr("Battery")
    if not b:
        print("  not readable")
        return
    prefix, vendor = classify(b.get("manufacturer_data") or "", BATTERY_VENDOR_PREFIXES)
    row("Pack serial", b.get("serial"))
    if prefix:
        row("Manufacturer code", f"{b['manufacturer_data']}   [prefix {prefix}]"
            + ("" if vendor else " - supplier table not populated yet"))
    row("Cycle count", b.get("cycle_count"))
    row("Design capacity", f"{b['design_capacity_mah']} mAh"
        if b.get("design_capacity_mah") else None)
    row("Full charge capacity", f"{b['full_charge_capacity_mah']} mAh"
        if b.get("full_charge_capacity_mah") else None)
    row("Measured health", f"{b['measured_health_pct']}% of design"
        if b.get("measured_health_pct") else None)
    row("Reported health", f"{b['reported_health_pct']}%"
        if b.get("reported_health_pct") else None)
    row("Charge now", f"{b['current_charge_pct']}%"
        if b.get("current_charge_pct") else None)
    row("Voltage", f"{b['voltage_mv']} mV" if b.get("voltage_mv") else None)


def report_storage(s):
    hdr("Storage")
    if not s:
        print("  not readable")
        return
    row("Model", s.get("model"))
    row("Vendor", s.get("vendor"))
    row("Serial", s.get("serial"))
    row("Controller firmware", s.get("firmware"))
    row("Interconnect", s.get("interconnect"))
    row("Encryption", s.get("encryption"))
    row("Free space", f"{s['bytes_available'] / 1e9:.1f} GB"
        if s.get("bytes_available") else None)
    nand = s.get("nand_info")
    if nand:
        row("NANDInfo", f"{nand['records']} records, "
            f"{nand['bytes_consumed']}/{nand['bytes_total']} bytes decoded")
        for tag, vals in nand["strings"].items():
            row(f"  tag {tag}", ", ".join(vals))
        print("  note: the flash die vendor (Kioxia, SK Hynix, Samsung) is not")
        print("        exposed. Apple's controller fronts it.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def show_devices():
    from pymobiledevice3.usbmux import list_devices
    devices = await call(list_devices)
    if not devices:
        die("no devices attached")
    for dev in devices:
        lockdown = await connect(dev.serial)
        name = await get_value(lockdown, key="DeviceName")
        ptype = await get_value(lockdown, key="ProductType")
        version = await get_value(lockdown, key="ProductVersion")
        print(f"{dev.serial}  {name}  ({ptype}, iOS {version})")


async def run(args):
    if args.list:
        await show_devices()
        return 0

    from pymobiledevice3.services.diagnostics import DiagnosticsService

    wanted = args.section or SECTIONS
    lockdown = await connect(args.udid)
    result = {}

    service = DiagnosticsService(lockdown)
    ctx = service.__aenter__() if hasattr(service, "__aenter__") else None
    diag = await ctx if ctx else service.__enter__()
    try:
        if "device" in wanted:
            result["device"] = await collect_device(lockdown)
        pci = await collect_pci(diag) if {"modem", "wireless"} & set(wanted) else []
        if "display" in wanted:
            result["display"] = await collect_display(diag)
        if "modem" in wanted:
            result["modem"] = await collect_modem(lockdown, diag, pci)
        if "wireless" in wanted:
            result["wireless"] = await collect_wireless(pci)
        if "battery" in wanted:
            result["battery"] = await collect_battery(diag)
        if "storage" in wanted:
            result["storage"] = await collect_storage(lockdown, diag)
    finally:
        if hasattr(service, "__aexit__"):
            await service.__aexit__(None, None, None)
        else:
            service.__exit__(None, None, None)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    if "device" in result:
        report_device(result["device"])
    if "display" in result:
        report_display(result["display"])
    if "modem" in result:
        report_modem(result["modem"])
    if "wireless" in result:
        report_wireless(result["wireless"])
    if "battery" in result:
        report_battery(result["battery"])
    if "storage" in result:
        report_storage(result["storage"])

    if args.raw and result.get("display", {}).get("panel_id_string"):
        hdr("AppleCLCD2 Panel_ID fields")
        for i, field in enumerate(result["display"]["panel_id_fields"]):
            print(f"  [{i}] {field}")
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Read hardware component identity from a connected iPhone.")
    ap.add_argument("--udid", help="target a specific device")
    ap.add_argument("--list", action="store_true", help="list attached devices and exit")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--raw", action="store_true", help="also print the Panel_ID breakdown")
    ap.add_argument("--section", action="append", choices=SECTIONS,
                    help="limit to one section, repeatable")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
