// Live ATC map: airfield geometry + aircraft with their flight phase.
// Polls /api/atc and redraws. Leaflet is loaded from a CDN in index.html.

const PHASE_COLOR = {
  ground: '#d29922',   // Ground agency
  tower: '#3fb950',    // Tower agency
  control: '#58a6ff',  // Control agency
};

let map, ctrLayer, staticLayer, aircraftLayer, aiLayer, chartLayer, pathLayer, commLayer;
let bullseyeLayer;  // STT readings, candidate repairs, and actual-position rings
let bullseyeCompassLayer;  // shown only while inspecting an actual-position popup
let baseLayer;  // the base-map tile layer (OSM or DCS); swapped on config change
let baseLayerName = '';
let vectorSpec = null;     // dcsmaps WMS overlay (airbases/towns/navaids)
let vectorOverlay = null;  // current single-tile ImageOverlay
let vectorPending = null;  // next image; swapped in only after it loads
let vectorsOn = true;
let vectorTimer = null;
const markers = new Map();  // callsign -> L.marker
const aiMarkers = new Map();
const paths = new Map();    // callsign -> L.layerGroup of path segments
const commMarkers = new Map();  // callsign -> L.layerGroup
// Which comm popup the user wants open (null = none). The map redraws every
// 2 s, so we reopen only the popup the user actually opened, and a map click
// clears this so a dismissed popup stays dismissed.
let commPopupKey = null;
let bullseyePopupKey = null;
let bullseyePopup = null;

function initMap(center) {
  map = L.map('map', { preferCanvas: true }).setView(center, 11);
  window.map = map;  // handy for debugging in the browser console
  // WMS vectors sit above the DCS tiles (tilePane 200) and below every ATC
  // overlay (overlayPane 400): CTR, chart, paths, traffic.
  map.createPane('dcsVectors');
  map.getPane('dcsVectors').style.zIndex = 250;
  map.getPane('dcsVectors').style.pointerEvents = 'none';
  map.on('moveend', scheduleVectors);
  map.on('zoomend', scheduleVectors);
  // Clicking the map (not a marker) dismisses any open comm popup for good.
  map.on('click', () => {
    commPopupKey = null;
    bullseyePopupKey = null;
    bullseyePopup = null;
    clearBullseyeCompass();
  });
  // The base layer is set from the server payload (OSM or DCS tiles) on the
  // first snapshot; start with OSM so the map renders immediately.
  baseLayer = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
  baseLayerName = 'osm';
  chartLayer = L.layerGroup().addTo(map);
  ctrLayer = L.layerGroup().addTo(map);
  staticLayer = L.layerGroup().addTo(map);
  pathLayer = L.layerGroup().addTo(map);
  commLayer = L.layerGroup().addTo(map);
  bullseyeLayer = L.layerGroup().addTo(map);
  bullseyeCompassLayer = L.layerGroup().addTo(map);
  aiLayer = L.layerGroup().addTo(map);
  aircraftLayer = L.layerGroup().addTo(map);
  document.getElementById('chart-toggle').addEventListener('change', (e) => {
    if (e.target.checked) chartLayer.addTo(map); else map.removeLayer(chartLayer);
  });
  document.getElementById('vectors-toggle').addEventListener('change', (e) => {
    vectorsOn = e.target.checked;
    if (vectorsOn) refreshVectors();
    else clearVectors();
  });
  initChatter();
}

function drawOverlay(af) {
  chartLayer.clearLayers();
  if (!af.overlay) return;
  L.imageOverlay(af.overlay.url, af.overlay.bounds, { opacity: 0.85 })
    .addTo(chartLayer);
}

// Swap the base-map tile layer (OSM <-> DCS tiles) when the server config says
// so. Only rebuilds when the choice actually changes.
function setBaseMap(bm) {
  if (!bm || !bm.url) {
    setVectorOverlay(null);
    return;
  }
  if (bm.name !== baseLayerName) {
    if (baseLayer) map.removeLayer(baseLayer);
    baseLayer = L.tileLayer(bm.url, {
      maxZoom: bm.maxZoom || 19,
      maxNativeZoom: bm.maxNativeZoom || bm.maxZoom || 19,
      attribution: bm.attribution || '',
      tms: !!bm.tms,
      noWrap: true,
    }).addTo(map);
    baseLayerName = bm.name;
  }
  setVectorOverlay(bm.wms || null);
}

function setVectorOverlay(wms) {
  const wrap = document.getElementById('vectors-toggle-wrap');
  if (wrap) wrap.hidden = !wms;
  const same = !!(wms && vectorSpec
    && wms.url === vectorSpec.url && wms.layers === vectorSpec.layers);
  vectorSpec = wms;
  if (!wms || !vectorsOn) {
    clearVectors();
    return;
  }
  // Do not refetch on the 2 s poll — only on first enable / pan / zoom.
  if (!same || (!vectorOverlay && !vectorPending)) refreshVectors();
}

function scheduleVectors() {
  if (vectorTimer) clearTimeout(vectorTimer);
  vectorTimer = setTimeout(refreshVectors, 150);
}

function clearVectors() {
  if (vectorPending) {
    map.removeLayer(vectorPending);
    vectorPending = null;
  }
  if (vectorOverlay) {
    map.removeLayer(vectorOverlay);
    vectorOverlay = null;
  }
}

function wmsGetMapUrl(wms, bounds, size) {
  const sw = bounds.getSouthWest();
  const ne = bounds.getNorthEast();
  const w = Math.max(64, Math.min(2048, Math.round(size.x)));
  const h = Math.max(64, Math.min(2048, Math.round(size.y)));
  const params = new URLSearchParams({
    SERVICE: 'WMS', VERSION: '1.1.1', REQUEST: 'GetMap',
    LAYERS: wms.layers, STYLES: '', FORMAT: 'image/png',
    TRANSPARENT: 'TRUE', SRS: 'EPSG:4326',
    BBOX: `${sw.lng},${sw.lat},${ne.lng},${ne.lat}`,
    WIDTH: String(w), HEIGHT: String(h),
  });
  const sep = wms.url.includes('?') ? '&' : '?';
  return `${wms.url}${sep}${params}`;
}

function refreshVectors() {
  if (!map || !vectorSpec || !vectorsOn) return;
  const bounds = map.getBounds();
  const size = map.getSize();
  if (size.x < 32 || size.y < 32) return;
  const url = wmsGetMapUrl(vectorSpec, bounds, size);
  // Keep the current overlay georeferenced until the new image is ready.
  // setBounds+setUrl stretched the old pixels across the new view (flash).
  if (vectorPending) {
    map.removeLayer(vectorPending);
    vectorPending = null;
  }
  const incoming = L.imageOverlay(url, bounds, {
    pane: 'dcsVectors', interactive: false, opacity: 1,
  });
  vectorPending = incoming;
  incoming.once('load', () => {
    if (vectorPending !== incoming) return;
    if (vectorOverlay) map.removeLayer(vectorOverlay);
    vectorOverlay = incoming;
    vectorPending = null;
  });
  incoming.once('error', () => {
    if (vectorPending === incoming) vectorPending = null;
    map.removeLayer(incoming);
  });
  incoming.addTo(map);
}

