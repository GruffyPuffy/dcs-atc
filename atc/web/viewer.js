// Read-only viewer page: reuses the map rendering from map.js but replaces the
// bot-coupled behaviors (POST-based debrief loading, restart/stop controls)
// with client-side playback from the viewer's GET-only API.

// Hide the restart/stop controls: the viewer has no authority and no POST
// endpoints, so the buttons would only produce errors.
document.addEventListener('DOMContentLoaded', () => {
  const control = document.getElementById('control');
  if (control) control.remove();
});

// The viewer is replay-only: it always shows a saved debrief (the newest one
// on load, or the one picked from the dropdown). No live feed, no polling of
// changing data — once drawn, a sortie stays exactly as saved.
let viewerMode = null;  // the debrief currently shown (always a name)

// Client-side debrief playback: fetch the saved payload and feed it to the
// same draw functions the live page uses. No server state is changed.
async function loadDebrief(name) {
  try {
    const res = await fetch(`/api/debrief/${encodeURIComponent(name)}`,
                            { cache: 'no-store' });
    if (!res.ok) {
      alert(`Could not load ${name}: HTTP ${res.status}`);
      return;
    }
    const data = await res.json();
    resetClientState();
    if (!map) initMap(data.airfield.center);
    document.getElementById('title').textContent =
      `ATC Map · ${data.airfield.name} · RWY ${data.airfield.active_runway}`;
    showBuildVersion(data.version);
    setBaseMap(data.airfield.basemap);
    drawOverlay(data.airfield);
    drawAirspace(data.airfield);
    drawAircraft(data.aircraft);
    fitReplayPaths(data.aircraft);
    drawBullseyeCalls(data.aircraft, data.airfield);
    drawTable(data.aircraft);
    drawChatter(data.chatter || [], data.airfield.agencies || []);
    const status = document.getElementById('status');
    status.textContent = 'replay';
    status.className = 'status ok';
  } catch (e) {
    const status = document.getElementById('status');
    status.textContent = 'server offline';
    status.className = 'status bad';
    status.title = String(e);
    console.error('Debrief viewer load failed:', e);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // Stop map.js's auto-run timers (its tick() polls /api/atc and would stomp
  // the selected replay every 2 s). viewer.js installs its own timers below.
  for (const id of window.__mapIntervals || []) clearInterval(id);
  window.__mapIntervals = [];
  window.__viewerTimersActive = true;  // our own intervals are not tagged

  // map.js's refreshDebriefs() prepends a "— live —" option every time it
  // rebuilds the dropdown. The viewer is replay-only, so replace it with a
  // version that keeps a disabled placeholder instead.
  refreshDebriefs = async function () {
    const sel = document.getElementById('debrief-select');
    if (!sel) return;
    try {
      const res = await fetch('/api/debriefs', { cache: 'no-store' });
      const data = await res.json();
      const current = sel.value;
      sel.innerHTML = '';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = '— pick a debrief —';
      placeholder.disabled = true;
      sel.appendChild(placeholder);
      for (const d of (data.debriefs || [])) {
        const o = document.createElement('option');
        o.value = d.name;
        o.textContent = d.name.replace(/^tracks_/, '').replace(/\.json$/, '');
        sel.appendChild(o);
      }
      sel.value = current || (data.debriefs?.[0]?.name ?? '');
    } catch (e) { /* server offline; leave the list as is */ }
  };

  const sel = document.getElementById('debrief-select');
  sel.addEventListener('change', () => {
    if (sel.value) {
      viewerMode = sel.value;
      loadDebrief(viewerMode);
    }
  });
  refreshDebriefs();
  setInterval(refreshDebriefs, 10000);
  // Open on the newest saved debrief (the first entry of the list).
  setTimeout(() => {
    const first = sel.options[1];  // [0] is the placeholder
    if (first) {
      viewerMode = first.value;
      sel.value = viewerMode;
      loadDebrief(viewerMode);
    }
  }, 0);
});
