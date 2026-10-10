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
// Which comm popup the user wants open (null = none). The map redraws every
// 2 s, so we reopen only the popup the user actually opened, and a map click
// clears this so a dismissed popup stays dismissed.
let commPopupKey = null;

function initMap(center) {
  map = L.map('map', { preferCanvas: true }).setView(center, 11);
  window.map = map;  // handy for debugging in the browser console
  // Clicking the map (not a marker) dismisses any open comm popup for good.
  map.on('click', () => { commPopupKey = null; });
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
  // A green trail reads clearly over the beige DCS chart (the controller-hued
  // gray melted into the apron). Active flight: brighter + solid; a completed /
  // replay sortie: slightly dimmer but still green and solid.
  const style = active
    ? { color: '#3fb950', weight: 3, opacity: 0.9, dashArray: null }
    : { color: '#2ea043', weight: 2.5, opacity: 0.85, dashArray: null };
  if (paths.has(ac.callsign)) {
    paths.get(ac.callsign).setLatLngs(pts).setStyle(style);
  } else {
    paths.set(ac.callsign, L.polyline(pts, {
      ...style, interactive: false,
    }).addTo(pathLayer));
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
  // The map redraws every 2s, which would close an open popup. Remember which
  // comm log the user wants open BEFORE clearing the layer (clearLayers fires
  // popupclose, which would otherwise clear the intent), and reopen it after.
  const wantKey = commPopupKey;
  group.clearLayers();

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

  for (const cl of clusters) {
    const last = cl.items[cl.items.length - 1];
    const color = commColor(last.kind);
    const tip = cl.items.length === 1
      ? commLine(cl.items[0])
      : `<b>${cl.items.length} calls</b><hr>`
        + cl.items.map(commLine).join('<hr>');
    if (cl.items.length === 1) {
      L.circleMarker([cl.lat, cl.lon], {
        radius: last.kind === 'state' ? 2.5 : 3, color, weight: 1,
        fillColor: color, fillOpacity: active ? 0.9 : 0.35,
      }).bindTooltip(tip, { direction: 'top' }).addTo(group);
    } else {
      // Scale the badge to the count so a busy stop stays legible (1-2 digits
      // small, 3 digits larger; cap the label so it never overflows).
      const n = cl.items.length;
      const label = n > 999 ? '999+' : String(n);
      const size = label.length <= 1 ? 18 : (label.length === 2 ? 22 : 26);
      const html = `<div class="comm-group" style="--c:${color};`
        + `width:${size}px;height:${size}px;`
        + `font-size:${label.length <= 1 ? 10 : 9}px">${label}</div>`;
      // A busy cluster can list many calls, and a Leaflet *tooltip* is neither
      // scrollable nor clickable. Use a click-to-open popup instead, which
      // scrolls (maxHeight) so 10-15 calls stay readable.
      const marker = L.marker([cl.lat, cl.lon], {
        icon: L.divIcon({ className: 'comm-group-icon', html,
                          iconSize: [size, size],
                          iconAnchor: [size / 2, size / 2] }),
        opacity: active ? 1 : 0.6,
        title: `${n} calls (click)`,
      }).bindPopup(`<div class="comm-tip">${tip}</div>`,
                   { maxWidth: 360, maxHeight: 320, className: 'comm-popup' }
      ).addTo(group);
      // Tag the marker so the next redraw can tell which log was open, and
      // reopen it (the redraw cleared the layer, which closed the popup).
      const key = `${cl.lat.toFixed(5)},${cl.lon.toFixed(5)}`;
      marker._commKey = key;
      // A popup that reopens on every 2 s redraw can never be dismissed by
      // clicking the map (the redraw reopens it). Track the user's intent: a
      // map click closes it for good; clicking the marker toggles it.
      marker.on('popupopen', () => { commPopupKey = key; });
      marker.on('popupclose', () => {
        if (commPopupKey === key) commPopupKey = null;
      });
      if (key === wantKey) marker.openPopup();
    }
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
  // Clear client-side layers so the new (or live) picture redraws cleanly.
  for (const m of markers.values()) aircraftLayer.removeLayer(m);
  markers.clear();
  for (const p of paths.values()) aircraftLayer.removeLayer(p);
  paths.clear();
  for (const g of commMarkers.values()) aircraftLayer.removeLayer(g);
  commMarkers.clear();
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
