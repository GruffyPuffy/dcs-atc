#!/usr/bin/env python3
"""Georeference an aerodrome chart against the **airfield geometry** (click two points).

Serves a single page that shows the chart. Click the two runway ends, press
**Apply**, and it fits a similarity transform to the runway, warps the chart
north-up, writes the overlay, and shows a **check** image (the runway drawn on
top of the warped chart).

**Align to `airspace.json`, not the raw DCS bridge.** Everything the map draws —
CTR, holding zones, the runway-occupancy corridor, the map's own runway markers —
comes from `atc/airspace.json`. DCS's own lat/lon can differ from that file (the
two are close but not identical), so aligning the image to the bridge would put
it off the zones. The zones are what matter (a plane in DCS must line up with
them), so the image is aligned to the *same* coordinates the zones use.

Usage
-----
    uv run --with pillow --with numpy python scripts/georef_tool.py Gudauta \
        --chart /tmp/gudauta.jpg --crop 30 60 1660 1560

Then open the printed URL (served on --picker-port, default 8124).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_AIRSPACE = Path(__file__).resolve().parent.parent / "atc" / "airspace.json"

EARTH_RADIUS_M = 6_371_000.0


# --------------------------------------------------------------------------- #
# Runway geometry (from airspace.json — the same source the zones use)
# --------------------------------------------------------------------------- #

def _reciprocal(name: str) -> str:
    n = int(name)
    return f"{(n + 18) % 36 or 36:02d}"


def _airfield_center(af: dict) -> tuple[float, float]:
    ctr = af.get("ctr", {})
    if "reference" in ctr:
        return tuple(ctr["reference"])
    if "polygon" in ctr and ctr["polygon"]:
        pts = ctr["polygon"]
        return (sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts))
    # fall back to the average of the runway thresholds
    thrs = [r["threshold"] for r in af.get("runways", {}).values()]
    return (sum(t[0] for t in thrs) / len(thrs),
            sum(t[1] for t in thrs) / len(thrs))


def load_runway(airspace_path: Path, airbase: str) -> dict:
    """The primary runway for an airfield from airspace.json.

    Prefers the active runway; resolves both thresholds from the configured
    runway thresholds (so the image aligns to exactly what the map draws).
    """
    data = json.loads(Path(airspace_path).read_text())
    airfields = data.get("airfields", data)
    if airbase not in airfields:
        raise SystemExit(f"airfield {airbase!r} not in {airspace_path} "
                         f"(have: {', '.join(sorted(airfields))})")
    af = airfields[airbase]
    runways = af.get("runways", {})
    if not runways:
        raise SystemExit(f"{airbase}: no runways configured")
    name = af.get("active_runway") or next(iter(runways))
    recip = _reciprocal(name)
    if name not in runways or recip not in runways:
        raise SystemExit(f"{airbase}: runway {name}/{recip} missing thresholds")
    thr = tuple(runways[name]["threshold"])
    thr_recip = tuple(runways[recip]["threshold"])
    # heading from the thresholds (pointing along the named runway)
    heading = (math.degrees(math.atan2(
        (thr_recip[1] - thr[1]) * math.cos(math.radians(thr[0])),
        thr_recip[0] - thr[0])) + 360) % 360
    return {
        "name": name, "recip": recip, "heading": heading,
        "center": _airfield_center(af), "thr": thr, "thr_recip": thr_recip,
    }


# --------------------------------------------------------------------------- #
# Similarity transform + warp
# --------------------------------------------------------------------------- #

def _enu(lat0: float, lon0: float):
    cos0 = math.cos(math.radians(lat0))
    def to_enu(lat: float, lon: float) -> tuple[float, float]:
        return ((lon - lon0) * cos0 * math.pi / 180.0 * EARTH_RADIUS_M,
                (lat - lat0) * math.pi / 180.0 * EARTH_RADIUS_M)
    return to_enu


def fit_similarity(src1, src2, dst1, dst2) -> tuple[float, float, float, float]:
    """px = a·E + b·N + tx ,  py = b·E − a·N + ty   (src pixels are y-down)."""
    dpx, dpy = src2[0] - src1[0], src2[1] - src1[1]
    de, dn = dst2[0] - dst1[0], dst2[1] - dst1[1]
    denom = de * de + dn * dn
    a = (dpx * de - dpy * dn) / denom
    b = (dpx * dn + dpy * de) / denom
    tx = src1[0] - a * dst1[0] - b * dst1[1]
    ty = src1[1] - b * dst1[0] + a * dst1[1]
    return a, b, tx, ty


def warp(source, a, b, tx, ty, ref, crop, metres_per_pixel=1.0):
    """Resample to a north-up grid. Returns (image, bounds, to_warped)."""
    import numpy as np
    from PIL import Image

    lat0, lon0 = ref
    to_enu = _enu(lat0, lon0)
    x0, y0, x1, y1 = crop
    denom = a * a + b * b

    def pixel_to_enu(px, py):
        return ((a * (px - tx) + b * (py - ty)) / denom,
                (b * (px - tx) - a * (py - ty)) / denom)

    es, ns = zip(*(pixel_to_enu(x, y)
                   for x, y in [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]))
    e_min, e_max, n_min, n_max = min(es), max(es), min(ns), max(ns)
    out_w = max(1, int((e_max - e_min) / metres_per_pixel))
    out_h = max(1, int((n_max - n_min) / metres_per_pixel))
    east = e_min + (np.arange(out_w) + 0.5) * metres_per_pixel
    north = n_max - (np.arange(out_h) + 0.5) * metres_per_pixel
    egrid, ngrid = np.meshgrid(east, north)
    sx = np.clip(np.round(a * egrid + b * ngrid + tx).astype(int), 0, source.width - 1)
    sy = np.clip(np.round(b * egrid - a * ngrid + ty).astype(int), 0, source.height - 1)
    warped = np.asarray(source.convert("RGB"))[sy, sx]

    def enu_to_latlon(e, n):
        return (lat0 + math.degrees(n / EARTH_RADIUS_M),
                lon0 + math.degrees(e / (EARTH_RADIUS_M * math.cos(math.radians(lat0)))))
    nw, se = enu_to_latlon(e_min, n_max), enu_to_latlon(e_max, n_min)
    bounds = {"north": nw[0], "west": nw[1], "south": se[0], "east": se[1]}

    def to_warped(lat: float, lon: float) -> tuple[float, float]:
        e, n = to_enu(lat, lon)
        return ((e - e_min) / metres_per_pixel, (n_max - n) / metres_per_pixel)

    return Image.fromarray(warped), bounds, to_warped


# --------------------------------------------------------------------------- #
# Picker page
# --------------------------------------------------------------------------- #

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Georef — {airbase}</title>
<style>
 body {{ margin:0; background:#0d1117; color:#c9d1d9; font:14px system-ui; }}
 #bar {{ position:sticky; top:0; z-index:5; background:#161b22; padding:10px 14px;
   border-bottom:1px solid #30363d; display:flex; gap:14px; align-items:center; }}
 #bar b {{ color:#7ee787; }}
 button {{ font:inherit; padding:6px 14px; cursor:pointer; }}
 #apply {{ background:#238636; color:#fff; border:0; font-weight:600; }}
 #apply:disabled {{ background:#30363d; cursor:not-allowed; }}
 #st {{ margin-left:auto; }}
 #wrap {{ position:relative; display:inline-block; }}
 #wrap img {{ display:block; max-width:100vw; }}
 .pin {{ position:absolute; width:18px; height:18px; margin:-9px 0 0 -9px;
   border:3px solid #f85149; border-radius:50%; pointer-events:none; box-sizing:border-box; }}
 .pin.b {{ border-color:#58a6ff; }}
 #check {{ display:none; padding:14px; }}
 #check img {{ max-width:100vw; border:1px solid #30363d; }}
 .ok {{ color:#7ee787; }} .err {{ color:#f85149; }}
</style></head><body>
<div id="bar">
  <span>Align <b>{airbase}</b> chart to the configured runway {name}/{recip}:</span>
  <span id="instr">click the <b>runway {name}</b> end, then the <b>runway {recip}</b> end</span>
  <button id="clear">Clear</button>
  <button id="apply" disabled>Apply</button>
  <span id="st"></span>
</div>
<div id="wrap"><img id="img" src="/chart"><div id="pins"></div></div>
<div id="check"><div id="chkmsg"></div><img id="chkimg"></div>
<script>
const img=document.getElementById('img'), pins=document.getElementById('pins');
let pts=[];
function redraw(){{
  pins.innerHTML='';
  const sw=img.clientWidth, sh=img.clientHeight;
  pts.forEach((p,i)=>{{
    const d=document.createElement('div'); d.className='pin'+(i===1?' b':'');
    d.style.left=(p.x*sw)+'px'; d.style.top=(p.y*sh)+'px'; pins.appendChild(d);
  }});
}}
function place(ev){{
  const r=img.getBoundingClientRect();
  return {{x:(ev.clientX-r.left)/r.width, y:(ev.clientY-r.top)/r.height}};
}}
img.addEventListener('click',ev=>{{
  if(pts.length>=2) return;
  pts.push(place(ev)); redraw();
  document.getElementById('apply').disabled=pts.length<2;
}});
window.addEventListener('resize',redraw);
img.addEventListener('load',redraw);
document.getElementById('clear').onclick=()=>{{
  pts=[]; redraw(); document.getElementById('apply').disabled=true;
  document.getElementById('st').textContent='';
}};
document.getElementById('apply').onclick=async()=>{{
  document.getElementById('st').textContent='fitting…';
  const r=await fetch('/apply',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{points:pts}})}});
  const j=await r.json();
  const st=document.getElementById('st');
  if(j.ok){{
    st.innerHTML='<span class="ok">✓ '+j.message+'</span>';
    document.getElementById('chkmsg').innerHTML=
      '<span class="ok">Check:</span> the red line is the configured runway (from airspace.json) — it should lie on the chart\\'s runway.';
    const ci=document.getElementById('chkimg'); ci.src='/check?t='+Date.now();
    document.getElementById('check').style.display='block';
  }} else {{
    st.innerHTML='<span class="err">✗ '+j.error+'</span>';
  }}
}};
</script></body></html>"""


