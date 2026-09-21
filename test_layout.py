"""
Assert the rendered page can never overlap or need truncation.

    .venv/bin/python iphone_audit.py --json > audit.json
    .venv/bin/python test_layout.py audit.json

The renderer derives the page width from measured text, so this re-runs the
same measurements against the same layout function and checks the slack is
non-negative everywhere.
""".strip()
import json, sys, render

path = sys.argv[1] if len(sys.argv) > 1 else "sample_audit.json"
result = json.load(open(path))
L = render.layout(result, model_name="iPhone 18 Pro Max")
m, col_w, width = L["measure"], L["col_w"], L["width"]
fails = []

# Every table row: label and right-aligned value must not meet.
tight = None
for title, rows in L["blocks"]:
    for label, value, mono in rows:
        gap = col_w - 2*render.CARD_PAD - m(label, render.SZ_ROW) - m(value, render.SZ_ROW, mono)
        if tight is None or gap < tight[0]:
            tight = (gap, title, label)
        if gap < 0:
            fails.append(f"row overlap: {title} / {label} by {-gap:.1f}px")
print(f"tightest row gap      {tight[0]:7.1f}px   ({tight[1]} / {tight[2]})")

# Every tile: widest line must fit inside the tile.
tiles = L["tiles"]
tw = (width - 2*render.PAD - render.TILE_GAP*(len(tiles)-1)) / len(tiles)
tile_tight = None
for cap, val, sub, _ in tiles:
    widest = max(m(cap.upper(), render.SZ_CAPTION, weight="bold"),
                 m(val, render.SZ_TILE_VALUE, weight="bold"),
                 m(sub, render.SZ_TILE_SUB, mono=True))
    slack = tw - 2*render.CARD_PAD - widest
    if tile_tight is None or slack < tile_tight[0]:
        tile_tight = (slack, cap)
    if slack < 0:
        fails.append(f"tile overflow: {cap} by {-slack:.1f}px")
print(f"tightest tile slack   {tile_tight[0]:7.1f}px   ({tile_tight[1]})")

# Header: title and date must not meet.
hgap = (width - 2*render.PAD - m(L["title_text"], render.SZ_TITLE, weight="bold")
        - m(L["date_text"], render.SZ_META))
print(f"header title/date gap {hgap:7.1f}px")
if hgap < 0:
    fails.append(f"header overlap by {-hgap:.1f}px")

# Cards must sit inside the page.
right_edge = render.PAD + col_w + render.GUTTER + col_w
print(f"right card edge       {right_edge:7.1f}px of {width:.0f}px page")
if right_edge > width - render.PAD + 0.5:
    fails.append("card column overflows the page")

print()
if fails:
    print("FAIL"); [print("  " + f) for f in fails]; sys.exit(1)
print("PASS: no overlap, nothing truncated")