function drawAirspace(af) {
  ctrLayer.clearLayers();
  staticLayer.clearLayers();

  if (af.ctr?.polygon?.length) {
    L.polygon(af.ctr.polygon, {
      color: '#58a6ff', weight: 1.5, fillColor: '#58a6ff', fillOpacity: 0.06,
    }).bindTooltip(`${af.name} CTR · surface–${af.ctr.ceiling_ft_agl} ft AGL`)
      .addTo(ctrLayer);
  }

  for (const [name, pos] of Object.entries(af.gates || {})) {
    L.circleMarker(pos, { radius: 5, color: '#c9d1d9', weight: 1.5,
      fillColor: '#0d1117', fillOpacity: 1 })
      .bindTooltip(`Entry/Exit ${name}`).addTo(staticLayer);
  }

  for (const [name, pos] of Object.entries(af.runways || {})) {
    L.circleMarker(pos, { radius: 4, color: '#f0883e', weight: 2,
      fillColor: '#f0883e', fillOpacity: 0.9 })
      .bindTooltip(`Runway ${name} threshold`).addTo(staticLayer);
  }

  // Taxi routes are per-runway/per-ramp names (no geometry), so show them as
  // a tooltip on each ramp rather than a marker. Runway numbers come from the
  // airfield (not hardcoded) so this works for any field (Kutaisi 25/07,
  // Gudauta 15/33, ...).
  const rwys = Object.keys(af.taxi_routes || {});
  for (const [name, pos] of Object.entries(af.parking_areas || {})) {
    const lines = rwys.map(r => `to ${r}: ${(af.taxi_routes[r] || {})[name] || '—'}`);
    const tip = [name, ...lines].join('<br>');
    L.circleMarker(pos, { radius: 4, color: '#6e7681', weight: 1,
      fillColor: '#6e7681', fillOpacity: 0.8 })
      .bindTooltip(tip).addTo(staticLayer);
  }

  // Runway holding positions (P1..P4 on the MA chart).
  for (const [name, pos] of Object.entries(af.holding_points || {})) {
    L.circleMarker(pos, { radius: 5, color: '#f85149', weight: 2,
      fillColor: '#0d1117', fillOpacity: 1 })
      .bindTooltip(`Holding position: ${name}`).addTo(staticLayer);
  }

  // Trainer check areas: exactly where the bot accepts a report / detects
  // occupancy, derived from the same parameters the brain uses.
  const checks = af.checks || {};
  for (const zone of checks.holding || []) {
    L.circle(zone.center, {
      radius: zone.radius_nm * 1852, color: '#d29922', weight: 1,
      dashArray: '4 4', fillColor: '#d29922', fillOpacity: 0.05,
    }).bindTooltip(`Holding check: ${zone.label}`).addTo(staticLayer);
  }
  // Departure "good exit" circles: a departure within this radius of its
  // assigned gate counts as having used the exit (mirrors the brain's check).
  for (const zone of checks.exit || []) {
    L.circle(zone.center, {
      radius: zone.radius_nm * 1852, color: '#3fb950', weight: 1,
      dashArray: '6 4', fillColor: '#3fb950', fillOpacity: 0.05,
    }).bindTooltip(`Good exit: ${zone.label} (within ${zone.radius_nm} NM)`)
      .addTo(staticLayer);
  }
  if (checks.final) {
    // A filled sector (wedge) from the threshold out to the check radius,
    // spanning ±30° of the approach — exactly the area is_on_final accepts.
    const f = checks.final;
    const c = f.center;
    const pts = [c];
    const steps = 24;
    let end = f.end_deg;
    if (end < f.start_deg) end += 360;  // handle wrap past north
    for (let i = 0; i <= steps; i++) {
      const deg = f.start_deg + (end - f.start_deg) * (i / steps);
      const rad = deg * Math.PI / 180;
      const dLat = (f.radius_nm * 1852 * Math.cos(rad)) / 111320;
      const dLon = (f.radius_nm * 1852 * Math.sin(rad))
        / (111320 * Math.cos(c[0] * Math.PI / 180));
      pts.push([c[0] + dLat, c[1] + dLon]);
    }
    L.polygon(pts, {
      color: '#58a6ff', weight: 1, dashArray: '6 4',
      fillColor: '#58a6ff', fillOpacity: 0.10,
    }).bindTooltip(`Final approach check · runway ${f.runway} `
      + `(${f.radius_nm} NM, ±30°)`).addTo(staticLayer);
  }
  // Pattern turn slices: one short-arc sector per runway end (pattern side),
  // from abeam back to the edge of the final cone. Future landing-clearance
  // arming zones — rendered for testing, both sides shown.
  const PATTERN_COLORS = { left: '#e3b341', right: '#39d2c0' };
  for (const zone of checks.pattern || []) {
    const c = zone.center;
    const pts = [c];
    const steps = 12;
    let end = zone.end_deg;
    if (end < zone.start_deg) end += 360;  // handle wrap past north
    for (let i = 0; i <= steps; i++) {
      const deg = zone.start_deg + (end - zone.start_deg) * (i / steps);
      const rad = deg * Math.PI / 180;
      const dLat = (zone.radius_nm * 1852 * Math.cos(rad)) / 111320;
      const dLon = (zone.radius_nm * 1852 * Math.sin(rad))
        / (111320 * Math.cos(c[0] * Math.PI / 180));
      pts.push([c[0] + dLat, c[1] + dLon]);
    }
    const color = PATTERN_COLORS[zone.side] || '#e3b341';
    L.polygon(pts, {
      color, weight: 1, dashArray: '5 3',
      fillColor: color, fillOpacity: 0.12,
    }).bindTooltip(`Pattern slice · runway ${zone.runway} `
      + `(${zone.side}, ${zone.radius_nm} NM)`).addTo(staticLayer);
  }
  if (checks.runway) {
    L.polygon(checks.runway.corners, {
      color: '#f85149', weight: 1, dashArray: '4 4',
      fillColor: '#f85149', fillOpacity: 0.08,
    }).bindTooltip('Runway occupancy corridor').addTo(staticLayer);
  }
  // Line-up corridor: the area at the threshold end where the bot accepts the
  // aircraft as lined up and issues the takeoff clearance (mirrors is_lined_up).
  for (const zone of checks.lineup || []) {
    L.polygon(zone.corners, {
      color: '#a371f7', weight: 2, dashArray: '2 3',
      fillColor: '#a371f7', fillOpacity: 0.25,
    }).bindTooltip(`Line-up check · runway ${zone.runway} `
      + `(takeoff clearance issued here)`).addTo(staticLayer);
  }

  drawWind(af);
  drawBullseye(af);
}

