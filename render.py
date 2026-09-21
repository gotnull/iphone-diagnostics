"""
render.py - draw an audit result as a PNG report card.

The audit is key-value facts plus three states (display supplier, parts-pairing
result, modem maker) and one magnitude (battery health). That is a report card,
not a chart: a row of stat tiles over a set of two-column tables.

Layout rule: every string is measured first and the canvas is sized to fit it.
Nothing is ever truncated or abbreviated to make it fit, and no two strings can
overlap, because each card is at least as wide as its widest label + value pair.

Colours are the reference data-viz palette's ink, surface and status tokens used
unchanged. No categorical series appear, so no series palette is in play.
"""

import datetime

# --- tokens ---------------------------------------------------------------
# Chart chrome and ink, plus the fixed status palette.
THEMES = {
    "light": {
        "page": "#f9f9f7",
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink_2": "#52514e",
        "muted": "#898781",
        "rule": "#e1e0d9",
        "border": (0.043, 0.043, 0.043, 0.10),
        "accent": "#2a78d6",
        "track": "#e1e0d9",
    },
    "dark": {
        "page": "#0d0d0d",
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "ink_2": "#c3c2b7",
        "muted": "#898781",
        "rule": "#2c2c2a",
        "border": (1.0, 1.0, 1.0, 0.10),
        "accent": "#3987e5",
        "track": "#383835",
    },
}

# Fixed in both modes by the palette spec.
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

SANS = "DejaVu Sans"
MONO = "DejaVu Sans Mono"

# Geometry. Everything else is derived from measured text.
PAD = 40           # page margin
CARD_PAD = 18      # card inner padding
COL_GAP = 40       # minimum gap between a label and its value
GUTTER = 24        # gap between the two card columns
TILE_GAP = 16
TILE_H = 110
ROW_H = 26
HEADER_GAP = 60    # minimum gap between the title and the date
SECTION_GAP = 26
MIN_WIDTH = 1100

SZ_TITLE = 25
SZ_SUBTITLE = 11.5
SZ_META = 11
SZ_CAPTION = 9.5
SZ_TILE_VALUE = 17
SZ_TILE_SUB = 9.5
SZ_SECTION = 10
SZ_ROW = 10.5


def _fmt_date(when):
    """Day-first with an ordinal, e.g. 21st September 2026."""
    day = when.day
    if 11 <= day <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix} {when:%B %Y}"


