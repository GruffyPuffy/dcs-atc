const map=L.map('map').setView([35,35],4);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap'}).addTo(map);
const aircraftLayer=L.layerGroup().addTo(map), pathLayer=L.layerGroup().addTo(map), refLayer=L.layerGroup().addTo(map);
const selected=new Set(), colors=['#58a6ff','#3fb950','#d29922','#bc8cff','#f778ba','#39c5cf','#ff7b72','#e3b341'];
let colorIndex=0, colorById=new Map(), lastIds='';
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function label(a){return a.player?`${a.player} (${a.name})`:a.name}
function update(data){
 document.querySelector('#status').textContent=data.error||`${data.aircraft.length} aircraft`;
 document.querySelector('#srs').textContent=`SRS: ${data.srs||'—'}`;
 document.querySelector('#updated').textContent=data.updated?`Updated ${new Date(data.updated*1000).toLocaleTimeString()}`:'';
 const ids=data.aircraft.map(a=>a.id).sort().join('|');
 if(ids!==lastIds){lastIds=ids;const list=document.querySelector('#aircraft'),focus=document.querySelector('#focus'),old=focus.value;
  list.innerHTML='';focus.innerHTML='<option value="">None</option>';
  for(const a of data.aircraft){if(!colorById.has(a.id))colorById.set(a.id,colors[colorIndex++%colors.length]);
   const opt=document.createElement('option');opt.value=a.id;opt.textContent=label(a);focus.append(opt);
   const row=document.createElement('label');row.className='item';row.innerHTML=`<input type="checkbox" data-id="${esc(a.id)}" ${selected.has(a.id)?'checked':''}><span>${esc(label(a))}<small>${esc(a.group)} · ${esc(a.type)}</small></span><small>${a.coalition===2?'Blue':a.coalition===1?'Red':'Neutral'}</small>`;list.append(row);
  } focus.value=old;
 }
 aircraftLayer.clearLayers();pathLayer.clearLayers();refLayer.clearLayers();
 for(const a of data.aircraft){const color=colorById.get(a.id);const marker=L.circleMarker([a.lat,a.lon],{radius:a.player?7:5,color,fillColor:color,fillOpacity:.9,weight:2}).bindPopup(`<b>${esc(label(a))}</b><br>${esc(a.group)} · ${esc(a.type)}<br>${Math.round(a.alt)} ft · ${Math.round(a.heading)}°`);marker.addTo(aircraftLayer);
  const minutes=Math.max(1,Math.min(180,Number(document.querySelector('#history').value)||180));
  const cutoff=Date.now()/1000-minutes*60;
  const points=(a.path||[]).filter(p=>p[0]>=cutoff).map(p=>[p[1],p[2]]);
  if(selected.has(a.id)&&points.length>1)L.polyline(points,{color,weight:2,opacity:.75}).addTo(pathLayer);
 }
 if(data.bullseye)L.marker(data.bullseye,{title:'Bullseye',icon:L.divIcon({className:'',html:'<div style="color:#ffdf5d;font-weight:bold;text-shadow:0 0 3px #000;font-size:18px">✚</div>',iconSize:[18,18],iconAnchor:[9,9]})}).bindPopup('Mission bullseye').addTo(refLayer);
 const focus=document.querySelector('#focus').value;const ac=data.aircraft.find(a=>a.id===focus);if(ac)map.panTo([ac.lat,ac.lon],{animate:true,duration:.5});
}
document.querySelector('#aircraft').addEventListener('change',e=>{if(e.target.matches('input[type=checkbox]')){e.target.checked?selected.add(e.target.dataset.id):selected.delete(e.target.dataset.id)}});
document.querySelector('#history').addEventListener('change',e=>{const n=Math.max(1,Math.min(180,Number(e.target.value)||180));e.target.value=n;});
async function poll(){try{update(await(await fetch('/api/state')).json())}catch(e){document.querySelector('#status').textContent=`Observer error: ${e}`}setTimeout(poll,1000)}poll();