function drawBullseye(af) {
  // Coalition bullseye (main reference point) from the mission. Drawn as a
  // small concentric-ring "target" symbol, sized like the other point markers
  // (the entry gates are 5 px) so it reads as a reference point, not a zone.
  // Blue, because we fly blue. Absent when the mission has no bullseye set (or
  // an older debrief without one).
  if (!af.bullseye || af.bullseye.length !== 2) return;
  const pos = [af.bullseye[0], af.bullseye[1]];
  // Rings in screen pixels (circleMarker), so the symbol keeps its size at any
  // zoom — a geographic circle would balloon when zoomed out.
  for (const r of [4, 8, 12]) {
    L.circleMarker(pos, {
      radius: r, color: '#1f6feb', weight: 1.5, opacity: 0.9,
      fill: false, interactive: false,
    }).addTo(staticLayer);
  }
  L.circleMarker(pos, {
    radius: 2, color: '#1f6feb', weight: 1, fillColor: '#1f6feb',
    fillOpacity: 1,
  }).bindTooltip('Bullseye (blue)').addTo(staticLayer);
}

// Bullseye picture-call tolerance, mirroring the brain's constants
// (BULLSEYE_TOLERANCE_DEG / _NM). Kept in sync by hand — the debug overlay is
// a visual aid, not the authority (the brain does the actual check).
const BULLSEYE_TOL_DEG = 10;
const BULLSEYE_TOL_NM = 3;

// Spoken number words -> digits, mirroring the brain's tables. STT writes what
// it hears ("bullseye zero six five for four zero"), so the overlay has to fold
// the words the same way the brain does or a spoken call would draw nothing.
const NUMBER_WORDS = {
  zero: '0', one: '1', two: '2', three: '3', four: '4', five: '5', six: '6',
  seven: '7', eight: '8', nine: '9', niner: '9',
  ten: '10', eleven: '11', twelve: '12', thirteen: '13', fourteen: '14',
  fifteen: '15', sixteen: '16', seventeen: '17', eighteen: '18',
  nineteen: '19',
  twenty: '20', thirty: '30', forty: '40', fifty: '50', sixty: '60',
  seventy: '70', eighty: '80', ninety: '90',
};
const TEENS = new Set(['ten', 'eleven', 'twelve', 'thirteen', 'fourteen',
  'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen']);
const TENS = new Set(['twenty', 'thirty', 'forty', 'fifty', 'sixty',
  'seventy', 'eighty', 'ninety']);
const FIGURE_WORD = Object.keys(NUMBER_WORDS)
  .sort((a, b) => b.length - a.length).join('|');

function spokenFigure(phrase) {
  // "four forty" -> "440", "zero six five" -> "065", "thirty two" -> "32".
  // Mirrors the brain's `_spoken_figure` (including the tens/teens tail rule).
  const words = phrase.trim().split(/\s+/);
  if (!words.length || words.some((w) => !(w in NUMBER_WORDS))) return '';
  if (words.length >= 2 && TENS.has(words[words.length - 2])
      && words[words.length - 1] in NUMBER_WORDS
      && !TENS.has(words[words.length - 1])
      && !TEENS.has(words[words.length - 1])) {
    const head = words.slice(0, -2).map((w) => NUMBER_WORDS[w]).join('');
    return head + String(Number(NUMBER_WORDS[words[words.length - 2]])
                         + Number(NUMBER_WORDS[words[words.length - 1]]));
  }
  const last = words[words.length - 1];
  if (TENS.has(last) || TEENS.has(last)) {
    return words.slice(0, -1).map((w) => NUMBER_WORDS[w]).join('')
      + NUMBER_WORDS[last];
  }
  return words.map((w) => NUMBER_WORDS[w]).join('');
}