class State:
    def __init__(self):
        self.chart_png = b""
        self.chart_ct = "image/jpeg"
        self.check_png = b""
        self.overlay_png = b""
        self.data = {}
        self.fn = None
        self.lock = threading.Lock()
        self.error = ""
        self.message = ""


def _make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body, ct):
            self.send_response(status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, PAGE.format(**state.data).encode(), "text/html")
            elif path == "/chart":
                self._send(200, state.chart_png, state.chart_ct)
            elif path == "/check":
                self._send(200, state.check_png, "image/png")
            else:
                self.send_error(404)

        def do_POST(self):
            if urlsplit(self.path).path != "/apply":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length", 0))
            pts = json.loads(self.rfile.read(n) or b"{}").get("points", [])
            if len(pts) != 2:
                self._send(200, json.dumps({"ok": False, "error": "need two points"}).encode(),
                           "application/json")
                return
            with state.lock:
                try:
                    msg = state.fn(pts)
                    resp = {"ok": True, "message": msg}
                    state.message = msg
                except Exception as exc:  # noqa: BLE001
                    state.error = repr(exc)
                    resp = {"ok": False, "error": repr(exc)}
            self._send(200, json.dumps(resp).encode(), "application/json")

    return Handler


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("airbase")
    ap.add_argument("--chart", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--crop", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                    default=None)
    ap.add_argument("--metres-per-pixel", type=float, default=1.0)
    ap.add_argument("--airspace", default=str(DEFAULT_AIRSPACE),
                    help="airspace.json to align to (default: atc/airspace.json)")
    ap.add_argument("--picker-port", type=int, default=8124)
    args = ap.parse_args()

    from io import BytesIO
    from PIL import Image, ImageDraw

    name = (args.name or args.airbase).lower()
    out_dir = Path(args.out) if args.out else (
        Path(__file__).resolve().parent.parent / "atc" / "web" / "overlays")
    chart_path = Path(args.chart)
    source = Image.open(chart_path)
    crop = tuple(args.crop) if args.crop else (0, 0, source.width, source.height)

    rwy = load_runway(Path(args.airspace), args.airbase)
    lat0, lon0 = rwy["center"]
    to_enu = _enu(lat0, lon0)
    thr, thr_recip = to_enu(*rwy["thr"]), to_enu(*rwy["thr_recip"])

    print(f"{args.airbase}: runway {rwy['name']}/{rwy['recip']} "
          f"heading {rwy['heading']:.0f}° (aligned to {Path(args.airspace).name})")

    state = State()
    buf = BytesIO()
    source.save(buf, format="PNG")
    state.chart_png = buf.getvalue()
    state.data = {"airbase": args.airbase, "name": rwy["name"], "recip": rwy["recip"]}

    def do_apply(points):
        # points are normalized (0..1) -> source pixels
        p1 = (points[0]["x"] * source.width, points[0]["y"] * source.height)
        p2 = (points[1]["x"] * source.width, points[1]["y"] * source.height)
        a, b, tx, ty = fit_similarity(p1, p2, thr, thr_recip)
        scale, rot = math.hypot(a, b), math.degrees(math.atan2(b, a))
        warped, bounds, to_warped = warp(source, a, b, tx, ty, (lat0, lon0), crop,
                                         args.metres_per_pixel)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.json").write_text(json.dumps(bounds, indent=2))
        warped.save(out_dir / f"{name}.png")

        check = warped.copy()
        d = ImageDraw.Draw(check)
        t1, t2 = to_warped(*rwy["thr"]), to_warped(*rwy["thr_recip"])
        d.line([t1, t2], fill=(255, 0, 0), width=3)
        for t in (t1, t2):
            d.ellipse((t[0] - 12, t[1] - 12, t[0] + 12, t[1] + 12),
                      outline=(255, 0, 0), width=3)
        cb = BytesIO()
        check.save(cb, format="PNG")
        state.check_png = cb.getvalue()
        (out_dir / f"{name}_check.png").write_bytes(state.check_png)
        (out_dir / f"{name}.georef.json").write_text(json.dumps({
            "airbase": args.airbase, "runway": rwy["name"],
            "click_px": [list(p1), list(p2)], "chart": chart_path.name,
            "px_per_m": scale, "rotation_deg": rot, "bounds": bounds}, indent=2))
        print(f"  clicks {p1[0]:.0f},{p1[1]:.0f} {p2[0]:.0f},{p2[1]:.0f} | "
              f"{scale:.4f} px/m, rotation {rot:+.2f}° -> {out_dir}/{name}.png")
        return f"wrote {name}.png ({warped.width}x{warped.height}), {scale:.3f} px/m, {rot:+.1f}°"

    state.fn = do_apply
    server = ThreadingHTTPServer(("127.0.0.1", args.picker_port), _make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"picker: http://127.0.0.1:{args.picker_port}/")
    print("open that URL (VS Code integrated browser), click the two runway ends, Apply.")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
