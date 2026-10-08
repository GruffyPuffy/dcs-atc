"""Georeference a scanned chart (e.g. a Master Arms kneeboard page) so it can be
overlaid on the live map at the correct scale and position.

A chart is georeferenced from a few **control points**: places whose lat/lon we
know and whose pixel position we can read off the image. We fit an affine
transform lat/lon -> pixel, then **warp** the image into a north-up
equirectangular grid (Leaflet's `imageOverlay` is axis-aligned, so a chart that
is rotated relative to north must be resampled first).

Output: a north-up PNG plus a JSON file with the lat/lon bounds, ready for
`L.imageOverlay(url, [[south, west], [north, east]])`.

Usage:
    uv run --with pillow georef.py --source chart.png --out web/overlays/kutaisi
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

# Kutaisi aerodrome chart (MA kneeboard page 1) control points.
# lat/lon are printed on the chart (P1..P4, holding positions); pixels were read
# off the image. See ATC.md §13.
KUTAISI_CONTROL_POINTS = [
    # (lat, lon, px, py)
    (42 + 10.747 / 60, 42 + 29.353 / 60, 516.8, 371.1),  # P1 Holding C
    (42 + 10.623 / 60, 42 + 28.381 / 60, 236.6, 445.3),  # P2 Holding B
    (42 + 10.754 / 60, 42 + 27.950 / 60, 107.6, 414.4),  # P3 TWY A/N
    (42 + 10.339 / 60, 42 + 28.043 / 60, 147.0, 562.5),  # P4 TWY S/W
]

# The map panel inside the page (excludes the title, coordinate box and tables).
KUTAISI_CROP = (30, 55, 740, 745)


def fit_affine(control_points) -> tuple[np.ndarray, np.ndarray]:
    """Fit lat/lon -> pixel: returns (M (2x2), t (2,)) with px = M @ [lat,lon] + t."""
    a = np.array([[lat, lon, 1.0] for lat, lon, _, _ in control_points])
    bx = np.array([px for _, _, px, _ in control_points])
    by = np.array([py for _, _, _, py in control_points])
    cx = np.linalg.lstsq(a, bx, rcond=None)[0]
    cy = np.linalg.lstsq(a, by, rcond=None)[0]
    return np.array([[cx[0], cx[1]], [cy[0], cy[1]]]), np.array([cx[2], cy[2]])


def warp_north_up(source: Image.Image, control_points, crop,
                  metres_per_pixel: float = 1.5) -> tuple[Image.Image, dict]:
    """Resample the chart into a north-up equirectangular grid.

    Returns the warped image and its lat/lon bounds.
    """
    m, t = fit_affine(control_points)
    m_inv = np.linalg.inv(m)

    def pixel_to_latlon(px, py):
        lat, lon = m_inv @ (np.array([px, py]) - t)
        return lat, lon

    x0, y0, x1, y1 = crop
    # Corners of the crop in lat/lon -> bounding box (north-up grid).
    corners = [pixel_to_latlon(x0, y0), pixel_to_latlon(x1, y0),
               pixel_to_latlon(x0, y1), pixel_to_latlon(x1, y1)]
    lats = [c[0] for c in corners]
    lons = [c[1] for c in corners]
    north, south = max(lats), min(lats)
    east, west = max(lons), min(lons)

    # Output grid resolution from metres/pixel.
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * np.cos(np.radians((north + south) / 2))
    out_h = max(1, int((north - south) * m_per_deg_lat / metres_per_pixel))
    out_w = max(1, int((east - west) * m_per_deg_lon / metres_per_pixel))

    # For every output pixel, find the source pixel and sample.
    out_lats = np.linspace(north, south, out_h)
    out_lons = np.linspace(west, east, out_w)
    lon_grid, lat_grid = np.meshgrid(out_lons, out_lats)
    flat = np.stack([lat_grid.ravel(), lon_grid.ravel()], axis=1)
    src = (flat @ m.T) + t
    src_x = np.clip(np.round(src[:, 0]).astype(int), 0, source.width - 1)
    src_y = np.clip(np.round(src[:, 1]).astype(int), 0, source.height - 1)
    src_arr = np.asarray(source.convert("RGB"))
    warped = src_arr[src_y, src_x].reshape(out_h, out_w, 3)

    bounds = {"north": north, "south": south, "east": east, "west": west}
    return Image.fromarray(warped), bounds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source chart PNG")
    parser.add_argument("--out", required=True,
                        help="output path without extension (writes .png + .json)")
    parser.add_argument("--metres-per-pixel", type=float, default=1.5)
    args = parser.parse_args()

    source = Image.open(args.source)
    warped, bounds = warp_north_up(source, KUTAISI_CONTROL_POINTS,
                                   KUTAISI_CROP, args.metres_per_pixel)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    warped.save(out.with_suffix(".png"))
    out.with_suffix(".json").write_text(json.dumps(bounds, indent=2))
    print(f"wrote {out.with_suffix('.png')} ({warped.width}x{warped.height})")
    print(f"bounds {bounds}")


if __name__ == "__main__":
    main()