function foldPictureFigures(text) {
  // Fold the bearing/range of a bullseye or marking call, mirroring the brain's
  // `_fold_bullseye_figures`. Only runs on a bullseye/marking call — the range
  // separator ("for"/"at"/",") is far too common to fold globally.
  if (!/\b(bull'?s?eye|marking)\b/i.test(text || '')) return text || '';
  const re = new RegExp(
    `\\b(bull'?s?eye|marking)((?:\\s+(?!(?:${FIGURE_WORD})\\b)[a-z]+)?)`
    + `([\\s,]+)((?:(?:${FIGURE_WORD})\\s*){1,4})`
    + `(?:(for|at|,)([\\s,]*)((?:(?:${FIGURE_WORD})\\s*){1,4}))?`, 'i');
  // A figure group carries the whitespace the regex allowed after it; keep that
  // whitespace so the next word stays separate (the brain's `_fold_spoken_
  // figures` welded "channel 7push" the same way).
  const fold = (spoken, pad) => {
    const trailing = spoken.slice(spoken.trimEnd().length);
    const digits = spokenFigure(spoken);
    if (!digits) return spoken;
    return (pad ? digits.padStart(3, '0') : String(Number(digits))) + trailing;
  };
  return (text || '').replace(re, (all, kw, point, sep, bearing, rsep, rws,
                                   range) => {
    let out = kw + (point || '') + sep + fold(bearing, true);
    if (range == null) return out;
    return out + (rsep || '') + (rws || '') + fold(range, false);
  });
}

function foldAngels(text) {
  // "angels five" -> "angels 5". The brain folds this in the general
  // `_fold_spoken_figures` pass, before the bullseye pass; the overlay needs
  // the same so a spoken angels shows in the tooltip.
  const re = new RegExp(`\\bangels\\s+((?:(?:${FIGURE_WORD})\\s*){1,4})`, 'i');
  return (text || '').replace(re, (all, spoken) => {
    const trailing = spoken.slice(spoken.trimEnd().length);
    const digits = spokenFigure(spoken);
    return digits ? 'angels ' + String(Number(digits)) + trailing : all;
  });
}

function parsePictureCall(text, af) {
  // "bullseye 065 for 40, angels 15" or "marking Kutaisi 200 for 20" ->
  // {point, lat, lon, bearing, range, angels}. Mirrors the brain's parsers so
  // the drawn arrow can never disagree with what was checked. Returns null when
  // the call names a point we cannot resolve (the brain acknowledges those
  // without a check, so there is nothing to draw).
  const t = foldAngels(foldPictureFigures(text));
  const m = /\bmarking\s+([a-z']+)\s*,?\s*(\d{2,3})\s*(?:for|at|,)\s*(\d{1,3})\b/i
    .exec(t)
    || /\bbull'?s?eye\b\s*,?\s*(\d{2,3})\s*(?:for|at|,)\s*(\d{1,3})/i.exec(t);
  if (!m) return null;
  let point, lat, lon;
  if (m.length === 4) {
    const name = m[1].toLowerCase();
    if (['bull', 'bulls', 'bullseye', "bull's"].includes(name)) {
      if (!af.bullseye || af.bullseye.length !== 2) return null;
      point = 'bullseye';
      [lat, lon] = af.bullseye;
    } else if (af.name && (name === af.name.toLowerCase()
        || (af.name.toLowerCase() === 'kutaisi' && name === 'kutasi'))) {
      point = af.name;
      [lat, lon] = af.center;
    } else {
      return null;  // unknown point: the brain does not verify it either
    }
  } else {
    if (!af.bullseye || af.bullseye.length !== 2) return null;
    point = 'bullseye';
    [lat, lon] = af.bullseye;
  }
  const bearing = Number(m[m.length - 2]);
  const range = Number(m[m.length - 1]);
  const a = /\bangels\s*,?\s*(\d{1,3})\b/i.exec(t);
  return { point, lat, lon, bearing, range,
           angels: a ? Number(a[1]) : null };
}

function drawBullseyeCalls(aircraft, af) {
  // Show the literal STT reading and plausible "for"-joined repairs. The
  // recorded position marks which interpretation, if any, agrees with the
  // flight; it does not erase the raw reading from the review.
  //
  // Colours are chosen to stay visible on the map's green/tan terrain: the
  // arrow is amber, repaired readings blue, and the accuracy ring magenta (a
  // green ring vanished against the terrain).
  bullseyeLayer.clearLayers();
  for (const ac of aircraft) {
    for (const c of (ac.comms || [])) {
      const call = parsePictureCall(c.text, af);
      if (!call) continue;
      const bearings = [call.bearing];
      const ranges = [call.range];
      const withoutJoinedFour = (value) => {
        if (value >= 400 && value < 500) return value % 100;
        if (value >= 40 && value < 50) return value % 10;
        return null;
      };
      const bearingVariant = withoutJoinedFour(call.bearing);
      const rangeVariant = withoutJoinedFour(call.range);
      if (bearingVariant != null) bearings.push(bearingVariant);
      if (rangeVariant != null) ranges.push(rangeVariant);
      const candidates = bearings.flatMap((bearing) =>
        ranges.map((range) => [bearing, range]));
      const actual = c.lat != null && c.lon != null
        ? bearingRange(call.lat, call.lon, c.lat, c.lon) : null;
      const matches = actual ? candidates.filter(([bearing, range]) =>
        Math.abs((bearing - actual[0] + 180) % 360 - 180)
          <= BULLSEYE_TOL_DEG
        && Math.abs(range - actual[1]) <= BULLSEYE_TOL_NM) : [];
      const raw = candidates[0];
      const rawMatches = matches.some(([bearing, range]) =>
        bearing === raw[0] && range === raw[1]);
      // If a repair matches the recorded position, it is the useful alternate
      // to show. If none match (or there is no recorded position), show every
      // plausible repair so the review makes the uncertainty visible.
      const repairs = candidates.slice(1);
      const repairsToDraw = rawMatches ? [] : (matches.length
        ? matches.filter(([bearing, range]) =>
          bearing !== raw[0] || range !== raw[1])
        : repairs);
      const angels = call.angels != null ? `, angels ${call.angels}` : '';
      const stamp = ` · ${c.t || ''}`;

      const drawArrow = (bearing, range, repaired, matched) => {
        const tip = destMeters(call.lat, call.lon, bearing, range * 1852);
        const bearingText = String(bearing).padStart(3, '0');
        let label = `${repaired ? 'Possible STT repair' : 'STT reading'}: `
          + `${call.point} ${bearingText} for ${range}${angels}`;
        if (repaired) {
          label += ` (from ${String(raw[0]).padStart(3, '0')} for ${raw[1]})`;
          label += actual ? (matched ? ' · matches recorded position'
                                     : ' · does not match recorded position')
            : ' · position unavailable';
        }
        label += stamp;
        const color = repaired ? '#58a6ff' : '#d29922';
        const style = { color, weight: 2, opacity: 0.95 };
        if (repaired) style.dashArray = '6 5';
        const popupKeyBase = `${ac.callsign}|${c.t || ''}|${c.lat},${c.lon}|`
          + `${c.text}|${repaired ? 'repair' : 'raw'}|${bearing},${range}`;
        const mainLine = L.polyline([[call.lat, call.lon], tip], {
          ...style, bubblingMouseEvents: false,
        });
        mainLine.addTo(bullseyeLayer);
        bindBullseyePopup(mainLine, `${popupKeyBase}|line`, label);
        for (const side of [-150, 150]) {
          const barb = destMeters(tip[0], tip[1], bearing + side, 2500);
          const arrowhead = L.polyline([tip, barb], {
            ...style, bubblingMouseEvents: false,
          });
          arrowhead.addTo(bullseyeLayer);
          bindBullseyePopup(arrowhead, `${popupKeyBase}|head:${side}`, label);
        }
        const dot = L.circleMarker(tip, {
          radius: 4, color, weight: 2, fillColor: color,
          fillOpacity: 0.9, bubblingMouseEvents: false,
        });
        dot.addTo(bullseyeLayer);
        bindBullseyePopup(dot, `${popupKeyBase}|dot`, label);
      };

      drawArrow(raw[0], raw[1], false, rawMatches);
      for (const [bearing, range] of repairsToDraw) {
        const matched = matches.some(([b, r]) => b === bearing && r === range);
        drawArrow(bearing, range, true, matched);
      }
      // Where the aircraft *actually* was, with the tolerance as a ring.
      if (c.lat != null && c.lon != null) {
        const actualBRA = bearingRange(call.lat, call.lon, c.lat, c.lon);
        const ring = L.circle([c.lat, c.lon], {
          radius: BULLSEYE_TOL_NM * 1852, color: '#db61a2', weight: 2,
          opacity: 0.95, dashArray: '4 4', fill: false,
          bubblingMouseEvents: false,
        });
        const actualBearing = String(Math.round(actualBRA[0]) % 360)
          .padStart(3, '0');
        const actualRange = Math.round(actualBRA[1]);
        const altitude = actualAngelsAt(ac, c);
        const angels = altitude == null ? ''
          : `, Angels ${Math.round(altitude / 1000)}`;
        const popupText = `Actual ${call.point}: ${actualBearing} for `
          + `${actualRange}${angels}`;
        const popupKey = `${ac.callsign}|${c.t || ''}|${c.lat},${c.lon}|`
          + `${c.text}|actual`;
        ring.addTo(bullseyeLayer);
        const showCompass = () => drawActualBullseyeCompass(
          call.lat, call.lon, c.lat, c.lon, actualBRA[0]);
        bindBullseyePopup(ring, popupKey, popupText, showCompass);
      }
    }
  }
}

function bindBullseyePopup(layer, key, text, onClick = null) {
  layer.on('click', (event) => {
    if (bullseyePopupKey === key && bullseyePopup
        && map.hasLayer(bullseyePopup)) {
      map.closePopup(bullseyePopup);
      bullseyePopup = null;
      bullseyePopupKey = null;
      clearBullseyeCompass();
      return;
    }
    clearBullseyeCompass();
    bullseyePopupKey = key;
    const popup = L.popup({ maxWidth: 320, className: 'comm-popup' })
      .setLatLng(event.latlng)
      .setContent(`<div class="comm-tip">${text}</div>`);
    bullseyePopup = popup;
    popup.on('remove', () => {
      if (bullseyePopup === popup) {
        bullseyePopup = null;
        bullseyePopupKey = null;
        clearBullseyeCompass();
      }
    });
    popup.openOn(map);
    if (onClick) onClick();
  });
  return layer;
}

function drawActualBullseyeCompass(bullLat, bullLon, actualLat, actualLon,
                                   bearing) {
  clearBullseyeCompass();
  if (!bullseyeCompassLayer) return;
  const bull = [bullLat, bullLon];
  const actual = [actualLat, actualLon];
  const color = '#db61a2';

  // The long magenta vector is the aircraft's real position from the call's
  // reference point (bullseye or named marking point).
  L.polyline([bull, actual], {
    color, weight: 3, opacity: 0.95, interactive: false,
  }).addTo(bullseyeCompassLayer);
  for (const side of [-150, 150]) {
    const barb = destMeters(actualLat, actualLon, bearing + side, 3000);
    L.polyline([actual, barb], {
      color, weight: 3, opacity: 0.95, interactive: false,
    }).addTo(bullseyeCompassLayer);
  }

  // A north reference and an arc make the actual bearing readable as an
  // angle, rather than just another long line across the map.
  const northEnd = destMeters(bullLat, bullLon, 0, 10 * 1852);
  L.polyline([bull, northEnd], {
    color: '#8b949e', weight: 2, opacity: 0.85, dashArray: '5 5',
    interactive: false,
  }).addTo(bullseyeCompassLayer);
  const arcRadiusM = 8 * 1852;
  const arc = [];
  for (let degree = 0; degree < bearing; degree += 3)
    arc.push(destMeters(bullLat, bullLon, degree, arcRadiusM));
  arc.push(destMeters(bullLat, bullLon, bearing, arcRadiusM));
  if (arc.length > 1) {
    L.polyline(arc, {
      color, weight: 2, opacity: 0.9, interactive: false,
    }).addTo(bullseyeCompassLayer);
  }

  const midpoint = bearing / 2;
  const labelPos = destMeters(bullLat, bullLon, midpoint, 10 * 1852);
  const bearingText = String(Math.round(bearing) % 360).padStart(3, '0');
  L.marker(labelPos, {
    icon: L.divIcon({
      className: '',
      html: `<div style="color:${color};font-weight:700;white-space:nowrap;`
        + `text-shadow:0 1px 2px #fff,0 0 4px #fff">${bearingText}°</div>`,
      iconSize: [44, 18], iconAnchor: [22, 9],
    }),
    interactive: false, keyboard: false,
  }).addTo(bullseyeCompassLayer);
}

function clearBullseyeCompass() {
  if (bullseyeCompassLayer) bullseyeCompassLayer.clearLayers();
}

function actualAngelsAt(ac, comm) {
  if (comm.alt_ft != null && Number.isFinite(Number(comm.alt_ft)))
    return Number(comm.alt_ft);
  // Older debriefs store altitude on path points, not on radio calls. Calls
  // are pinned to the last recorded position, so use the nearest path sample
  // when it is close enough to represent the call's position.
  let closestAlt = null;
  let closestNm = Infinity;
  for (const point of (ac.path || [])) {
    if (!Array.isArray(point) || point.length < 3) continue;
    const distance = bearingRange(comm.lat, comm.lon, point[0], point[1])[1];
    if (distance < closestNm) {
      closestNm = distance;
      closestAlt = Number(point[2]);
    }
  }
  return closestNm <= 0.1 && Number.isFinite(closestAlt) ? closestAlt : null;
}

function bearingRange(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const p1 = lat1 * rad, p2 = lat2 * rad;
  const dl = (lon2 - lon1) * rad;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2)
    - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  const bearing = (Math.atan2(y, x) / rad + 360) % 360;
  const a = Math.sin((p2-p1)/2) ** 2
    + Math.cos(p1) * Math.cos(p2) * Math.sin(dl/2) ** 2;
  return [bearing, 3440.065 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a))];
}

function destMeters(lat, lon, deg, meters) {
  const rad = deg * Math.PI / 180;
  const north = Math.cos(rad) * meters;
  const east = Math.sin(rad) * meters;
  return [lat + north / 111320,
          lon + east / (111320 * Math.cos(lat * Math.PI / 180))];
}

function drawWind(af) {
  // Tower marker + TopGun bust circle + downwind arrow (label is FROM).
  const pos = af.tower_pos || af.center;
  if (!pos) return;
  const radiusM = af.topgun && af.topgun.radius_m;
  if (radiusM) {
    L.circle(pos, {
      radius: radiusM,
      color: '#f0883e', weight: 2, dashArray: '4 3',
      fillColor: '#f0883e', fillOpacity: 0.2,
    }).bindTooltip(`Tower · TopGun bust ${radiusM} m`).addTo(staticLayer);
  }
  L.circleMarker(pos, {
    radius: 5, color: '#79c0ff', weight: 2,
    fillColor: '#79c0ff', fillOpacity: 1,
  }).bindTooltip('Tower').addTo(staticLayer);

  const wind = af.wind || {};
  const speed = Number(wind.speed) || 0;
  const from = Number(wind.dir) || 0;
  const dir3 = String(Math.round(from)).padStart(3, '0');
  if (speed >= 1) {
    const to = (from + 180) % 360;
    const tip = destMeters(pos[0], pos[1], to, 250);
    const left = destMeters(tip[0], tip[1], to + 140, 60);
    const right = destMeters(tip[0], tip[1], to - 140, 60);
    const style = { color: '#79c0ff', weight: 3, lineCap: 'round', lineJoin: 'round' };
    L.polyline([pos, tip], style).addTo(staticLayer);
    L.polyline([left, tip, right], style).addTo(staticLayer);
  }
  const label = speed < 1 ? 'calm' : `${dir3}°`;
  const tipText = speed < 1
    ? 'Wind calm'
    : `Wind from ${dir3}° · ${Math.round(speed)} kt`;
  L.marker(pos, {
    icon: L.divIcon({
      className: 'wind',
      html: `<div class="wind-label">${label}</div>`,
      iconSize: [0, 0],
      iconAnchor: [0, 0],
    }),
    keyboard: false,
  }).bindTooltip(tipText).addTo(staticLayer);
}

function aircraftIcon(ac) {
  const color = PHASE_COLOR[ac.controller] || '#8b949e';
  const label = `${ac.callsign} · ${ac.phase}`;
  return L.divIcon({
    className: 'ac',
    html: `<div class="ac-arrow" style="transform:rotate(${ac.heading}deg);color:${color}">▲</div>`
        + `<div class="ac-label" style="color:${color}">${label}</div>`,
    iconSize: [0, 0],
    iconAnchor: [9, 9],
  });
}

function drawAircraft(list) {
  const seen = new Set();
  for (const ac of list) {
    seen.add(ac.callsign);
    if (ac.active === false) {
      // Retained (logged-off) sortie: no live marker, just the trail so the
      // whole flight stays reviewable after the pilot leaves the slot.
      const m = markers.get(ac.callsign);
      if (m) { aircraftLayer.removeLayer(m); markers.delete(ac.callsign); }
    } else {
      const pos = [ac.lat, ac.lon];
      const gates = [ac.entry_gate ? `in ${ac.entry_gate}` : '',
                     ac.exit_gate ? `out ${ac.exit_gate}` : '']
        .filter(Boolean).join(' · ');
      const tip = `${ac.callsign} (${ac.type})<br>${ac.phase} · ${ac.controller || '—'}<br>`
        + `${ac.alt_ft} ft · ${ac.heading}°${gates ? '<br>' + gates : ''}`;
      if (markers.has(ac.callsign)) {
        const m = markers.get(ac.callsign);
        m.setLatLng(pos).setIcon(aircraftIcon(ac)).setTooltipContent(tip);
      } else {
        const m = L.marker(pos, { icon: aircraftIcon(ac) })
          .bindTooltip(tip).addTo(aircraftLayer);
        markers.set(ac.callsign, m);
      }
    }
    drawPath(ac);
    drawComms(ac);
  }
  // Drop only the *markers* for aircraft no longer reported. Paths and comm
  // markers are kept client-side (the server retains them too), so a trail
  // never disappears mid-review during a transient server hiccup.
  for (const [callsign, m] of markers) {
    if (!seen.has(callsign)) {
      aircraftLayer.removeLayer(m); markers.delete(callsign);
    }
  }
}

// A 2 s poll at jet speed is a few tenths of a NM. A gap bigger than this is
// a teleport / identity switch / two sorties glued together — drawing it as
// one polyline produces a fake straight chord (seen 2026-10-17).
const PATH_JUMP_NM = 2.5;

function nmBetween(a, b) {
  const dLat = (b[0] - a[0]) * 60;
  const dLon = (b[1] - a[1]) * 60 * Math.cos(a[0] * Math.PI / 180);
  return Math.hypot(dLat, dLon);
}

function splitPath(pts, maxNm = PATH_JUMP_NM) {
  const segs = [];
  let cur = [];
  for (const p of pts) {
    if (cur.length && nmBetween(cur[cur.length - 1], p) > maxNm) {
      if (cur.length >= 2) segs.push(cur);
      cur = [];
    }
    cur.push(p);
  }
  if (cur.length >= 2) segs.push(cur);
  return segs;
}

function drawPath(ac) {
  const pts = ac.path || [];
  const segs = splitPath(pts);
  const active = ac.active !== false;
  // A green trail reads clearly over the beige DCS chart (the controller-hued
  // gray melted into the apron). Active flight: brighter + solid; a completed /
  // replay sortie: slightly dimmer but still green and solid.
  const style = active
    ? { color: '#3fb950', weight: 3, opacity: 0.9, dashArray: null }
    : { color: '#2ea043', weight: 2.5, opacity: 0.85, dashArray: null };
  let group = paths.get(ac.callsign);
  if (!group) {
    group = L.layerGroup().addTo(pathLayer);
    paths.set(ac.callsign, group);
  }
  group.clearLayers();
  for (const seg of segs) {
    L.polyline(seg, { ...style, interactive: false }).addTo(group);
  }
}

function commColor(kind) {
  if (kind === 'tx') return '#3fb950';       // ATC
  if (kind === 'state') return '#d29922';    // phase change
  return '#58a6ff';                          // pilot
}

// One tooltip line per call inside a cluster. "state" events read as the phase.
// The sender label is one bold token showing the agency: an ATC reply reads
// "ATC-<agency>" (e.g. "ATC-GROUND") and a pilot call "PILOT-<agency>" (the
// channel the pilot keyed, so a wrong-channel call is visible), else "PILOT".
function commLine(c) {
  let sender;
  if (c.kind === 'tx') sender = 'ATC' + (c.controller ? '-' + c.controller : '');
  else if (c.kind === 'state') sender = 'STATE';
  else sender = 'PILOT' + (c.controller ? '-' + c.controller : '');
  sender = sender.toUpperCase();
  const body = c.kind === 'state' ? `phase → <b>${c.text}</b>` : c.text;
  return `<b>${c.t} ${sender}</b><br>${body}`;
}

// Small dots on the path where the pilot called / the reply came / the brain
// changed phase. Click to see the text (a debug aid: "where was I when I said
// that?"). Calls at (almost) the same spot — e.g. several while holding short —
// are merged into one numbered marker, so a busy point is not a pile of dots.
function drawComms(ac) {
  const comms = ac.comms || [];
  const active = ac.active !== false;
  let group = commMarkers.get(ac.callsign);
  if (!group) { group = L.layerGroup().addTo(commLayer); commMarkers.set(ac.callsign, group); }

  const EPS = 0.0007;  // ~75 m: below this, treat two calls as the same place
  const clusters = [];
  for (const c of comms) {
    const last = clusters[clusters.length - 1];
    if (last && Math.abs(last.lat - c.lat) < EPS
             && Math.abs(last.lon - c.lon) < EPS) {
      last.items.push(c);
    } else {
      clusters.push({ lat: c.lat, lon: c.lon, items: [c] });
    }
  }

  // Reuse the existing markers instead of clearing and recreating them: a
  // popup lives on its marker, so recreating it every 2 s closed the popup and
  // reset its scroll position (you could never scroll a long call list).
  const seen = new Set();
  for (const cl of clusters) {
    const last = cl.items[cl.items.length - 1];
    const color = commColor(last.kind);
    const n = cl.items.length;
    const tip = n === 1
      ? commLine(cl.items[0])
      : `<b>${n} calls</b><hr>`
        + cl.items.map(commLine).join('<hr>');
    // Every comm is a numbered badge — a single call is just a "1". Same size,
    // same click-to-open popup, so a lone call is as easy to spot as a cluster.
    // Scale the badge to the count so a busy stop stays legible (1-2 digits
    // small, 3 digits larger; cap the label so it never overflows).
    const label = n > 999 ? '999+' : String(n);
    const size = label.length <= 1 ? 18 : (label.length === 2 ? 22 : 26);
    const html = `<div class="comm-group" style="--c:${color};`
      + `width:${size}px;height:${size}px;`
      + `font-size:${label.length <= 1 ? 10 : 9}px">${label}</div>`;
    const key = `${cl.lat.toFixed(5)},${cl.lon.toFixed(5)}`;
    seen.add(key);
    let marker = group.getLayers().find((m) => m._commKey === key);
    if (marker) {
      // Same spot: update the badge and the popup body in place. Both
      // `setIcon` and `setPopupContent` rebuild DOM (Leaflet's setIcon even
      // re-binds the popup), which resets the scroll position of a long call
      // list — so only touch them when the content actually changed.
      if (marker._commHtml !== html) {
        marker._commHtml = html;
        const el = marker.getElement();
        const badge = el && el.querySelector('.comm-group');
        if (badge) badge.outerHTML = html;
        else marker.setIcon(L.divIcon({ className: 'comm-group-icon', html,
                                        iconSize: [size, size],
                                        iconAnchor: [size / 2, size / 2] }));
      }
      marker.setOpacity(active ? 1 : 0.6);
      if (marker._commTip !== tip) {
        marker._commTip = tip;
        marker.setPopupContent(`<div class="comm-tip">${tip}</div>`);
      }
      continue;
    }
    // A busy cluster can list many calls, and a Leaflet *tooltip* is neither
    // scrollable nor clickable. Use a click-to-open popup instead, which
    // scrolls (maxHeight) so 10-15 calls stay readable.
    marker = L.marker([cl.lat, cl.lon], {
      icon: L.divIcon({ className: 'comm-group-icon', html,
                        iconSize: [size, size],
                        iconAnchor: [size / 2, size / 2] }),
      opacity: active ? 1 : 0.6,
      title: n === 1 ? '1 call (click)' : `${n} calls (click)`,
    }).bindPopup(`<div class="comm-tip">${tip}</div>`,
                 { maxWidth: 360, maxHeight: 320, className: 'comm-popup' }
    ).addTo(group);
    marker._commKey = key;
    marker._commTip = tip;
    marker._commHtml = html;
    // A popup that reopens on every 2 s redraw can never be dismissed by
    // clicking the map (the redraw reopens it). Track the user's intent: a
    // map click closes it for good; clicking the marker toggles it.
    marker.on('popupopen', () => { commPopupKey = key; });
    marker.on('popupclose', () => {
      if (commPopupKey === key) commPopupKey = null;
    });
  }
  // Drop markers whose cluster is gone (e.g. after a debrief reload).
  for (const m of group.getLayers().slice()) {
    if (!seen.has(m._commKey)) group.removeLayer(m);
  }
}

function aiIcon(ac) {
  const color = ac.coalition === 2 ? '#8b949e' : '#f85149';
  return L.divIcon({
    className: 'ac',
    html: `<div class="ac-arrow" style="transform:rotate(${ac.heading}deg);color:${color};font-size:13px">▲</div>`,
    iconSize: [0, 0], iconAnchor: [6, 6],
  });
}

function drawAiAir(list) {
  const seen = new Set();
  const bounds = map.getBounds();
  for (const ac of list) {
    // Only draw AI traffic inside the current viewport: zoom/pan declutters.
    if (!bounds.contains([ac.lat, ac.lon])) continue;
    seen.add(ac.callsign);
    const tip = `${ac.callsign} (${ac.type})<br>AI · ${ac.alt_ft} ft · ${ac.heading}°`;
    if (aiMarkers.has(ac.callsign)) {
      aiMarkers.get(ac.callsign).setLatLng([ac.lat, ac.lon])
        .setIcon(aiIcon(ac)).setTooltipContent(tip);
    } else {
      aiMarkers.set(ac.callsign, L.marker([ac.lat, ac.lon], { icon: aiIcon(ac) })
        .bindTooltip(tip).addTo(aiLayer));
    }
  }
  for (const [callsign, m] of aiMarkers) {
    if (!seen.has(callsign)) { aiLayer.removeLayer(m); aiMarkers.delete(callsign); }
  }
}

function drawTable(list) {
  const body = document.querySelector('#traffic tbody');
  // Live aircraft only: retained (logged-off) trails have no current phase.
  const rows = list.filter(ac => ac.active !== false);
  body.innerHTML = rows.map(ac =>
    `<tr><td>${ac.callsign}</td><td>${ac.phase}</td>`
    + `<td>${ac.alt_ft}</td><td>${String(ac.heading).padStart(3, '0')}</td></tr>`
  ).join('');
}

// ---------- Chatter log ----------

const chatterHidden = new Set();  // agency names to hide
let chatterAgenciesSeen = new Set();

function buildChatterFilters(agencies) {
  const box = document.getElementById('chatter-filters');
  box.innerHTML = agencies.map(a =>
    `<label><input type="checkbox" data-agency="${a}"`
    + `${chatterHidden.has(a) ? '' : ' checked'}> ${a}</label>`
  ).join('');
  box.querySelectorAll('input').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) chatterHidden.delete(cb.dataset.agency);
      else chatterHidden.add(cb.dataset.agency);
      renderChatter(lastChatter);
    });
  });
}

