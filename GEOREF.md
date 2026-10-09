# Georeferencing charts (map overlay)

How to take a scanned aerodrome chart (a Master Arms kneeboard page, a Hoggit
TERPS chart, …) and overlay it on the live map at the correct scale and
position.

> See also `ATC.md` §13 (Live map view → Chart overlay) for how the overlay is
> consumed by the map.

---

## 0. TL;DR — the recommended way

Use **`scripts/georef_tool.py`**: it shows the chart in a browser page, you
**click the two runway ends**, and it writes the overlay. Two clicks, no control
points, no guessing.

```bash
uv run --with pillow --with numpy python scripts/georef_tool.py Gudauta \
    --chart /tmp/gudauta.jpg --crop 30 60 1660 1560
# open the printed URL (http://127.0.0.1:8124/) in VS Code's integrated browser,
# click the runway 15 end, then the runway 33 end, Apply.
```

It writes `atc/web/overlays/<name>.png` + `.json` (plus a `_check.png`). The map
picks it up automatically by airfield name.

**What it aligns to — important.** The chart is aligned to the airfield geometry
in **`atc/airspace.json`** (the runway thresholds), *not* to the raw DCS bridge
and *not* to the real-world map. That is deliberate: everything the map draws —
the CTR, holding zones, the runway-occupancy corridor, the runway markers — comes
from `airspace.json`, and a plane in DCS lines up with those zones. DCS's own
lat/lon and the real-world (OSM) positions can both differ from `airspace.json`
(the Caucasus terrain is offset from reality by ~1 km at Gudauta; Kutaisi is much
closer). Aligning the image to `airspace.json` makes the overlay agree with the
zones, which is what matters for the trainer. OSM alignment is *not* the goal.

**Two points → a similarity.** A scanned chart is a *similarity* of the ground:
uniform scale + rotation, no shear. Two points (the runway ends) fully determine
it. This also avoids the shear that a 3-point affine fit introduces (that shear
was the source of an earlier "wrong angle/scale" bug). If your two clicks are
slightly off, the fit simply rotates/scales a touch — it stays a valid chart.

---

## 1. Why the overlay needs warping at all

Leaflet's `L.imageOverlay(url, [[south, west], [north, east]])` places an image
on an **axis-aligned** lat/lon rectangle. That only works if the image is a
north-up, linear (equirectangular / "plate carrée") lat/lon picture.

A chart is **not** north-up: the runway is drawn along its true bearing, so the
whole chart is rotated relative to north (Gudauta ~5°, self-consistent with
`airspace.json`). If you drop the raw scan in, it lands skewed and mis-scaled.

So the job is two parts:

1. **Fit** a transform from chart pixels to local east/north metres.
2. **Warp** the image into a north-up grid, then hand Leaflet the resulting PNG
   plus its lat/lon bounds.

`scripts/georef_tool.py` does both. (`atc/georef.py` is the older control-point
tool; the click tool supersedes it for new charts.)

---

## 2. Where the charts come from

| Source | What | Notes |
| --- | --- | --- |
| Master Arms kneeboard | `https://wiki.masterarms.se/images/f/f8/MA_Kutaisi_CTA_kneeboard.zip` | Page 1 = aerodrome chart. Has a lat/lon graticule **and** printed coordinates (P1–P4, ARP, thresholds). |
| Hoggit DCS wiki | `https://wiki.hoggitworld.com/view/<Airfield>` | TERPS aerodrome chart (e.g. `Gudauta`). Also has a graticule + printed P1/P2/P3, ARP, thresholds. |

Download, unzip, and note the page you want (usually the aerodrome chart).

---

## 3. The pipeline (click tool)

### Step 1 — Pick the crop

The chart page includes title, coordinate box and tables. Crop to the **map
panel** so the overlay doesn't include the page furniture. Open the chart, note
the pixel rectangle, pass it as `--crop x0 y0 x1 y1`.

### Step 2 — Click the two runway ends

Run the tool; it shows the chart. Click the **runway `<name>` end first, then the
`<recip>` end** (the page tells you which; e.g. runway 15 end then runway 33
end). The two points are sent to the server, which fits the similarity to the
`airspace.json` thresholds, warps, writes the overlay, and shows a **check**
image: the configured runway drawn in red over the warped chart. The red line
should lie on the chart's drawn runway — it will, by construction, if you clicked
the ends.

### Step 3 — Verify on the map

Open the map (the bot's `--map-port`, or `uv run map_server.py --airfield <name>
--port <p>`), tick **Chart**, and check the chart runway sits on the zones
(orange holding circles, red occupancy corridor). See `_check.png` for the fit.

### Advanced — the older control-point tool

`atc/georef.py` fits an **affine** from printed control points (lat/lon + pixel)
and warps. It's fine for charts with a good set of *consistent* control points,
but an affine can absorb control-point error as shear (the earlier Gudauta bug).
Prefer the click tool.

---

## 4. What is on file

### Gudauta (Hoggit TERPS chart, 1700×2448)

Aligned from two clicks (`--crop 30 60 1660 1560`): runway 15 end `553,397`,
runway 33 end `1131,1435`. Fit: **0.4787 px/m, rotation −5.42°**. Output
`web/overlays/gudauta.png`. (The −5.4° is the difference between the chart's
drawn runway and `airspace.json`; the fit reconciles them.)

### Kutaisi (MA kneeboard page 1)

Georeferenced earlier; see `atc/georef.py` `CHARTS["kutaisi"]` and
`web/overlays/kutaisi.json`.

---

## 5. Gotchas

- **Align to `airspace.json`, not DCS-bridge or OSM.** The zones come from
  `airspace.json`; a plane in DCS lines up with them. DCS and reality can each be
  ~1 km off (Gudauta); the image should match the *zones*, not the base map.
- **Charts are rotated.** Always warp to north-up; never feed the raw scan to
  `imageOverlay`.
- **A similarity needs only two points.** Don't reach for an affine — it can turn
  a bad point into shear (wrong scale/angle).
- **The graticule is dotted/faint.** Naive line detection catches page borders
  and table rules. The click tool avoids the whole problem.
- **Set the crop** to the map panel, or the overlay includes the title/legend.
- **Attribute the source** when shipping someone else's chart (MA / Hoggit).
