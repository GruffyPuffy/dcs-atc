// Live ATC map: airfield geometry + aircraft with their flight phase.
// Polls /api/atc and redraws. Leaflet is loaded from a CDN in index.html.

const PHASE_COLOR = {
  ground: '#d29922',   // Ground agency
  tower: '#3fb950',    // Tower agency
  control: '#58a6ff',  // Control agency
};

let map, ctrLayer, staticLayer, aircraftLayer, aiLayer, chartLayer;
const markers = new Map();  // callsign -> L.marker
const aiMarkers = new Map();

function initMap(center) {
  map = L.map('map', { preferCanvas: true }).setView(center, 11);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
  chartLayer = L.layerGroup().addTo(map);
  ctrLayer = L.layerGroup().addTo(map);
  staticLayer = L.layerGroup().addTo(map);
  aiLayer = L.layerGroup().addTo(map);
  aircraftLayer = L.layerGroup().addTo(map);
  document.getElementById('chart-toggle').addEventListener('change', (e) => {
    if (e.target.checked) chartLayer.addTo(map); else map.removeLayer(chartLayer);
  });
}

function drawOverlay(af) {
  chartLayer.clearLayers();
  if (!af.overlay) return;
  L.imageOverlay(af.overlay.url, af.overlay.bounds, { opacity: 0.85 })
    .addTo(chartLayer);
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

  for (const [name, pos] of Object.entries(af.taxi_routes || {})) {
    L.circleMarker(pos, { radius: 3, color: '#8b949e', weight: 1,
      fillColor: '#8b949e', fillOpacity: 0.8 })
      .bindTooltip(`Taxi ${name}`).addTo(staticLayer);
  }

  for (const [name, pos] of Object.entries(af.parking_areas || {})) {
    L.circleMarker(pos, { radius: 3, color: '#6e7681', weight: 1,
      fillColor: '#6e7681', fillOpacity: 0.8 })
      .bindTooltip(name).addTo(staticLayer);
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
    const pos = [ac.lat, ac.lon];
    const tip = `${ac.callsign} (${ac.type})<br>${ac.phase} · ${ac.controller || '—'}<br>`
      + `${ac.alt_ft} ft · ${ac.heading}°`;
    if (markers.has(ac.callsign)) {
      const m = markers.get(ac.callsign);
      m.setLatLng(pos).setIcon(aircraftIcon(ac)).setTooltipContent(tip);
    } else {
      const m = L.marker(pos, { icon: aircraftIcon(ac) })
        .bindTooltip(tip).addTo(aircraftLayer);
      markers.set(ac.callsign, m);
    }
  }
  for (const [callsign, m] of markers) {
    if (!seen.has(callsign)) { aircraftLayer.removeLayer(m); markers.delete(callsign); }
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
  body.innerHTML = list.map(ac =>
    `<tr><td>${ac.callsign}</td><td>${ac.phase}</td>`
    + `<td>${ac.alt_ft}</td><td>${String(ac.heading).padStart(3, '0')}</td></tr>`
  ).join('');
}

async function tick() {
  const status = document.getElementById('status');
  try {
    const res = await fetch('/api/atc', { cache: 'no-store' });
    const data = await res.json();
    if (!map) initMap(data.airfield.center);
    document.getElementById('title').textContent =
      `ATC Map · ${data.airfield.name} · RWY ${data.airfield.active_runway}`;
    drawOverlay(data.airfield);
    drawAirspace(data.airfield);
    drawAiAir(data.ai_air || []);
    drawAircraft(data.aircraft);
    drawTable(data.aircraft);
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