let lastChatter = [];

function renderChatter(list) {
  const log = document.getElementById('chatter-log');
  const rows = list.filter(e => !chatterHidden.has(e.controller || 'atc'));
  log.innerHTML = rows.map(e => {
    const cls = `chat-row ${e.kind} ${e.controller || ''}`;
    const who = e.kind === 'tx' ? `ATC ${e.controller || ''}`.trim() : e.who;
    // Show the flight callsign next to the SRS name once the brain has learned
    // it (e.g. "Caveman (Colt 1)"), so the log is easy to follow.
    const tag = (e.kind !== 'tx' && e.callsign)
      ? `${who} <span class="cs">(${e.callsign})</span>` : who;
    return `<div class="${cls}"><span class="t">${e.t}</span>`
      + `<span class="f">${e.freq.toFixed(3)}</span>`
      + `<span class="who">${tag}</span>`
      + `<span class="txt">${e.text}</span></div>`;
  }).join('');
  log.scrollTop = log.scrollHeight;
}

function drawChatter(list, configured) {
  lastChatter = list;
  // Show every configured agency (ground/tower/control/atis) plus any that
  // appear in the log, so the filter list is stable from the start.
  const seen = new Set([...(configured || []), ...list.map(e => e.controller || 'atc')]);
  const agencies = [...seen].sort();
  if (agencies.join(',') !== [...chatterAgenciesSeen].sort().join(',')) {
    chatterAgenciesSeen = new Set(agencies);
    buildChatterFilters(agencies);
  }
  renderChatter(list);
}

