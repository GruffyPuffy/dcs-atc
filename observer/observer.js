const DCS_TILES='http://dcsmaps.com/caucasus/{z}/{x}/{y}.png';
const OSM_TILES='https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const DCS_WMS='http://dcsmaps.com/cgi-bin/mapserv?map=CAUCASUS_MAPFILE';
const DCS_LAYERS='Airbases,Cities,Towns,Roads,Railroad,Landmarks,TACAN,VOR,NDB,DME,DB';
const map=L.map('map',{preferCanvas:true}).setView([42,44],6);
map.createPane('dcsVectors');map.getPane('dcsVectors').style.zIndex=250;map.getPane('dcsVectors').style.pointerEvents='none';
// dcsmaps only has terrain tiles within its regional coverage and at a subset
// of zoom levels. Keep a world basemap underneath so missing regional tiles
// (especially at low zoom) reveal geographic context instead of the map's
// dark background. The transparent error tile also prevents broken-image
// icons from obscuring that fallback.
L.tileLayer(OSM_TILES,{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
L.tileLayer(DCS_TILES,{maxZoom:16,maxNativeZoom:12,errorTileUrl:'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=',attribution:'&copy; <a href="http://dcsmaps.com">dcsmaps.com</a>',tms:true,noWrap:true}).addTo(map);
const aircraftLayer=L.layerGroup().addTo(map), pathLayer=L.layerGroup().addTo(map), refLayer=L.layerGroup().addTo(map);
let vectorOverlay=null,vectorPending=null,vectorTimer=null;
function refreshVectors(){
 if(!document.querySelector('#vectors').checked)return;
 const bounds=map.getBounds(),size=map.getSize();if(size.x<32||size.y<32)return;
 const sw=bounds.getSouthWest(),ne=bounds.getNorthEast();
 const params=new URLSearchParams({SERVICE:'WMS',VERSION:'1.1.1',REQUEST:'GetMap',LAYERS:DCS_LAYERS,STYLES:'',FORMAT:'image/png',TRANSPARENT:'TRUE',SRS:'EPSG:4326',BBOX:`${sw.lng},${sw.lat},${ne.lng},${ne.lat}`,WIDTH:String(Math.min(2048,Math.round(size.x))),HEIGHT:String(Math.min(2048,Math.round(size.y)))});
 if(vectorPending){map.removeLayer(vectorPending);vectorPending=null;}
 const layer=L.imageOverlay(`${DCS_WMS}&${params}`,bounds,{pane:'dcsVectors',interactive:false});vectorPending=layer;
 layer.once('load',()=>{if(vectorPending!==layer)return;if(vectorOverlay)map.removeLayer(vectorOverlay);vectorOverlay=layer;vectorPending=null;});
 layer.once('error',()=>{if(vectorPending===layer)vectorPending=null;map.removeLayer(layer);});layer.addTo(map);
}
function scheduleVectors(){clearTimeout(vectorTimer);vectorTimer=setTimeout(refreshVectors,150)}
map.on('moveend zoomend',scheduleVectors);window.addEventListener('resize',scheduleVectors);
document.querySelector('#vectors').addEventListener('change',e=>{if(e.target.checked)refreshVectors();else{if(vectorPending)map.removeLayer(vectorPending);if(vectorOverlay)map.removeLayer(vectorOverlay);vectorPending=vectorOverlay=null;}});
const selected=new Set(), colors=['#58a6ff','#3fb950','#d29922','#bc8cff','#f778ba','#39c5cf','#ff7b72','#e3b341'];
let colorIndex=0, colorById=new Map(), lastIds='', lastRoster=null, lastData=null;
const unitMarkers=new Map();
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function label(a){return a.player?`${a.player} (${a.name})`:a.name}
function unitSymbol(a,size,quantity,showHeading){
 // Keep DCS side colors absolute: blue is blue and red is red regardless of
 // which coalition bullseye the observer displays.
 const affiliation=a.coalition===0?'neutral':a.coalition===2?'friendly':'hostile';
 const sidcByType={
  0:{friendly:'10030100001101000000',hostile:'10060100001101000000',neutral:'10040100001101000000'},
  1:{friendly:'SFAPMHR----',hostile:'SHAPMHR----',neutral:'SNAPMHR----'},
  2:{friendly:'10031000000000000000',hostile:'10061000000000000000',neutral:'10041000000000000000'},
  3:{friendly:'10033000001201000000',hostile:'10063000001201000000',neutral:'10043000001201000000'},
 };
 const sidc=(sidcByType[a.category]||sidcByType[0])[affiliation];
 const options={size};
 if(quantity>1)options.quantity=String(quantity);
 if(showHeading)options.direction=Number(a.heading)||0;
 const symbol=new ms.Symbol(sidc,options);
 return L.divIcon({className:'unit-symbol',html:symbol.asSVG(),
  iconSize:[symbol.getSize().width,symbol.getSize().height],
  iconAnchor:[symbol.getAnchor().x,symbol.getAnchor().y]});
}
function update(data){
 lastData=data;
 document.querySelector('#status').textContent=data.error||`${data.aircraft.length} units`;
 document.querySelector('#srs').textContent=`SRS: ${data.srs||'—'}`;
 document.querySelector('#updated').textContent=data.updated?`Updated ${new Date(data.updated*1000).toLocaleTimeString()}`:'';
 const roster=data.aircraft.map(a=>[a.id,a.player||'']).sort((a,b)=>a[0].localeCompare(b[0]));
 const signature=JSON.stringify(roster);
 if(signature!==lastIds){
  const current=new Map(roster),focus=document.querySelector('#focus'),old=focus.value;
  if(lastRoster){
   for(const id of lastRoster.keys())if(!current.has(id))selected.delete(id);
   for(const [id,player] of current){const wasPlayer=!!lastRoster.get(id),isPlayer=!!player;
    if(isPlayer&&!wasPlayer)selected.add(id);
    if(wasPlayer&&!isPlayer)selected.delete(id);
   }
  }else for(const [id,player] of current)if(player)selected.add(id);
  lastRoster=current;lastIds=signature;
  const players=document.querySelector('#players'),ai=document.querySelector('#ai');
  players.innerHTML='';ai.innerHTML='';focus.innerHTML='<option value="">None</option>';
  for(const a of data.aircraft){if(!colorById.has(a.id))colorById.set(a.id,colors[colorIndex++%colors.length]);
   const opt=document.createElement('option');opt.value=a.id;opt.textContent=label(a);focus.append(opt);
   const row=document.createElement('label');row.className='item';row.innerHTML=`<input type="checkbox" data-id="${esc(a.id)}" ${selected.has(a.id)?'checked':''}><span title="${esc(a.group)} · ${esc(a.type)}">${esc(label(a))}<small>${esc(a.group)} · ${esc(a.type)} · ${a.coalition===2?'Blue':a.coalition===1?'Red':'Neutral'}</small></span>`;
   (a.player?players:ai).append(row);
  } focus.value=old;
 }
 const playerCount=data.aircraft.filter(a=>a.player).length;
 document.querySelector('#player-count').textContent=`(${playerCount})`;
 document.querySelector('#ai-count').textContent=`(${data.aircraft.length-playerCount})`;
 pathLayer.clearLayers();refLayer.clearLayers();
 const zoom=map.getZoom();
 const symbolSize=zoom<6?13:zoom<9?15:zoom<11?17:zoom<13?20:24;
 const displayUnits=[];
 // Aircraft unfold at zoom 11; ground and sea units stay grouped until 15,
 // near the deepest DCS map zoom. Before that, groups tighten by screen-space
 // cells. Keep coalition and domain separate in every cluster.
 const clusters=new Map();
 for(const a of data.aircraft){
  const individualAt=a.category<=1?11:15;
  if(zoom>=individualAt){displayUnits.push({unit:a,members:[a]});continue;}
  const cellSize=zoom<5?180:zoom<6?140:zoom<8?100:zoom<11?72:zoom<14?56:40;
  const point=map.project([a.lat,a.lon],zoom),key=`${Math.floor(point.x/cellSize)}:${Math.floor(point.y/cellSize)}:${a.coalition}:${a.category}`;
  if(!clusters.has(key))clusters.set(key,[]);clusters.get(key).push(a);
 }
 for(const members of clusters.values()){
  const lead=members[0],lat=members.reduce((sum,a)=>sum+a.lat,0)/members.length,lon=members.reduce((sum,a)=>sum+a.lon,0)/members.length;
  const groupNames=[...new Set(members.map(a=>a.group).filter(Boolean))];
  displayUnits.push({unit:{...lead,lat,lon,group:groupNames.join(', ')},members});
 }
 const seenMarkerKeys=new Set();
 for(const {unit:a,members} of displayUnits){
  const popup=members.length>1?`<b>${esc(a.group||'Nearby units')}</b> · ${members.length} units<br>${members.map(x=>esc(label(x))).join('<br>')}`:
   `<b>${esc(label(a))}</b><br>${esc(a.group)} · ${esc(a.type)}<br>${Math.round(a.alt)} ft · ${Math.round(a.heading)}°`;
  const memberIds=members.map(x=>x.id).sort();
  const popupKey=members.length===1?`unit:${memberIds[0]}`:`group:${memberIds.join('|')}`;
  const showHeading=zoom>=12;
  const iconKey=`${a.category}:${a.coalition}:${symbolSize}:${members.length}:${showHeading}:${showHeading?Math.round(a.heading):''}`;
  const icon=unitSymbol(a,symbolSize,members.length,showHeading);
  let marker=unitMarkers.get(popupKey);
  if(!marker){
   marker=L.marker([a.lat,a.lon],{icon,title:members.length>1?`${members.length} units · ${a.group||'nearby'}`:label(a)}).bindPopup(popup).addTo(aircraftLayer);
   marker._unitIconKey=iconKey;marker._unitPopup=popup;unitMarkers.set(popupKey,marker);
  }else{
   const pos=marker.getLatLng();
   if(pos.lat!==a.lat||pos.lng!==a.lon)marker.setLatLng([a.lat,a.lon]);
   marker.options.title=members.length>1?`${members.length} units · ${a.group||'nearby'}`:label(a);
   const popupOpen=marker.isPopupOpen();
   if(marker._unitIconKey!==iconKey){
    if(popupOpen)marker._pendingUnitIcon={icon,key:iconKey};
    else{marker.setIcon(icon);marker._unitIconKey=iconKey;marker._pendingUnitIcon=null;}
   }
   if(marker._unitPopup!==popup){
    if(popupOpen)marker._pendingUnitPopup=popup;
    else{marker.setPopupContent(popup);marker._unitPopup=popup;marker._pendingUnitPopup=null;}
   }
   if(!popupOpen){
    if(marker._pendingUnitIcon){marker.setIcon(marker._pendingUnitIcon.icon);marker._unitIconKey=marker._pendingUnitIcon.key;marker._pendingUnitIcon=null;}
    if(marker._pendingUnitPopup){marker.setPopupContent(marker._pendingUnitPopup);marker._unitPopup=marker._pendingUnitPopup;marker._pendingUnitPopup=null;}
   }
  }
  seenMarkerKeys.add(popupKey);
 }
 for(const [key,marker] of unitMarkers){
  if(!seenMarkerKeys.has(key)){aircraftLayer.removeLayer(marker);unitMarkers.delete(key);}
 }
 for(const a of data.aircraft){
  const minutes=Math.max(1,Math.min(180,Number(document.querySelector('#history').value)||180));
  const cutoff=Date.now()/1000-minutes*60;
  const points=(a.path||[]).filter(p=>p[0]>=cutoff).map(p=>[p[1],p[2]]);
  if(selected.has(a.id)&&points.length>1)L.polyline(points,{color:colorById.get(a.id),weight:2,opacity:.75}).addTo(pathLayer);
 }
 const bullseyes=data.bullseyes||(data.bullseye?{'2':data.bullseye}:{});
 for(const [coalition,position] of Object.entries(bullseyes)){
  const color=coalition==='2'?'#1f6feb':'#f85149';
  const side=coalition==='2'?'Blue':'Red';
  for(const radius of [4,8,12])L.circleMarker(position,{radius,color,weight:1.5,opacity:.9,fill:false,interactive:false}).addTo(refLayer);
  L.circleMarker(position,{radius:2,color,weight:1,fillColor:color,fillOpacity:1}).bindTooltip(`Bullseye (${side})`).addTo(refLayer);
 }
 const focus=document.querySelector('#focus').value;const ac=data.aircraft.find(a=>a.id===focus);if(ac)map.panTo([ac.lat,ac.lon],{animate:true,duration:.5});
}
map.on('zoomend',()=>{if(lastData)update(lastData)});
document.querySelector('.unit-columns').addEventListener('change',e=>{if(e.target.matches('input[type=checkbox]')){e.target.checked?selected.add(e.target.dataset.id):selected.delete(e.target.dataset.id)}});
document.querySelector('#history').addEventListener('change',e=>{const n=Math.max(1,Math.min(180,Number(e.target.value)||180));e.target.value=n;});
async function poll(){try{update(await(await fetch('/api/state')).json())}catch(e){document.querySelector('#status').textContent=`Observer error: ${e}`}setTimeout(poll,1000)}poll();refreshVectors();
