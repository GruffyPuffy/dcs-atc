// Live ATC map: airfield geometry + aircraft with their flight phase.
// Polls /api/atc and redraws. Leaflet is loaded from a CDN in index.html.

const PHASE_COLOR = {
  ground: '#d29922',   // Ground agency
  tower: '#3fb950',    // Tower agency
  control: '#58a6ff',  // Control agency
};

let map, ctrLayer, staticLayer, aircraftLayer, aiLayer, chartLayer, pathLayer, commLayer;
let baseLayer;  // the base-map tile layer (OSM or DCS); swapped on config change
let baseLayerName = '';
const markers = new Map();  // callsign -> L.marker
const aiMarkers = new Map();
const paths = new Map();    // callsign -> L.polyline
const commMarkers = new Map();  // callsign -> L.layerGroup

function initMap(center) {
  map = L.map('map', { preferCanvas: true }).setView(center, 11);
  window.map = map;  // handy for debugging in the browser console
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
  aiLayer = L.layerGroup().addTo(map);
  aircraftLayer = L.layerGroup().addTo(map);
  document.getElementById('chart-toggle').addEventListener('change', (e) => {
    if (e.target.checked) chartLayer.addTo(map); else map.removeLayer(chartLayer);
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
  if (!bm || !bm.url || bm.name === baseLayerName) return;
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
  if (checks.runway) {
    L.polygon(checks.runway.corners, {
      color: '#f85149', weight: 1, dashArray: '4 4',
      fillColor: '#f85149', fillOpacity: 0.08,
    }).bindTooltip('Runway occupancy corridor').addTo(staticLayer);
  }
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

function drawPath(ac) {
  const pts = ac.path || [];
  if (pts.length < 2) return;
  const active = ac.active !== false;
  const color = PHASE_COLOR[ac.controller] || '#8b949e';
  // Completed (logged-off) sorties are dimmed + dashed, so they read as
  // history rather than live traffic.
  const style = active
    ? { color, weight: 2, opacity: 0.6, dashArray: null }
    : { color, weight: 1.5, opacity: 0.3, dashArray: '3 3' };
  if (paths.has(ac.callsign)) {
    paths.get(ac.callsign).setLatLngs(pts).setStyle(style);
  } else {
    paths.set(ac.callsign, L.polyline(pts, {
      ...style, interactive: false,
    }).addTo(pathLayer));
  }
}

// Small dots on the path where the pilot called / the reply came / the brain
// changed phase. Click to see the text (a debug aid: "where was I when I said
// that?").
function drawComms(ac) {
  const comms = ac.comms || [];
  const active = ac.active !== false;
  let group = commMarkers.get(ac.callsign);
  if (!group) { group = L.layerGroup().addTo(commLayer); commMarkers.set(ac.callsign, group); }
  group.clearLayers();
  for (const c of comms) {
    let color, label;
    if (c.kind === 'tx') { color = '#3fb950'; label = 'ATC'; }
    else if (c.kind === 'state') { color = '#d29922'; label = 'STATE'; }
    else { color = '#58a6ff'; label = 'PILOT'; }
    const body = c.kind === 'state'
      ? `phase → <b>${c.text}</b>`
      : c.text;
    L.circleMarker([c.lat, c.lon], {
      radius: c.kind === 'state' ? 2.5 : 3, color, weight: 1,
      fillColor: color, fillOpacity: active ? 0.9 : 0.35,
    }).bindTooltip(
      `<b>${c.t} ${label}</b>${c.controller ? ' · ' + c.controller : ''}<br>${body}`,
      { direction: 'top' }
    ).addTo(group);
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
    drawTable(data.aircraft);
    drawChatter(data.chatter || [], data.airfield.agencies || []);
    const err = document.getElementById('error');
    if (data.error) { err.hidden = false; err.textContent = `DCS: ${data.error}`; }
    else { err.hidden = true; }
    status.textContent = data.error ? 'DCS offline' : 'live';
    status.className = 'status ' + (data.error ? 'bad' : 'ok');
  } catch (e) {
    status.textContent = 'server offline';
    status.className = 'status bad';
  }
}

tick();
setInterval(tick, 2000);