function initChatter() {
  document.getElementById('chatter-toggle').addEventListener('click', () => {
    const el = document.getElementById('chatter');
    el.classList.toggle('collapsed');
    const open = !el.classList.contains('collapsed');
    document.getElementById('chatter-toggle').textContent =
      (open ? '▼' : '▲') + ' Chatter';
    document.getElementById('chatter-toggle')
      .setAttribute('aria-expanded', String(open));
  });
}

async function tick() {
  const status = document.getElementById('status');
  try {
    const res = await fetch('/api/atc', { cache: 'no-store' });
    const data = await res.json();
    if (!map) initMap(data.airfield.center);
    document.getElementById('title').textContent =
      `ATC Map · ${data.airfield.name} · RWY ${data.airfield.active_runway}`;
    setBaseMap(data.airfield.basemap);
    drawOverlay(data.airfield);
    drawAirspace(data.airfield);
    drawAiAir(data.ai_air || []);
    drawAircraft(data.aircraft);
    drawBullseyeCalls(data.aircraft, data.airfield);
    drawTable(data.aircraft);
    drawChatter(data.chatter || [], data.airfield.agencies || []);
    drawControl(data.control);
    const err = document.getElementById('error');
    if (data.error) { err.hidden = false; err.textContent = `DCS: ${data.error}`; }
    else { err.hidden = true; }
    if (data.replay) {
      status.textContent = 'replay';
      status.className = 'status ok';
    } else {
      status.textContent = data.error ? 'DCS offline' : 'live';
      status.className = 'status ' + (data.error ? 'bad' : 'ok');
    }
    if (data.replay) selectDebrief(data.replay_name || '');
  } catch (e) {
    status.textContent = 'server offline';
    status.className = 'status bad';
  }
}

