#!/usr/bin/env python3
"""Rasterise Natural Earth's 1:110m land polygons (public domain) into share/land.bits.

    curl -fsSLO https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@master/geojson/ne_110m_land.geojson
    python3 tools/make-land.py ne_110m_land.geojson

The output is a 720x360 equirectangular bitmap (half a degree per pixel, north-west corner
first), one bit per pixel, packed MSB first and base64-encoded. Needs Pillow.
"""
import base64, json, sys
from pathlib import Path
from PIL import Image, ImageDraw

W, H = 720, 360
img = Image.new("1", (W, H), 0)
d = ImageDraw.Draw(img)
for feat in json.load(open(sys.argv[1]))["features"]:
    geom = feat["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        for i, ring in enumerate(poly):
            pts = [((lon + 180) / 360 * W, (90 - lat) / 180 * H) for lon, lat in ring]
            d.polygon(pts, fill=0 if i else 1)
out = Path(__file__).resolve().parent.parent / "share" / "land.bits"
out.parent.mkdir(exist_ok=True)
out.write_text(base64.b64encode(img.tobytes()).decode() + "\n")
print(f"wrote {out} ({W}x{H})")