def _measurer():
    """Text width in pixels at dpi 100, which is the drawing unit below."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    renderer = fig.canvas.get_renderer()

    def measure(s, size, mono=False, weight="normal"):
        if not s:
            return 0.0
        obj = ax.text(0, 0, str(s), fontsize=size,
                      family=MONO if mono else SANS, fontweight=weight)
        width = obj.get_window_extent(renderer=renderer).width
        obj.remove()
        return width

    return measure


def _tiles(result):
    """The headline facts, each as (caption, value, sub, status_role)."""
    out = []
    disp = result.get("display") or {}
    if disp.get("panel_serial"):
        from iphone_audit import DISPLAY_VENDOR_PREFIXES, classify
        prefix, vendor = classify(disp["panel_serial"], DISPLAY_VENDOR_PREFIXES)
        out.append(("Display supplier", vendor or "Unknown",
                    f"prefix {prefix}", None if vendor else "warning"))

    modem = result.get("modem") or {}
    if modem.get("maker"):
        part = f"{modem['maker']} {modem['part']}" if modem.get("part") else modem["maker"]
        pci = modem.get("pci") or {}
        sub = (f"PCI {pci.get('vendor_id', 0):04x}:{pci.get('device_id', 0):04x}"
               if pci else "")
        out.append(("Modem", part, sub, None))

    if disp.get("auth_passed") is not None:
        ok = disp["auth_passed"]
        out.append(("Parts pairing", "PASS" if ok else "FAIL",
                    disp.get("auth_ca") or "", "good" if ok else "critical"))

    batt = result.get("battery") or {}
    if batt.get("measured_health_pct") is not None:
        cycles = batt.get("cycle_count")
        out.append(("Battery health", f"{batt['measured_health_pct']}%",
                    f"{cycles} cycles" if cycles is not None else "", None))
    return out


def _sections(result):
    """(title, [(label, value, mono)]) for each populated section."""
    blocks = []

    d = result.get("device")
    if d:
        blocks.append(("Device", [
            ("Model", d.get("product_type"), True),
            ("Hardware model", d.get("hardware_model"), True),
            ("SoC", f"{d.get('soc')}  ({d['soc_name']})" if d.get("soc_name")
             else d.get("soc"), True),
            ("SoC chip ID", f"0x{d['chip_id']:04X}" if d.get("chip_id") else None, True),
            ("GPU", d.get("gpu"), True),
            ("iOS", f"{d.get('ios_version')} ({d.get('build')})", True),
            ("Region", d.get("region_label") or d.get("region"), False),
            ("Bootloader", d.get("bootloader"), True),
        ]))

    disp = result.get("display")
    if disp and disp.get("panel_serial"):
        rows = [
            ("Panel serial", disp["panel_serial"], True),
            ("Read from", disp.get("source"), False),
            ("Signed by", disp.get("auth_ca"), False),
            ("Auth IC serial", disp.get("auth_ic_serial"), True),
            ("Pixel density", f"{disp['ppi']} ppi" if disp.get("ppi") else None, True),
        ]
        for key, label in (("panel-vendor-id", "Panel vendor ID"),
                           ("panel-device-id", "Panel device ID"),
                           ("panel-build-id", "Panel build ID"),
                           ("panel-program-id", "Panel program ID"),
                           ("panel-variant-id", "Panel variant ID")):
            value = (disp.get("device_tree_ids") or {}).get(key)
            if value is not None:
                rows.append((label, f"0x{value:02X}", True))
        blocks.append(("Display", rows))

    m = result.get("modem")
    if m:
        blocks.append(("Modem", [
            ("PCIe node", (m.get("pci") or {}).get("node"), True),
            ("Radio type", m.get("radio_type"), True),
            ("Compatible", m.get("compatible"), True),
            ("Firmware", m.get("firmware"), True),
            ("Baseband chip ID", f"0x{m['chip_id']:04X}"
             if isinstance(m.get("chip_id"), int) else m.get("chip_id"), True),
            ("Status", m.get("status"), False),
        ]))

    w = result.get("wireless")
    if w:
        blocks.append(("Wireless and other PCIe", [
            (dev["node"], f"{dev['vendor'] or 'unknown'}   "
                          f"PCI {dev['vendor_id']:04x}:{dev['device_id']:04x}", True)
            for dev in w]))

    b = result.get("battery")
    if b:
        blocks.append(("Battery", [
            ("Pack serial", b.get("serial"), True),
            ("Manufacturer code", b.get("manufacturer_data"), True),
            ("Cycle count", b.get("cycle_count"), True),
            ("Design capacity", f"{b['design_capacity_mah']} mAh"
             if b.get("design_capacity_mah") else None, True),
            ("Full charge capacity", f"{b['full_charge_capacity_mah']} mAh"
             if b.get("full_charge_capacity_mah") else None, True),
            ("Charge now", f"{b['current_charge_pct']}%"
             if b.get("current_charge_pct") is not None else None, True),
            ("Voltage", f"{b['voltage_mv']} mV" if b.get("voltage_mv") else None, True),
        ]))

    s = result.get("storage")
    if s:
        rows = [
            ("Model", s.get("model"), True),
            ("Serial", s.get("serial"), True),
            ("Controller firmware", s.get("firmware"), True),
            ("Interconnect", s.get("interconnect"), False),
            ("Encryption", s.get("encryption"), True),
            ("Free space", f"{s['bytes_available'] / 1e9:.1f} GB"
             if s.get("bytes_available") else None, True),
        ]
        nand = s.get("nand_info")
        if nand:
            rows.append(("NANDInfo records", f"{nand['records']}", True))
            rows.append(("NANDInfo decoded",
                         f"{nand['bytes_consumed']}/{nand['bytes_total']} bytes", True))
            for tag, vals in (nand.get("strings") or {}).items():
                rows.append((f"NANDInfo {tag}", ", ".join(vals), True))
        blocks.append(("Storage", rows))

    return [(title, [(label, str(value), mono) for label, value, mono in rows
                     if value is not None and value != ""])
            for title, rows in blocks]


def layout(result, model_name=None):
    """
    Work out the page geometry from measured text.

    Split out so the same maths can be checked independently of drawing: the
    page is widened until every label + value pair, every tile and the header
    all fit, which is what makes truncation unnecessary.
    """
    measure = _measurer()
    tiles = _tiles(result)
    blocks = _sections(result)

    # Two columns of cards. Split at the point that most evenly divides the
    # total height, which keeps reading order intact (a greedy fill does not).
    heights = [40 + len(rows) * ROW_H + SECTION_GAP for _, rows in blocks]
    total = sum(heights)
    best, running, cut = None, 0, 0
    for i, h in enumerate(heights):
        running += h
        diff = abs(running - (total - running))
        if best is None or diff < best:
            best, cut = diff, i + 1
    left, right = blocks[:cut], blocks[cut:]
    lh, rh = sum(heights[:cut]), sum(heights[cut:])

    def block_width(block):
        title, rows = block
        need = measure(title.upper(), SZ_SECTION, weight="bold")
        for label, value, mono in rows:
            need = max(need, measure(label, SZ_ROW)
                       + COL_GAP + measure(value, SZ_ROW, mono))
        return need + 2 * CARD_PAD

    width = 2 * PAD + 2 * max([block_width(b) for b in blocks], default=300) + GUTTER

    d = result.get("device") or {}
    title_text = model_name or d.get("product_type") or "iPhone"
    date_text = _fmt_date(datetime.date.today())
    width = max(width, PAD * 2 + HEADER_GAP
                + measure(title_text, SZ_TITLE, weight="bold")
                + measure(date_text, SZ_META))

    subtitle_bits = [d.get("product_type"),
                     f"iOS {d.get('ios_version')}" if d.get("ios_version") else None,
                     d.get("soc"),
                     f"Region {d.get('region')}" if d.get("region") else None]
    subtitle = "   .   ".join(b for b in subtitle_bits if b)
    width = max(width, PAD * 2 + HEADER_GAP + measure(subtitle, SZ_SUBTITLE)
                + measure("iphone_audit.py", 10, mono=True))

    if tiles:
        widest_tile = max(
            max(measure(cap.upper(), SZ_CAPTION, weight="bold"),
                measure(val, SZ_TILE_VALUE, weight="bold"),
                measure(sub, SZ_TILE_SUB, mono=True))
            for cap, val, sub, _role in tiles) + 2 * CARD_PAD
        width = max(width, 2 * PAD + len(tiles) * widest_tile
                    + TILE_GAP * (len(tiles) - 1))

    width = max(width, MIN_WIDTH)
    tiles_h = TILE_H + 24 if tiles else 0
    return {
        "measure": measure,
        "tiles": tiles,
        "blocks": blocks,
        "left": left,
        "right": right,
        "width": width,
        "col_w": (width - 2 * PAD - GUTTER) / 2,
        "header_h": 96,
        "tiles_h": tiles_h,
        "height": PAD + 96 + tiles_h + max(lh, rh) + PAD,
        "title_text": title_text,
        "subtitle": subtitle,
        "date_text": date_text,
    }


def render(result, path, theme="light", model_name=None):
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.patches import FancyBboxPatch, Rectangle

    t = THEMES[theme]
    L = layout(result, model_name)
    tiles, left, right = L["tiles"], L["left"], L["right"]
    width, height, col_w = L["width"], L["height"], L["col_w"]
    header_h, tiles_h = L["header_h"], L["tiles_h"]
    title_text, subtitle, date_text = L["title_text"], L["subtitle"], L["date_text"]

    # --- draw ---
    fig = Figure(figsize=(width / 100, height / 100), dpi=100)
    fig.patch.set_facecolor(t["page"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)  # y grows downward, like a page
    ax.axis("off")

    def card(x, y, w, h):
        ax.add_patch(FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0,rounding_size=10",
            linewidth=1, edgecolor=t["border"], facecolor=t["surface"],
            mutation_aspect=1))

    def text(x, y, s, size=SZ_ROW, color=None, weight="normal", mono=False,
             ha="left"):
        ax.text(x, y, str(s), fontsize=size, color=color or t["ink"],
                fontweight=weight, family=MONO if mono else SANS,
                ha=ha, va="center")

    y = PAD
    text(PAD, y + 16, title_text, size=SZ_TITLE, weight="bold")
    text(PAD, y + 48, subtitle, size=SZ_SUBTITLE, color=t["ink_2"])
    text(width - PAD, y + 16, date_text, size=SZ_META, color=t["muted"], ha="right")
    text(width - PAD, y + 44, "iphone_audit.py", size=10, color=t["muted"],
         ha="right", mono=True)
    y += header_h - 24
    ax.add_line(Line2D([PAD, width - PAD], [y, y], color=t["rule"], linewidth=1))
    y += 24

    if tiles:
        tw = (width - 2 * PAD - TILE_GAP * (len(tiles) - 1)) / len(tiles)
        for i, (caption, value, sub, role) in enumerate(tiles):
            x = PAD + i * (tw + TILE_GAP)
            card(x, y, tw, TILE_H)
            text(x + CARD_PAD, y + 22, caption.upper(), size=SZ_CAPTION,
                 color=t["muted"], weight="bold")
            text(x + CARD_PAD, y + 52, value, size=SZ_TILE_VALUE, weight="bold",
                 color=STATUS[role] if role else t["ink"])
            if sub:
                text(x + CARD_PAD, y + 76, sub, size=SZ_TILE_SUB,
                     color=t["ink_2"], mono=True)
            # Battery health is the one magnitude here, so it gets a bar.
            if caption == "Battery health":
                pct = (result.get("battery") or {}).get("measured_health_pct") or 0
                bar_w = tw - 2 * CARD_PAD
                ax.add_patch(Rectangle((x + CARD_PAD, y + 92), bar_w, 5,
                                       facecolor=t["track"], edgecolor="none"))
                ax.add_patch(Rectangle((x + CARD_PAD, y + 92),
                                       bar_w * min(pct, 100) / 100, 5,
                                       facecolor=t["accent"], edgecolor="none"))
        y += tiles_h

    for column, x0 in ((left, PAD), (right, PAD + col_w + GUTTER)):
        cy = y
        for title_, rows in column:
            h = 40 + len(rows) * ROW_H
            card(x0, cy, col_w, h)
            text(x0 + CARD_PAD, cy + 22, title_.upper(), size=SZ_SECTION,
                 color=t["accent"], weight="bold")
            for j, (label, value, mono) in enumerate(rows):
                ry = cy + 44 + j * ROW_H
                text(x0 + CARD_PAD, ry, label, size=SZ_ROW, color=t["ink_2"])
                text(x0 + col_w - CARD_PAD, ry, value, size=SZ_ROW, mono=mono,
                     ha="right")
            cy += h + SECTION_GAP

    fig.savefig(path, facecolor=t["page"], dpi=200)
    return path
