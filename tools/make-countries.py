#!/usr/bin/env python3
"""Rasterise Natural Earth's 1:50m countries (public domain) into share/countries.bin, the
lookup behind issquatch's "flying over" line.

    curl -fsSLO https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@master/geojson/ne_50m_admin_0_countries.geojson
    python3 tools/make-countries.py ne_50m_admin_0_countries.geojson

The output is JSON: a list of names and a zlib-compressed, base64-encoded 1440x720 grid (a quarter
of a degree per cell, north-west corner first), one byte per cell: 0 for sea, n for names[n-1].
Needs Pillow.
"""
import base64, json, sys, zlib
from pathlib import Path
from PIL import Image, ImageDraw

W, H = 1440, 720
img = Image.new("L", (W, H), 0)
d = ImageDraw.Draw(img)
names = []
features = json.load(open(sys.argv[1]))["features"]
# big countries first, so the small ones drawn after them aren't painted over by a neighbour's edge
features.sort(key=lambda f: -len(json.dumps(f["geometry"])))
for feat in features:
    name = feat["properties"].get("NAME") or feat["properties"].get("ADMIN")
    if name not in names:
        names.append(name)
    idx = names.index(name) + 1
    assert idx < 256
    geom = feat["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        for i, ring in enumerate(poly):
            pts = [((lon + 180) / 360 * W, (90 - lat) / 180 * H) for lon, lat in ring]
            if i == 0:
                d.polygon(pts, fill=idx)
out = Path(__file__).resolve().parent.parent / "share" / "countries.bin"
out.write_text(json.dumps({"w": W, "h": H, "names": names,
                           "grid": base64.b64encode(zlib.compress(img.tobytes(), 9)).decode()}))
print(f"wrote {out}: {len(names)} countries, {out.stat().st_size // 1024} KB")