// ---- web-driven restart / airfield switch ----

// Populate the airfield dropdown + Restart/Stop buttons from the snapshot's
// `control` block (present only when the bot was started via run_server.sh).
function drawControl(control) {
  const box = document.getElementById('control');
  if (!box) return;
  if (!control) { box.hidden = true; return; }
  box.hidden = false;
  const sel = document.getElementById('airfield-select');
  const names = control.airfields || [];
  if (sel.options.length !== names.length) {
    sel.innerHTML = '';
    for (const n of names) {
      const o = document.createElement('option');
      o.value = n; o.textContent = n;
      sel.appendChild(o);
    }
  }
  if (control.current) sel.value = control.current;
}

async function postControl(path, body) {
  const status = document.getElementById('status');
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const result = await res.json().catch(() => ({}));
    if (!result.ok) {
      alert(`Control failed: ${result.error || 'unknown error'}`);
      return;
    }
    status.textContent = result.restarting ? 'restarting…' : 'stopping…';
    status.className = 'status';
  } catch (e) {
    // The bot exits right after replying, so a dropped connection is expected.
    status.textContent = 'restarting…';
    status.className = 'status';
  }
}

function initControl() {
  const restart = document.getElementById('restart-btn');
  const stop = document.getElementById('stop-btn');
  if (restart) {
    restart.addEventListener('click', () => {
      const sel = document.getElementById('airfield-select');
      const airfield = sel ? sel.value : '';
      if (!confirm(`Restart the bot on ${airfield}?`)) return;
      postControl('/api/control/restart', { airfield });
    });
  }
  if (stop) {
    stop.addEventListener('click', () => {
      if (!confirm('Stop the bot?')) return;
      postControl('/api/control/stop', {});
    });
  }
}

// ---- debrief loader (load a saved sortie, read-only) ----

function selectDebrief(name) {
  const sel = document.getElementById('debrief-select');
  if (!sel) return;
  // Ensure the loaded name is present even if the dropdown predates the file.
  if (name && ![...sel.options].some(o => o.value === name)) {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  }
  sel.value = name || '';
}

async function refreshDebriefs() {
  const sel = document.getElementById('debrief-select');
  if (!sel) return;
  try {
    const res = await fetch('/api/debriefs', { cache: 'no-store' });
    const data = await res.json();
    const current = sel.value;
    sel.innerHTML = '';
    const live = document.createElement('option');
    live.value = ''; live.textContent = '— live —';
    sel.appendChild(live);
    for (const d of (data.debriefs || [])) {
      const o = document.createElement('option');
      o.value = d.name;
      o.textContent = d.name.replace(/^tracks_/, '').replace(/\.json$/, '');
      sel.appendChild(o);
    }
    sel.value = current;
  } catch (e) { /* server offline; leave the default */ }
}

// Drop every client-side layer and cached list, so a newly loaded debrief (or
// a switch back to live) redraws from scratch. Without this the previous
// sortie's trails, comm markers and chatter stayed on screen — the layers were
// removed from the wrong layer group, so they were never actually cleared.
function resetClientState() {
  for (const m of markers.values()) aircraftLayer.removeLayer(m);
  markers.clear();
  for (const p of paths.values()) pathLayer.removeLayer(p);
  paths.clear();
  for (const g of commMarkers.values()) commLayer.removeLayer(g);
  commMarkers.clear();
  for (const m of aiMarkers.values()) aiLayer.removeLayer(m);
  aiMarkers.clear();
  commPopupKey = null;
  lastChatter = [];
  chatterAgenciesSeen = new Set();
  document.querySelector('#traffic tbody').innerHTML = '';
  document.getElementById('chatter-log').innerHTML = '';
  document.getElementById('chatter-filters').innerHTML = '';
}

async function loadDebrief(name) {
  const url = name ? '/api/debrief/load' : '/api/debrief/unload';
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const result = await res.json().catch(() => ({}));
    if (name && !result.ok) {
      // Surface why nothing loaded (empty file, not found, …).
      alert(`Could not load ${name}: ${result.error || 'unknown error'}`);
      // Fall back to whatever is actually loaded now.
      refreshDebriefs();
      tick();
      return;
    }
  } catch (e) {
    /* tick() will surface the state */
  }
  resetClientState();
  tick();
}

document.addEventListener('DOMContentLoaded', () => {
  initControl();
  const sel = document.getElementById('debrief-select');
  if (sel) {
    sel.addEventListener('change', () => loadDebrief(sel.value));
    refreshDebriefs();
    setInterval(refreshDebriefs, 10000);
  }
});

tick();
setInterval(tick, 2000);
